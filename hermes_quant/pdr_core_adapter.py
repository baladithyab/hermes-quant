"""hermes_quant.pdr_core_adapter — the SHELL boundary adapter (ADR-0092 Increment-2).

This is the ONLY module that knows BOTH the live ``hermes_quant.protocol`` money
types AND the host-blind ``hermes_quant.pdr_core.gate`` core types. It lives in
the SHELL (``hermes_quant/``, NOT under ``hermes_quant/pdr_core/``), so the purity
gate (``tests/pdr_core/test_contract_purity.py`` — which only walks ``pdr_core/``)
never sees it; host->core imports are exactly what the shell is for.

Increment 2 is the FIRST increment that touches LIVE wiring. The safety model is
SHADOW, NOT cutover:

  * The live gate's returned ``protocol.Action`` continues to drive the decision
    UNCHANGED. This module never assigns to anything the caller returns.
  * :func:`run_shadow_gate` ADDITIONALLY runs the ported core gate over the SAME
    inputs, maps its ``GateDecision -> protocol.Action``, compares field-by-field
    to the LIVE action, and LOGS divergence at WARNING. It returns a divergence
    REPORT (a plain dict) for the caller to record; it NEVER mutates the live
    action and NEVER re-raises (best-effort observation).

The four mechanical builders mirror EXACTLY the gate-read surface of the protocol
types (no wider — see ``pdr_core/gate_types.py`` docstrings):

  * :func:`core_signal_from`   — protocol.AggregatedSignal -> CoreSignal
  * :func:`core_market_from`   — protocol.MarketState      -> CoreMarketState
  * :func:`core_portfolio_from`— protocol.Portfolio        -> CorePortfolio
  * :func:`core_risk_config_from` — live risk.gate.RiskConfig -> core RiskConfig
    (mirror every shared field; event_risk_enabled comes from the env flag the
    live gate reads internally — coupling edit (b) in the core gate)

``protocol.HaltState`` is passed straight through as the core ``halt_state``: it
satisfies ``CoreHaltState`` structurally (both expose
``is_halted(account_id, asset_class, asset=None)``).

:func:`gate_decision_to_action` is LIFTED VERBATIM from
``tests/pdr_core/test_gate_parity.py`` (the proven shell map) — it carries the
full durable-HALT triple so a Rule-1/Rule-2 circuit-breaker verdict survives the
core boundary intact (dropping it would be a money-safety regression).
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any

from hermes_quant.pdr_core.gate import DefaultRiskGate as CoreDefaultRiskGate
from hermes_quant.pdr_core.gate import RiskConfig as CoreRiskConfig
from hermes_quant.pdr_core.gate_types import (
    CoreMarketState,
    CorePortfolio,
    CoreSignal,
    GateDecision,
)
from hermes_quant.protocol import (
    Action,
    AggregatedSignal,
    MarketState,
    Portfolio,
)

logger = logging.getLogger(__name__)

# cut/01f0 (ADR-0097): the operator flag the SHELL reads to wire the slippage
# haircut into the LIVE decision gate (Rule-5 cost gate + Rule-6 sizer). The pure
# core (pdr_core/gate.py) reads NO env — it consumes the pre-computed penalty as a
# RiskConfig field. This env read lives in the shell so the FLAG-INVENTORY scanner
# (which walks hermes_quant/) sees it. DEFAULT-OFF => byte-identical raw-edge path.
SLIPPAGE_GATE_FLAG = "HERMES_QUANT_SLIPPAGE_GATE"


def slippage_gate_enabled() -> bool:
    """True iff the operator opted into the ADR-0097 decision-gate slippage
    haircut. The shell gates on this; default-OFF (byte-identical raw edge)."""
    return os.environ.get(SLIPPAGE_GATE_FLAG, "0") == "1"


def _slippage_penalty_frac_for(asset_class: str) -> float:
    """Pre-compute the conservative live-vs-paper execution penalty (NAV-fraction)
    for ``asset_class`` so the PURE core need not import the estimator.

    Delegates to ``hermes_quant.risk.slippage_haircut.estimate_live_penalty`` (the
    canonical ADR-0097 estimator, also used by the clean_window evidence seam b61c).
    FAIL-CLOSED: any failure or a non-finite estimate falls back to the conservative
    ``_DEFAULT_PRIOR`` floor — never 0.0, so a thin edge is still haircut toward
    silence even when the estimator misbehaves (the ar08 finite-guard family)."""
    # Lazy import: the estimator is shell-only; importing it here (not in pdr_core)
    # keeps the purity gate green.
    from hermes_quant.risk.slippage_haircut import _DEFAULT_PRIOR, estimate_live_penalty

    try:
        est = estimate_live_penalty(asset_class)
        pen = float(est.penalty_frac)
    except Exception as exc:  # noqa: BLE001 — fail-closed to the conservative floor
        logger.warning(
            "slippage penalty estimate failed for asset_class=%r: %s; "
            "using conservative floor %s",
            asset_class,
            exc,
            _DEFAULT_PRIOR,
        )
        return float(_DEFAULT_PRIOR)
    if not math.isfinite(pen) or pen < 0.0:
        logger.warning(
            "non-finite/negative slippage penalty %r for asset_class=%r; "
            "using conservative floor %s",
            pen,
            asset_class,
            _DEFAULT_PRIOR,
        )
        return float(_DEFAULT_PRIOR)
    return pen


# ---------------------------------------------------------------------------
# Protocol -> core read-interface builders. Each copies ONLY the gate-read
# fields (mirrors the pdr_core/gate_types.py read-surface; no wider).
# ---------------------------------------------------------------------------


def core_signal_from(agg: AggregatedSignal) -> CoreSignal:
    """Project a live AggregatedSignal onto the host-blind CoreSignal.

    ``components`` is deliberately OMITTED: the ADR-0033 lookahead/evidence check
    is dropped-to-shell (coupling edit c). The advisor's live gate path likewise
    consumes the already-aggregated signal, so this is faithful.
    """
    return CoreSignal(
        asset=agg.asset,
        asset_class=agg.asset_class,
        asof=agg.asof,
        direction=agg.direction,
        magnitude=agg.magnitude,
        confidence=agg.confidence,
        metadata=agg.metadata,
    )


def core_market_from(m: MarketState) -> CoreMarketState:
    """Project a live MarketState onto CoreMarketState (1:1 field names).

    ``funding_cost`` / ``borrow_cost`` are not read by the gate and are omitted.
    """
    return CoreMarketState(
        asset=m.asset,
        asof=m.asof,
        volatility=m.volatility,
        commission=m.commission,
        spread=m.spread,
        slippage_estimate=m.slippage_estimate,
        tz=m.tz,
    )


def core_portfolio_from(p: Portfolio) -> CorePortfolio:
    """Project a live Portfolio onto CorePortfolio.

    The raw state fields (equity/peak/daily_open/positions) are passed through;
    CorePortfolio re-derives drawdown_pct / daily_loss_pct / current_position_pct
    with the SAME verbatim ``_finite_or`` bodies as protocol.Portfolio (proven in
    tests/pdr_core/test_gate_types), so the derived reads are bit-for-bit equal.
    ``cash`` / ``realized_pnl_total`` / ``realized_fees_total`` are not gate-read
    and are omitted.
    """
    return CorePortfolio(
        account_id=p.account_id,
        asset_class=p.asset_class,
        asof=p.asof,
        positions=p.positions,
        equity_total=p.equity_total,
        peak_equity=p.peak_equity,
        daily_open_equity=p.daily_open_equity,
    )


def core_risk_config_from(
    live_config: Any,
    *,
    event_risk_enabled: bool,
    slippage_gate_enabled: bool = False,
    slippage_penalty_frac: float = 0.0,
) -> CoreRiskConfig:
    """Build a core RiskConfig MIRRORED from the live gate's ``.config``.

    The live ``risk.gate.RiskConfig`` shares every field below; the core gate
    additionally has ``event_risk_enabled`` (coupling edit b) which replaces the
    live gate's internal ``HERMES_QUANT_EVENT_RISK`` env-read. The caller reads
    that env ONCE (call-time) and passes it here so the shadow snapshots the same
    instant as the live gate.

    cut/01f0 (ADR-0097): the core gate ALSO has ``slippage_gate_enabled`` +
    ``slippage_penalty_frac`` (the DEFAULT-OFF decision-gate haircut). The pure
    core reads NO env and does NOT import the estimator, so the SHELL reads the
    ``HERMES_QUANT_SLIPPAGE_GATE`` flag and PRE-COMPUTES the conservative penalty
    here, passing both in. Defaults (False / 0.0) keep the config byte-identical
    when the operator hasn't opted in.

    If ``live_config`` is None (or any field is missing), fall back to the core
    default for that field — the resulting divergence is logged but harmless.
    """
    if live_config is None:
        return CoreRiskConfig(
            event_risk_enabled=event_risk_enabled,
            slippage_gate_enabled=slippage_gate_enabled,
            slippage_penalty_frac=slippage_penalty_frac,
        )
    default = CoreRiskConfig()

    def _g(name: str) -> Any:
        return getattr(live_config, name, getattr(default, name))

    return CoreRiskConfig(
        max_position_pct=_g("max_position_pct"),
        action_step=_g("action_step"),
        cost_multiple=_g("cost_multiple"),
        max_drawdown_pct=_g("max_drawdown_pct"),
        max_daily_loss_pct=_g("max_daily_loss_pct"),
        min_trade_size=_g("min_trade_size"),
        quarter_kelly=_g("quarter_kelly"),
        cooldown_after_loss_minutes=_g("cooldown_after_loss_minutes"),
        event_risk_window_days=_g("event_risk_window_days"),
        paper_zero_costs=_g("paper_zero_costs"),
        event_risk_enabled=event_risk_enabled,
        slippage_gate_enabled=slippage_gate_enabled,
        slippage_penalty_frac=slippage_penalty_frac,
    )


# ---------------------------------------------------------------------------
# GateDecision -> protocol.Action — LIFTED VERBATIM from
# tests/pdr_core/test_gate_parity.py (the proven shell map). Carries the full
# durable-HALT triple; dropping any halt field would be a money-safety
# regression.
# ---------------------------------------------------------------------------


def gate_decision_to_action(decision: GateDecision | None) -> Action | None:
    """Map a core GateDecision onto the live protocol.Action, 1:1.

    None (gate silence) maps to None. Every field maps straight across; the halt
    triple (halt / halt_scope / halt_until) is preserved verbatim.
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


