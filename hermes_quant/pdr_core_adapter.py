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

import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
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

# ADR-0092 Phase-4 (parity proof): the shadow gate's per-tick divergence report
# is APPENDED here so the operator can drive divergences to zero before the
# cutover (HERMES_QUANT_PDR_CORE_LIVE). Mirrors react/alpaca_shadow.py's
# divergence-log pattern (append-only JSONL, line-buffered, fail-closed). The
# path is resolved at CALL TIME via hermes_quant.home (ADR-0092 ph3 home
# decouple) so HERMES_HOME / HERMES_QUANT_HOME overrides are honored. The
# offline report harness (ops/scripts/quant-pdr-core-parity-report.py) READS
# this log; it is never written outside the SHADOW path (flag-OFF => no write).
_SHADOW_DIVERGENCE_FILE = "pdr-core-shadow-divergence.jsonl"


def _shadow_divergence_path() -> Path:
    """Resolve the shadow-divergence log path at call time (home-decouple honest).

    Uses the ADR-0092 ph3 resolver so an injected HERMES_QUANT_HOME / HERMES_HOME
    lands the log in the same quant home the live gate writes to. Imported lazily
    to keep this module's import graph minimal (and to never fail the SHADOW path
    on a home-resolver import hiccup — the caller swallows any raise).
    """
    from hermes_quant.home import quant_home as _resolve_quant_home

    return _resolve_quant_home() / _SHADOW_DIVERGENCE_FILE


def _persist_divergence_report(report: dict[str, Any], *, path: Path | None = None) -> None:
    """Append one shadow divergence report to the JSONL log (best-effort).

    NEVER raises: a persistence failure must not affect the (already-final) live
    decision — the caller runs this inside the shadow seam's try/except, but we
    also swallow here so a write error can't even surface a warning into the
    live path's exception handler. Line-buffered + flush so an interrupted run
    still leaves complete prior lines (the parity harness reads line-by-line).

    The persisted record is a FLATTENED, JSON-serializable view of the report:
    the live/shadow Action objects are reduced to their primitive fields (an
    Action is not JSON-serializable as-is). asof stamps the wall-clock so the
    operator can window the divergence sample (clean-window aligned).
    """
    try:
        out = path or _shadow_divergence_path()
        out.parent.mkdir(parents=True, exist_ok=True)
        live = report.get("live")
        shadow = report.get("shadow")
        record = {
            "asof": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "diverged": bool(report.get("diverged")),
            "fields": list(report.get("fields") or []),
            "live": _action_primitives(live),
            "shadow": _action_primitives(shadow),
        }
        line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        with out.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
    except Exception as exc:  # noqa: BLE001 — persistence is strictly best-effort
        logger.warning("pdr_core shadow: divergence persistence failed (non-blocking): %s", exc)


def _action_primitives(action: Action | None) -> dict[str, Any] | None:
    """Reduce a protocol.Action to a JSON-serializable dict (None passes through).

    halt_until may be a pd.Timestamp; isoformat() it (or str()) so the log line
    is serializable and the harness can compare halt windows.
    """
    if action is None:
        return None
    halt_until = action.halt_until
    if halt_until is not None and not isinstance(halt_until, str):
        try:
            halt_until = halt_until.isoformat()
        except Exception:  # noqa: BLE001 — fall back to str() for any exotic type
            halt_until = str(halt_until)
    return {
        "target_position_pct": action.target_position_pct,
        "reason": action.reason,
        "signal_id": action.signal_id,
        "halt": action.halt,
        "halt_scope": action.halt_scope,
        "halt_until": halt_until,
    }

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
    persist: bool = True,
    divergence_path: Path | None = None,
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

    ADR-0092 Phase-4 (parity proof): when ``persist`` is True (the default for
    the live shadow seam) the report is APPENDED to the shadow-divergence JSONL
    (``pdr-core-shadow-divergence.jsonl`` under the resolved quant home) so the
    operator can accumulate the parity sample and drive divergences to zero
    before building the cutover. Persistence is strictly best-effort — a write
    failure is swallowed and never affects the (already-final) live decision.
    Tests pass ``persist=False`` (or a throwaway ``divergence_path``) to avoid
    touching real operator state.
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
        # ADR-0092 Phase-4: accumulate the parity sample (best-effort; never
        # affects the live decision — persistence swallows its own errors).
        if persist:
            _persist_divergence_report(report, path=divergence_path)
        return report
    except Exception as exc:  # noqa: BLE001 — best-effort shadow; never affects live
        logger.warning("pdr_core shadow failed: %s", exc, exc_info=True)
        return None


