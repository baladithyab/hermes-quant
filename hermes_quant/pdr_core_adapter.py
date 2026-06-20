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

# The default-OFF learning flags that, when ANY is set, make the live BMA diverge
# from the cold-start core port BY DESIGN — so a tick under any of them is NOT
# parity-comparable (recording it as diverged would slander a faithful port).
#
# This MUST be exactly the set of vote-branching flags ``BMAAggregator`` actually
# reads at call time — DERIVED from the live ``aggregators/bma.py`` env reads, NOT
# copied from the static parity test's ``_FLAGS`` (the static test only DELETES
# these before running the oracle, so an omission there is harmless; an omission
# HERE causes a FALSE divergence the operator would chase as a port bug). The live
# reads (bma.py): L2_POSTERIOR_DECAY (:234), L2_PER_ANALYST_CALIB (:249),
# L2_LESSON_HAIRCUT (:262), STACKING (:280), HIERARCHICAL_POOLING (:296 — the
# aegis-ag03 / ADR-0096 Gate-3 pooled-weights path; OMITTED in the first cut, which
# let a pooling-on settled aggregator log a FALSE magnitude/weights divergence),
# L2_POSTERIOR_PERSIST (:308), DISSENT_CAP (:1246). NOTE: ``HERMES_QUANT_L2_STACKING``
# is NOT here — the live BMA reads ``HERMES_QUANT_STACKING`` only; an L2_STACKING
# entry (carried by the static test's set) is a phantom that would over-conservatively
# skip an otherwise-comparable tick.
_AGG_LEARNING_FLAGS: tuple[str, ...] = (
    "HERMES_QUANT_L2_POSTERIOR_DECAY",
    "HERMES_QUANT_L2_PER_ANALYST_CALIB",
    "HERMES_QUANT_L2_LESSON_HAIRCUT",
    "HERMES_QUANT_L2_POSTERIOR_PERSIST",
    "HERMES_QUANT_STACKING",
    "HERMES_QUANT_HIERARCHICAL_POOLING",
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


def _agg_uncomparable_state(aggregator: Any) -> str | None:
    """Return a not-comparable reason if the live aggregator is in a state the
    cold-start core port does NOT reproduce, else None (codex-review findings D+E).

    The core ports only the FRESH, uniform-0.5-weight, no-collaborator cold-start
    vote. Two live states diverge BY DESIGN even with a cold-start calibrator and no
    env flag set — comparing against them would log a FALSE port-bug:

      * LEARNED posteriors: once any analyst has accumulated >= n_min_observations
        settled outcomes, the live ``_weight_for`` returns the learned posterior
        accuracy instead of 0.5 (bma.py: ``if stats.n_observations < n_min: return 0.5``).
        We detect a settled aggregator via its ``_stats`` map + ``n_min_observations``.
      * INJECTED collaborators: an ``ic_dedup_gate`` or ``regime_detector`` is consulted
        whenever PRESENT (bma.py:940/988), adjusting the vote independent of its env flag.

    All reads are getattr-guarded so a non-BMA / minimally-stubbed aggregator (or a
    missing attribute) is treated as comparable (the caller's broad try/except is the
    final backstop). Returns the FIRST reason found.
    """
    # (E) injected collaborators that adjust the live vote when merely present.
    if getattr(aggregator, "ic_dedup_gate", None) is not None:
        return "ic_dedup_gate_injected"
    if getattr(aggregator, "regime_detector", None) is not None:
        return "regime_detector_injected"
    # (D) a reused aggregator that has accumulated learned per-analyst posteriors.
    stats = getattr(aggregator, "_stats", None)
    n_min = getattr(aggregator, "n_min_observations", None)
    if isinstance(stats, dict) and isinstance(n_min, int):
        for s in stats.values():
            n_obs = getattr(s, "n_observations", 0)
            if isinstance(n_obs, int) and n_obs >= n_min:
                return "learned_posteriors_active"
    return None


def _signal_primitives(signal: Any | None) -> dict[str, Any] | None:
    """Reduce a fused signal to a JSON-serializable comparable primitive dict.

    Accepts EITHER a live ``protocol.AggregatedSignal`` OR a core
    ``pdr_core.aggregate.CoreAggregatedSignal`` (they share the same scalar vote
    surface + metadata audit keys, which is the whole point of the parity port).
    None passes through (a defensive guard — the live aggregate path never
    returns None, but the comparator stays None-safe like _compare_actions).

    The scalar surface (direction / magnitude / confidence / confidence_raw /
    horizon / aggregator) is copied straight across, plus the IDENTITY fields
    (asset / timeframe / asset_class / asof) and the ``components`` tuple reduced
    to its scalar vote fields. ``asof`` is isoformat()'d like
    :func:`_action_primitives` does for halt_until, so the persisted line is
    serializable. The metadata is FLATTENED to ONLY the proven OFF-path audit
    keys (``_AGG_METADATA_AUDIT_KEYS``) plus the per-analyst ``weights`` map; the
    flag-gated injections and the ADR-0084 ``event_risk`` carrier are excluded so
    a comparison never trips on a key that is expected to differ. On the silence
    path the live/core metadata carries only ``{"reason": ...}`` — that reason is
    surfaced so the comparator can match the silence reason too.

    codex-review-2026-06-20 finding C: ``components`` + the identity fields
    (asset/timeframe/asof) are LOAD-BEARING (the static parity test asserts
    component scalar parity; downstream reads signal identity/asof/components for
    halt/event-risk/outcome-credit). Dropping them let a runtime projection/core
    bug in those fields log as AGREEMENT (a missed divergence). They are now in the
    compared surface. ``components`` is reduced to the cross-shape scalar fields
    (analyst/direction/magnitude/confidence/confidence_raw/horizon) so a live
    ``protocol.AnalystView`` and a core ``contracts.AnalystView`` compare 1:1.
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
    # components reduced to the cross-shape scalar vote fields (the static parity
    # test's component-parity surface). Tuple-of-tuples so order is preserved.
    components = tuple(
        (
            getattr(c, "analyst", None),
            getattr(c, "direction", None),
            getattr(c, "magnitude", None),
            getattr(c, "confidence", None),
            getattr(c, "confidence_raw", None),
            getattr(c, "horizon", None),
        )
        for c in (getattr(signal, "components", ()) or ())
    )
    return {
        "asset": getattr(signal, "asset", None),
        "timeframe": getattr(signal, "timeframe", None),
        "asset_class": getattr(signal, "asset_class", None),
        "direction": signal.direction,
        "magnitude": signal.magnitude,
        "confidence": signal.confidence,
        "confidence_raw": signal.confidence_raw,
        "horizon": signal.horizon,
        "aggregator": signal.aggregator,
        "asof": asof,
        "components": components,
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
    # Exact-equality scalar + identity fields (codex-review finding C: identity
    # fields asset/timeframe/asset_class/asof are load-bearing for downstream
    # halt/event-risk/replay; a projection bug there must surface as a divergence).
    for field in ("direction", "horizon", "aggregator", "asset", "timeframe",
                  "asset_class", "asof"):
        if sp.get(field) != lp.get(field):
            diverged.append(field)
    # Float scalar fields (1e-12 tolerance).
    if not _floats_close(sp["magnitude"], lp["magnitude"]):
        diverged.append("magnitude")
    if not _floats_close(sp["confidence"], lp["confidence"]):
        diverged.append("confidence")
    if not _floats_close(sp["confidence_raw"], lp["confidence_raw"]):
        diverged.append("confidence_raw")

    # components: same length + scalar-field parity (the static parity test's
    # component surface; drives outcome crediting / joint-state replay). The
    # magnitude/confidence/confidence_raw within each component get float tolerance;
    # analyst/direction/horizon exact.
    lc, sc = lp["components"], sp["components"]
    if len(lc) != len(sc):
        diverged.append("components")
    else:
        for lcomp, scomp in zip(lc, sc, strict=False):
            # tuple = (analyst, direction, magnitude, confidence, confidence_raw, horizon)
            if (
                scomp[0] != lcomp[0]  # analyst
                or scomp[1] != lcomp[1]  # direction
                or scomp[5] != lcomp[5]  # horizon
                or not _floats_close(scomp[2], lcomp[2])  # magnitude
                or not _floats_close(scomp[3], lcomp[3])  # confidence
                or not _floats_close(scomp[4], lcomp[4])  # confidence_raw
            ):
                diverged.append("components")
                break

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
            # codex-review finding H: surface WHICH flag / detail blocked a
            # not-comparable tick so the operator can see why coverage was skipped
            # (e.g. learning_flag_active -> which flag) when driving the sample.
            "flag": report.get("flag"),
            "detail": report.get("detail"),
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

        # COMPARABILITY GATE 3 (codex-review-2026-06-20, findings D + E): the gate-1/2
        # checks (calibrator type + env flags) are NECESSARY but not SUFFICIENT. The
        # core ports ONLY the FRESH (uniform-0.5-weight) cold-start vote, so two more
        # live states diverge from it BY DESIGN even with no fitted calibrator + no env
        # flag set:
        #   (D) a REUSED aggregator that has accumulated settled outcomes — once any
        #       analyst's _AnalystStats.n_observations >= n_min_observations, the live
        #       _weight_for returns the LEARNED posterior accuracy, not 0.5. The core's
        #       fixed uniform weight then diverges -> a FALSE port-bug signal.
        #   (E) an INJECTED ic_dedup_gate or regime_detector adjusts the live vote
        #       inputs/weights even when its env flag is unset (the collaborator is
        #       consulted whenever it is present, see bma.py:940/988); the core runs the
        #       unadjusted off-path vote -> FALSE divergence.
        # Detect both and record not-comparable (the live decision is untouched either way).
        reason3 = _agg_uncomparable_state(aggregator)
        if reason3 is not None:
            report = {
                "comparable": False,
                "reason": reason3,
                "diverged": False,
                "fields": [],
                "live": live_signal,
                "shadow": None,
            }
            logger.info("pdr_core aggregate shadow: not comparable (%s)", reason3)
            if persist:
                _persist_aggregate_divergence_report(report, path=divergence_path)
            return report

        # Lazy import: keep the import paid only when the shadow actually runs
        # (the flag-off advisor path never reaches here). core_aggregate is
        # stdlib-only and lives in pdr_core, which this SHELL module may import
        # freely (same as the top-level pdr_core.gate import).
        from hermes_quant.pdr_core.aggregate import (
            DEFAULT_AGREEMENT_BONUS as _CORE_DEFAULT_AGREEMENT_BONUS,
        )
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
        #
        # The core CoreView.__post_init__ rejects magnitude / confidence_raw > 1.0,
        # but the live protocol.AnalystView accepts them. A live view that exceeds the
        # core bounds would make this projection raise; without a guard the outer
        # try/except would swallow it and return None, SILENTLY dropping the tick from
        # the parity sample with no record of why. Record it as not-comparable instead
        # so the operator's coverage stays honest (the live decision is untouched).
        try:
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
        except (ValueError, TypeError) as proj_exc:
            report = {
                "comparable": False,
                "reason": "view_out_of_core_bounds",
                "detail": str(proj_exc),
                "diverged": False,
                "fields": [],
                "live": live_signal,
                "shadow": None,
            }
            logger.info(
                "pdr_core aggregate shadow: not comparable (live view outside core "
                "bounds: %s)",
                proj_exc,
            )
            if persist:
                _persist_aggregate_divergence_report(report, path=divergence_path)
            return report

        # Thread the LIVE aggregator's config so the core matches it (don't
        # hardcode defaults). cold_start=True matches the CalibratorNotReady arm
        # the comparison requires (gate 1 already proved the live calibrator is
        # cold-start). Defaults stand in only when an attribute is genuinely ABSENT.
        #
        # codex-review-2026-06-20 finding B: the live BMA also reads the multi-horizon
        # multipliers off self.config (bma.py:1314/1319) — thread them so a recipe with
        # custom horizon_agreement_bonus / horizon_disagreement_penalty does not log a
        # FALSE divergence. And pass horizon_weights FAITHFULLY: the live _horizon_weight
        # does ``self.horizon_weights.get(h, 1.0)``, so a PRESENT-but-empty dict means
        # "every horizon -> 1.0" — replacing a falsy (empty) dict with the non-uniform
        # default table (the prior ``or {...}``) was itself a false-divergence source.
        # Use a sentinel so we fall back to the core default ONLY when the attr is missing.
        _MISSING = object()
        live_hw = getattr(aggregator, "horizon_weights", _MISSING)
        cfg = getattr(aggregator, "config", None)
        core_kwargs: dict[str, Any] = dict(
            require_ensemble=getattr(aggregator, "require_ensemble", True),
            agreement_bonus=getattr(aggregator, "agreement_bonus", _CORE_DEFAULT_AGREEMENT_BONUS),
            cold_start=True,
        )
        if live_hw is not _MISSING and live_hw is not None:
            core_kwargs["horizon_weights"] = live_hw  # verbatim (incl. an empty dict)
        if cfg is not None:
            if hasattr(cfg, "horizon_agreement_bonus"):
                core_kwargs["horizon_agreement_bonus"] = cfg.horizon_agreement_bonus
            if hasattr(cfg, "horizon_disagreement_penalty"):
                core_kwargs["horizon_disagreement_penalty"] = cfg.horizon_disagreement_penalty
        shadow_signal = core_aggregate(core_views, core_ctx, **core_kwargs)

        diverged_fields = _compare_signals(live_signal, shadow_signal)
        report = {
            "comparable": True,
            "diverged": bool(diverged_fields),
            "fields": diverged_fields,
            "live": live_signal,
            "shadow": shadow_signal,
        }
        if diverged_fields:
            # codex-review-2026-06-20 finding F: log ONLY the diverged field names,
            # NOT the full signal repr. A live AggregatedSignal.components carries the
            # original AnalystView objects incl. rationale / metadata / evidence_ids —
            # a shadow-only diagnostic must not leak proprietary/semantic analyst
            # payloads to the logs (the JSONL persist path already reduces to
            # primitives). The full reduced signals are in the persisted report for
            # offline analysis; the WARNING names only what diverged.
            logger.warning(
                "pdr_core aggregate shadow DIVERGED on %s (see %s for the reduced "
                "live/shadow primitives)",
                diverged_fields,
                _SHADOW_AGGREGATE_DIVERGENCE_FILE,
            )
        else:
            logger.info("pdr_core aggregate shadow agreed with live aggregator")
        if persist:
            _persist_aggregate_divergence_report(report, path=divergence_path)
        return report
    except Exception as exc:  # noqa: BLE001 — best-effort shadow; never affects live
        logger.warning("pdr_core aggregate shadow failed: %s", exc, exc_info=True)
        return None
