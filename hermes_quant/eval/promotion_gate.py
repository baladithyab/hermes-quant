"""hermes_quant.eval.promotion_gate — Decision-support gate for production promotion.

The PromotionGate is a DECISION SUPPORT TOOL, not an automatic promoter.
It returns a recommendation; the operator (or a higher-level orchestrator)
makes the final call.

Promotion criteria (per docs/roadmap Wave 6 acceptance):
    1. vs_buyhold_alpha > 0      — must beat buy-and-hold
    2. sortino > 0.5             — acceptable risk-adjusted return
    3. max_drawdown > -0.20      — no catastrophic drawdown (< 20%)

All three conditions must pass for promotion = True.  The gate enumerates
each failing criterion in the ``reasons`` list so operators know exactly what
needs to improve.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hermes_quant.eval.stockbench import STOCKBENCHResult


@dataclass(frozen=True)
class PromotionDecision:
    """Result of :class:`PromotionGate`.check().

    Attributes:
        promote:          True when ALL promotion criteria pass.
        reasons:          List of human-readable strings — one per criterion
                          that *failed* (empty when promote=True).
        suggested_action: High-level recommendation for the operator.
    """

    promote: bool
    reasons: list[str]
    suggested_action: str


# ---------------------------------------------------------------------------
# Configurable thresholds (exposed as class attributes so subclasses can
# override without re-implementing the logic)
# ---------------------------------------------------------------------------

_DEFAULT_ALPHA_THRESHOLD = 0.0       # must beat buy-and-hold
_DEFAULT_SORTINO_THRESHOLD = 0.5     # minimum acceptable Sortino ratio
_DEFAULT_MAX_DRAWDOWN_FLOOR = -0.20  # drawdown must be better than -20%


class PromotionGate:
    """Gate that decides whether a strategy result warrants production promotion.

    Args:
        alpha_threshold:        Minimum vs_buyhold_alpha required (default 0.0).
        sortino_threshold:      Minimum Sortino ratio required (default 0.5).
        max_drawdown_floor:     Maximum permitted drawdown (default -0.20, i.e.
                                a 20% drawdown is the worst tolerated).  Must be
                                ≤ 0.
    """

    def __init__(
        self,
        *,
        alpha_threshold: float = _DEFAULT_ALPHA_THRESHOLD,
        sortino_threshold: float = _DEFAULT_SORTINO_THRESHOLD,
        max_drawdown_floor: float = _DEFAULT_MAX_DRAWDOWN_FLOOR,
    ) -> None:
        self.alpha_threshold = alpha_threshold
        self.sortino_threshold = sortino_threshold
        self.max_drawdown_floor = max_drawdown_floor

    def check(self, result: STOCKBENCHResult) -> PromotionDecision:
        """Evaluate a STOCKBENCH result against the promotion criteria.

        Args:
            result: Output of :class:`STOCKBENCHHarness`.run().

        Returns:
            PromotionDecision with promote=True iff all criteria pass.
        """
        failures: list[str] = []

        # Criterion 1 — must beat buy-and-hold
        # Sortino/alpha = +inf is the BEST possible (no downside variance),
        # so we treat +inf as a pass; only NaN is rejected as malformed.
        import math
        if math.isnan(result.vs_buyhold_alpha) or result.vs_buyhold_alpha <= self.alpha_threshold:
            failures.append(
                f"vs_buyhold_alpha={result.vs_buyhold_alpha:.4f} <= "
                f"threshold={self.alpha_threshold:.4f} "
                "(strategy did not beat buy-and-hold)"
            )

        # Criterion 2 — acceptable Sortino ratio
        if math.isnan(result.sortino) or result.sortino <= self.sortino_threshold:
            failures.append(
                f"sortino={result.sortino:.4f} <= "
                f"threshold={self.sortino_threshold:.4f} "
                "(insufficient risk-adjusted return)"
            )

        # Criterion 3 — no catastrophic drawdown
        if math.isnan(result.max_drawdown) or result.max_drawdown <= self.max_drawdown_floor:
            failures.append(
                f"max_drawdown={result.max_drawdown:.4f} <= "
                f"floor={self.max_drawdown_floor:.4f} "
                "(drawdown exceeds tolerance)"
            )

        # Contamination guard — treat as a disqualifier
        if result.contamination_guard_fired:
            failures.append(
                "contamination_guard_fired=True — evaluation window may overlap "
                "LLM training data; result is not trustworthy for promotion"
            )

        promote = len(failures) == 0

        if promote:
            suggested_action = (
                "All promotion criteria passed.  Recommend paper-trading trial "
                "for at least one additional 6-month window before live deployment."
            )
        elif len(failures) == 1:
            suggested_action = (
                f"One criterion failed: {failures[0]!r}.  "
                "Tune the strategy and re-evaluate before considering promotion."
            )
        else:
            suggested_action = (
                f"{len(failures)} criteria failed.  "
                "Strategy requires significant improvement before promotion."
            )

        return PromotionDecision(
            promote=promote,
            reasons=failures,
            suggested_action=suggested_action,
        )