# ===========================================================================
# ADR-0092 Phase-4 (parity proof) — the AGGREGATE-layer runtime SHADOW.
#
# This extends the DECIDE-layer shadow above (run_shadow_gate) one seam UP the
# pipeline, to the PERCEIVE/AGGREGATE layer. ``hermes_quant.pdr_core.aggregate.
# core_aggregate`` is the host-blind port of the FLAGS-OFF, COLD-START path of
# the live ``BMAAggregator.aggregate`` (ADR-0003 vote fusion). It has a STATIC
# parity test (tests/pdr_core/test_aggregate_parity.py) but ZERO live exercise.
# :func:`run_shadow_aggregate` is the analogous RUNTIME SHADOW: it runs the core
# aggregator over the SAME views the live aggregator just consumed, compares the
# fused-signal surface field-by-field, and LOGS divergence — purely to validate
# the port before any cutover, exactly like run_shadow_gate does for the gate.
#
# THE CRITICAL CORRECTNESS RAIL (parity-valid path only): core_aggregate ports
# ONLY the FLAGS-OFF / COLD-START arm. When a FITTED isotonic calibrator is
# active, OR any of the 7 learning flags is set, OR the live aggregator has had
# ``update()`` calls (non-uniform posteriors), the live BMA diverges from
# core_aggregate BY DESIGN — that is NOT a port bug. So the shadow DETECTS that
# precondition and records an ``{"comparable": false, "reason": ...}`` report
# rather than flagging a FALSE divergence. The two comparability gates mirror the
# static test's parity driver (cold-start calibrator + every learning flag unset).
# ===========================================================================

# The aggregate-layer shadow divergence log (sibling to the gate log). Resolved
# at CALL TIME via hermes_quant.home so HERMES_HOME / HERMES_QUANT_HOME overrides
# land it in the same quant home the live aggregator writes to. An offline parity
# harness can READ this (line-by-line JSONL); it is never written outside the
# SHADOW path (flag-OFF => no write).
_SHADOW_AGGREGATE_DIVERGENCE_FILE = "pdr-core-shadow-aggregate-divergence.jsonl"

# The 7 default-OFF learning flags that, when ANY is set, make the live BMA
# diverge from the cold-start core port BY DESIGN. Lifted VERBATIM from the
# static parity test's ``_FLAGS`` (tests/pdr_core/test_aggregate_parity.py) — the
# proven set the parity oracle requires unset. A drift here would silently flag
# expected (learning-on) divergence as a port bug.
_AGG_LEARNING_FLAGS: tuple[str, ...] = (
    "HERMES_QUANT_L2_POSTERIOR_DECAY",
    "HERMES_QUANT_L2_PER_ANALYST_CALIB",
    "HERMES_QUANT_L2_LESSON_HAIRCUT",
    "HERMES_QUANT_L2_POSTERIOR_PERSIST",
    "HERMES_QUANT_L2_STACKING",
    "HERMES_QUANT_STACKING",
    "HERMES_QUANT_DISSENT_CAP",
)

# The operator flag the SHELL reads to wire the aggregate shadow into the LIVE
# advisor path. DEFAULT-OFF => the seam is dark, byte-identical to today.
PDR_CORE_AGG_SHADOW_FLAG = "HERMES_QUANT_PDR_CORE_AGG_SHADOW"

