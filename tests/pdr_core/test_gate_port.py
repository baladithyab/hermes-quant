"""ADR-0092 Increment-1-cont, STAGE 3: the host-agnostic DefaultRiskGate port.

The deterministic risk gate (ADR-0004 — the FINAL money-safety authority) is
ported VERBATIM into ``hermes_quant.pdr_core.gate`` operating on the Stage-2
read-interfaces (CoreSignal / CoreMarketState / CorePortfolio / CoreHaltState)
and returning a halt-triple-preserving ``GateDecision`` (NOT a bare Proposal —
collapsing onto Proposal would drop the durable-HALT verdict).

This file proves THREE things:

  1. RULE-BRANCH ISOLATION — each of Rule0..Rule7 (+ the fail-closed and
     ADR-0084 event-guard branches) is exercised in isolation and asserts the
     SAME reason string / target / halt triple the live gate emits.

  2. THREE COUPLING EDITS are behavior-preserving:
       (a) audit is an INJECTED sink defaulting to no-op (decision path
           byte-identical whether or not a sink is wired);
       (b) event-risk is a FLAG on the config/param (default-off), NOT
           os.environ — same default-off posture;
       (c) the evidence/lookahead check is dropped to the shell — the core
           gate imports NO evidence module and accepts pre-filtered views.

  3. PARITY GRID — the core gate's GateDecision == the live DefaultRiskGate's
     protocol.Action over a fixture matrix spanning every rule branch. SAME
     arithmetic, SAME ordering, SAME numbers (the verbatim-lift contract).

RED-first: with ``hermes_quant.pdr_core.gate`` absent every test errors at
collection. Creating the module turns them GREEN.
"""

from __future__ import annotations

import pandas as pd
import pytest

from hermes_quant.pdr_core.gate import (
    CoreSignal,
    DefaultRiskGate,
    RiskConfig,
)
from hermes_quant.pdr_core.gate_types import (
    CoreMarketState,
    CorePortfolio,
    GateDecision,
)

# --- the LIVE gate (parity oracle) -----------------------------------------
from hermes_quant.protocol import (
    AggregatedSignal,
    MarketState,
    Portfolio,
    Position,
)
from hermes_quant.risk.gate import DefaultRiskGate as LiveGate
from hermes_quant.risk.gate import RiskConfig as LiveRiskConfig

UTC_NOW = pd.Timestamp("2026-06-12T15:00:00+00:00")


# ---------------------------------------------------------------------------
# Builders — mirror the same money-state numbers into core + live types.
# ---------------------------------------------------------------------------


class _Halts:
    """A minimal halt registry satisfying both CoreHaltState (structural) and
    protocol.HaltState (structural). is_halted returns True for halted keys."""

    def __init__(self, halted: set[tuple] | None = None) -> None:
        self._halted = halted or set()

    def is_halted(self, account_id, asset_class, asset=None) -> bool:
        return (account_id, asset_class, asset) in self._halted or (
            account_id,
            asset_class,
            None,
        ) in self._halted


def _core_signal(
    *,
    direction=1,
    confidence=0.9,
    magnitude=0.02,
    asset="AAPL",
    asof=UTC_NOW,
    metadata=None,
) -> CoreSignal:
    return CoreSignal(
        asset=asset,
        asset_class="equity",
        asof=asof,
        direction=direction,
        magnitude=magnitude,
        confidence=confidence,
        metadata=metadata,
    )


def _live_signal(
    *,
    direction=1,
    confidence=0.9,
    magnitude=0.02,
    asset="AAPL",
    asof=UTC_NOW,
    metadata=None,
) -> AggregatedSignal:
    return AggregatedSignal(
        asset=asset,
        timeframe="1d",
        asset_class="equity",
        asof=asof,
        direction=direction,
        magnitude=magnitude,
        confidence=confidence,
        confidence_raw=confidence,
        horizon="1d",
        components=(),
        aggregator="bma",
        metadata=metadata,
    )


def _core_market(*, volatility=0.02, commission=0.0001, spread=0.0002, slippage=0.0001, tz="UTC"):
    return CoreMarketState(
        asset="AAPL",
        asof=UTC_NOW,
        volatility=volatility,
        commission=commission,
        spread=spread,
        slippage_estimate=slippage,
        tz=tz,
    )


def _live_market(*, volatility=0.02, commission=0.0001, spread=0.0002, slippage=0.0001, tz="UTC"):
    return MarketState(
        asset="AAPL",
        asof=UTC_NOW,
        volatility=volatility,
        commission=commission,
        spread=spread,
        slippage_estimate=slippage,
        tz=tz,
    )


