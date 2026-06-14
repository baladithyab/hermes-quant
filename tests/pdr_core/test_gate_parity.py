"""ADR-0092 Increment-1-cont, STAGE 4 — THE PARITY GATE.

This is the whole safety argument for the gate port. The deterministic risk gate
(ADR-0004 — the FINAL money-safety authority) was lifted VERBATIM into
``hermes_quant.pdr_core.gate.DefaultRiskGate`` operating on host-blind
read-interfaces and emitting a halt-triple-preserving ``GateDecision``. This file
proves the ported core gate is BEHAVIORALLY IDENTICAL to the live
``hermes_quant.risk.gate.DefaultRiskGate`` over an EXHAUSTIVE fixture matrix
hitting EVERY rule branch:

  * Rule 0  — halt-active (silence)
  * Rule 1  — drawdown circuit breaker → flatten + halt, halt_until=None
  * Rule 2  — daily-loss circuit breaker → flatten + halt-until-next-session
              (BOTH the UTC-crypto midnight-normalize branch AND the non-UTC
              equities +24h branch of ``_next_session_open``)
  * Rule 3  — flat (direction=0) / zero-confidence silence
  * Rule 3.5 — ADR-0084 pre-event blackout (default-OFF parity AND enabled-parity)
  * Rule 4  — post-loss cooldown silence (and cooldown-expired pass-through)
  * Rule 5  — cost gate: edge-sign silence, below-threshold silence, non-finite
              risk-input silence, paper_zero_costs override
  * Rule 6  — quarter-Kelly sizing snapped across the discrete ladder
              {0, ±0.05, ±0.10, ±0.15, ±0.20}
  * Rule 7  — minimum-trade-size silence (already-held position)
  * fail-closed — NaN equity (→ drawdown sentinel) and NaN position qty
              (→ _flatten_nonfinite_portfolio) both flatten+halt
  * profiles — conservative / moderate / aggressive, by name from PROFILES

THE SAFETY ASSERTION (the riskiest coupling): for every fixture we build the live
``protocol`` inputs AND the equivalent core read-interface inputs, run both gates,
map the core ``GateDecision`` onto a ``protocol.Action`` via the SAME mechanical
shell map a host shell would use, and assert the mapped Action is FIELD-BY-FIELD
identical to the live gate's ``Action`` — ESPECIALLY the full durable-HALT triple
(``halt`` / ``halt_scope`` / ``halt_until``). A bare ``Proposal`` has no halt
fields; collapsing onto it would silently DROP a Rule-1/Rule-2 circuit-breaker
verdict = a money-safety regression. ``GateDecision`` carries the triple; this
test proves the triple survives the core boundary intact.

Any divergence between the mapped core Action and the live Action is a PORT BUG.
"""

from __future__ import annotations

import os

import pandas as pd
import pytest

from hermes_quant.pdr_core.gate import (
    PROFILES as CORE_PROFILES,
)
from hermes_quant.pdr_core.gate import (
    DefaultRiskGate as CoreGate,
)
from hermes_quant.pdr_core.gate import (
    RiskConfig as CoreRiskConfig,
)
from hermes_quant.pdr_core.gate_types import (
    CoreMarketState,
    CorePortfolio,
    CoreSignal,
    GateDecision,
)

# --- the LIVE gate + protocol types (the parity ORACLE) --------------------
from hermes_quant.protocol import (
    Action,
    AggregatedSignal,
    MarketState,
    Portfolio,
    Position,
)
from hermes_quant.risk.gate import DefaultRiskGate as LiveGate
from hermes_quant.risk.gate import RiskConfig as LiveRiskConfig

UTC_NOW = pd.Timestamp("2026-06-12T15:00:00+00:00")


# ===========================================================================
# THE SHELL MAP — GateDecision -> protocol.Action.
#
# This is the mechanical mapping a host shell performs at the core boundary.
# It is the ENTIRE point of GateDecision carrying the halt triple: the shell
# can reconstruct a protocol.Action with the durable-HALT verdict intact. If
# this map dropped any halt field, a circuit-breaker verdict would silently
# vanish — the money-safety regression STAGE 4 exists to forbid.
# ===========================================================================


def gate_decision_to_action(decision: GateDecision | None) -> Action | None:
    """Map a core GateDecision onto the live protocol.Action, 1:1.

    None (gate silence) maps to None. Every field maps straight across; the
    halt triple (halt / halt_scope / halt_until) is preserved verbatim.
    """
    if decision is None:
        return None
    return Action(
        target_position_pct=decision.target_position_pct,
        reason=decision.reason,
        signal_id=decision.signal_id,
        halt=decision.halt,
        halt_scope=decision.halt_scope,
        halt_until=decision.halt_until,
    )