# The metadata audit keys the static parity test compares (the byte-identical
# OFF-path surface). The 4 flag-gated metadata injections (stacking / per-analyst
# calib / lesson-haircut / hierarchical-pooling) and the ADR-0084 ``event_risk``
# carrier key are DELIBERATELY NOT in this set: when a learning flag is on the
# shadow is already short-circuited as not-comparable, and the event_risk carrier
# is an additive advisory key the core never produces — comparing only these
# proven keys avoids a false divergence on a key that is expected to differ.
_AGG_METADATA_AUDIT_KEYS: tuple[str, ...] = (
    "vote_share",
    "n_contributing",
    "n_views",
    "horizons_present",
    "horizon_agreement",
    "ic_dedup_excluded_analysts",
    "regime_state",
    "regime_weight_multipliers",
)

# Float tolerance for the scalar vote surface — matches the static parity test's
# ``pytest.approx(abs=1e-12)``. The arithmetic is the SAME ordering so the values
# should be near-exact; 1e-12 absorbs benign float-op-ordering noise. A real port
# bug is orders of magnitude larger.
_AGG_FLOAT_ATOL: float = 1e-12


def _shadow_aggregate_divergence_path() -> Path:
    """Resolve the aggregate-shadow divergence log path at call time.

    Mirror of :func:`_shadow_divergence_path` — uses the ADR-0092 ph3 home
    resolver so an injected HERMES_QUANT_HOME / HERMES_HOME lands the log in the
    same quant home the live aggregator writes to. Imported lazily to keep the
    import graph minimal and to never fail the SHADOW path on a resolver hiccup
    (the caller swallows any raise).
    """
    from hermes_quant.home import quant_home as _resolve_quant_home

    return _resolve_quant_home() / _SHADOW_AGGREGATE_DIVERGENCE_FILE


def _agg_learning_flag_active() -> str | None:
    """Return the name of the first set learning flag, else None.

    A learning flag being set means the live BMA path diverges from the
    cold-start core port BY DESIGN (ADR-0092 Inc-1 step 6) — the shadow records
    this as not-comparable rather than a port bug.
    """
    for name in _AGG_LEARNING_FLAGS:
        if os.environ.get(name, "0") == "1":
            return name
    return None


def _signal_primitives(signal: Any | None) -> dict[str, Any] | None:
    """Reduce a fused signal to a JSON-serializable comparable primitive dict.

    Accepts EITHER a live ``protocol.AggregatedSignal`` OR a core
    ``pdr_core.aggregate.CoreAggregatedSignal`` (they share the same scalar vote
    surface + metadata audit keys, which is the whole point of the parity port).
    None passes through (a defensive guard — the live aggregate path never
    returns None, but the comparator stays None-safe like _compare_actions).

    The scalar surface (direction / magnitude / confidence / confidence_raw /
    horizon / aggregator) is copied straight across. ``asof`` is isoformat()'d
    like :func:`_action_primitives` does for halt_until, so the persisted line is
    serializable. The metadata is FLATTENED to ONLY the proven OFF-path audit
    keys (``_AGG_METADATA_AUDIT_KEYS``) plus the per-analyst ``weights`` map; the
    flag-gated injections and the ADR-0084 ``event_risk`` carrier are excluded so
    a comparison never trips on a key that is expected to differ. On the silence
    path the live/core metadata carries only ``{"reason": ...}`` — that reason is
    surfaced so the comparator can match the silence reason too.
    """
    if signal is None:
        return None
    asof = signal.asof
    if asof is not None and not isinstance(asof, str):
        try:
            asof = asof.isoformat()
        except Exception:  # noqa: BLE001 — fall back to str() for any exotic type
            asof = str(asof)
    meta = signal.metadata or {}
    flat: dict[str, Any] = {}
    # Silence / flat path: metadata carries only the reason.
    if "reason" in meta:
        flat["reason"] = meta.get("reason")
    else:
        for k in _AGG_METADATA_AUDIT_KEYS:
            flat[k] = meta.get(k)
        # weights keyed by analyst name -> float (compared with float tolerance).
        weights = meta.get("weights")
        if isinstance(weights, dict):
            flat["weights"] = {str(a): float(w) for a, w in weights.items()}
        else:
            flat["weights"] = weights
    return {
        "direction": signal.direction,
        "magnitude": signal.magnitude,
        "confidence": signal.confidence,
        "confidence_raw": signal.confidence_raw,
        "horizon": signal.horizon,
        "aggregator": signal.aggregator,
        "asof": asof,
        "metadata": flat,
    }


