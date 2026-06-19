"""Unit tests for AlpacaPaperReactor + the Alpaca shadow hook (additive, default-OFF).

NO network: a FAKE Alpaca TradingClient is injected into the reactor (constructor
``client=`` arg). The fake mimics the alpaca-py surface the reactor uses:
``get_account()`` -> object with ``.equity``; ``submit_order(req)`` -> an order
object with ``.id``/``.status``/``.filled_avg_price``/``.filled_qty``;
``get_order_by_id(id)`` -> the same/evolving order object.

Coverage:
  1. long fill: notional conversion + ExecutionRecord fields from broker fill.
  2. short fill: SELL side + negative realized fill_size_pct.
  3. partial fill: realized fill_size_pct reflects partial, not target.
  4. buying-power reject: raises AlpacaSubmitError (NOT a silent fabricated fill).
  5. unfilled timeout: fill_size_pct=0.0 + metadata.unfilled_timeout + order id.
  6. NAV-fraction round-trip: requested 0.20 of $98k -> ~$19.6k notional.
  7. reconcile writes to the "alpaca-paper" account partition (not paper-default).
  8. select_reactor: flag-OFF -> PaperReactor; flag-ON + equity -> AlpacaPaperReactor.
  9. shadow divergence log written (and shadow is non-blocking).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hermes_quant.proposals import Proposal
from hermes_quant.react.alpaca_paper import AlpacaPaperReactor, AlpacaSubmitError

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _FakeAccount:
    def __init__(self, equity: float) -> None:
        self.equity = equity


class _FakeOrder:
    """Mimics an alpaca-py Order. ``status`` can evolve across get_order_by_id."""

    def __init__(
        self,
        *,
        order_id: str = "ord-123",
        status: str = "filled",
        filled_avg_price: float | None = None,
        filled_qty: float | None = None,
    ) -> None:
        self.id = order_id
        self.client_order_id = order_id
        self.status = status
        self.filled_avg_price = filled_avg_price
        self.filled_qty = filled_qty


class _FakeClient:
    """Injectable fake Alpaca TradingClient.

    Records the last submitted MarketOrderRequest so tests can assert the
    notional/side conversion. ``submit_order`` either returns a preset order or
    raises (buying-power reject). ``get_order_by_id`` returns a (possibly
    evolving) order so partial/unfilled-timeout paths are exercisable.

    ``poll_sequence`` (optional): a list of _FakeOrder snapshots returned on
    successive get_order_by_id calls (the last one repeats), so a test can model
    an order that transitions partially_filled -> filled across polls. When set,
    it takes precedence over ``poll_order``.

    ``cancel_order_by_id`` records the cancel and (optionally) sets a
    ``post_cancel_order`` that subsequent get_order_by_id calls return — modeling
    a cancel that raced a realized partial.

    ``post_cancel_get_raises`` (optional): if set, the FIRST get_order_by_id after
    a cancel raises this exception — modeling a transient broker/network error on
    the post-cancel re-read (settlement UNKNOWN). Combined with ``cancel_raises``
    this models an UNCONFIRMED cancel followed by an UNKNOWN settlement.
    """

    def __init__(
        self,
        *,
        equity: float = 98_000.0,
        submit_result: _FakeOrder | None = None,
        submit_raises: Exception | None = None,
        poll_order: _FakeOrder | None = None,
        poll_sequence: list[_FakeOrder] | None = None,
        post_cancel_order: _FakeOrder | None = None,
        cancel_raises: Exception | None = None,
        post_cancel_get_raises: Exception | None = None,
    ) -> None:
        self._equity = equity
        self._submit_result = submit_result
        self._submit_raises = submit_raises
        self._poll_order = poll_order
        self._poll_sequence = list(poll_sequence) if poll_sequence else None
        self._post_cancel_order = post_cancel_order
        self._cancel_raises = cancel_raises
        self._post_cancel_get_raises = post_cancel_get_raises
        self.submitted: list[Any] = []
        self.poll_calls = 0
        self.cancel_calls: list[str] = []
        self._cancelled = False
        self._post_cancel_get_fired = False

    def get_account(self) -> _FakeAccount:
        return _FakeAccount(self._equity)

    def submit_order(self, request: Any) -> _FakeOrder:
        self.submitted.append(request)
        if self._submit_raises is not None:
            raise self._submit_raises
        assert self._submit_result is not None
        return self._submit_result

    def get_order_by_id(self, order_id: str) -> _FakeOrder:
        self.poll_calls += 1
        # A transient error on the post-cancel re-read (settlement UNKNOWN).
        if (
            self._cancelled
            and self._post_cancel_get_raises is not None
            and not self._post_cancel_get_fired
        ):
            self._post_cancel_get_fired = True
            raise self._post_cancel_get_raises
        # After a cancel, return the post-cancel snapshot if one was configured.
        if self._cancelled and self._post_cancel_order is not None:
            return self._post_cancel_order
        if self._poll_sequence:
            # Advance through the sequence; the last snapshot repeats.
            idx = min(self.poll_calls - 1, len(self._poll_sequence) - 1)
            return self._poll_sequence[idx]
        return self._poll_order if self._poll_order is not None else self._submit_result

    def cancel_order_by_id(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)
        self._cancelled = True
        if self._cancel_raises is not None:
            raise self._cancel_raises


def _proposal(
    *,
    symbol: str = "AAPL",
    asset_class: str = "equity",
    decision_price: float = 100.0,
) -> Proposal:
    return Proposal(
        proposal_id=f"prop_2026-06-05T00:00:00_{symbol}_abc123",
        state="pending",
        symbol=symbol,
        asset_class=asset_class,
        timeframe="1d",
        created_at="2026-06-05T00:00:00Z",
        expires_at="2026-06-05T01:00:00Z",
        advisor_result={
            "as_of": "2026-06-05T00:00:00Z",
            "decision_price": decision_price,
            "signal_id": "sig-1",
        },
    )


@pytest.fixture(autouse=True)
def _no_admissibility(monkeypatch):
    # Keep the admissibility precondition OFF by default so tests exercise the
    # Alpaca fill path; the short test relies on this too (ADR-0077 is its own seam).
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)


def _reactor(client: _FakeClient, tmp_path: Path, **kw) -> AlpacaPaperReactor:
    return AlpacaPaperReactor(
        client=client,
        executions_path=tmp_path / "executions.jsonl",
        poll_timeout_s=kw.get("poll_timeout_s", 2.0),
        poll_interval_s=kw.get("poll_interval_s", 0.0),
    )


# --------------------------------------------------------------------------- #
# 1. Long fill
# --------------------------------------------------------------------------- #


def test_long_fill_notional_conversion_and_record(tmp_path):
    order = _FakeOrder(status="filled", filled_avg_price=101.0, filled_qty=194.0)
    client = _FakeClient(equity=98_000.0, submit_result=order)
    reactor = _reactor(client, tmp_path)

    rec = reactor.execute(_proposal(), fill_size_pct=0.20)

    # Notional = 0.20 * 98_000 = 19_600 (BUY side)
    req = client.submitted[0]
    assert float(req.notional) == pytest.approx(19_600.0)
    from alpaca.trading.enums import OrderSide

    assert req.side == OrderSide.BUY

    # ExecutionRecord reflects BROKER truth.
    assert rec.reactor_name == "alpaca_paper"
    assert rec.target_position_pct == 0.20  # the REQUESTED fraction
    assert rec.fill_price == 101.0  # broker-reported avg fill
    assert rec.decision_price == 100.0
    # realized = filled_notional / equity = (101 * 194) / 98000 ≈ 0.1999...
    assert rec.fill_size_pct == pytest.approx((101.0 * 194.0) / 98_000.0)
    assert rec.fill_size_pct > 0
    assert rec.reactor_metadata["alpaca_order_id"] == "ord-123"
    assert rec.reactor_metadata["account_id"] == "alpaca-paper"
    assert rec.reactor_metadata["quantity"] == pytest.approx(194.0)  # signed +long

    # Exactly one fill line written to the bus.
    lines = [ln for ln in reactor.executions_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1


# --------------------------------------------------------------------------- #
# 2. Short fill
# --------------------------------------------------------------------------- #


def test_short_fill_sell_side_and_negative_realized(tmp_path):
    order = _FakeOrder(status="filled", filled_avg_price=50.0, filled_qty=100.0)
    client = _FakeClient(equity=98_000.0, submit_result=order)
    reactor = _reactor(client, tmp_path)

    rec = reactor.execute(_proposal(symbol="GME"), fill_size_pct=-0.10)

    from alpaca.trading.enums import OrderSide

    req = client.submitted[0]
    assert req.side == OrderSide.SELL
    assert float(req.notional) == pytest.approx(9_800.0)  # abs(-0.10)*98000

    assert rec.target_position_pct == -0.10
    assert rec.fill_price == 50.0
    # realized must be NEGATIVE (short) = -(50*100/98000)
    assert rec.fill_size_pct == pytest.approx(-(50.0 * 100.0) / 98_000.0)
    assert rec.fill_size_pct < 0
    assert rec.reactor_metadata["quantity"] == pytest.approx(-100.0)  # signed -short


# --------------------------------------------------------------------------- #
# 3. Partial fill
# --------------------------------------------------------------------------- #


def test_partial_then_full_fill_keeps_polling(tmp_path):
    """P1-D: partially_filled is NON-terminal — the reactor must keep polling
    until the order is terminally 'filled', recording the FULL realized fill,
    not the first partial snapshot."""
    # Poll sequence: partial (100 sh) -> partial (300 sh) -> fully filled (490 sh).
    seq = [
        _FakeOrder(status="partially_filled", filled_avg_price=100.0, filled_qty=100.0),
        _FakeOrder(status="partially_filled", filled_avg_price=100.0, filled_qty=300.0),
        _FakeOrder(status="filled", filled_avg_price=100.0, filled_qty=490.0),
    ]
    # submit returns the first (partial) snapshot; polls advance through seq.
    client = _FakeClient(equity=98_000.0, submit_result=seq[0], poll_sequence=seq)
    reactor = _reactor(client, tmp_path)

    rec = reactor.execute(_proposal(), fill_size_pct=0.50)

    assert rec.target_position_pct == 0.50  # requested unchanged
    # Records the FULL fill (490 sh), NOT the first 100-sh partial snapshot.
    assert rec.reactor_metadata["filled_qty"] == pytest.approx(490.0)
    assert rec.fill_size_pct == pytest.approx((100.0 * 490.0) / 98_000.0)
    assert rec.reactor_metadata["alpaca_status"] == "filled"
    # It actually had to poll past the partial snapshots.
    assert client.poll_calls >= 2


def test_unfilled_timeout_cancels_working_order(tmp_path):
    """P1-C: on timeout the still-working DAY order must be CANCELED so it cannot
    fill later and orphan an unrecorded position."""
    order = _FakeOrder(order_id="ord-stuck", status="accepted")
    # cancel -> post-cancel snapshot is canceled with NO fill.
    post = _FakeOrder(order_id="ord-stuck", status="canceled")
    client = _FakeClient(
        equity=98_000.0, submit_result=order, poll_order=order, post_cancel_order=post
    )
    reactor = _reactor(client, tmp_path, poll_timeout_s=0.05, poll_interval_s=0.0)

    rec = reactor.execute(_proposal(), fill_size_pct=0.20)

    assert rec.fill_size_pct == 0.0
    assert rec.fill_price == 0.0
    assert rec.reactor_metadata["unfilled_timeout"] is True
    # The working order WAS canceled (P1-C: no orphan).
    assert client.cancel_calls == ["ord-stuck"]


def test_timeout_cancel_races_partial_fill_records_it(tmp_path):
    """P1-C: if the cancel races a realized partial at the broker, that partial
    must be RECORDED (the cancel only removes the unfilled remainder), not lost."""
    order = _FakeOrder(order_id="ord-race", status="accepted")
    # Post-cancel the broker reports a realized partial (canceled w/ 50 sh filled).
    post = _FakeOrder(
        order_id="ord-race", status="canceled", filled_avg_price=100.0, filled_qty=50.0
    )
    client = _FakeClient(
        equity=98_000.0, submit_result=order, poll_order=order, post_cancel_order=post
    )
    reactor = _reactor(client, tmp_path, poll_timeout_s=0.05, poll_interval_s=0.0)

    rec = reactor.execute(_proposal(), fill_size_pct=0.20)

    # The realized partial is recorded, NOT a 0-fill.
    assert client.cancel_calls == ["ord-race"]
    assert rec.fill_size_pct == pytest.approx((100.0 * 50.0) / 98_000.0)
    assert rec.reactor_metadata["filled_qty"] == pytest.approx(50.0)
    # A recorded partial is a REAL fill (normal fill record), so the
    # unfilled_timeout key is absent — not present-and-False.
    assert "unfilled_timeout" not in rec.reactor_metadata
    assert rec.fill_price == 100.0


def test_timeout_unconfirmed_cancel_then_reread_raises_fails_closed(tmp_path):
    """Settlement UNKNOWN must NOT collapse to a clean 0.0 no-fill.

    On poll-budget timeout cancel_and_settle is invoked for a still-WORKING DAY
    order. If (1) cancel_order_by_id raises a transient broker/network error (the
    cancel is UNCONFIRMED) AND (2) the post-cancel get_order_by_id raises the SAME
    transient condition, the order may STILL be working at the broker and may yet
    fill — creating a real LIVE position. The book MUST NOT record a clean
    fill_size_pct=0.0 / unfilled_timeout no-fill (which never reconciles state.db
    and is treated terminally by every consumer), orphaning that position
    PERMANENTLY. It must fail CLOSED — matching the active-poll re-read which
    RAISES AlpacaSubmitError on the byte-identical broker condition.
    """
    order = _FakeOrder(order_id="ord-unknown", status="accepted")
    client = _FakeClient(
        equity=98_000.0,
        submit_result=order,
        poll_order=order,
        cancel_raises=Exception("503 service unavailable (cancel)"),
        post_cancel_get_raises=Exception("503 service unavailable (re-read)"),
    )
    reactor = _reactor(client, tmp_path, poll_timeout_s=0.05, poll_interval_s=0.0)

    with pytest.raises(AlpacaSubmitError) as ei:
        reactor.execute(_proposal(), fill_size_pct=0.20)
    # The error surfaces the UNKNOWN settlement, not a fabricated no-fill.
    msg = str(ei.value).lower()
    assert "ord-unknown" in msg
    assert "settle" in msg or "unknown" in msg or "working" in msg

    # NOTHING was written to the bus — a 0.0 no-fill record would orphan a
    # possibly-live position the book never tracked.
    lines = [ln for ln in reactor.executions_path.read_text().splitlines() if ln.strip()]
    assert lines == []


def test_timeout_confirmed_cancel_then_reread_raises_degrades_to_no_fill(tmp_path):
    """When the cancel SUCCEEDS (the working order is provably gone) but the
    post-cancel re-read raises transiently, there is no working order left to
    orphan, so degrading to a clean unfilled_timeout no-fill is safe. This keeps
    the fail-closed change narrow — it only fires when the order MIGHT still be
    working (cancel unconfirmed)."""
    order = _FakeOrder(order_id="ord-gone", status="accepted")
    client = _FakeClient(
        equity=98_000.0,
        submit_result=order,
        poll_order=order,
        # cancel_raises is None -> the cancel is CONFIRMED.
        post_cancel_get_raises=Exception("503 service unavailable (re-read)"),
    )
    reactor = _reactor(client, tmp_path, poll_timeout_s=0.05, poll_interval_s=0.0)

    rec = reactor.execute(_proposal(), fill_size_pct=0.20)

    assert rec.fill_size_pct == 0.0
    assert rec.fill_price == 0.0
    assert rec.reactor_metadata["unfilled_timeout"] is True
    assert client.cancel_calls == ["ord-gone"]


def test_done_for_day_with_partial_is_recorded_not_discarded(tmp_path):
    """P3-B: done_for_day (a terminal-close status) can carry a realized partial —
    record it rather than raising as a pure reject and discarding the fill."""
    order = _FakeOrder(
        status="done_for_day", filled_avg_price=100.0, filled_qty=80.0
    )
    client = _FakeClient(equity=98_000.0, submit_result=order, poll_order=order)
    reactor = _reactor(client, tmp_path)

    rec = reactor.execute(_proposal(), fill_size_pct=0.20)
    assert rec.reactor_metadata["filled_qty"] == pytest.approx(80.0)
    assert rec.fill_size_pct == pytest.approx((100.0 * 80.0) / 98_000.0)


def test_double_submit_guard_client_order_id_set(tmp_path):
    """P2-A: the order carries client_order_id == proposal_id so a retry collides
    at the broker (rejected) instead of double-submitting."""
    order = _FakeOrder(status="filled", filled_avg_price=100.0, filled_qty=196.0)
    client = _FakeClient(equity=98_000.0, submit_result=order)
    reactor = _reactor(client, tmp_path)

    prop = _proposal()
    reactor.execute(prop, fill_size_pct=0.20)
    req = client.submitted[0]
    assert getattr(req, "client_order_id", None) == prop.proposal_id


def test_zero_notional_fails_closed(tmp_path):
    """P2-C: a notional that rounds below the $1 minimum is refused, not submitted
    as a meaningless $0.00 order."""
    order = _FakeOrder(status="filled", filled_avg_price=100.0, filled_qty=1.0)
    client = _FakeClient(equity=98_000.0, submit_result=order)
    reactor = _reactor(client, tmp_path)

    # 0.000001 * 98_000 = $0.098 -> rounds to $0.10 -> below $1 minimum.
    with pytest.raises(AlpacaSubmitError) as ei:
        reactor.execute(_proposal(), fill_size_pct=0.000001)
    assert "minimum" in str(ei.value).lower() or "below" in str(ei.value).lower()
    # Nothing submitted.
    assert client.submitted == []


# --------------------------------------------------------------------------- #
# 6. NAV-fraction round-trip
# --------------------------------------------------------------------------- #


def test_buying_power_reject_raises_not_silent(tmp_path):
    client = _FakeClient(
        equity=98_000.0,
        submit_raises=Exception("insufficient buying power (403)"),
    )
    reactor = _reactor(client, tmp_path)

    with pytest.raises(AlpacaSubmitError) as ei:
        reactor.execute(_proposal(), fill_size_pct=0.20)
    assert "insufficient buying power" in str(ei.value)

    # NO fabricated fill written to the bus.
    lines = [ln for ln in reactor.executions_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 0


def test_terminal_reject_status_raises(tmp_path):
    # submit succeeds but the order goes to a terminal 'rejected' status.
    order = _FakeOrder(status="rejected", filled_avg_price=None, filled_qty=None)
    client = _FakeClient(equity=98_000.0, submit_result=order, poll_order=order)
    reactor = _reactor(client, tmp_path)

    with pytest.raises(AlpacaSubmitError) as ei:
        reactor.execute(_proposal(), fill_size_pct=0.20)
    assert "rejected" in str(ei.value).lower()


# --------------------------------------------------------------------------- #
# Non-finite NAV guard (ar32/ar49 family, on the NAV numerator)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad_equity",
    [
        __import__("decimal").Decimal("NaN"),  # Alpaca returns Decimals
        "inf",  # transient string equity
        "1e400",  # overflows to +inf via float()
        "nan",
        float("inf"),
        float("nan"),
    ],
)
def test_non_finite_equity_fails_closed(tmp_path, bad_equity):
    """A non-finite broker equity (NaN/inf) must FAIL CLOSED in
    _fetch_account_equity rather than sizing an order off a bad NAV.

    ``to_float`` catches only (TypeError, ValueError), so float(Decimal('NaN')),
    float('inf'), float('1e400') all SUCCEED and return nan/inf. The ``equity<=0``
    guard does NOT catch them (nan<=0 is False, inf<=0 is False), and the
    downstream ``round(notional,2) < 1.0`` zero-notional guard also misses them
    (nan<1.0 is False, +inf<1.0 is False) — so a NaN/inf-notional order would
    otherwise reach client.submit_order. The guard must be finite-aware."""
    order = _FakeOrder(status="filled", filled_avg_price=100.0, filled_qty=1.0)
    client = _FakeClient(equity=bad_equity, submit_result=order)
    reactor = _reactor(client, tmp_path)

    # 1. _fetch_account_equity must RAISE (not return nan/inf).
    with pytest.raises(AlpacaSubmitError) as ei:
        reactor._fetch_account_equity(client)
    assert "equity" in str(ei.value).lower()

    # 2. The full execute() path must also fail closed and NEVER submit an order.
    with pytest.raises(AlpacaSubmitError):
        reactor.execute(_proposal(), fill_size_pct=0.20)
    assert client.submitted == []  # no NaN/inf-notional order reached the broker

    # 3. No fabricated/garbage fill written to the bus.
    lines = [ln for ln in reactor.executions_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 0


# --------------------------------------------------------------------------- #
# 5. Unfilled timeout -> fill_size_pct=0 + metadata
# --------------------------------------------------------------------------- #


def test_unfilled_timeout_zero_fill_and_metadata(tmp_path):
    # Order stays 'accepted' (non-terminal, never fills) -> poll budget elapses.
    order = _FakeOrder(order_id="ord-stuck", status="accepted")
    client = _FakeClient(equity=98_000.0, submit_result=order, poll_order=order)
    reactor = _reactor(client, tmp_path, poll_timeout_s=0.05, poll_interval_s=0.0)

    rec = reactor.execute(_proposal(), fill_size_pct=0.20)

    assert rec.fill_size_pct == 0.0
    assert rec.fill_price == 0.0  # NEVER fabricated
    assert rec.reactor_metadata["unfilled_timeout"] is True
    assert rec.reactor_metadata["alpaca_order_id"] == "ord-stuck"
    # The unfilled record IS written to the bus (for later reconciliation) but
    # moves no position.
    lines = [ln for ln in reactor.executions_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1


# --------------------------------------------------------------------------- #
# 6. NAV-fraction round-trip
# --------------------------------------------------------------------------- #


def test_nav_fraction_round_trip_notional(tmp_path):
    order = _FakeOrder(status="filled", filled_avg_price=100.0, filled_qty=196.0)
    client = _FakeClient(equity=98_000.0, submit_result=order)
    reactor = _reactor(client, tmp_path)

    reactor.execute(_proposal(), fill_size_pct=0.20)
    req = client.submitted[0]
    # 0.20 of $98k = $19,600 notional.
    assert float(req.notional) == pytest.approx(19_600.0)


# --------------------------------------------------------------------------- #
# 7. Reconcile writes to the "alpaca-paper" account partition
# --------------------------------------------------------------------------- #


def test_reconcile_writes_alpaca_paper_partition(tmp_path, monkeypatch):
    captured: dict[str, Any] = {}

    class _FakePS:
        def apply_execution(self, record: dict[str, Any]) -> None:
            captured.update(record)

    import hermes_quant.state.portfolio_state as ps_mod

    monkeypatch.setattr(ps_mod, "get_portfolio_state", lambda: _FakePS())

    order = _FakeOrder(status="filled", filled_avg_price=100.0, filled_qty=196.0)
    client = _FakeClient(equity=98_000.0, submit_result=order)
    reactor = _reactor(client, tmp_path)

    reactor.execute(_proposal(), fill_size_pct=0.20)

    assert captured["account_id"] == "alpaca-paper"
    assert captured["account_id"] != "paper-default"
    # signed-shares quantity carried for true-unit position tracking.
    assert captured["reactor_metadata"]["quantity"] == pytest.approx(196.0)


# --------------------------------------------------------------------------- #
# 8. select_reactor flag gating
# --------------------------------------------------------------------------- #


def test_select_reactor_flag_off_returns_paper(monkeypatch):
    # Clear BOTH equity-routing flags so the test asserts the documented legacy
    # default (PaperReactor) hermetically — independent of ambient env. The operator
    # shell / daemon may export HERMES_QUANT_DETERMINISTIC_EQUITY=1, which would
    # otherwise correctly route to DeterministicEquityReactor and fail this assertion.
    monkeypatch.delenv("HERMES_QUANT_ALPACA_PAPER", raising=False)
    monkeypatch.delenv("HERMES_QUANT_DETERMINISTIC_EQUITY", raising=False)
    from hermes_quant.react.dispatch import select_reactor
    from hermes_quant.react.paper import PaperReactor

    r = select_reactor(_proposal())
    assert isinstance(r, PaperReactor)
    assert r.name == "paper"


def test_select_reactor_flag_on_returns_alpaca(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_ALPACA_PAPER", "1")
    from hermes_quant.react.dispatch import select_reactor

    r = select_reactor(_proposal())
    assert isinstance(r, AlpacaPaperReactor)
    assert r.name == "alpaca_paper"


def test_select_reactor_flag_on_multileg_untouched(monkeypatch):
    """Flag-ON must NOT touch the multi-leg branch."""
    monkeypatch.setenv("HERMES_QUANT_ALPACA_PAPER", "1")
    from hermes_quant.react.dispatch import select_reactor
    from hermes_quant.react.multileg import MultiLegPaperReactor

    class _MLProposal:
        proposal_kind = "multi_leg"
        option_legs = ()
        strategy_kind = "vertical"

    r = select_reactor(_MLProposal())
    assert isinstance(r, MultiLegPaperReactor)


# --------------------------------------------------------------------------- #
# 9. Shadow divergence log
# --------------------------------------------------------------------------- #


def test_shadow_logs_divergence(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_ALPACA_SHADOW", "1")
    from hermes_quant.react.alpaca_shadow import run_shadow

    # Synthetic record (the REAL fill PaperReactor would have produced): fills at
    # decision_price 100.0, full requested size.
    class _SynRec:
        fill_price = 100.0
        fill_size_pct = 0.20
        reactor_metadata: dict[str, Any] = {}

    # Alpaca fills at 101.5 (1.5 divergence) and a slightly different realized size.
    order = _FakeOrder(status="filled", filled_avg_price=101.5, filled_qty=193.0)
    client = _FakeClient(equity=98_000.0, submit_result=order)
    shadow_reactor = _reactor(client, tmp_path)

    div_path = tmp_path / "alpaca-shadow-divergence.jsonl"
    div = run_shadow(
        _proposal(),
        _SynRec(),
        fill_size_pct=0.20,
        divergence_path=div_path,
        reactor=shadow_reactor,
    )

    assert div is not None
    assert div["synthetic_fill_price"] == 100.0
    assert div["alpaca_fill_price"] == 101.5
    assert div["fill_price_divergence"] == pytest.approx(1.5)
    assert div["alpaca_order_id"] == "ord-123"
    # Written to the log.
    lines = [ln for ln in div_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1


def test_shadow_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_ALPACA_SHADOW", raising=False)
    from hermes_quant.react.alpaca_shadow import run_shadow

    class _SynRec:
        fill_price = 100.0
        fill_size_pct = 0.20
        reactor_metadata: dict[str, Any] = {}

    # No reactor needed — shadow short-circuits on the disabled flag.
    div = run_shadow(_proposal(), _SynRec(), fill_size_pct=0.20)
    assert div is None


def test_shadow_never_raises_on_reactor_failure(tmp_path, monkeypatch):
    """Shadow must swallow ALL errors — never block/alter the synthetic fill."""
    monkeypatch.setenv("HERMES_QUANT_ALPACA_SHADOW", "1")
    from hermes_quant.react.alpaca_shadow import run_shadow

    class _SynRec:
        fill_price = 100.0
        fill_size_pct = 0.20
        reactor_metadata: dict[str, Any] = {}

    # Reactor whose client raises on submit — shadow must catch and return None.
    client = _FakeClient(equity=98_000.0, submit_raises=Exception("boom"))
    shadow_reactor = _reactor(client, tmp_path)

    div = run_shadow(
        _proposal(),
        _SynRec(),
        fill_size_pct=0.20,
        divergence_path=tmp_path / "div.jsonl",
        reactor=shadow_reactor,
    )
    assert div is None  # swallowed, non-blocking


# --------------------------------------------------------------------------- #
# Missing-creds fail-closed (no client injected, no env)
# --------------------------------------------------------------------------- #


def test_missing_creds_fails_closed(tmp_path, monkeypatch):
    for var in (
        "ALPACA_API_KEY",
        "ALPACA_API_KEY_ID",
        "ALPACA_API_SECRET",
        "ALPACA_API_SECRET_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    # No client injected -> lazy build -> missing creds -> AlpacaSubmitError.
    reactor = AlpacaPaperReactor(executions_path=tmp_path / "executions.jsonl")
    with pytest.raises(AlpacaSubmitError):
        reactor.execute(_proposal(), fill_size_pct=0.20)


# --------------------------------------------------------------------------- #
# 10. Non-finite / <=0 decision_price -> fail-closed silence (precondition parity
#     with PaperReactor / DeterministicEquityReactor). A proposal that reaches
#     execute() with no advisor decision_price AND no analyst_views.last_close
#     makes _extract_decision_price return the 0.0 sentinel; the reactor must NOT
#     submit a broker order off a corrupt entry-basis and must NOT record/reconcile
#     a 0.0/NaN decision_price verbatim.
# --------------------------------------------------------------------------- #


def _proposal_missing_price(*, symbol: str = "AAPL") -> Proposal:
    """A proposal whose advisor_result has no decision_price and empty views.

    ``_extract_decision_price`` falls all the way through to the 0.0 sentinel.
    """
    return Proposal(
        proposal_id=f"prop_2026-06-05T00:00:00_{symbol}_nodp",
        state="pending",
        symbol=symbol,
        asset_class="equity",
        timeframe="1d",
        created_at="2026-06-05T00:00:00Z",
        expires_at="2026-06-05T01:00:00Z",
        advisor_result={
            "as_of": "2026-06-05T00:00:00Z",
            "signal_id": "sig-nodp",
            "analyst_views": [],  # no last_close fallback
            # NOTE: no "decision_price" key -> 0.0 sentinel from the extractor.
        },
    )


def test_zero_decision_price_fails_closed_no_submit(tmp_path):
    # The extractor would return 0.0 for this proposal.
    assert AlpacaPaperReactor._extract_decision_price(_proposal_missing_price()) == 0.0

    # Configure a fake that WOULD fill if the submit path were reached, so the
    # test proves the guard short-circuits BEFORE any broker order.
    order = _FakeOrder(status="filled", filled_avg_price=101.0, filled_qty=194.0)
    client = _FakeClient(equity=98_000.0, submit_result=order)
    reactor = _reactor(client, tmp_path)

    rec = reactor.execute(_proposal_missing_price(), fill_size_pct=0.20)

    # Fail-closed silence/no-fill record: zero fill, no fabricated price.
    assert rec.fill_size_pct == 0.0
    assert rec.fill_price == 0.0
    assert rec.reactor_name == "alpaca_paper"
    assert rec.reactor_metadata.get("silence_reason") == "zero_decision_price"

    # The broker submit path was NEVER reached (no order submitted, no poll).
    assert client.submitted == []
    assert client.poll_calls == 0


def test_zero_decision_price_not_reconciled_to_state(tmp_path, monkeypatch):
    # apply_execution must NOT be called for the fail-closed silence record (an
    # unfilled silence moves no position -> never poison state.db cost-basis).
    order = _FakeOrder(status="filled", filled_avg_price=101.0, filled_qty=194.0)
    client = _FakeClient(equity=98_000.0, submit_result=order)
    reactor = _reactor(client, tmp_path)

    calls: list[Any] = []

    class _SpyState:
        def apply_execution(self, record_dict: Any) -> None:
            calls.append(record_dict)

    monkeypatch.setattr(
        "hermes_quant.state.portfolio_state.get_portfolio_state",
        lambda: _SpyState(),
    )

    rec = reactor.execute(_proposal_missing_price(), fill_size_pct=0.20)

    assert rec.fill_size_pct == 0.0
    assert calls == []  # never reconciled