# ---------------------------------------------------------------------------
# Field-by-field comparator (the SAME polarity as test_gate_parity's
# _assert_action_field_identical, but returns a structured report instead of
# asserting). EXACT equality — a mismatch is a port bug, not float noise.
# ---------------------------------------------------------------------------


def _compare_actions(live: Action | None, shadow: Action | None) -> list[str]:
    """Return the list of field names on which live and shadow Actions diverge.

    Empty list == agreement. Mirrors the parity test's field set, including the
    None-vs-Action presence check and the halt_until type-identity check (to
    surface str-vs-pd.Timestamp drift rather than silently passing).
    """
    # Presence asymmetry: one silenced, the other acted.
    if (live is None) != (shadow is None):
        return ["presence"]
    if live is None and shadow is None:
        return []

    diverged: list[str] = []
    if shadow.target_position_pct != live.target_position_pct:
        diverged.append("target_position_pct")
    if shadow.reason != live.reason:
        diverged.append("reason")
    if shadow.signal_id != live.signal_id:
        diverged.append("signal_id")
    if shadow.halt != live.halt:
        diverged.append("halt")
    if shadow.halt_scope != live.halt_scope:
        diverged.append("halt_scope")
    if live.halt_until is None:
        if shadow.halt_until is not None:
            diverged.append("halt_until")
    else:
        if shadow.halt_until != live.halt_until or type(shadow.halt_until) is not type(
            live.halt_until
        ):
            diverged.append("halt_until")
    return diverged