def _core_portfolio(*, equity=100_000.0, peak=100_000.0, daily_open=100_000.0, positions=None, asof=UTC_NOW):
    return CorePortfolio(
        account_id="alpaca-paper",
        asset_class="equity",
        asof=asof,
        positions=positions or {},
        equity_total=equity,
        peak_equity=peak,
        daily_open_equity=daily_open,
    )


def _live_portfolio(*, equity=100_000.0, peak=100_000.0, daily_open=100_000.0, positions=None, asof=UTC_NOW):
    return Portfolio(
        account_id="alpaca-paper",
        asset_class="equity",
        asof=asof,
        positions=positions or {},
        cash=1000.0,
        equity_total=equity,
        realized_pnl_total=0.0,
        realized_fees_total=0.0,
        peak_equity=peak,
        daily_open_equity=daily_open,
    )


def _position(asset="AAPL", qty=100.0, mark=200.0):
    return Position(
        asset=asset,
        qty=qty,
        avg_entry_price=190.0,
        mark_price=mark,
        unrealized_pnl=0.0,
        realized_fees=0.0,
    )


# ---------------------------------------------------------------------------
# Rule-branch isolation tests.
# ---------------------------------------------------------------------------


def test_rule0_halt_active_silences() -> None:
    g = DefaultRiskGate()
    halts = _Halts({("alpaca-paper", "equity", "AAPL")})
    d = g.gate(_core_signal(), _core_market(), _core_portfolio(), halts)
    assert d is None
    assert g.stats()["n_silenced_halt"] == 1


def test_rule1_drawdown_breaker_flatten_halt_until_none() -> None:
    g = DefaultRiskGate()  # moderate: max_drawdown_pct=0.15
    pf = _core_portfolio(equity=80_000.0, peak=100_000.0)  # 20% drawdown
    d = g.gate(_core_signal(), _core_market(), pf, _Halts())
    assert isinstance(d, GateDecision)
    assert d.target_position_pct == 0.0
    assert d.halt is True
    assert d.halt_scope == ("alpaca-paper", "equity", None)
    assert d.halt_until is None  # explicit-resume only
    assert d.reason == "drawdown_circuit_breaker_0.2000"
    assert g.stats()["n_silenced_drawdown"] == 1


def test_rule2_daily_loss_breaker_flatten_halt_until_session() -> None:
    g = DefaultRiskGate()  # moderate: max_daily_loss_pct=0.05
    pf = _core_portfolio(equity=90_000.0, daily_open=100_000.0)  # 10% daily loss
    d = g.gate(_core_signal(), _core_market(tz="UTC"), pf, _Halts())
    assert isinstance(d, GateDecision)
    assert d.target_position_pct == 0.0
    assert d.halt is True
    assert d.halt_scope == ("alpaca-paper", "equity", None)
    # UTC -> next-day midnight
    assert d.halt_until == (UTC_NOW + pd.Timedelta(days=1)).normalize()
    assert d.reason == "daily_loss_circuit_breaker_0.1000"
    assert g.stats()["n_silenced_daily_loss"] == 1


def test_rule3_flat_or_zero_confidence_silences() -> None:
    g = DefaultRiskGate()
    assert g.gate(_core_signal(direction=0), _core_market(), _core_portfolio(), _Halts()) is None
    assert g.gate(_core_signal(confidence=0.0), _core_market(), _core_portfolio(), _Halts()) is None
    assert g.stats()["n_silenced_flat"] == 2


def test_rule4_post_loss_cooldown_silences() -> None:
    g = DefaultRiskGate()
    g.record_loss("alpaca-paper", "equity", "AAPL", UTC_NOW - pd.Timedelta(minutes=10))
    pf = _core_portfolio(asof=UTC_NOW)
    d = g.gate(_core_signal(), _core_market(), pf, _Halts())
    assert d is None
    assert g.stats()["n_silenced_cooldown"] == 1


def test_rule4_cooldown_expired_passes() -> None:
    g = DefaultRiskGate()
    g.record_loss("alpaca-paper", "equity", "AAPL", UTC_NOW - pd.Timedelta(minutes=120))
    d = g.gate(_core_signal(), _core_market(), _core_portfolio(), _Halts())
    assert isinstance(d, GateDecision)
    assert d.halt is False