def _floats_close(a: Any, b: Any) -> bool:
    """True iff a and b are both finite and within ``_AGG_FLOAT_ATOL`` (abs).

    Non-finite or non-numeric inputs compare by exact equality so a NaN-vs-NaN
    (or None-vs-None) does not silently pass the tolerance window.
    """
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return a == b
    if not (math.isfinite(fa) and math.isfinite(fb)):
        return a == b
    return abs(fa - fb) <= _AGG_FLOAT_ATOL


def _compare_signals(live: Any | None, shadow: Any | None) -> list[str]:
    """Return the list of field names on which live and shadow fused signals diverge.

    Empty list == agreement. EXACT equality for direction / horizon / aggregator
    and the metadata ints / lists; ``_AGG_FLOAT_ATOL`` (1e-12) tolerance for the
    floats (magnitude / confidence / confidence_raw / vote_share / weights) — the
    SAME-ordering arithmetic of the port, so near-exact (a real port bug is far
    larger). Mirrors the static parity test's assertion set, including the
    silence-path ``reason`` match and the None-vs-signal presence check.

    Both inputs are first reduced via :func:`_signal_primitives`, so this also
    accepts a live AggregatedSignal vs a core CoreAggregatedSignal directly.
    """
    lp = _signal_primitives(live)
    sp = _signal_primitives(shadow)
    # Presence asymmetry: one silenced-to-None, the other a signal.
    if (lp is None) != (sp is None):
        return ["presence"]
    if lp is None and sp is None:
        return []

    diverged: list[str] = []
    # Exact-equality scalar fields.
    if sp["direction"] != lp["direction"]:
        diverged.append("direction")
    if sp["horizon"] != lp["horizon"]:
        diverged.append("horizon")
    if sp["aggregator"] != lp["aggregator"]:
        diverged.append("aggregator")
    # Float scalar fields (1e-12 tolerance).
    if not _floats_close(sp["magnitude"], lp["magnitude"]):
        diverged.append("magnitude")
    if not _floats_close(sp["confidence"], lp["confidence"]):
        diverged.append("confidence")
    if not _floats_close(sp["confidence_raw"], lp["confidence_raw"]):
        diverged.append("confidence_raw")

    lm, sm = lp["metadata"], sp["metadata"]
    # Silence / flat path: both carry only a reason.
    if "reason" in lm or "reason" in sm:
        if lm.get("reason") != sm.get("reason"):
            diverged.append("metadata.reason")
        return diverged

    # Full audit-dict parity.
    if not _floats_close(sm.get("vote_share"), lm.get("vote_share")):
        diverged.append("metadata.vote_share")
    for k in ("n_contributing", "n_views", "horizons_present", "horizon_agreement",
              "ic_dedup_excluded_analysts", "regime_state", "regime_weight_multipliers"):
        if sm.get(k) != lm.get(k):
            diverged.append(f"metadata.{k}")
    # weights: same analyst key set + value-identical (float tolerance).
    lw = lm.get("weights") or {}
    sw = sm.get("weights") or {}
    if not isinstance(lw, dict) or not isinstance(sw, dict) or set(lw) != set(sw):
        diverged.append("metadata.weights")
    else:
        for analyst, w in lw.items():
            if not _floats_close(sw.get(analyst), w):
                diverged.append("metadata.weights")
                break
    return diverged


