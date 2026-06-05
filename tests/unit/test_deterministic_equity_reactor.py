"""Unit tests for DeterministicEquityReactor + its dispatch wiring (ADR-0088 follow-up).

NO network, NO real DB writes: the BP-enforcing backend is injected via a FAKE
``select_backend`` (monkeypatched in the reactor module) and state.db reconciliation
is captured via a fake ``get_portfolio_state``. The executions bus is a tmp_path file.

The headline behavior being verified: an over-buying-power equity fire is REJECTED at
the reactor (surfaced as a no-fill record), not silently appended to an unbounded book
— closing the 880%-gross root cause on the EQUITY path (not just multileg). The
NAV-fraction -> signed-TRUE-shares conversion mirrors AlpacaPaperReactor EXACTLY:

    notional_usd = abs(fill_size_pct) * account_equity
    shares       = notional_usd / decision_price
    signed_qty   = +shares if fill_size_pct > 0 else -shares

Coverage:
  1.  flag-OFF: select_reactor -> legacy PaperReactor (bit-identical isinstance).
  2.  flag-ON (+ deterministic backend): select_reactor -> DeterministicEquityReactor.
  3.  flag-ON but backend overridden to alpaca: NOT deterministic-equity.
  4.  within-BP long fill: quantity=+true shares, fill_size_pct=NAV fraction.
  5.  OVER-BP fire (InsufficientBuyingPowerError): no-fill, bp_rejected, no crash,
      state.db NOT moved. THE HEADLINE TEST.
  6.  SELL (close/short): NOT BP-blocked, fills.
  7.  unknown equity (account_equity None) -> fail-closed no-fill, no fabrication.
  8.  BackendUnavailableError -> no-fill backend_unavailable.
  9.  NAV->shares conversion matches the AlpacaPaperReactor formula exactly.
  10. within-BP fill reconciles state.db with the signed TRUE-share quantity.
  11. no-fill records carry fill_price=0.0 (never fabricated) + are bus-appended.
  12. account_id partition is the shared 'paper-default' book.
  13. cap clip honored when HERMES_QUANT_PORTFOLIO_CAPS=1 (BP + cap both apply).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import hermes_quant.react.deterministic_equity as det_mod
from hermes_quant.proposals import Proposal
from hermes_quant.react.backend import (
    BackendUnavailableError,
    FillResult,
    InsufficientBuyingPowerError,
)
from hermes_quant.react.deterministic_equity import (
    DETERMINISTIC_EQUITY_ACCOUNT_ID,
    DeterministicEquityReactor,
)

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _FakeBackend:
    """A BP-enforcing fake mirroring DeterministicBackend.submit_equity semantics.

    ``equity`` feeds account_equity(); ``bp`` feeds buying_power() and the BP gate.
    A BUY (signed_qty>0) whose notional exceeds bp raises InsufficientBuyingPowerError;
    a SELL is never BP-blocked. ``equity=None`` / ``bp=None`` model unknown account.
    """

    name = "deterministic"

    def __init__(self, *, equity: float | None = 100_000.0, bp: float | None = 100_000.0) -> None:
        self._equity = equity
        self._bp = bp
        self.submitted: list[dict[str, Any]] = []

    def account_equity(self) -> float | None:
        return self._equity

    def buying_power(self) -> float | None:
        return self._bp

    def submit_equity(
        self, *, symbol: str, signed_qty: float, decision_price: float, client_order_id: str
    ) -> FillResult:
        self.submitted.append(
            {"symbol": symbol, "signed_qty": signed_qty, "decision_price": decision_price}
        )
        if signed_qty > 0:
            notional = abs(signed_qty) * decision_price
            if self._bp is None:
                raise BackendUnavailableError("buying power unknown")
            if notional > self._bp + 1e-6:
                raise InsufficientBuyingPowerError(
                    f"requires ${notional:,.2f} but only ${self._bp:,.2f} available"
                )
        return FillResult(
            symbol=symbol,
            filled_avg_price=decision_price,
            filled_qty=float(signed_qty),
            status="filled",
            position_intent="buy_to_open" if signed_qty > 0 else "sell_to_open",
            order_id=f"det-{client_order_id[:16]}",
            source=self.name,
        )


class _CapturePS:
    """Fake PortfolioState capturing the last apply_execution record."""

    def __init__(self) -> None:
        self.applied: list[dict[str, Any]] = []

    def apply_execution(self, record: dict[str, Any]) -> None:
        self.applied.append(record)


def _proposal(
    *, symbol: str = "AAPL", asset_class: str = "equity", decision_price: float = 100.0
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
def _clean_env(monkeypatch):
    # Keep all the optional seams OFF by default so tests exercise the plain
    # deterministic-equity fill path unless a test opts a seam in.
    for var in (
        "HERMES_QUANT_ADMISSIBILITY",
        "HERMES_QUANT_PORTFOLIO_CAPS",
        "HERMES_QUANT_PAPER_SLIPPAGE_MODEL",
        "HERMES_QUANT_REFLECTION",
        "HERMES_QUANT_BROKER_BACKEND",
        "HERMES_QUANT_ALPACA_PAPER",
        "HERMES_QUANT_DETERMINISTIC_EQUITY",
    ):
        monkeypatch.delenv(var, raising=False)


def _reactor_with_backend(
    tmp_path: Path, backend: _FakeBackend, ps: _CapturePS, monkeypatch
) -> DeterministicEquityReactor:
    """Build a reactor with the fake backend + captured state injected (no network/DB)."""
    monkeypatch.setattr(det_mod, "select_backend", lambda *a, **kw: backend)
    import hermes_quant.state.portfolio_state as ps_mod

    monkeypatch.setattr(ps_mod, "get_portfolio_state", lambda: ps)
    return DeterministicEquityReactor(executions_path=tmp_path / "executions.jsonl")


def _bus_lines(reactor: DeterministicEquityReactor) -> list[str]:
    return [ln for ln in reactor.executions_path.read_text().splitlines() if ln.strip()]


# --------------------------------------------------------------------------- #
# 1-3. Dispatch flag gating
# --------------------------------------------------------------------------- #


def test_select_reactor_flag_off_returns_legacy_paper(monkeypatch):
    """Flag-OFF -> bit-identical legacy PaperReactor (no behavior change)."""
    from hermes_quant.react.dispatch import select_reactor
    from hermes_quant.react.paper import PaperReactor

    r = select_reactor(_proposal())
    assert isinstance(r, PaperReactor)
    assert r.name == "paper"


def test_select_reactor_flag_on_returns_deterministic_equity(monkeypatch):
    """Flag-ON (+ default deterministic backend) -> DeterministicEquityReactor."""
    monkeypatch.setenv("HERMES_QUANT_DETERMINISTIC_EQUITY", "1")
    from hermes_quant.react.dispatch import select_reactor

    r = select_reactor(_proposal())
    assert isinstance(r, DeterministicEquityReactor)
    assert r.name == "deterministic-equity"


def test_select_reactor_flag_on_but_backend_alpaca_not_det_equity(monkeypatch):
    """Flag-ON but the resolved backend is alpaca (override) -> NOT deterministic-equity.

    The branch requires BOTH the explicit flag AND resolve_backend_choice()=='deterministic'.
    """
    monkeypatch.setenv("HERMES_QUANT_DETERMINISTIC_EQUITY", "1")
    monkeypatch.setenv("HERMES_QUANT_BROKER_BACKEND", "alpaca")
    from hermes_quant.react.dispatch import select_reactor
    from hermes_quant.react.paper import PaperReactor

    r = select_reactor(_proposal())
    # Backend override to alpaca means the deterministic-equity branch is skipped;
    # ALPACA_PAPER flag is unset so it falls through to the legacy PaperReactor.
    assert isinstance(r, PaperReactor)
    assert not isinstance(r, DeterministicEquityReactor)


def test_select_reactor_flag_on_multileg_untouched(monkeypatch):
    """Flag-ON must NOT touch the multi-leg branch."""
    monkeypatch.setenv("HERMES_QUANT_DETERMINISTIC_EQUITY", "1")
    from hermes_quant.react.dispatch import select_reactor
    from hermes_quant.react.multileg import MultiLegPaperReactor

    class _MLProposal:
        proposal_kind = "multi_leg"
        option_legs = ()
        strategy_kind = "vertical"

    r = select_reactor(_MLProposal())
    assert isinstance(r, MultiLegPaperReactor)


# --------------------------------------------------------------------------- #
# 4. Within-BP long fill: true shares + NAV-fraction
# --------------------------------------------------------------------------- #


def test_within_bp_long_fill_true_shares_and_fraction(tmp_path, monkeypatch):
    backend = _FakeBackend(equity=100_000.0, bp=100_000.0)
    ps = _CapturePS()
    reactor = _reactor_with_backend(tmp_path, backend, ps, monkeypatch)

    # 0.10 of $100k = $10k notional; at $100/sh -> 100 shares.
    rec = reactor.execute(_proposal(), fill_size_pct=0.10)

    assert backend.submitted[0]["signed_qty"] == pytest.approx(100.0)
    assert backend.submitted[0]["decision_price"] == pytest.approx(100.0)
    # quantity is signed TRUE shares (+long); fill_size_pct is the realized NAV frac.
    assert rec.reactor_metadata["quantity"] == pytest.approx(100.0)
    assert rec.fill_price == pytest.approx(100.0)
    assert rec.target_position_pct == pytest.approx(0.10)
    assert rec.fill_size_pct == pytest.approx(0.10)  # 100*100 / 100000
    assert rec.reactor_metadata["account_id"] == DETERMINISTIC_EQUITY_ACCOUNT_ID
    assert rec.reactor_metadata["deterministic_backend"] is True
    # exactly one fill line on the bus.
    assert len(_bus_lines(reactor)) == 1


# --------------------------------------------------------------------------- #
# 5. THE HEADLINE: over-BP fire -> rejected no-fill, state.db NOT moved, no crash
# --------------------------------------------------------------------------- #


def test_over_bp_fire_rejected_as_nofill_no_crash(tmp_path, monkeypatch):
    # bp is only $5k but the fire wants 0.50 of $100k = $50k notional (10x over BP).
    backend = _FakeBackend(equity=100_000.0, bp=5_000.0)
    ps = _CapturePS()
    reactor = _reactor_with_backend(tmp_path, backend, ps, monkeypatch)

    rec = reactor.execute(_proposal(), fill_size_pct=0.50)  # MUST NOT raise

    # No-fill: never a fabricated fill price/size.
    assert rec.fill_price == 0.0
    assert rec.fill_size_pct == 0.0
    assert rec.reactor_metadata["no_fill"] is True
    assert rec.reactor_metadata["bp_rejected"] is True
    assert "no_fill_reason" in rec.reactor_metadata
    # state.db was NOT moved (no position change on a rejection).
    assert ps.applied == []
    # The rejection IS surfaced on the bus (so the autonomous tick logs it).
    assert len(_bus_lines(reactor)) == 1
    # quantity key is absent on a no-fill (nothing filled).
    assert "quantity" not in rec.reactor_metadata


# --------------------------------------------------------------------------- #
# 6. SELL is NOT BP-blocked
# --------------------------------------------------------------------------- #


def test_sell_not_bp_blocked_fills(tmp_path, monkeypatch):
    # bp is near-zero, but a SELL (negative fraction) must still fill (not cash-BP-checked).
    backend = _FakeBackend(equity=100_000.0, bp=0.0)
    ps = _CapturePS()
    reactor = _reactor_with_backend(tmp_path, backend, ps, monkeypatch)

    rec = reactor.execute(_proposal(symbol="GME"), fill_size_pct=-0.10)

    # 0.10 of $100k / $100 = 100 shares, signed negative (short/close).
    assert backend.submitted[0]["signed_qty"] == pytest.approx(-100.0)
    assert rec.reactor_metadata["quantity"] == pytest.approx(-100.0)
    assert rec.fill_price == pytest.approx(100.0)
    assert rec.fill_size_pct == pytest.approx(-0.10)  # negative realized fraction
    assert rec.fill_size_pct < 0
    # SELL fill DOES reconcile state.db (it moves a position).
    assert len(ps.applied) == 1


# --------------------------------------------------------------------------- #
# 7. Unknown NAV -> fail-closed no-fill (no fabrication)
# --------------------------------------------------------------------------- #


def test_unknown_equity_fails_closed_no_fill(tmp_path, monkeypatch):
    backend = _FakeBackend(equity=None, bp=100_000.0)
    ps = _CapturePS()
    reactor = _reactor_with_backend(tmp_path, backend, ps, monkeypatch)

    rec = reactor.execute(_proposal(), fill_size_pct=0.10)

    assert rec.fill_price == 0.0
    assert rec.fill_size_pct == 0.0
    assert rec.reactor_metadata["no_fill"] is True
    assert rec.reactor_metadata["equity_unknown"] is True
    # Nothing submitted to the backend; state.db not moved.
    assert backend.submitted == []
    assert ps.applied == []


def test_non_positive_equity_fails_closed_no_fill(tmp_path, monkeypatch):
    backend = _FakeBackend(equity=0.0, bp=100_000.0)
    ps = _CapturePS()
    reactor = _reactor_with_backend(tmp_path, backend, ps, monkeypatch)

    rec = reactor.execute(_proposal(), fill_size_pct=0.10)
    assert rec.reactor_metadata["equity_unknown"] is True
    assert backend.submitted == []


# --------------------------------------------------------------------------- #
# 8. BackendUnavailableError -> no-fill backend_unavailable
# --------------------------------------------------------------------------- #


def test_backend_unavailable_bp_none_no_fill(tmp_path, monkeypatch):
    # equity known (sizing works) but BP unknown -> backend raises BackendUnavailableError
    # on the BUY guard.
    backend = _FakeBackend(equity=100_000.0, bp=None)
    ps = _CapturePS()
    reactor = _reactor_with_backend(tmp_path, backend, ps, monkeypatch)

    rec = reactor.execute(_proposal(), fill_size_pct=0.10)

    assert rec.fill_price == 0.0
    assert rec.fill_size_pct == 0.0
    assert rec.reactor_metadata["no_fill"] is True
    assert rec.reactor_metadata["backend_unavailable"] is True
    assert ps.applied == []


# --------------------------------------------------------------------------- #
# 9. NAV->shares conversion matches the AlpacaPaperReactor formula exactly
# --------------------------------------------------------------------------- #


def test_nav_to_shares_formula_matches_alpaca(tmp_path, monkeypatch):
    backend = _FakeBackend(equity=98_000.0, bp=1_000_000.0)
    ps = _CapturePS()
    reactor = _reactor_with_backend(tmp_path, backend, ps, monkeypatch)

    # Same numbers as the AlpacaPaperReactor round-trip test: 0.20 of $98k.
    reactor.execute(_proposal(decision_price=100.0), fill_size_pct=0.20)

    expected_notional = abs(0.20) * 98_000.0  # 19_600
    expected_shares = expected_notional / 100.0  # 196
    assert backend.submitted[0]["signed_qty"] == pytest.approx(expected_shares)
    assert backend.submitted[0]["signed_qty"] == pytest.approx(196.0)


# --------------------------------------------------------------------------- #
# 10. Within-BP fill reconciles state.db with signed TRUE-share quantity
# --------------------------------------------------------------------------- #


def test_fill_reconciles_state_with_true_shares(tmp_path, monkeypatch):
    backend = _FakeBackend(equity=100_000.0, bp=100_000.0)
    ps = _CapturePS()
    reactor = _reactor_with_backend(tmp_path, backend, ps, monkeypatch)

    reactor.execute(_proposal(), fill_size_pct=0.10)

    assert len(ps.applied) == 1
    applied = ps.applied[0]
    assert applied["account_id"] == DETERMINISTIC_EQUITY_ACCOUNT_ID
    # The true-share quantity drives apply_execution's real-notional cash accounting.
    assert applied["reactor_metadata"]["quantity"] == pytest.approx(100.0)
    assert applied["fill_price"] == pytest.approx(100.0)


# --------------------------------------------------------------------------- #
# 11. No-fill records: fill_price 0.0, bus-appended, no reconcile
# --------------------------------------------------------------------------- #


def test_nofill_record_appended_but_not_reconciled(tmp_path, monkeypatch):
    backend = _FakeBackend(equity=100_000.0, bp=1_000.0)
    ps = _CapturePS()
    reactor = _reactor_with_backend(tmp_path, backend, ps, monkeypatch)

    reactor.execute(_proposal(), fill_size_pct=0.50)  # over BP

    # Appended to the bus (audit), but state.db untouched.
    assert len(_bus_lines(reactor)) == 1
    assert ps.applied == []


# --------------------------------------------------------------------------- #
# 12. account_id partition is the shared paper-default book
# --------------------------------------------------------------------------- #


def test_account_id_is_shared_paper_default(tmp_path, monkeypatch):
    backend = _FakeBackend(equity=100_000.0, bp=100_000.0)
    ps = _CapturePS()
    reactor = _reactor_with_backend(tmp_path, backend, ps, monkeypatch)

    rec = reactor.execute(_proposal(), fill_size_pct=0.05)
    assert rec.reactor_metadata["account_id"] == "paper-default"
    assert DETERMINISTIC_EQUITY_ACCOUNT_ID == "paper-default"


# --------------------------------------------------------------------------- #
# 13. Portfolio-cap clip honored when HERMES_QUANT_PORTFOLIO_CAPS=1
# --------------------------------------------------------------------------- #


def test_cap_clip_and_bp_both_apply(tmp_path, monkeypatch):
    """With HERMES_QUANT_PORTFOLIO_CAPS=1 the cap clips the NAV fraction first; the
    backend then fills the (clipped) order under BP — BP + cap both honored.

    We monkeypatch the reactor's reused PaperReactor cap helper to a deterministic
    partial-scale so the test doesn't depend on the cap module internals.
    """
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
    backend = _FakeBackend(equity=100_000.0, bp=100_000.0)
    ps = _CapturePS()
    reactor = _reactor_with_backend(tmp_path, backend, ps, monkeypatch)

    # Force the reused cap helper to clip 0.50 -> 0.10 and surface cap metadata.
    def _fake_cap(proposal, fill_size_pct, now, *, play_tag="advisor"):
        return None, 0.10, {
            "cap_scaled_from": fill_size_pct,
            "cap_scaled_to": 0.10,
            "cap_scale_factor": 0.20,
        }

    monkeypatch.setattr(reactor._pre, "_portfolio_cap_clip", _fake_cap)

    rec = reactor.execute(_proposal(), fill_size_pct=0.50)

    # Fill happened at the CLIPPED 0.10 fraction -> 100 shares, not 500.
    assert backend.submitted[0]["signed_qty"] == pytest.approx(100.0)
    assert rec.target_position_pct == pytest.approx(0.10)
    # Cap audit trail is merged into the record.
    assert rec.reactor_metadata["cap_scaled_from"] == pytest.approx(0.50)
    assert rec.reactor_metadata["cap_scaled_to"] == pytest.approx(0.10)


def test_cap_full_silence_returns_silence_record(tmp_path, monkeypatch):
    """A full-silence cap outcome short-circuits to the silenced record (no backend call)."""
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
    backend = _FakeBackend(equity=100_000.0, bp=100_000.0)
    ps = _CapturePS()
    reactor = _reactor_with_backend(tmp_path, backend, ps, monkeypatch)

    from hermes_quant.react.base import ExecutionRecord

    silence = ExecutionRecord(
        proposal_id="prop_x",
        signal_id=None,
        asset="AAPL",
        asset_class="equity",
        timeframe="1d",
        asof_decision="2026-06-05T00:00:00Z",
        asof_execution="2026-06-05T00:00:00Z",
        target_position_pct=0.50,
        decision_price=100.0,
        fill_price=100.0,
        fill_size_pct=0.0,
        reactor_name="paper",
        human_in_the_loop=True,
        reactor_metadata={"silenced": True},
    )

    monkeypatch.setattr(
        reactor._pre,
        "_portfolio_cap_clip",
        lambda *a, **k: (silence, 0.0, None),
    )

    rec = reactor.execute(_proposal(), fill_size_pct=0.50)
    assert rec is silence
    # No backend submit on a full silence.
    assert backend.submitted == []


# --------------------------------------------------------------------------- #
# 14. Admissibility reject short-circuits (reused seam parity)
# --------------------------------------------------------------------------- #


def test_admissibility_reject_short_circuits(tmp_path, monkeypatch):
    backend = _FakeBackend(equity=100_000.0, bp=100_000.0)
    ps = _CapturePS()
    reactor = _reactor_with_backend(tmp_path, backend, ps, monkeypatch)

    from hermes_quant.react.base import ExecutionRecord

    reject = ExecutionRecord(
        proposal_id="prop_x",
        signal_id=None,
        asset="AAPL",
        asset_class="equity",
        timeframe="1d",
        asof_decision="2026-06-05T00:00:00Z",
        asof_execution="2026-06-05T00:00:00Z",
        target_position_pct=-0.10,
        decision_price=100.0,
        fill_price=0.0,
        fill_size_pct=0.0,
        reactor_name="paper",
        human_in_the_loop=True,
        reactor_metadata={"admissibility_rejected": True},
    )
    monkeypatch.setattr(
        reactor._pre, "_admissibility_reject", lambda *a, **k: reject
    )

    rec = reactor.execute(_proposal(), fill_size_pct=-0.10)
    assert rec is reject
    assert backend.submitted == []


def test_refire_is_idempotent_no_double_book(tmp_path, monkeypatch):
    """ADR-0088 F-1: re-executing an already-FILLED proposal returns the existing
    fill (no-op) instead of double-booking. The deterministic backend has no
    server-side client_order_id dedup, so the reactor's bus-scan is the guard."""
    backend = _FakeBackend(equity=100_000.0, bp=100_000.0)
    ps = _CapturePS()
    reactor = _reactor_with_backend(tmp_path, backend, ps, monkeypatch)
    p = _proposal(symbol="AAPL", decision_price=100.0)

    r1 = reactor.execute(p, fill_size_pct=0.1, play_tag="autonomous")
    assert r1.fill_price > 0.0
    n_applied_after_first = len(ps.applied)
    n_bus_after_first = len(_bus_lines(reactor))

    # Re-fire the SAME proposal: must be a no-op (no second apply_execution, no
    # second bus fill line, identical record returned).
    r2 = reactor.execute(p, fill_size_pct=0.1, play_tag="autonomous")
    assert r2.proposal_id == r1.proposal_id
    assert r2.fill_price == r1.fill_price
    assert (r2.reactor_metadata or {}).get("quantity") == (r1.reactor_metadata or {}).get("quantity")
    # No double-book: state.db not touched again on the re-fire.
    assert len(ps.applied) == n_applied_after_first, "re-fire double-reconciled state.db"
    # The bus may gain at most a duplicate audit line of the SAME fill; the
    # idempotency path returns BEFORE appending, so the fill-line count is stable.
    assert len(_bus_lines(reactor)) == n_bus_after_first, "re-fire wrote a second fill to the bus"


def test_unexpected_backend_exception_becomes_nofill_not_crash(tmp_path, monkeypatch):
    """ADR-0088 F-2: an UNEXPECTED backend exception (not BP/Unavailable) must
    become a clean no-fill, honoring the 'reactor never crashes' contract."""

    class _BoomBackend:
        name = "boom"

        def account_equity(self):
            return 100_000.0

        def buying_power(self):
            return 100_000.0

        def submit_equity(self, **kwargs):
            raise RuntimeError("unexpected venue glitch")

    ps = _CapturePS()
    reactor = _reactor_with_backend(tmp_path, _BoomBackend(), ps, monkeypatch)
    p = _proposal(symbol="AAPL", decision_price=100.0)

    # Must NOT raise.
    rec = reactor.execute(p, fill_size_pct=0.1, play_tag="autonomous")
    md = rec.reactor_metadata or {}
    assert rec.fill_price == 0.0, "fabricated a fill on an unexpected backend error"
    assert rec.fill_size_pct == 0.0
    assert md.get("backend_error") is True, "unexpected exception not tagged backend_error"
    # No-fill moves no position.
    assert ps.applied == [], "reconciled state.db on a no-fill"
