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

OUT-OF-SAMPLE fold-rate (seed 3767, anti-overfit lane L3)
---------------------------------------------------------
Criteria 1-3 are IN-SAMPLE: they score one contiguous window. A strategy can
clear them by overfitting that window. ``check()`` therefore accepts an OPTIONAL
out-of-sample ``oos_fold_rate`` — the fraction of walk-forward OOS folds whose
excess-return beats buy-and-hold, computed by the (previously orphaned)
``hermes_quant.backtest.walk_forward_replay`` instrument
(``WalkForwardBacktestResult.positive_excess_fold_rate``). When supplied, the
candidate must clear ``oos_fold_rate_floor`` (default 0.60 — a clear majority of
folds reproduce) IN ADDITION to the in-sample checks.

The wiring is strictly ADDITIVE and FAIL-CLOSED:
  * additive — supplying a fold-rate can only ADD a reject reason; with
    ``require_oos=False`` (default) and no fold-rate supplied, the gate is
    byte-identical to the in-sample-only behavior.
  * fail-closed — ``require_oos=True`` with no fold-rate REJECTS (never falls back
    to in-sample-only), and a non-finite fold-rate REJECTS (an ``x < NaN``
    comparison never blocks, so we guard it explicitly rather than promote on a
    degenerate measurement).
  * stricter never looser — the OOS check can only reject MORE candidates.
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
# Conservative OOS default (seed 3767): a CLEAR MAJORITY of walk-forward folds
# must beat buy-and-hold. Gate-closed by construction — err toward rejecting a
# strategy that only reproduces in a minority of out-of-sample windows.
_DEFAULT_OOS_FOLD_RATE_FLOOR = 0.60


class PromotionGate:
    """Gate that decides whether a strategy result warrants production promotion.

    Args:
        alpha_threshold:        Minimum vs_buyhold_alpha required (default 0.0).
        sortino_threshold:      Minimum Sortino ratio required (default 0.5).
        max_drawdown_floor:     Maximum permitted drawdown (default -0.20, i.e.
                                a 20% drawdown is the worst tolerated).  Must be
                                ≤ 0.
        oos_fold_rate_floor:    Minimum fraction of walk-forward OOS folds whose
                                excess-return beats buy-and-hold, required when an
                                ``oos_fold_rate`` is supplied to (or required by)
                                ``check()`` (seed 3767). Default 0.60 — a clear
                                majority of folds must reproduce. Inclusive
                                (fold-rate ≥ floor passes).
        require_oos:            When True, ``check()`` REJECTS unless an
                                ``oos_fold_rate`` is supplied (fail-closed —
                                in-sample alpha alone is never sufficient). Default
                                False keeps the gate byte-identical to today when no
                                OOS evidence is passed.
    """

    def __init__(
        self,
        *,
        alpha_threshold: float = _DEFAULT_ALPHA_THRESHOLD,
        sortino_threshold: float = _DEFAULT_SORTINO_THRESHOLD,
        max_drawdown_floor: float = _DEFAULT_MAX_DRAWDOWN_FLOOR,
        oos_fold_rate_floor: float = _DEFAULT_OOS_FOLD_RATE_FLOOR,
        require_oos: bool = False,
    ) -> None:
        self.alpha_threshold = alpha_threshold
        self.sortino_threshold = sortino_threshold
        self.max_drawdown_floor = max_drawdown_floor
        self.oos_fold_rate_floor = oos_fold_rate_floor
        self.require_oos = require_oos

    def check(
        self,
        result: STOCKBENCHResult,
        *,
        oos_fold_rate: float | None = None,
    ) -> PromotionDecision:
        """Evaluate a STOCKBENCH result against the promotion criteria.

        Args:
            result: Output of :class:`STOCKBENCHHarness`.run().
            oos_fold_rate: Optional out-of-sample fold-rate (seed 3767) — the
                fraction of walk-forward folds whose excess-return beats
                buy-and-hold, from
                ``walk_forward_replay(...).positive_excess_fold_rate``. When
                supplied, the candidate must clear ``oos_fold_rate_floor`` IN
                ADDITION to the in-sample checks. A non-finite value REJECTS
                (fail-closed). When ``require_oos`` is True and this is None, the
                gate REJECTS rather than promote on in-sample evidence alone.

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

        # Criterion 4 (seed 3767) — OUT-OF-SAMPLE walk-forward fold-rate.
        # ADDITIVE + FAIL-CLOSED. Three cases:
        #   (a) require_oos and no evidence  -> REJECT (never in-sample-only).
        #   (b) evidence supplied            -> must be finite AND >= floor, else
        #       REJECT (a non-finite rate would slip past `< floor`, so guard it).
        #   (c) not required and no evidence -> skip (byte-identical to today).
        if oos_fold_rate is None:
            if self.require_oos:
                failures.append(
                    "oos_fold_rate missing — require_oos=True demands walk-forward "
                    "out-of-sample evidence; refusing to promote on in-sample alpha alone"
                )
        elif not math.isfinite(oos_fold_rate):
            failures.append(
                f"oos_fold_rate={oos_fold_rate} is not finite — a degenerate "
                "out-of-sample measurement cannot clear the fold-rate floor"
            )
        elif oos_fold_rate < self.oos_fold_rate_floor:
            failures.append(
                f"oos_fold_rate={oos_fold_rate:.4f} < "
                f"floor={self.oos_fold_rate_floor:.4f} "
                "(strategy reproduces in too few out-of-sample folds — overfit risk)"
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
