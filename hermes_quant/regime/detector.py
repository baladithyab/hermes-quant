"""hermes_quant.regime.detector — Wave 7 deterministic rule-based regime classifier.

v0.1: Rule-based deterministic classifier (no HMM, no external libraries).
v0.2: Wire in Mantshimuli & Mwamba HMM classifier via hmm_classifier hook.

Classification rules (v0.1):
    BULL:     trend_strength >= +0.5  AND  realized_vol_percentile <= 0.6
    BEAR:     trend_strength <= -0.5  AND  realized_vol_percentile <= 0.7
    VOLATILE: realized_vol_percentile > 0.7
    UNKNOWN:  none of the above (including insufficient data)

Priority order: VOLATILE > BEAR > BULL > UNKNOWN.
(VOLATILE overrides trend; high-vol regimes dominate BMA adjustments.)

Reference: Mantshimuli & Mwamba, Springer 2026.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import Enum
from typing import Any

from hermes_quant.regime.state_variables import StateVariables

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# RegimeState enum
# ---------------------------------------------------------------------------


class RegimeState(str, Enum):
    """Market regime classification.

    Values are lowercase strings for easy JSON serialization and metadata
    round-tripping without an extra encoder.
    """

    BULL = "bull"
    BEAR = "bear"
    VOLATILE = "volatile"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Classification thresholds (v0.1 rule-based)
# ---------------------------------------------------------------------------

# BULL: trend_strength >= BULL_TREND_MIN AND vol_percentile <= BULL_VOL_MAX
BULL_TREND_MIN: float = 0.5
BULL_VOL_MAX: float = 0.6

# BEAR: trend_strength <= BEAR_TREND_MAX AND vol_percentile <= BEAR_VOL_MAX
BEAR_TREND_MAX: float = -0.5
BEAR_VOL_MAX: float = 0.7

# VOLATILE: vol_percentile > VOLATILE_VOL_MIN
VOLATILE_VOL_MIN: float = 0.7


# ---------------------------------------------------------------------------
# RegimeDetector
# ---------------------------------------------------------------------------


class RegimeDetector:
    """Classify a MarketContext into one of BULL / BEAR / VOLATILE / UNKNOWN.

    v0.1 is deterministic and rule-based.  The optional ``hmm_classifier``
    parameter is a v0.2 hook: when provided it is called with the StateVariables
    and its return value (expected to be a RegimeState) overrides the rule-based
    result.  This allows progressive substitution without a flag change.

    Args:
        hmm_classifier: Optional callable ``(StateVariables) -> RegimeState``.
            None (default) = v0.1 rule-based only.
    """

    def __init__(
        self,
        *,
        hmm_classifier: Callable[[StateVariables], RegimeState] | None = None,
    ) -> None:
        self.hmm_classifier = hmm_classifier

    def classify(self, state_vars: StateVariables) -> tuple[RegimeState, str]:
        """Classify the current market regime from state variables.

        Returns:
            (RegimeState, reason_string) — the reason is a short human-readable
            diagnostic suitable for inclusion in AggregatedSignal.metadata.

        Design invariants:
            - Never raises. Missing/None fields → UNKNOWN with a reason.
            - Deterministic: same inputs always produce the same output.
            - Priority: VOLATILE > BEAR > BULL > UNKNOWN (vol dominates).
        """
        # v0.2 HMM hook (plumbing only — not exercised in v0.1)
        if self.hmm_classifier is not None:
            try:
                hmm_result = self.hmm_classifier(state_vars)
                if isinstance(hmm_result, RegimeState):
                    return hmm_result, "hmm_classifier"
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "regime: hmm_classifier raised %s; falling back to rule-based", exc
                )

        # --- guard: realized_vol_percentile is always required ---
        vol_pct = state_vars.realized_vol_percentile
        if vol_pct is None:
            return RegimeState.UNKNOWN, "realized_vol_percentile is None"

        # --- VOLATILE: high volatility overrides trend signal ---
        if vol_pct > VOLATILE_VOL_MIN:
            reason = (
                f"realized_vol_percentile={vol_pct:.3f} > {VOLATILE_VOL_MIN} → VOLATILE"
            )
            logger.debug("regime: %s", reason)
            return RegimeState.VOLATILE, reason

        # --- trend_strength is needed for BULL / BEAR ---
        trend = state_vars.trend_strength
        if trend is None:
            # Can't distinguish BULL/BEAR without trend — but vol is not extreme.
            return (
                RegimeState.UNKNOWN,
                f"trend_strength is None; realized_vol_percentile={vol_pct:.3f}",
            )

        # --- BEAR ---
        if trend <= BEAR_TREND_MAX and vol_pct <= BEAR_VOL_MAX:
            reason = (
                f"trend_strength={trend:.3f} <= {BEAR_TREND_MAX} AND "
                f"realized_vol_percentile={vol_pct:.3f} <= {BEAR_VOL_MAX} → BEAR"
            )
            logger.debug("regime: %s", reason)
            return RegimeState.BEAR, reason

        # --- BULL ---
        if trend >= BULL_TREND_MIN and vol_pct <= BULL_VOL_MAX:
            reason = (
                f"trend_strength={trend:.3f} >= {BULL_TREND_MIN} AND "
                f"realized_vol_percentile={vol_pct:.3f} <= {BULL_VOL_MAX} → BULL"
            )
            logger.debug("regime: %s", reason)
            return RegimeState.BULL, reason

        # --- UNKNOWN: middle zone (moderate vol, moderate trend) ---
        reason = (
            f"trend_strength={trend:.3f}, "
            f"realized_vol_percentile={vol_pct:.3f}: no regime rule matched → UNKNOWN"
        )
        logger.debug("regime: %s", reason)
        return RegimeState.UNKNOWN, reason

    def status(self) -> dict[str, Any]:
        """Diagnostic snapshot for health checks and audit logs."""
        return {
            "version": "0.1",
            "classifier": "rule_based",
            "hmm_classifier_wired": self.hmm_classifier is not None,
            "thresholds": {
                "bull_trend_min": BULL_TREND_MIN,
                "bull_vol_max": BULL_VOL_MAX,
                "bear_trend_max": BEAR_TREND_MAX,
                "bear_vol_max": BEAR_VOL_MAX,
                "volatile_vol_min": VOLATILE_VOL_MIN,
            },
        }
