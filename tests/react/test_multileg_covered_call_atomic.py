"""e572 (P1): the covered-call (short-option-covered-by-equity) execution in
``MultiLegPaperReactor._fill`` must be no-orphan ATOMIC.

THE BUG (pre-fix): the single-leg branch submitted + FILLED the SHORT CALL first,
then submitted the equity (cover) leg. An equity-leg failure AFTER the short call
filled stranded a NAKED SHORT CALL (undefined risk) at the broker — the reactor
wrote a no-fill parent (so the system believed nothing happened) while the broker
held a real filled short with no cover.

THE FIX (cover-first): submit the EQUITY (cover) leg FIRST and only submit the short
option after the cover is confirmed filled. A short-option failure then leaves only
a long stock position (defined risk). If the short option fails after the cover
filled, the cover is unwound and a no-fill record is returned (NEVER a standing
naked short, and NEVER a stranded uncovered stock).

The cardinal invariant asserted here: a SUBMITTED-AND-FILLED short option is NEVER
left without its filled equity cover.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from hermes_quant.options.data import (
    NetGreeks,
    OptionGreeksSnapshot,
    OptionLeg,
    StockLeg,
)
from hermes_quant.options.multileg import MultiLegProposal
from hermes_quant.react.backend import FillResult
from hermes_quant.react.multileg import MultiLegPaperReactor
from hermes_quant.risk.options_gate import OptionsGateResult, StructureBucket
from hermes_quant.state.portfolio_state import PortfolioState


# --------------------------------------------------------------------------- #
# Fixtures / builders (mirror tests/unit/test_multileg_reactor_fill.py)
# --------------------------------------------------------------------------- #
@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_MULTILEG_REACTOR", "1")
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    monkeypatch.delenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", raising=False)
    monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)
    monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    ps = PortfolioState(state_db_path=db)
    import hermes_quant.state.portfolio_state as ps_mod

    monkeypatch.setattr(ps_mod, "_singleton", ps, raising=False)
    return ps


def _snap(**kw) -> OptionGreeksSnapshot:
    base = dict(delta=0.25, gamma=0.01, theta=-0.02, vega=0.10, rho=0.01, iv=0.4)
    base.update(kw)
    return OptionGreeksSnapshot(**base)


def _admitted_gate() -> OptionsGateResult:
    return OptionsGateResult(
        admitted=True,
        bucket=StructureBucket.COVERED_CALL,
        reason=None,
        net_greeks=NetGreeks(delta=75.0, gamma=-1.0, theta=3.0, vega=-10.0),
        bpr_estimate=0.0,
        max_loss=None,
        contracts=1,
        warnings=(),
    )


def _cc(*, pid="prop_20260530T180000_NVDA_cc0001") -> MultiLegProposal:
    call = OptionLeg(
        symbol="NVDA260626C00160000",
        side="sell",
        position_intent="sell_to_open",
        ratio_qty=1,
        greeks_at_decision=_snap(),
        fill_price=4.50,
    )
    return MultiLegProposal.from_gate_result(
        gate_result=_admitted_gate(),
        proposal_id=pid,
        asof=datetime(2026, 5, 30, 18, 0, 0, tzinfo=UTC),
        strategy_kind="covered_call",
        underlying="NVDA",
        option_legs=(call,),
        stock_leg=StockLeg(underlying="NVDA", qty=100, basis_per_share=160.0),
        outer_qty=1,
        net_debit_credit=Decimal("-4.50"),
        max_gain=Decimal("450"),
        breakeven_underlying=(Decimal("155.50"),),
        rationale="cc",
        source_recipe_id="r_cc",
    )


class _CCBackend:
    """A spy backend whose equity leg FAILS but whose short option leg FILLS.

    Records the order in which legs were submitted so the test can assert the
    no-orphan ordering invariant directly. ``submit_equity`` raises on the FIRST
    (cover) call but SUCCEEDS on a later opposite-signed (unwind) call so a fix that
    unwinds the cover after a short-fill failure can still settle — though the
    cover-first fix never reaches that path here.
    """

    name = "deterministic"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.short_filled = False
        self.equity_filled = False
        self.equity_unwound = False

    def account_equity(self) -> float | None:
        return 1_000_000.0

    def buying_power(self) -> float | None:
        return 1_000_000.0

    def submit_option_single(self, leg, *, qty, limit_price, client_order_id) -> FillResult:
        self.calls.append("option")
        # The short call FILLS at the broker.
        self.short_filled = True
        sgn = 1.0 if leg.side == "buy" else -1.0
        return FillResult(
            symbol=leg.symbol,
            filled_avg_price=4.50,
            filled_qty=sgn * qty,
            status="filled",
            position_intent=leg.position_intent,
            source=self.name,
        )

    def submit_equity(self, *, symbol, signed_qty, decision_price, client_order_id) -> FillResult:
        if signed_qty < 0 and self.equity_filled:
            # An unwind of a previously-filled long cover — allow it to settle.
            self.calls.append("equity_unwind")
            self.equity_unwound = True
            return FillResult(
                symbol=symbol,
                filled_avg_price=decision_price or 160.0,
                filled_qty=float(signed_qty),
                status="filled",
                position_intent="sell_to_close",
                source=self.name,
            )
        # The COVER (long equity) submission FAILS — simulate a broker reject on the
        # equity leg (e.g. a transient 422 / venue outage). This is the exact failure
        # that, under the OLD short-first ordering, stranded a naked short call.
        self.calls.append("equity_FAIL")
        raise RuntimeError("simulated equity-leg broker reject (cover failed)")

    def submit_option_mleg(self, *args, **kwargs) -> FillResult:  # pragma: no cover
        raise AssertionError("CC is a single-leg-option structure; mleg not used")


# --------------------------------------------------------------------------- #
# The no-orphan invariant
# --------------------------------------------------------------------------- #
def test_cc_equity_leg_failure_never_strands_naked_short(
    enabled, state_db, tmp_path, monkeypatch
) -> None:
    """With the equity (cover) leg failing, the reactor must NEVER leave a filled
    short option without its filled cover.

    PRE-FIX (RED): the reactor filled the short call FIRST, then the equity cover
    failed -> ``backend.short_filled is True`` while ``backend.equity_filled is
    False`` and ``backend.equity_unwound is False`` => a STANDING NAKED SHORT.

    POST-FIX (GREEN): either the short is never submitted (cover failed first ->
    ``calls == ['equity_FAIL']``), or the cover-filled-then-short-failed path
    unwinds the cover. In BOTH cases there is NO standing naked short.
    """
    bus = tmp_path / "executions.jsonl"
    backend = _CCBackend()

    import hermes_quant.react.multileg as mleg_mod

    monkeypatch.setattr(mleg_mod, "select_backend", lambda *a, **k: backend)

    reactor = MultiLegPaperReactor(executions_path=bus)
    parent = reactor.execute(_cc(), fill_size_pct=0.05)

    # ---- The atomic no-orphan invariant (the load-bearing assertion). ----
    # A filled short option with no filled (and not-unwound) cover is a NAKED SHORT.
    naked_short = backend.short_filled and not (
        backend.equity_filled or backend.equity_unwound
    )
    assert not naked_short, (
        "no-orphan VIOLATION: short option filled but its equity cover is neither "
        f"filled nor unwound (calls={backend.calls})"
    )

    # The failed atomic attempt must surface as a no-fill record (the shared
    # _apply_fire_accounting guard reads reactor_metadata.no_fill).
    assert (parent.reactor_metadata or {}).get("no_fill") is True
    assert parent.fill_size_pct == 0.0

    # Never fabricate a fill: no family written, no positions mutated.
    assert not bus.exists()
    assert state_db.get_positions("paper-default") == {}


def test_cc_cover_submitted_before_short(enabled, state_db, tmp_path, monkeypatch) -> None:
    """Cover-first ordering: the EQUITY (cover) leg is submitted BEFORE the short
    option. PRE-FIX the first submit was the option (short-first), so the equity
    submit only happened AFTER the short already filled.
    """
    bus = tmp_path / "executions.jsonl"
    backend = _CCBackend()

    import hermes_quant.react.multileg as mleg_mod

    monkeypatch.setattr(mleg_mod, "select_backend", lambda *a, **k: backend)

    reactor = MultiLegPaperReactor(executions_path=bus)
    reactor.execute(_cc(), fill_size_pct=0.05)

    # With the equity cover failing on submit, a cover-first reactor records ONLY
    # the equity attempt and NEVER submits the short option (no naked short can
    # exist). PRE-FIX: calls == ['option', 'equity_FAIL'] (short filled first).
    assert "option" not in backend.calls, (
        f"short option was submitted despite a cover-first contract (calls={backend.calls})"
    )
    assert backend.calls[0] == "equity_FAIL"


class _ShortFailsAfterCoverBackend:
    """Cover FILLS, then the SHORT option leg fails. Exercises the unwind branch.

    Records calls so the test can assert: cover filled, short submitted+failed,
    cover unwound (opposite-signed equity). The cardinal invariant — no standing
    naked short — holds because the short never filled.
    """

    name = "deterministic"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.short_filled = False
        self.cover_signed_qty = 0.0
        self.unwind_signed_qty = None

    def account_equity(self) -> float | None:
        return 1_000_000.0

    def buying_power(self) -> float | None:
        return 1_000_000.0

    def submit_equity(self, *, symbol, signed_qty, decision_price, client_order_id) -> FillResult:
        if client_order_id.endswith("-eq-unwind"):
            self.calls.append("equity_unwind")
            self.unwind_signed_qty = float(signed_qty)
            return FillResult(
                symbol=symbol,
                filled_avg_price=decision_price or 160.0,
                filled_qty=float(signed_qty),
                status="filled",
                position_intent="sell_to_close",
                source=self.name,
            )
        self.calls.append("equity_cover")
        self.cover_signed_qty = float(signed_qty)
        return FillResult(
            symbol=symbol,
            filled_avg_price=decision_price or 160.0,
            filled_qty=float(signed_qty),
            status="filled",
            position_intent="buy_to_open",
            source=self.name,
        )

    def submit_option_single(self, leg, *, qty, limit_price, client_order_id) -> FillResult:
        self.calls.append("option_FAIL")
        # The short option submission FAILS after the cover already filled.
        raise RuntimeError("simulated short-option broker reject (after cover filled)")

    def submit_option_mleg(self, *args, **kwargs) -> FillResult:  # pragma: no cover
        raise AssertionError("CC is a single-leg-option structure; mleg not used")


def test_cc_short_fails_after_cover_unwinds_cover(
    enabled, state_db, tmp_path, monkeypatch
) -> None:
    """Cover filled, short option then failed -> the cover is UNWOUND and a no-fill
    record is returned. No naked short (the short never filled) and no STANDING
    uncovered stock (the cover was unwound).
    """
    bus = tmp_path / "executions.jsonl"
    backend = _ShortFailsAfterCoverBackend()

    import hermes_quant.react.multileg as mleg_mod

    monkeypatch.setattr(mleg_mod, "select_backend", lambda *a, **k: backend)

    reactor = MultiLegPaperReactor(executions_path=bus)
    parent = reactor.execute(_cc(), fill_size_pct=0.05)

    # Cover-first: cover submitted, short submitted+failed, cover unwound.
    assert backend.calls == ["equity_cover", "option_FAIL", "equity_unwind"]
    # No naked short (short never filled).
    assert backend.short_filled is False
    # The unwind closed the exact cover (opposite sign).
    assert backend.unwind_signed_qty == -backend.cover_signed_qty

    # Fail-closed to a no-fill record; nothing fabricated.
    assert (parent.reactor_metadata or {}).get("no_fill") is True
    assert parent.fill_size_pct == 0.0
    assert not bus.exists()
    assert state_db.get_positions("paper-default") == {}


# --------------------------------------------------------------------------- #
# Byte-identical happy path (the paper reactor path that already works)
# --------------------------------------------------------------------------- #
def test_cc_happy_path_fills_both_legs(enabled, state_db, tmp_path) -> None:
    """The default (deterministic-backend) CC path that already works must still
    fill BOTH legs and reconcile both positions — the fix is byte-identical when
    both legs succeed.
    """
    bus = tmp_path / "executions.jsonl"
    reactor = MultiLegPaperReactor(executions_path=bus)
    reactor.execute(_cc(), fill_size_pct=0.05)

    positions = state_db.get_positions("paper-default")
    assert positions[("equity", "NVDA")].quantity == 100
    assert positions[("us_option", "NVDA260626C00160000")].quantity == -1