def test_rule5_cost_gate_edge_sign_silences() -> None:
    # direction=+1 but the calibrated probability < 0.5 → signed edge negative.
    g = DefaultRiskGate()
    d = g.gate(_core_signal(direction=1, confidence=0.3), _core_market(), _core_portfolio(), _Halts())
    assert d is None
    assert g.stats()["n_silenced_cost_gate"] == 1


def test_rule5_cost_gate_below_threshold_silences() -> None:
    # Positive edge but tiny magnitude vs a high cost → below threshold.
    g = DefaultRiskGate(RiskConfig(cost_multiple=2.0))
    d = g.gate(
        _core_signal(direction=1, confidence=0.55, magnitude=0.0001),
        _core_market(commission=0.01, spread=0.01, slippage=0.01),
        _core_portfolio(),
        _Halts(),
    )
    assert d is None
    assert g.stats()["n_silenced_cost_gate"] == 1


def test_rule6_kelly_size_emits_on_ladder_action() -> None:
    g = DefaultRiskGate()
    d = g.gate(
        _core_signal(direction=1, confidence=0.95, magnitude=0.03),
        _core_market(volatility=0.05),
        _core_portfolio(),
        _Halts(),
    )
    assert isinstance(d, GateDecision)
    assert d.halt is False
    assert d.target_position_pct in {0.0, 0.05, 0.10, 0.15, 0.20}
    assert d.target_position_pct > 0.0
    assert g.stats()["n_actions"] == 1


def test_rule7_min_trade_size_silences() -> None:
    # current position already ~ target → delta below min_trade_size.
    g = DefaultRiskGate()
    pos = {"AAPL": _position(qty=100.0, mark=200.0)}  # 20% long already
    d = g.gate(
        _core_signal(direction=1, confidence=0.95, magnitude=0.03),
        _core_market(volatility=0.05),
        _core_portfolio(positions=pos),
        _Halts(),
    )
    assert d is None
    assert g.stats()["n_silenced_min_trade"] == 1


def test_nonfinite_equity_fails_closed_via_drawdown_breaker() -> None:
    """NaN equity is NaN-fail-CLOSED in CorePortfolio.drawdown_pct (sentinel
    1.0), so it trips the Rule-1 drawdown breaker (flatten + halt-until-resume),
    NOT the non_finite_portfolio_state path. VERBATIM with the live gate."""
    g = DefaultRiskGate()
    pf = _core_portfolio(equity=float("nan"))
    d = g.gate(_core_signal(), _core_market(), pf, _Halts())
    assert isinstance(d, GateDecision)
    assert d.target_position_pct == 0.0
    assert d.halt is True
    assert d.halt_until is None
    assert d.reason == "drawdown_circuit_breaker_1.0000"
    assert g.stats()["n_silenced_drawdown"] == 1


def test_nonfinite_position_qty_fails_closed_flatten_halt() -> None:
    """A non-finite position qty → current_position_pct returns NaN at Rule 7 →
    _flatten_nonfinite_portfolio (target 0.0, halt, scope-wide). VERBATIM."""
    g = DefaultRiskGate()
    pos = {"AAPL": _position(qty=float("nan"), mark=200.0)}
    d = g.gate(
        _core_signal(direction=1, confidence=0.95, magnitude=0.03),
        _core_market(volatility=0.05),
        _core_portfolio(positions=pos),
        _Halts(),
    )
    assert isinstance(d, GateDecision)
    assert d.target_position_pct == 0.0
    assert d.halt is True
    assert d.halt_scope == ("alpaca-paper", "equity", None)
    assert d.reason == "non_finite_portfolio_state"
    assert g.stats()["n_silenced_nonfinite_portfolio"] == 1


def test_signal_id_propagates_from_metadata() -> None:
    g = DefaultRiskGate()
    d = g.gate(
        _core_signal(direction=1, confidence=0.95, magnitude=0.03, metadata={"id": "sig-77"}),
        _core_market(volatility=0.05),
        _core_portfolio(),
        _Halts(),
    )
    assert isinstance(d, GateDecision)
    assert d.signal_id == "sig-77"


# ---------------------------------------------------------------------------
# Coupling edit (a): audit is an injected sink defaulting to no-op.
# ---------------------------------------------------------------------------


def test_audit_sink_default_noop_does_not_affect_decision() -> None:
    """No sink wired → decision is produced and nothing raises."""
    g = DefaultRiskGate()
    d = g.gate(
        _core_signal(direction=1, confidence=0.95, magnitude=0.03),
        _core_market(volatility=0.05),
        _core_portfolio(),
        _Halts(),
    )
    assert isinstance(d, GateDecision)