def _persist_aggregate_divergence_report(
    report: dict[str, Any], *, path: Path | None = None
) -> None:
    """Append one aggregate-shadow report to the JSONL log (best-effort).

    NEVER raises (mirror of :func:`_persist_divergence_report`): a persistence
    failure must not affect the already-final live decision. The live/shadow
    signals are reduced to their primitive dicts so the line is JSON-serializable.
    Line-buffered + flushed so an interrupted run still leaves complete prior
    lines. A not-comparable report (``comparable: False``) is still persisted so
    the operator can see WHY a tick was skipped (fitted calibrator / learning
    flag) when driving the parity sample.
    """
    try:
        out = path or _shadow_aggregate_divergence_path()
        out.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "asof": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "comparable": bool(report.get("comparable", True)),
            "diverged": bool(report.get("diverged")),
            "reason": report.get("reason"),
            "fields": list(report.get("fields") or []),
            "live": _signal_primitives(report.get("live")),
            "shadow": _signal_primitives(report.get("shadow")),
        }
        line = json.dumps(record, separators=(",", ":"), sort_keys=True, default=str) + "\n"
        with out.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
    except Exception as exc:  # noqa: BLE001 — persistence is strictly best-effort
        logger.warning(
            "pdr_core aggregate shadow: divergence persistence failed (non-blocking): %s",
            exc,
        )


