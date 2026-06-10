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

v0.2 HMM feature flag:
    Set env var HERMES_QUANT_REGIME_HMM=1 to opt-in to HMM classification.
    The HMM is off by default; the rule-based v0.1 remains the default path.
    On HMM failure the detector silently falls back to rule-based (ADR-0031).

Reference: Mantshimuli & Mwamba, Springer 2026.
"""

from __future__ import annotations

import logging
import os
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
    # Wave 7.1 (ADR-0053 amendment): weak-lean + neutral zones fill the former
    # 0.60–0.70 vol dead zone. These are real states, not no-ops:
    #   BULL_WEAK / BEAR_WEAK — moderate trend in the mid/elevated-vol band;
    #     gentle conviction (multipliers halfway between identity and full BULL/BEAR).
    #   NEUTRAL — genuinely flat trend at moderate vol; an honest "no edge" state
    #     (identity weights, like the old UNKNOWN no-op) but named so the brief
    #     reports "flat" rather than implying the classifier is broken.
    BULL_WEAK = "bull_weak"
    BEAR_WEAK = "bear_weak"
    NEUTRAL = "neutral"
    # UNKNOWN is now reserved STRICTLY for insufficient/missing data (vol_pct or
    # trend is None). Silence-by-default is preserved: NEUTRAL and UNKNOWN both
    # carry identity weights, so widening the taxonomy never adds conviction the
    # old code wouldn't have had.
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Classification thresholds (v0.1 rule-based)
# ---------------------------------------------------------------------------

# BULL: trend_strength >= BULL_TREND_MIN AND vol_percentile <= BULL_VOL_MAX
BULL_TREND_MIN: float = 0.5
# Wave 7.1: widened 0.60 -> 0.70 to be symmetric with BEAR_VOL_MAX. The old
# 0.60 ceiling left a 0.60–0.70 hole where strong uptrends fell to UNKNOWN.
BULL_VOL_MAX: float = 0.7

# BEAR: trend_strength <= BEAR_TREND_MAX AND vol_percentile <= BEAR_VOL_MAX
BEAR_TREND_MAX: float = -0.5
BEAR_VOL_MAX: float = 0.7

# VOLATILE: vol_percentile > VOLATILE_VOL_MIN
VOLATILE_VOL_MIN: float = 0.7

# Wave 7.1 weak-lean band: a moderate (sub-strong) trend at non-volatile vol.
# |trend| in [WEAK_TREND_MIN, BULL_TREND_MIN) leans BULL_WEAK / BEAR_WEAK.
# Below WEAK_TREND_MIN the market is genuinely flat → NEUTRAL.
WEAK_TREND_MIN: float = 0.15


# ---------------------------------------------------------------------------
# RegimeDetector
# ---------------------------------------------------------------------------


class RegimeDetector:
    """Classify a MarketContext into one of BULL / BEAR / VOLATILE / UNKNOWN.

    v0.1 is deterministic and rule-based.  The optional ``hmm_classifier``
    parameter is a v0.2 hook: when provided it is called with the StateVariables
    and its return value (expected to be a RegimeState) overrides the rule-based
    result.  This allows progressive substitution without a flag change.

    v0.2 env-var feature flag:
        When the environment variable ``HERMES_QUANT_REGIME_HMM=1`` is set AND
        no explicit ``hmm_classifier`` is supplied, the constructor instantiates
        an ``HMMClassifier`` and assigns it to ``hmm_classifier``.  This keeps
        the default behaviour bit-identical to v0.1 (rule-based) for all existing
        callers while making the HMM trivially opt-in.

    Args:
        hmm_classifier: Optional callable ``(StateVariables) -> RegimeState``.
            None (default) = auto-detect from env var; if env var is not set,
            falls back to v0.1 rule-based only.
    """

    def __init__(
        self,
        *,
        hmm_classifier: Callable[[StateVariables], RegimeState] | None = None,
    ) -> None:
        # v0.2: auto-wire HMMClassifier when env var is set and no explicit callable given
        if hmm_classifier is None and os.environ.get("HERMES_QUANT_REGIME_HMM") == "1":
            try:
                from hermes_quant.regime.hmm import HMMClassifier  # lazy import
                _hmm = HMMClassifier()
                hmm_classifier = _hmm.classify  # type: ignore[assignment]
                logger.info(
                    "regime: HERMES_QUANT_REGIME_HMM=1 — HMMClassifier wired (v0.2)"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "regime: HERMES_QUANT_REGIME_HMM=1 but HMMClassifier failed to "
                    "initialise (%s); falling back to rule-based", exc
                )
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
                # HMMClassifier.classify returns tuple[RegimeState, str]; handle both forms
                if isinstance(hmm_result, tuple) and len(hmm_result) == 2:
                    regime, reason = hmm_result
                    if isinstance(regime, RegimeState):
                        return regime, reason
                elif isinstance(hmm_result, RegimeState):
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

        # --- Wave 7.1 weak-lean zones (fill the former dead zone) ---
        # We are here iff vol_pct <= 0.70 and neither strong-BULL nor strong-BEAR
        # fired (i.e. |trend| < 0.5). Lean by trend sign with gentle conviction;
        # a genuinely flat trend is NEUTRAL (honest "no edge", identity weights).
        if trend >= WEAK_TREND_MIN:
            reason = (
                f"trend_strength={trend:.3f} in [{WEAK_TREND_MIN}, {BULL_TREND_MIN}) AND "
                f"realized_vol_percentile={vol_pct:.3f} <= {BULL_VOL_MAX} → BULL_WEAK"
            )
            logger.debug("regime: %s", reason)
            return RegimeState.BULL_WEAK, reason

        if trend <= -WEAK_TREND_MIN:
            reason = (
                f"trend_strength={trend:.3f} in ({BEAR_TREND_MAX}, -{WEAK_TREND_MIN}] AND "
                f"realized_vol_percentile={vol_pct:.3f} <= {BEAR_VOL_MAX} → BEAR_WEAK"
            )
            logger.debug("regime: %s", reason)
            return RegimeState.BEAR_WEAK, reason

        # --- NEUTRAL: genuinely flat trend at moderate vol (no edge, no-op) ---
        reason = (
            f"trend_strength={trend:.3f} within ±{WEAK_TREND_MIN}, "
            f"realized_vol_percentile={vol_pct:.3f}: flat → NEUTRAL"
        )
        logger.debug("regime: %s", reason)
        return RegimeState.NEUTRAL, reason

    def status(self) -> dict[str, Any]:
        """Diagnostic snapshot for health checks and audit logs."""
        hmm_wired = self.hmm_classifier is not None
        return {
            "version": "0.2" if hmm_wired else "0.1",
            "classifier": "hmm" if hmm_wired else "rule_based",
            "hmm_classifier_wired": hmm_wired,
            "thresholds": {
                "bull_trend_min": BULL_TREND_MIN,
                "bull_vol_max": BULL_VOL_MAX,
                "bear_trend_max": BEAR_TREND_MAX,
                "bear_vol_max": BEAR_VOL_MAX,
                "volatile_vol_min": VOLATILE_VOL_MIN,
            },
        }