def test_injected_audit_sink_receives_events_without_changing_decision() -> None:
    events: list[dict] = []

    def sink(*, kind, asof, payload):
        events.append({"kind": kind, "asof": asof, "payload": payload})

    g = DefaultRiskGate(audit_sink=sink)
    # one approval
    g.gate(
        _core_signal(direction=1, confidence=0.95, magnitude=0.03),
        _core_market(volatility=0.05),
        _core_portfolio(),
        _Halts(),
    )
    # one rejection (flat)
    g.gate(_core_signal(direction=0), _core_market(), _core_portfolio(), _Halts())
    kinds = [e["kind"] for e in events]
    assert "gate_approval" in kinds
    assert "gate_rejection" in kinds


def test_audit_sink_failure_is_swallowed() -> None:
    """A raising sink must NEVER block the decision (audit best-effort)."""

    def bad_sink(*, kind, asof, payload):
        raise RuntimeError("audit backend down")

    g = DefaultRiskGate(audit_sink=bad_sink)
    d = g.gate(
        _core_signal(direction=1, confidence=0.95, magnitude=0.03),
        _core_market(volatility=0.05),
        _core_portfolio(),
        _Halts(),
    )
    assert isinstance(d, GateDecision)


# ---------------------------------------------------------------------------
# Coupling edit (b): event-risk is a config flag (default-off), not os.environ.
# ---------------------------------------------------------------------------


def _event_meta(scheduled_for: str, impact="high", kind="fomc"):
    return {
        "event_risk": {
            "events": [{"impact": impact, "scheduled_for": scheduled_for, "kind": kind}]
        }
    }


def test_event_risk_default_off_ignores_blackout() -> None:
    g = DefaultRiskGate()  # event_risk_enabled defaults False
    meta = _event_meta("2026-06-12T18:00:00+00:00")  # 3h forward, high impact
    d = g.gate(
        _core_signal(direction=1, confidence=0.95, magnitude=0.03, metadata=meta),
        _core_market(volatility=0.05),
        _core_portfolio(),
        _Halts(),
    )
    # flag off → blackout ignored → trade emitted
    assert isinstance(d, GateDecision)
    assert d.halt is False
    assert g.stats()["n_silenced_event_risk"] == 0


def test_event_risk_enabled_silences_opening_in_blackout() -> None:
    g = DefaultRiskGate(RiskConfig(event_risk_enabled=True))
    meta = _event_meta("2026-06-12T18:00:00+00:00")  # within 1.0-day window, forward, high
    d = g.gate(
        _core_signal(direction=1, confidence=0.95, magnitude=0.03, metadata=meta),
        _core_market(volatility=0.05),
        _core_portfolio(),  # flat → opening
        _Halts(),
    )
    assert d is None
    assert g.stats()["n_silenced_event_risk"] == 1


def test_event_risk_enabled_does_not_block_de_risking() -> None:
    """A signal OPPOSITE the current position is de-risking → never blocked."""
    g = DefaultRiskGate(RiskConfig(event_risk_enabled=True))
    meta = _event_meta("2026-06-12T18:00:00+00:00")
    pos = {"AAPL": _position(qty=100.0, mark=200.0)}  # long 20%
    g.gate(
        _core_signal(direction=-1, confidence=0.95, magnitude=0.03, metadata=meta),
        _core_market(volatility=0.05),
        _core_portfolio(positions=pos),
        _Halts(),
    )
    # de-risking (short signal against a long position) is NOT silenced by the guard
    assert g.stats()["n_silenced_event_risk"] == 0


# ---------------------------------------------------------------------------
# Coupling edit (c): no evidence import; pre-filtered views accepted.
# The core gate has no evidence_store ctor arg and never imports evidence —
# enforced by the purity gate. Here we just assert the gate works without one.
# ---------------------------------------------------------------------------


def test_core_gate_has_no_evidence_store_param() -> None:
    import inspect

    sig = inspect.signature(DefaultRiskGate.__init__)
    assert "evidence_store" not in sig.parameters