# ---------------------------------------------------------------------------
# The shadow runner — best-effort, observe-only.
# ---------------------------------------------------------------------------


def run_shadow_gate(
    *,
    agg_signal: AggregatedSignal,
    market: MarketState,
    portfolio: Portfolio,
    halt_state: Any,
    live_action: Action | None,
    live_config: Any,
    event_risk_enabled: bool = False,
) -> dict[str, Any] | None:
    """Run the ported core gate in SHADOW over the live inputs and compare.

    The LIVE ``live_action`` is the ORACLE — this function NEVER mutates it and
    NEVER returns it as the decision. It builds core inputs from the SAME
    protocol objects the live gate saw, runs ``CoreDefaultRiskGate.gate(...)``,
    maps the verdict onto a ``protocol.Action``, and compares field-by-field.

    Returns a divergence report dict on success::

        {"diverged": bool, "fields": [..], "live": <Action|None>, "shadow": <Action|None>}

    Logs at WARNING on any divergence (INFO on agreement). On ANY exception it
    swallows the error (logging at WARNING, best-effort) and returns ``None`` —
    a shadow failure must NEVER affect the live decision.
    """
    try:
        # cut/01f0 (ADR-0097): read the DEFAULT-OFF slippage-gate flag ONCE and
        # pre-compute the conservative per-asset-class penalty so the pure core
        # consumes a plain float (no estimator import in pdr_core). Default-OFF =>
        # penalty unused / 0.0 => byte-identical raw-edge core path.
        slip_enabled = slippage_gate_enabled()
        slip_penalty = (
            _slippage_penalty_frac_for(agg_signal.asset_class) if slip_enabled else 0.0
        )
        core_cfg = core_risk_config_from(
            live_config,
            event_risk_enabled=event_risk_enabled,
            slippage_gate_enabled=slip_enabled,
            slippage_penalty_frac=slip_penalty,
        )
        # Fresh per call — matches the advisor, which builds a fresh live gate
        # per call and never records a loss (cooldowns empty).
        core_gate = CoreDefaultRiskGate(core_cfg)
        core_signal = core_signal_from(agg_signal)
        core_market = core_market_from(market)
        core_portfolio = core_portfolio_from(portfolio)
        shadow_decision = core_gate.gate(core_signal, core_market, core_portfolio, halt_state)
        shadow_action = gate_decision_to_action(shadow_decision)

        diverged_fields = _compare_actions(live_action, shadow_action)
        report = {
            "diverged": bool(diverged_fields),
            "fields": diverged_fields,
            "live": live_action,
            "shadow": shadow_action,
        }
        if diverged_fields:
            logger.warning(
                "pdr_core shadow DIVERGED on %s: live=%r shadow=%r",
                diverged_fields,
                live_action,
                shadow_action,
            )
        else:
            logger.info("pdr_core shadow agreed with live gate")
        return report
    except Exception as exc:  # noqa: BLE001 — best-effort shadow; never affects live
        logger.warning("pdr_core shadow failed: %s", exc, exc_info=True)
        return None