# ===========================================================================
# Halt registry — satisfies BOTH CoreHaltState (structural) and
# protocol.HaltState (structural). is_halted matches asset-scoped AND
# class-wide (asset=None) halts.
# ===========================================================================


class _Halts:
    def __init__(self, halted: set[tuple] | None = None) -> None:
        self._halted = halted or set()

    def is_halted(self, account_id, asset_class, asset=None) -> bool:
        return (account_id, asset_class, asset) in self._halted or (
            account_id,
            asset_class,
            None,
        ) in self._halted

    def active_halts(self) -> list:  # protocol.HaltState completeness
        return []


# ===========================================================================
# Paired builders — push the SAME money-state numbers into core + live types.
# ===========================================================================


def _core_signal(
    *, direction=1, confidence=0.9, magnitude=0.02, asset="AAPL", asof=UTC_NOW, metadata=None
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
    *, direction=1, confidence=0.9, magnitude=0.02, asset="AAPL", asof=UTC_NOW, metadata=None
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


def _live_position(asset="AAPL", qty=100.0, mark=200.0) -> Position:
    return Position(
        asset=asset,
        qty=qty,
        avg_entry_price=190.0,
        mark_price=mark,
        unrealized_pnl=0.0,
        realized_fees=0.0,
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


# ===========================================================================
# THE FIXTURE MATRIX.
#
# Each row is one decision scenario, expressed as the SHARED kwargs the paired
# builders consume PLUS the gate-construction shape. The same numbers flow into
# BOTH the live and the core types so the only thing under test is the port.
#
# Fields per row:
#   label            — human name (appears in assert messages)
#   sig              — _signal kwargs
#   mkt              — _market kwargs
#   pf               — _portfolio kwargs
#   cfg              — RiskConfig kwargs (None => moderate defaults)
#   halted           — set of halt keys for the halt registry
#   record_loss      — (asset, minutes_ago) to seed a cooldown, or None
#   event_risk_env   — value to set HERMES_QUANT_EVENT_RISK for the LIVE gate
#                      (the core mirrors it via cfg["event_risk_enabled"])
# ===========================================================================


def _row(
    label,
    *,
    sig=None,
    mkt=None,
    pf=None,
    cfg=None,
    halted=None,
    record_loss=None,
    event_risk_env=None,
):
    return {
        "label": label,
        "sig": sig or {},
        "mkt": mkt or {},
        "pf": pf or {},
        "cfg": cfg,
        "halted": halted,
        "record_loss": record_loss,
        "event_risk_env": event_risk_env,
    }


_EVENT_META = {
    "event_risk": {
        "events": [
            {"impact": "high", "scheduled_for": "2026-06-12T18:00:00+00:00", "kind": "fomc"}
        ]
    }
}

# Profile kwargs lifted verbatim from RiskConfig.{conservative,aggressive}.
_CONSERVATIVE = dict(
    max_position_pct=0.10, action_step=0.05, cost_multiple=3.0, max_drawdown_pct=0.10, max_daily_loss_pct=0.03
)
_AGGRESSIVE = dict(
    max_position_pct=0.40, action_step=0.10, cost_multiple=1.5, max_drawdown_pct=0.20, max_daily_loss_pct=0.10
)


PARITY_MATRIX = [
    # --- Rule 0: halt-active (asset-scoped) ---
    _row("rule0_halt_asset", halted={("alpaca-paper", "equity", "AAPL")}),
    # --- Rule 0: halt-active (class-wide, asset=None) ---
    _row("rule0_halt_classwide", halted={("alpaca-paper", "equity", None)}),
    # --- Rule 1: drawdown breaker -> halt, halt_until=None ---
    _row("rule1_drawdown", pf=dict(equity=80_000.0, peak=100_000.0)),
    # drawdown exactly AT threshold (0.15) must NOT trip (> strict) -> proceeds.
    # daily_open held equal to equity so Rule 2 (daily-loss) does NOT pre-empt it,
    # isolating the at-threshold drawdown PASS-THROUGH to a sized action.
    _row("rule1_drawdown_at_threshold", pf=dict(equity=85_000.0, peak=100_000.0, daily_open=85_000.0),
         sig=dict(direction=1, confidence=0.95, magnitude=0.03), mkt=dict(volatility=0.05)),
    # --- Rule 2: daily-loss breaker -> halt-until-session (UTC midnight) ---
    _row("rule2_daily_loss_utc", pf=dict(equity=90_000.0, daily_open=100_000.0), mkt=dict(tz="UTC")),
    # --- Rule 2: daily-loss breaker, NON-UTC equities tz -> now + 24h ---
    _row("rule2_daily_loss_ny", pf=dict(equity=90_000.0, daily_open=100_000.0), mkt=dict(tz="America/New_York")),
    # --- Rule 3: flat (direction=0) ---
    _row("rule3_flat", sig=dict(direction=0)),
    # --- Rule 3: zero confidence ---
    _row("rule3_zero_conf", sig=dict(direction=1, confidence=0.0)),
    _row("rule3_subepsilon_conf", sig=dict(direction=1, confidence=5e-7)),
    # --- Rule 3.5: event blackout, flag OFF -> ignored (parity: trade emitted) ---
    _row("rule35_event_off", sig=dict(direction=1, confidence=0.95, magnitude=0.03, metadata=_EVENT_META),
         mkt=dict(volatility=0.05), event_risk_env=None),
    # --- Rule 3.5: event blackout, flag ON, opening -> silence ---
    _row("rule35_event_on_opening", sig=dict(direction=1, confidence=0.95, magnitude=0.03, metadata=_EVENT_META),
         mkt=dict(volatility=0.05), cfg=dict(event_risk_enabled=True), event_risk_env="1"),
    # --- Rule 3.5: event blackout, flag ON, de-risking -> NOT blocked ---
    _row("rule35_event_on_derisk", sig=dict(direction=-1, confidence=0.95, magnitude=0.03, metadata=_EVENT_META),
         mkt=dict(volatility=0.05), pf=dict(positions={"AAPL": _live_position(qty=100.0, mark=200.0)}),
         cfg=dict(event_risk_enabled=True), event_risk_env="1"),
    # --- Rule 4: post-loss cooldown silence ---
    _row("rule4_cooldown_active", record_loss=("AAPL", 10),
         sig=dict(direction=1, confidence=0.95, magnitude=0.03), mkt=dict(volatility=0.05)),
    # --- Rule 4: cooldown expired -> pass through to sizing ---
    _row("rule4_cooldown_expired", record_loss=("AAPL", 120),
         sig=dict(direction=1, confidence=0.95, magnitude=0.03), mkt=dict(volatility=0.05)),
    # --- Rule 5: edge-sign silence (direction +1 but p<0.5 -> negative edge) ---
    _row("rule5_edge_sign", sig=dict(direction=1, confidence=0.3)),
    # --- Rule 5: below-threshold silence (tiny edge vs high cost) ---
    _row("rule5_below_threshold", sig=dict(direction=1, confidence=0.55, magnitude=0.0001),
         mkt=dict(commission=0.01, spread=0.01, slippage=0.01)),
    # --- Rule 5: non-finite risk input (NaN volatility) -> silence ---
    _row("rule5_nonfinite_vol", sig=dict(direction=1, confidence=0.95, magnitude=0.03),
         mkt=dict(volatility=float("nan"))),
    # --- Rule 5: paper_zero_costs override -> trade where live cost would silence ---
    _row("rule5_paper_zero_costs", sig=dict(direction=1, confidence=0.95, magnitude=0.03),
         mkt=dict(volatility=0.05, commission=0.01, spread=0.01, slippage=0.01),
         cfg=dict(paper_zero_costs=True)),
    # --- Rule 6: quarter-Kelly long across the ladder ---
    _row("rule6_action_long", sig=dict(direction=1, confidence=0.95, magnitude=0.03), mkt=dict(volatility=0.05)),
    # --- Rule 6: quarter-Kelly short ---
    _row("rule6_action_short", sig=dict(direction=-1, confidence=0.95, magnitude=0.03), mkt=dict(volatility=0.05)),
    # --- Rule 6: very strong signal clamps to max_position_pct rung ---
    _row("rule6_clamp_max", sig=dict(direction=1, confidence=0.99, magnitude=0.20), mkt=dict(volatility=0.02)),
    # --- Rule 7: min-trade-size silence (already holding ~target) ---
    _row("rule7_min_trade", sig=dict(direction=1, confidence=0.95, magnitude=0.03), mkt=dict(volatility=0.05),
         pf=dict(positions={"AAPL": _live_position(qty=100.0, mark=200.0)})),
    # --- fail-closed: NaN equity -> drawdown sentinel 1.0 -> Rule-1 breaker ---
    _row("failclosed_nan_equity", pf=dict(equity=float("nan"))),
    # --- fail-closed: NaN position qty -> Rule-7 _flatten_nonfinite_portfolio ---
    _row("failclosed_nan_qty", sig=dict(direction=1, confidence=0.95, magnitude=0.03), mkt=dict(volatility=0.05),
         pf=dict(positions={"AAPL": _live_position(qty=float("nan"), mark=200.0)})),
    # --- profile: conservative, action ---
    _row("profile_conservative_action", sig=dict(direction=1, confidence=0.95, magnitude=0.03),
         mkt=dict(volatility=0.05), cfg=_CONSERVATIVE),
    # --- profile: conservative, tighter drawdown breaker (0.10) trips earlier ---
    _row("profile_conservative_drawdown", pf=dict(equity=88_000.0, peak=100_000.0), cfg=_CONSERVATIVE),
    # --- profile: aggressive, bigger ladder (action_step 0.10, cap 0.40) ---
    _row("profile_aggressive_action", sig=dict(direction=1, confidence=0.97, magnitude=0.08),
         mkt=dict(volatility=0.04), cfg=_AGGRESSIVE),
    # --- profile: aggressive, looser daily-loss (0.10) does NOT trip at 0.06 ---
    _row("profile_aggressive_daily_loss_ok", pf=dict(equity=94_000.0, daily_open=100_000.0),
         sig=dict(direction=1, confidence=0.95, magnitude=0.03), mkt=dict(volatility=0.05), cfg=_AGGRESSIVE),
]


def _build_core_gate(row):
    cfg = CoreRiskConfig(**row["cfg"]) if row["cfg"] else None
    g = CoreGate(cfg)
    if row["record_loss"]:
        asset, minutes = row["record_loss"]
        g.record_loss("alpaca-paper", "equity", asset, UTC_NOW - pd.Timedelta(minutes=minutes))
    return g


def _build_live_gate(row):
    # The live gate's RiskConfig has no event_risk_enabled flag (it reads the
    # env var instead). Strip that key for the live config; the env var is set
    # by the test wrapper around the call.
    cfg_kw = dict(row["cfg"]) if row["cfg"] else {}
    cfg_kw.pop("event_risk_enabled", None)
    cfg = LiveRiskConfig(**cfg_kw) if cfg_kw else None
    g = LiveGate(cfg)
    if row["record_loss"]:
        asset, minutes = row["record_loss"]
        g.record_loss("alpaca-paper", "equity", asset, UTC_NOW - pd.Timedelta(minutes=minutes))
    return g


def _run_core(row) -> GateDecision | None:
    g = _build_core_gate(row)
    return g.gate(
        _core_signal(**row["sig"]),
        _core_market(**row["mkt"]),
        _core_portfolio(**row["pf"]),
        _Halts(row["halted"]),
    )


def _run_live(row) -> Action | None:
    g = _build_live_gate(row)
    prev = os.environ.get("HERMES_QUANT_EVENT_RISK")
    try:
        if row["event_risk_env"] is None:
            os.environ.pop("HERMES_QUANT_EVENT_RISK", None)
        else:
            os.environ["HERMES_QUANT_EVENT_RISK"] = row["event_risk_env"]
        return g.gate(
            _live_signal(**row["sig"]),
            _live_market(**row["mkt"]),
            _live_portfolio(**row["pf"]),
            _Halts(row["halted"]),
        )
    finally:
        if prev is None:
            os.environ.pop("HERMES_QUANT_EVENT_RISK", None)
        else:
            os.environ["HERMES_QUANT_EVENT_RISK"] = prev


def _assert_action_field_identical(label, mapped: Action | None, live: Action | None) -> None:
    """The whole safety assertion: mapped core Action == live Action, field by
    field, with the halt triple held to byte-identity."""
    if live is None:
        assert mapped is None, f"[{label}] live SILENCED but core acted: {mapped!r}"
        return
    assert mapped is not None, f"[{label}] live ACTED but core silenced (dropped verdict)"

    assert mapped.target_position_pct == live.target_position_pct, (
        f"[{label}] target_position_pct: core={mapped.target_position_pct!r} live={live.target_position_pct!r}"
    )
    assert mapped.reason == live.reason, (
        f"[{label}] reason: core={mapped.reason!r} live={live.reason!r}"
    )
    assert mapped.signal_id == live.signal_id, (
        f"[{label}] signal_id: core={mapped.signal_id!r} live={live.signal_id!r}"
    )
    # THE HALT TRIPLE — the riskiest coupling. Any divergence here is the
    # money-safety regression STAGE 4 exists to forbid.
    assert mapped.halt == live.halt, f"[{label}] halt: core={mapped.halt!r} live={live.halt!r}"
    assert mapped.halt_scope == live.halt_scope, (
        f"[{label}] halt_scope: core={mapped.halt_scope!r} live={live.halt_scope!r}"
    )
    if live.halt_until is None:
        assert mapped.halt_until is None, (
            f"[{label}] halt_until: live=None core={mapped.halt_until!r} (DROPPED/INVENTED halt clock)"
        )
    else:
        assert mapped.halt_until == live.halt_until, (
            f"[{label}] halt_until: core={mapped.halt_until!r} live={live.halt_until!r}"
        )
        # exact type parity — both must be pd.Timestamp (no str/Timestamp drift)
        assert type(mapped.halt_until) is type(live.halt_until), (
            f"[{label}] halt_until type: core={type(mapped.halt_until)} live={type(live.halt_until)}"
        )


@pytest.mark.parametrize("row", PARITY_MATRIX, ids=lambda r: r["label"])
def test_core_gate_action_is_field_identical_to_live(row) -> None:
    """For every rule-branch fixture: the core GateDecision, mapped onto a
    protocol.Action by the shell map, is FIELD-BY-FIELD identical to the live
    DefaultRiskGate's Action — especially the durable-HALT triple."""
    core_decision = _run_core(row)
    mapped = gate_decision_to_action(core_decision)
    live_action = _run_live(row)
    _assert_action_field_identical(row["label"], mapped, live_action)


# ---------------------------------------------------------------------------
# Coverage assertions ON the matrix: the matrix actually HITS every branch.
# (A parity grid is only a safety argument if it provably exercises each rule.)
# ---------------------------------------------------------------------------


def test_matrix_exercises_every_rule_branch() -> None:
    """Fail-fast guard: prove the matrix lands on each gate outcome at least
    once by replaying it through the LIVE gate and tallying the stat counters
    plus the halt-triple shapes. If a future edit silently drops a branch from
    the matrix, this test goes red."""
    saw_halt_active = False
    saw_drawdown = False
    saw_daily_loss_until_none = False  # drawdown/nonfinite -> halt_until None
    saw_daily_loss_until_ts = False  # daily-loss -> halt_until a Timestamp
    saw_flat = False
    saw_cooldown = False
    saw_cost_gate = False
    saw_min_trade = False
    saw_event_risk = False
    saw_nonfinite = False
    saw_action = False

    for row in PARITY_MATRIX:
        g = _build_live_gate(row)
        prev = os.environ.get("HERMES_QUANT_EVENT_RISK")
        try:
            if row["event_risk_env"] is None:
                os.environ.pop("HERMES_QUANT_EVENT_RISK", None)
            else:
                os.environ["HERMES_QUANT_EVENT_RISK"] = row["event_risk_env"]
            action = g.gate(
                _live_signal(**row["sig"]),
                _live_market(**row["mkt"]),
                _live_portfolio(**row["pf"]),
                _Halts(row["halted"]),
            )
        finally:
            if prev is None:
                os.environ.pop("HERMES_QUANT_EVENT_RISK", None)
            else:
                os.environ["HERMES_QUANT_EVENT_RISK"] = prev

        s = g.stats()
        saw_halt_active |= s["n_silenced_halt"] > 0
        saw_drawdown |= s["n_silenced_drawdown"] > 0
        saw_flat |= s["n_silenced_flat"] > 0
        saw_cooldown |= s["n_silenced_cooldown"] > 0
        saw_cost_gate |= s["n_silenced_cost_gate"] > 0
        saw_min_trade |= s["n_silenced_min_trade"] > 0
        saw_event_risk |= s["n_silenced_event_risk"] > 0
        saw_nonfinite |= s["n_silenced_nonfinite_portfolio"] > 0
        if action is not None and not action.halt:
            saw_action = True
        if action is not None and action.halt:
            if action.halt_until is None:
                saw_daily_loss_until_none = True
            elif isinstance(action.halt_until, pd.Timestamp):
                saw_daily_loss_until_ts = True

    assert saw_halt_active, "matrix never hit Rule 0 (halt active)"
    assert saw_drawdown, "matrix never hit Rule 1 (drawdown breaker)"
    assert saw_daily_loss_until_none, "matrix never produced a halt with halt_until=None"
    assert saw_daily_loss_until_ts, "matrix never produced a halt with a Timestamp halt_until (daily-loss)"
    assert saw_flat, "matrix never hit Rule 3 (flat/zero-conf)"
    assert saw_cooldown, "matrix never hit Rule 4 (cooldown)"
    assert saw_cost_gate, "matrix never hit Rule 5 (cost gate)"
    assert saw_min_trade, "matrix never hit Rule 7 (min trade size)"
    assert saw_event_risk, "matrix never hit Rule 3.5 (event blackout)"
    assert saw_nonfinite, "matrix never hit the _flatten_nonfinite_portfolio path"
    assert saw_action, "matrix never produced a sized non-halt action"


# ---------------------------------------------------------------------------
# Rule 2 session-reset arithmetic parity, asserted on the mapped Action.
# Both _next_session_open branches must produce the SAME pd.Timestamp.
# ---------------------------------------------------------------------------


def test_rule2_session_reset_utc_branch_parity() -> None:
    """UTC (crypto) daily-loss breaker -> next-UTC-day midnight, identical."""
    row = _row("r2_utc", pf=dict(equity=90_000.0, daily_open=100_000.0), mkt=dict(tz="UTC"))
    mapped = gate_decision_to_action(_run_core(row))
    live = _run_live(row)
    assert mapped is not None and live is not None
    assert mapped.halt_until == (UTC_NOW + pd.Timedelta(days=1)).normalize()
    assert mapped.halt_until == live.halt_until


def test_rule2_session_reset_nonutc_branch_parity() -> None:
    """Non-UTC (equities) daily-loss breaker -> now + 24h (NOT normalized),
    identical between core and live."""
    row = _row("r2_ny", pf=dict(equity=90_000.0, daily_open=100_000.0), mkt=dict(tz="America/New_York"))
    mapped = gate_decision_to_action(_run_core(row))
    live = _run_live(row)
    assert mapped is not None and live is not None
    assert mapped.halt_until == UTC_NOW + pd.Timedelta(days=1)
    assert mapped.halt_until == live.halt_until


# ---------------------------------------------------------------------------
# All three NAMED profiles agree core<->live (PROFILES registry parity).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_name", ["conservative", "moderate", "aggressive"])
def test_named_profiles_parity_on_a_sized_action(profile_name) -> None:
    """Construct each gate from its NAMED profile factory (core PROFILES vs the
    live profile classmethods) and assert a sized action is identical, proving
    the profile NUMBERS were ported verbatim."""
    core_cfg = CORE_PROFILES[profile_name]()
    # live config from the same-named classmethod on the LIVE RiskConfig
    live_cfg = getattr(LiveRiskConfig, profile_name)()

    core_g = CoreGate(core_cfg)
    live_g = LiveGate(live_cfg)

    sig_kw = dict(direction=1, confidence=0.97, magnitude=0.05)
    mkt_kw = dict(volatility=0.04)

    core_decision = core_g.gate(
        _core_signal(**sig_kw), _core_market(**mkt_kw), _core_portfolio(), _Halts()
    )
    live_action = live_g.gate(
        _live_signal(**sig_kw), _live_market(**mkt_kw), _live_portfolio(), _Halts()
    )
    mapped = gate_decision_to_action(core_decision)
    _assert_action_field_identical(f"profile_{profile_name}", mapped, live_action)


def test_core_and_live_profile_numbers_match() -> None:
    """Belt-and-suspenders: the per-field RiskConfig numbers in the core
    PROFILES registry equal the live RiskConfig profile classmethods, field by
    field, for every profile."""
    fields = (
        "max_position_pct",
        "action_step",
        "cost_multiple",
        "max_drawdown_pct",
        "max_daily_loss_pct",
        "min_trade_size",
        "quarter_kelly",
        "cooldown_after_loss_minutes",
    )
    for name in ("conservative", "moderate", "aggressive"):
        core_cfg = CORE_PROFILES[name]()
        live_cfg = getattr(LiveRiskConfig, name)()
        for f in fields:
            assert getattr(core_cfg, f) == getattr(live_cfg, f), (
                f"profile {name}: field {f} core={getattr(core_cfg, f)} live={getattr(live_cfg, f)}"
            )