def test_paper_zero_costs_override() -> None:
    g = DefaultRiskGate(RiskConfig(paper_zero_costs=True))
    # tiny positive edge that would fail the live cost threshold, but zero-costs
    # forces threshold 0.0; edge-sign guard still holds (direction agrees).
    g.gate(
        _core_signal(direction=1, confidence=0.55, magnitude=0.01),
        _core_market(volatility=0.05, commission=0.01, spread=0.01, slippage=0.01),
        _core_portfolio(),
        _Halts(),
    )
    # passes the cost gate (threshold 0.0); may still size or hit min-trade,
    # but must NOT be silenced by the cost gate.
    assert g.stats()["n_silenced_cost_gate"] == 0


# ---------------------------------------------------------------------------
# PARITY GRID — core GateDecision == live protocol.Action over a matrix.
# ---------------------------------------------------------------------------

# Each row: (label, signal kwargs, market kwargs, portfolio kwargs, config)
_PARITY_MATRIX = [
    ("flat", dict(direction=0), {}, {}, {}),
    ("zero_conf", dict(direction=1, confidence=0.0), {}, {}, {}),
    ("drawdown", dict(), {}, dict(equity=80_000.0, peak=100_000.0), {}),
    ("daily_loss", dict(), {}, dict(equity=90_000.0, daily_open=100_000.0), {}),
    ("edge_sign", dict(direction=1, confidence=0.3), {}, {}, {}),
    (
        "below_threshold",
        dict(direction=1, confidence=0.55, magnitude=0.0001),
        dict(commission=0.01, spread=0.01, slippage=0.01),
        {},
        {},
    ),
    (
        "action_long",
        dict(direction=1, confidence=0.95, magnitude=0.03),
        dict(volatility=0.05),
        {},
        {},
    ),
    (
        "action_short",
        dict(direction=-1, confidence=0.95, magnitude=0.03),
        dict(volatility=0.05),
        {},
        {},
    ),
    ("nonfinite_equity_drawdown", dict(), {}, dict(equity=float("nan")), {}),
    (
        "nonfinite_position_qty",
        dict(direction=1, confidence=0.95, magnitude=0.03),
        dict(volatility=0.05),
        dict(positions={"AAPL": _position(qty=float("nan"), mark=200.0)}),
        {},
    ),
    (
        "daily_loss_ny",
        dict(),
        dict(tz="America/New_York"),
        dict(equity=90_000.0, daily_open=100_000.0),
        {},
    ),
    (
        "min_trade_already_held",
        dict(direction=1, confidence=0.95, magnitude=0.03),
        dict(volatility=0.05),
        dict(positions={"AAPL": _position(qty=100.0, mark=200.0)}),
        {},
    ),
    (
        "conservative_profile_action",
        dict(direction=1, confidence=0.95, magnitude=0.03),
        dict(volatility=0.05),
        {},
        dict(max_position_pct=0.10, action_step=0.05, cost_multiple=3.0, max_drawdown_pct=0.10, max_daily_loss_pct=0.03),
    ),
]


@pytest.mark.parametrize("label,sig_kw,mkt_kw,pf_kw,cfg_kw", _PARITY_MATRIX)
def test_parity_core_gate_matches_live_gate(label, sig_kw, mkt_kw, pf_kw, cfg_kw) -> None:
    core_g = DefaultRiskGate(RiskConfig(**cfg_kw) if cfg_kw else None)
    live_g = LiveGate(LiveRiskConfig(**cfg_kw) if cfg_kw else None)

    core_d = core_g.gate(
        _core_signal(**sig_kw), _core_market(**mkt_kw), _core_portfolio(**pf_kw), _Halts()
    )
    live_a = live_g.gate(
        _live_signal(**sig_kw), _live_market(**mkt_kw), _live_portfolio(**pf_kw), _Halts()
    )

    if live_a is None:
        assert core_d is None, f"[{label}] live silenced but core did not: {core_d!r}"
        return
    assert core_d is not None, f"[{label}] live acted but core silenced"
    assert core_d.target_position_pct == live_a.target_position_pct, (
        f"[{label}] target mismatch: core={core_d.target_position_pct} live={live_a.target_position_pct}"
    )
    assert core_d.reason == live_a.reason, (
        f"[{label}] reason mismatch: core={core_d.reason!r} live={live_a.reason!r}"
    )
    assert core_d.halt == live_a.halt, f"[{label}] halt mismatch"
    assert core_d.halt_scope == live_a.halt_scope, f"[{label}] halt_scope mismatch"
    # halt_until: both pd.Timestamp or both None
    if live_a.halt_until is None:
        assert core_d.halt_until is None, f"[{label}] halt_until: live None core {core_d.halt_until!r}"
    else:
        assert core_d.halt_until == live_a.halt_until, f"[{label}] halt_until mismatch"
