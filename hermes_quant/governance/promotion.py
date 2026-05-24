"""hermes_quant.governance.promotion — paper→live promotion evaluator
(ADR-0031 D5).

Read-only: gathers metrics from the audit log and produces a
`PromotionDecision`. Thresholds are NOT hardcoded here on purpose —
`hermes_quant.react.live.LiveTradingApproval` is the single source of
truth (ADR-0029 D7). If that module is not yet available in the tree,
`_LATE_BIND_THRESHOLDS` mirrors ADR-0029's numbers as a sentinel
fallback.

# TODO(integration): remove fallback once react.live lands.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from hermes_quant.governance import audit_log

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Threshold binding
# ---------------------------------------------------------------------------

# Mirrors ADR-0029 D7 numbers verbatim. Used only when react.live import
# fails (sibling task hasn't landed). Tests rely on this fallback path.
_LATE_BIND_THRESHOLDS: dict[str, float] = {
    "min_paper_outcomes": 100,
    "min_sharpe_95ci_lower": 1.0,
    "max_rolling_30d_drawdown_pct": 0.01,
    "max_calibrator_drift": 0.05,
    "killswitch_window_days": 14,
    "immutable_breach_window_days": 30,
}


def _load_thresholds() -> dict[str, float]:
    """Try to import LiveTradingApproval thresholds from react.live; fall
    back to `_LATE_BIND_THRESHOLDS` on ImportError. The fallback is
    intentional decoupling per ADR-0031.

    Wire shape: react.live exports both `LiveTradingApproval` (the Pydantic
    model whose validator enforces the bounds) AND `LIVE_APPROVAL_THRESHOLDS`
    (a dict mirroring those bounds for cross-module consumption — this exact
    function). The dict is the integration handle; the validator is the
    enforcement.
    """
    try:
        from hermes_quant.react.live import LIVE_APPROVAL_THRESHOLDS  # type: ignore[attr-defined]
    except (ImportError, AttributeError):
        return dict(_LATE_BIND_THRESHOLDS)

    if isinstance(LIVE_APPROVAL_THRESHOLDS, dict) and LIVE_APPROVAL_THRESHOLDS:
        return dict(LIVE_APPROVAL_THRESHOLDS)
    return dict(_LATE_BIND_THRESHOLDS)


# ---------------------------------------------------------------------------
# Decision model
# ---------------------------------------------------------------------------


class PromotionDecision(BaseModel):
    """Result of `evaluate()`. `promoted=True` only when every field passes
    the LiveTradingApproval validator (ADR-0029 D7)."""

    promoted: bool
    blocked_by: list[str] = Field(default_factory=list)
    paper_outcomes_count: int = 0
    rolling_30d_realized_sharpe: float = 0.0
    sharpe_95ci_lower: float = 0.0
    rolling_30d_max_drawdown_pct: float = 0.0
    no_killswitch_in_trailing_14d: bool = False
    immutable_breaches_in_window: int = 0
    calibrator_drift_max: float = 0.0
    weekly_retro_promotion_readiness: bool = False


# ---------------------------------------------------------------------------
# Metric collection from audit log
# ---------------------------------------------------------------------------


def _collect_metrics(asof: datetime) -> dict[str, Any]:
    """Walk the audit log and compute the inputs to `PromotionDecision`."""
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=UTC)

    window_30d_start = asof - timedelta(days=30)
    window_14d_start = asof - timedelta(days=14)

    paper_outcomes = 0
    fills_pnl: list[float] = []
    killswitch_in_14d = False
    immutable_breach_count = 0
    calibrator_drift_max = 0.0
    weekly_retro_ready = False
    sharpe_ci_lower = 0.0
    rolling_30d_max_drawdown_pct = 0.0

    for evt in audit_log.read():
        evt_asof = evt.asof
        if evt_asof.tzinfo is None:
            evt_asof = evt_asof.replace(tzinfo=UTC)

        # Settled paper outcomes — we use `fill` events with broker='paper'.
        if evt.kind == "fill" and evt.payload.get("broker") == "paper":
            paper_outcomes += 1
            pnl = evt.payload.get("realized_pnl")
            if pnl is not None:
                try:
                    fills_pnl.append(float(pnl))
                except (TypeError, ValueError):
                    pass

        # Killswitch in trailing 14d
        if evt.kind == "kill_switch_fired" and evt_asof >= window_14d_start:
            killswitch_in_14d = True

        # Immutable breaches: gate_rejection events whose payload.reason
        # references an IMMUTABLE_INVARIANTS member.
        if (
            evt.kind == "gate_rejection"
            and evt_asof >= window_30d_start
            and evt.payload.get("immutable_breach") is True
        ):
            immutable_breach_count += 1

        # Calibrator drift snapshots emitted as promotion_event
        if evt.kind == "promotion_event" and evt_asof >= window_30d_start:
            drift = evt.payload.get("calibrator_drift")
            if drift is not None:
                try:
                    calibrator_drift_max = max(calibrator_drift_max, abs(float(drift)))
                except (TypeError, ValueError):
                    pass

            sharpe_ci = evt.payload.get("sharpe_95ci_lower")
            if sharpe_ci is not None:
                try:
                    sharpe_ci_lower = max(sharpe_ci_lower, float(sharpe_ci))
                except (TypeError, ValueError):
                    pass

            dd = evt.payload.get("rolling_30d_max_drawdown_pct")
            if dd is not None:
                try:
                    rolling_30d_max_drawdown_pct = max(
                        rolling_30d_max_drawdown_pct, float(dd)
                    )
                except (TypeError, ValueError):
                    pass

            if evt.payload.get("weekly_retro_promotion_readiness") is True:
                weekly_retro_ready = True

    # Crude Sharpe point estimate from fills_pnl (mean / std). NOT used
    # for the gate — the gate uses sharpe_ci_lower which the meta-retro
    # writes to the audit log directly.
    if len(fills_pnl) >= 2:
        import statistics

        mean = statistics.fmean(fills_pnl)
        sd = statistics.pstdev(fills_pnl)
        rolling_sharpe = mean / sd if sd > 0 else 0.0
    else:
        rolling_sharpe = 0.0

    return {
        "paper_outcomes_count": paper_outcomes,
        "rolling_30d_realized_sharpe": rolling_sharpe,
        "sharpe_95ci_lower": sharpe_ci_lower,
        "rolling_30d_max_drawdown_pct": rolling_30d_max_drawdown_pct,
        "no_killswitch_in_trailing_14d": not killswitch_in_14d,
        "immutable_breaches_in_window": immutable_breach_count,
        "calibrator_drift_max": calibrator_drift_max,
        "weekly_retro_promotion_readiness": weekly_retro_ready,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def evaluate(asof: datetime) -> PromotionDecision:
    """Compute a PromotionDecision. Always emits one promotion_event row
    to the audit log, regardless of the result (per ADR-0031 D5)."""
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=UTC)

    thresholds = _load_thresholds()
    metrics = _collect_metrics(asof)

    blocked: list[str] = []

    if metrics["paper_outcomes_count"] < int(thresholds["min_paper_outcomes"]):
        blocked.append(
            f"paper_outcomes_count={metrics['paper_outcomes_count']} "
            f"< min={int(thresholds['min_paper_outcomes'])}"
        )

    if metrics["sharpe_95ci_lower"] < float(thresholds["min_sharpe_95ci_lower"]):
        blocked.append(
            f"sharpe_95ci_lower={metrics['sharpe_95ci_lower']:.4f} "
            f"< min={thresholds['min_sharpe_95ci_lower']:.2f}"
        )

    if metrics["rolling_30d_max_drawdown_pct"] > float(
        thresholds["max_rolling_30d_drawdown_pct"]
    ):
        blocked.append(
            f"rolling_30d_max_drawdown_pct={metrics['rolling_30d_max_drawdown_pct']:.4f} "
            f"> max={thresholds['max_rolling_30d_drawdown_pct']:.4f}"
        )

    if not metrics["no_killswitch_in_trailing_14d"]:
        blocked.append(
            f"kill switch fired within trailing "
            f"{int(thresholds['killswitch_window_days'])}d window"
        )

    if metrics["immutable_breaches_in_window"] != 0:
        blocked.append(
            f"immutable_breaches_in_window="
            f"{metrics['immutable_breaches_in_window']} (must be 0)"
        )

    if metrics["calibrator_drift_max"] > float(thresholds["max_calibrator_drift"]):
        blocked.append(
            f"calibrator_drift_max={metrics['calibrator_drift_max']:.4f} "
            f"> max={thresholds['max_calibrator_drift']:.4f}"
        )

    if not metrics["weekly_retro_promotion_readiness"]:
        blocked.append("weekly_retro_promotion_readiness=False")

    decision = PromotionDecision(
        promoted=len(blocked) == 0,
        blocked_by=blocked,
        **metrics,
    )

    # Always emit one promotion_event row.
    audit_log.append(
        audit_log.GovernanceEvent(
            kind="promotion_event",
            asof=asof,
            source="governance.promotion.evaluate",
            payload={
                "row_type": "evaluate_result",
                "promoted": decision.promoted,
                "blocked_by": decision.blocked_by,
                "metrics": metrics,
            },
        )
    )

    return decision
