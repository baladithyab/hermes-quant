"""hermes_quant.governance.promotion — paper→live promotion evaluator
(ADR-0031 D5).

Read-only: gathers metrics from the audit log and produces a
`PromotionDecision`. Thresholds are NOT hardcoded here on purpose —
`hermes_quant.react.live` is the single source of truth for the
numerical bounds (ADR-0029 D7). It exports both `LiveTradingApproval`
(the Pydantic validator that ENFORCES the bounds at approval-construction
time) and `LIVE_APPROVAL_THRESHOLDS` (the dict this read-only evaluator
CONSUMES to pre-check those same bounds against audit-log metrics).

react.live is the live binding and a guaranteed-present core module. If
it cannot be imported, or has dropped a key this evaluator depends on, we
fail CLOSED and LOUD (raise) rather than promote on guessed numbers:
duplicating the authoritative thresholds here is exactly the failure mode
ADR-0031 D5 consolidates against, so no local fallback copy survives.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from hermes_quant.governance import audit_log

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Threshold binding
# ---------------------------------------------------------------------------

# The prefix-style keys this evaluator reads out of
# `react.live.LIVE_APPROVAL_THRESHOLDS`. react.live owns the *values*
# (ADR-0029 D7); this set is only the contract of *which keys* must be
# present for the gate to make a decision. If react.live ever drops one,
# `_load_thresholds()` raises rather than silently reading a default —
# `test_promotion_threshold_keys_match_react_live` pins this contract so a
# future key rename in react.live fails CI instead of failing open in prod.
_REQUIRED_THRESHOLD_KEYS: frozenset[str] = frozenset(
    {
        "min_paper_outcomes",
        "min_sharpe_95ci_lower",
        "max_rolling_30d_drawdown_pct",
        "max_calibrator_drift",
        "killswitch_window_days",
    }
)


def _load_thresholds() -> dict[str, float]:
    """Return the live promotion thresholds from `react.live` — the single
    source of truth (ADR-0029 D7 / ADR-0031 D5).

    Wire shape: react.live exports both `LiveTradingApproval` (the Pydantic
    model whose validator ENFORCES the bounds at approval-construction time)
    AND `LIVE_APPROVAL_THRESHOLDS` (a dict mirroring those bounds for
    cross-module consumption — this exact function). The dict is the
    integration handle; the validator is the enforcement.

    Fails CLOSED and LOUD. react.live is a guaranteed-present core module,
    so an import failure, a non-dict export, a missing required key, or a
    degenerate value (non-numeric / non-finite / non-positive) is a contract
    breach — not a routine condition to paper over with a local copy of the
    numbers. We raise so the gate never promotes on guessed or meaningless
    thresholds (a silent fallback that drifted from ADR-0029, or a poisoned
    bound, would fail OPEN — the one failure mode a promotion gate must never
    have).
    """
    try:
        from hermes_quant.react.live import LIVE_APPROVAL_THRESHOLDS
    except ImportError as exc:
        raise RuntimeError(
            "hermes_quant.react.live is the single source of truth for "
            "promotion thresholds (ADR-0029 D7) but could not be imported. "
            "Refusing to evaluate the paper→live gate on guessed numbers."
        ) from exc

    if not isinstance(LIVE_APPROVAL_THRESHOLDS, dict) or not LIVE_APPROVAL_THRESHOLDS:
        raise RuntimeError(
            "react.live.LIVE_APPROVAL_THRESHOLDS is not a non-empty dict; "
            "cannot evaluate the paper→live gate. Got: "
            f"{type(LIVE_APPROVAL_THRESHOLDS).__name__}."
        )

    missing = _REQUIRED_THRESHOLD_KEYS - LIVE_APPROVAL_THRESHOLDS.keys()
    if missing:
        raise RuntimeError(
            "react.live.LIVE_APPROVAL_THRESHOLDS is missing keys this "
            f"evaluator depends on: {sorted(missing)}. react.live must keep "
            "these prefix-style keys in sync with ADR-0029 D7 (see "
            "_REQUIRED_THRESHOLD_KEYS)."
        )

    # Structural value sanity — NOT policy magnitudes. A required key present
    # with a degenerate value (None, NaN, a string, 0, or negative) would NOT
    # crash: it would flow into evaluate()'s `metric < threshold` blocks and
    # quietly flip the gate OPEN (`x < 0` / `x < NaN` never blocks). We reject
    # any non-finite or non-positive bound so that "thresholds present" cannot
    # mean "thresholds meaningless". We deliberately do NOT re-assert the
    # ADR-0029 numbers (>=100, >=1.0, ...) here — that magnitude policy lives
    # in react.live alone (ADR-0031 D5); duplicating it is the failure mode
    # this module avoids. We only require each bound be a finite, positive
    # number so the comparison operators in evaluate() behave.
    for key in _REQUIRED_THRESHOLD_KEYS:
        value = LIVE_APPROVAL_THRESHOLDS[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError(
                f"react.live.LIVE_APPROVAL_THRESHOLDS[{key!r}] must be a "
                f"number, got {type(value).__name__}={value!r}. Refusing to "
                "evaluate the paper→live gate on a non-numeric bound."
            )
        if not math.isfinite(value) or value <= 0:
            raise RuntimeError(
                f"react.live.LIVE_APPROVAL_THRESHOLDS[{key!r}]={value!r} is "
                "not a finite positive number. A non-finite or non-positive "
                "bound would fail the gate OPEN (e.g. `x < NaN` never blocks); "
                "refusing to evaluate."
            )

    return dict(LIVE_APPROVAL_THRESHOLDS)


# ---------------------------------------------------------------------------
# Decision model
# ---------------------------------------------------------------------------


class PromotionDecision(BaseModel):
    """Result of `evaluate()`. `promoted=True` only when every promotion
    check passes — a SUPERSET of the LiveTradingApproval validator (ADR-0029
    D7): in addition to the validator's five fields, `evaluate()` also blocks
    on calibrator drift and weekly-retro readiness, so it is strictly at
    least as conservative as the validator, never looser."""

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
    # sharpe_95ci_lower gates on `<` (a FLOOR), so it must reflect the CURRENT
    # (latest in-window) snapshot, not the window's single best moment. Reducing
    # with max() is the PERMISSIVE direction — one momentarily-good snapshot would
    # admit promotion even after every later snapshot degraded below the floor (a
    # latent fail-OPEN). We therefore keep the LATEST in-window value, tracked by
    # the snapshot's asof. (drawdown / calibrator_drift gate on `>` so their max()
    # reducers below stay correctly conservative and unchanged.)
    sharpe_ci_latest_asof: datetime | None = None
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
                    val = float(sharpe_ci)
                except (TypeError, ValueError):
                    pass
                else:
                    # Keep the LATEST in-window snapshot (by asof), not the max —
                    # a FLOOR gate must see the current value, not the best historical.
                    if sharpe_ci_latest_asof is None or evt_asof >= sharpe_ci_latest_asof:
                        sharpe_ci_lower = val
                        sharpe_ci_latest_asof = evt_asof

            dd = evt.payload.get("rolling_30d_max_drawdown_pct")
            if dd is not None:
                try:
                    rolling_30d_max_drawdown_pct = max(rolling_30d_max_drawdown_pct, float(dd))
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

    if metrics["rolling_30d_max_drawdown_pct"] > float(thresholds["max_rolling_30d_drawdown_pct"]):
        blocked.append(
            f"rolling_30d_max_drawdown_pct={metrics['rolling_30d_max_drawdown_pct']:.4f} "
            f"> max={thresholds['max_rolling_30d_drawdown_pct']:.4f}"
        )

    if not metrics["no_killswitch_in_trailing_14d"]:
        blocked.append(
            f"kill switch fired within trailing {int(thresholds['killswitch_window_days'])}d window"
        )

    if metrics["immutable_breaches_in_window"] != 0:
        blocked.append(
            f"immutable_breaches_in_window={metrics['immutable_breaches_in_window']} (must be 0)"
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