def run_shadow_aggregate(
    *,
    views: Any,
    ctx: Any,
    aggregator: Any,
    live_signal: Any,
    persist: bool = True,
    divergence_path: Path | None = None,
) -> dict[str, Any] | None:
    """Run the ported core aggregator in SHADOW over the live views and compare.

    The LIVE ``live_signal`` is the ORACLE — this function NEVER mutates it and
    NEVER returns it as the decision. It builds a :class:`CoreAggregateContext`
    from ``ctx``, projects each live ``protocol.AnalystView`` onto the host-blind
    ``pdr_core.contracts.AnalystView`` (the projection is lifted from the static
    parity test's ``_core_view``, reading the scalar fields off the LIVE view and
    sourcing asset / asset_class / asof_decision / bar_ts from ``ctx`` since the
    core AnalystView needs them and the live view does not carry them), threads
    the LIVE aggregator's config (require_ensemble / agreement_bonus /
    horizon_weights) into ``core_aggregate``, maps the result onto a comparable
    primitive dict, and compares field-by-field via :func:`_compare_signals`.

    Returns a report dict on success::

        {"comparable": True,  "diverged": bool, "fields": [..], "live": <sig>, "shadow": <sig>}
        {"comparable": False, "reason": "fitted_calibrator_active" | "learning_flag_active", ...}

    THE PARITY-VALID-PATH RAIL: core_aggregate ports ONLY the FLAGS-OFF /
    COLD-START arm. If the live aggregator's calibrator is NOT a
    ``ColdStartCalibrator`` (a fitted isotonic is active) OR any of the 7 learning
    flags is set, the live BMA diverges from the core BY DESIGN — so the report is
    ``{"comparable": False, ...}`` and NO divergence is flagged (avoids a FALSE
    port-bug signal). Non-uniform posteriors (from ``update()`` calls) are also
    a learning-on state, but the cold-start-calibrator gate is the proxy the
    static parity oracle uses (a fresh aggregator has no fitted calibrator); a
    posterior-bearing aggregator that still reports cold-start would surface as a
    real weights divergence, which is the honest signal.

    Logs at WARNING on divergence (INFO on agreement / not-comparable). On ANY
    exception it swallows the error (WARNING, best-effort) and returns ``None`` —
    a shadow failure must NEVER affect the live decision. When ``persist`` is True
    the report is APPENDED to the aggregate-shadow JSONL (best-effort). Tests pass
    ``persist=False`` or a throwaway ``divergence_path`` to avoid touching real
    operator state.
    """
    try:
        # COMPARABILITY GATE 1: a fitted (non-cold-start) calibrator means the
        # live confidence is NOT the pure (raw+2)/8 cold-start arithmetic the core
        # reproduces — the divergence is BY DESIGN, not a port bug.
        calibrator = getattr(aggregator, "calibrator", None)
        if type(calibrator).__name__ != "ColdStartCalibrator":
            report = {
                "comparable": False,
                "reason": "fitted_calibrator_active",
                "diverged": False,
                "fields": [],
                "live": live_signal,
                "shadow": None,
            }
            logger.info(
                "pdr_core aggregate shadow: not comparable (fitted calibrator %r active)",
                type(calibrator).__name__,
            )
            if persist:
                _persist_aggregate_divergence_report(report, path=divergence_path)
            return report

        # COMPARABILITY GATE 2: any learning flag set => the live path diverges
        # from the cold-start core port BY DESIGN.
        flag = _agg_learning_flag_active()
        if flag is not None:
            report = {
                "comparable": False,
                "reason": "learning_flag_active",
                "flag": flag,
                "diverged": False,
                "fields": [],
                "live": live_signal,
                "shadow": None,
            }
            logger.info("pdr_core aggregate shadow: not comparable (learning flag %s set)", flag)
            if persist:
                _persist_aggregate_divergence_report(report, path=divergence_path)
            return report

        # Lazy import: keep the import paid only when the shadow actually runs
        # (the flag-off advisor path never reaches here). core_aggregate is
        # stdlib-only and lives in pdr_core, which this SHELL module may import
        # freely (same as the top-level pdr_core.gate import).
        from hermes_quant.pdr_core.aggregate import CoreAggregateContext, core_aggregate
        from hermes_quant.pdr_core.contracts import AnalystView as CoreView

        # Build the core context from the live ctx (the 4 scalars the vote stamps).
        core_ctx = CoreAggregateContext(
            asset=ctx.asset,
            timeframe=ctx.timeframe,
            asset_class=ctx.asset_class,
            asof=ctx.asof,
        )

        # Project each live protocol.AnalystView -> pdr_core.contracts.AnalystView.
        # The scalar vote fields come off the LIVE view; asset / asset_class /
        # asof_decision / bar_ts come from ctx (the core view needs them; the live
        # view does not carry them) — replicates the static test's _core_view
        # construction faithfully.
        core_views = [
            CoreView(
                analyst=v.analyst,
                asset=ctx.asset,
                asset_class=ctx.asset_class,
                direction=v.direction,
                magnitude=v.magnitude,
                confidence=v.confidence,
                confidence_raw=v.confidence_raw,
                horizon=v.horizon,
                asof_decision=ctx.asof,
                bar_ts=ctx.asof,
            )
            for v in (views or [])
        ]

        # Thread the LIVE aggregator's config so the core matches it (don't
        # hardcode defaults). cold_start=True matches the CalibratorNotReady arm
        # the comparison requires (gate 1 already proved the live calibrator is
        # cold-start). Defaults stand in if an attribute is absent.
        shadow_signal = core_aggregate(
            core_views,
            core_ctx,
            require_ensemble=getattr(aggregator, "require_ensemble", True),
            agreement_bonus=getattr(aggregator, "agreement_bonus", 0.10),
            horizon_weights=getattr(
                aggregator, "horizon_weights", None
            ) or {"1d": 1.00, "1w": 1.20, "1M": 0.80, "1Q": 0.60},
            cold_start=True,
        )

        diverged_fields = _compare_signals(live_signal, shadow_signal)
        report = {
            "comparable": True,
            "diverged": bool(diverged_fields),
            "fields": diverged_fields,
            "live": live_signal,
            "shadow": shadow_signal,
        }
        if diverged_fields:
            logger.warning(
                "pdr_core aggregate shadow DIVERGED on %s: live=%r shadow=%r",
                diverged_fields,
                live_signal,
                shadow_signal,
            )
        else:
            logger.info("pdr_core aggregate shadow agreed with live aggregator")
        if persist:
            _persist_aggregate_divergence_report(report, path=divergence_path)
        return report
    except Exception as exc:  # noqa: BLE001 — best-effort shadow; never affects live
        logger.warning("pdr_core aggregate shadow failed: %s", exc, exc_info=True)
        return None
