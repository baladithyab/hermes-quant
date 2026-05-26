"""hermes_quant.evaluation.lookahead — CI gate against lookahead bias.

Per ADR-0019 §D3. The lookahead-detection invariant from ADR-0006:
analysts that perform better than chance on SHUFFLED timestamps have
look-ahead bias. The CI gate at `tests/test_no_lookahead.py` runs this
test against every shipped analyst and aggregator on every commit.

Promoted from inline test scaffolding in v0.1.2 → reusable module in v0.3.

The "polluted but sliced" pattern:
1. Analyst sees the FULL bars DataFrame (including future)
2. We compare its score on REAL timestamps vs SHUFFLED timestamps
3. If shuffling barely changes the score, the analyst has no edge
4. If shuffling DRAMATICALLY hurts the score, the analyst has a real
   signal — but we want this difference to be statistically significant
   per the alpha threshold

The module does NOT prove the analyst is correct; it only fails analysts
that exhibit lookahead. Other forms of overfitting are caught by
walk-forward CV (see cv.py) and DSR (see dsr.py).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class LookaheadTestResult:
    """Result of shuffle_timestamps_test."""

    p_value: float
    """Probability that the observed real-vs-shuffled difference is due
    to chance (null: shuffled performance == real)."""

    real_score: float
    """The analyst's score on real timestamps. Higher = better."""

    shuffled_scores: list[float]
    """Distribution of scores under the null (shuffled timestamps).
    Length = n_shuffles."""

    n_shuffles: int
    """How many shuffles were tried."""

    alpha: float
    """The significance threshold (default 0.05). p_value > alpha = pass."""

    @property
    def passed(self) -> bool:
        """True if the analyst passes the lookahead test (no leakage detected
        OR detected leakage is statistically insignificant)."""
        return self.p_value > self.alpha

    @property
    def shuffled_mean(self) -> float:
        return float(np.mean(self.shuffled_scores)) if self.shuffled_scores else 0.0

    @property
    def shuffled_std(self) -> float:
        return float(np.std(self.shuffled_scores)) if self.shuffled_scores else 0.0

    def __str__(self) -> str:
        return (
            f"LookaheadTestResult(p_value={self.p_value:.4f}, "
            f"real={self.real_score:.4f}, "
            f"shuffled_mean={self.shuffled_mean:.4f}±{self.shuffled_std:.4f}, "
            f"passed={self.passed})"
        )


def shuffle_timestamps_test(
    score_fn,
    bars: pd.DataFrame,
    *,
    n_shuffles: int = 10,
    alpha: float = 0.05,
    seed: int | None = 42,
) -> LookaheadTestResult:
    """Test whether an analyst/aggregator's score depends on real timestamps.

    The test:
    1. Compute `real_score = score_fn(bars)` (with original timestamps)
    2. For each of n_shuffles iterations:
        a. Shuffle the timestamp column (other columns unchanged)
        b. Compute `score_fn(shuffled_bars)`
    3. Compute p-value as fraction of shuffled scores >= real_score
    4. Pass if p_value > alpha (the analyst's signal IS statistically
       distinguishable from shuffled, which means it relies on the
       temporal structure — i.e., does NOT exhibit lookahead)

    INVERSION CHECK: This is the OPPOSITE convention from naïve
    "no-information-loss" tests. We want the analyst to PERFORM BETTER
    on real timestamps than on shuffled. If the analyst performs equally
    well shuffled, it's looking at FUTURE bars (lookahead) — because
    shuffling preserves access to future information for any single bar.

    Args:
        score_fn: Callable that takes a bars DataFrame and returns a
            scalar score (higher = better). Typically: directional
            accuracy, hit rate, or Sharpe ratio.
        bars: OHLCV DataFrame with 'timestamp' column.
        n_shuffles: Number of shuffles. Default 10. More = tighter
            p-value estimate; cost is linear.
        alpha: Significance threshold. Default 0.05.
        seed: Random seed for reproducibility. Default 42.

    Returns:
        LookaheadTestResult with p_value, real_score, and the shuffled
        distribution.
    """
    if "timestamp" not in bars.columns:
        raise ValueError("bars must have a 'timestamp' column for shuffle_timestamps_test")

    rng = np.random.default_rng(seed)

    real_score = float(score_fn(bars))

    shuffled_scores: list[float] = []
    for _ in range(n_shuffles):
        shuffled = bars.copy()
        # Shuffle ONLY the timestamp column; OHLCV stays in original order
        # so the analyst still sees real (unshuffled) features but with
        # randomized timestamps. If the analyst peeks at future bars via
        # timestamp ordering, this should DEGRADE its score.
        permuted_indices = rng.permutation(len(shuffled))
        shuffled["timestamp"] = shuffled["timestamp"].values[permuted_indices]
        shuffled = shuffled.sort_values("timestamp").reset_index(drop=True)
        score = float(score_fn(shuffled))
        shuffled_scores.append(score)

    # P-value: fraction of shuffles where shuffled_score >= real_score.
    # If many shuffled scores match or exceed real, the analyst is robust
    # to timestamp noise (suggesting it doesn't depend on temporal
    # ordering — could be lookahead OR noise-blind).
    n_at_or_above = sum(1 for s in shuffled_scores if s >= real_score)
    p_value = (n_at_or_above + 1) / (n_shuffles + 1)  # Laplace smoothing

    return LookaheadTestResult(
        p_value=p_value,
        real_score=real_score,
        shuffled_scores=shuffled_scores,
        n_shuffles=n_shuffles,
        alpha=alpha,
    )
