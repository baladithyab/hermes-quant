"""hermes_quant.factors.ic_metrics — IC / ICIR / factor-correlation helpers.

Three public functions:

    compute_ic(predictions, realizations) -> float
        Spearman rank information coefficient between predicted ranks and
        realised returns/ranks. Uses scipy.stats.spearmanr when available,
        falls back to a manual rank-correlation implementation so the module
        works in environments without scipy installed.

    compute_icir(predictions_history, realizations_history) -> float
        Information Coefficient Information Ratio: mean(IC) / std(IC) computed
        over a rolling or full history. Returns nan when fewer than 2
        observations are present (std is undefined).

    factor_correlation(returns_a, returns_b) -> float
        Pearson correlation on raw returns — used by ICDedupGate to measure
        similarity between two factor return series.

References:
    C5 — Factor/Signal Deduplication via IC Correlation Gating
    (Research SOTA scan, May 2026; R&D-Agent NeurIPS 2025 §IC-gating)
"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np

# ---------------------------------------------------------------------------
# Scipy optional import — graceful fallback
# ---------------------------------------------------------------------------
try:
    from scipy.stats import spearmanr as _scipy_spearmanr  # type: ignore[import-untyped]

    _HAS_SCIPY = True
except ImportError:  # pragma: no cover
    _HAS_SCIPY = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rank(arr: np.ndarray) -> np.ndarray:
    """Average-rank tie handling (manual implementation, no scipy dependency)."""
    n = len(arr)
    order = np.argsort(arr, kind="stable")
    ranks = np.empty(n, dtype=float)
    ranks[order] = np.arange(1, n + 1, dtype=float)
    # Tie handling: find ties and replace with average rank
    i = 0
    while i < n:
        j = i
        while j < n - 1 and arr[order[j]] == arr[order[j + 1]]:
            j += 1
        if j > i:
            avg = (ranks[order[i]] + ranks[order[j]]) / 2.0
            for k in range(i, j + 1):
                ranks[order[k]] = avg
        i = j + 1
    return ranks


def _spearman_manual(a: np.ndarray, b: np.ndarray) -> float:
    """Spearman correlation via rank transformation, no scipy required."""
    ra = _rank(a)
    rb = _rank(b)
    n = len(ra)
    if n < 2:
        return float("nan")
    ra_c = ra - ra.mean()
    rb_c = rb - rb.mean()
    denom = math.sqrt((ra_c**2).sum() * (rb_c**2).sum())
    if denom < 1e-12:
        return float("nan")
    return float((ra_c * rb_c).sum() / denom)


def _drop_nan_pairs(
    a: np.ndarray, b: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Return copies of a, b with rows where either is NaN removed."""
    mask = np.isfinite(a) & np.isfinite(b)
    return a[mask], b[mask]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_ic(
    predictions: Sequence[float] | np.ndarray,
    realizations: Sequence[float] | np.ndarray,
) -> float:
    """Compute Spearman rank Information Coefficient.

    Args:
        predictions:  Predicted values / scores (e.g. analyst view magnitude).
        realizations: Realised forward returns / outcomes.

    Returns:
        Spearman IC in [-1, +1] or nan if insufficient data.
    """
    p = np.asarray(predictions, dtype=float)
    r = np.asarray(realizations, dtype=float)
    p, r = _drop_nan_pairs(p, r)
    if len(p) < 2:
        return float("nan")

    if _HAS_SCIPY:
        result = _scipy_spearmanr(p, r)
        # scipy ≥ 1.9 returns a SpearmanrResult; older returns a tuple
        corr = result.statistic if hasattr(result, "statistic") else result[0]
        return float(corr) if math.isfinite(corr) else float("nan")

    return _spearman_manual(p, r)


def compute_icir(
    predictions_history: Sequence[Sequence[float]] | np.ndarray,
    realizations_history: Sequence[Sequence[float]] | np.ndarray,
) -> float:
    """Compute Information Coefficient Information Ratio (ICIR).

    ICIR = mean(IC_t) / std(IC_t)

    Each element of predictions_history / realizations_history is the
    cross-sectional slice at time t.

    Args:
        predictions_history:  List-of-lists / 2-D array; rows are time steps.
        realizations_history: Same shape as predictions_history.

    Returns:
        ICIR (float) or nan when fewer than 2 non-nan IC values present.
    """
    p_hist = [np.asarray(p, dtype=float) for p in predictions_history]
    r_hist = [np.asarray(r, dtype=float) for r in realizations_history]
    ics = [compute_ic(p, r) for p, r in zip(p_hist, r_hist)]
    valid = np.array([x for x in ics if math.isfinite(x)], dtype=float)
    if len(valid) < 2:
        return float("nan")
    std = float(np.std(valid, ddof=1))
    if std < 1e-12:
        return float("nan")
    return float(np.mean(valid) / std)


def factor_correlation(
    returns_a: Sequence[float] | np.ndarray,
    returns_b: Sequence[float] | np.ndarray,
) -> float:
    """Pearson correlation between two factor return series.

    Used by ICDedupGate to measure how redundant a new factor is relative
    to each member of the existing library.

    Args:
        returns_a: Factor A return series.
        returns_b: Factor B return series.

    Returns:
        Pearson correlation in [-1, +1] or nan if insufficient data.
    """
    a = np.asarray(returns_a, dtype=float)
    b = np.asarray(returns_b, dtype=float)
    a, b = _drop_nan_pairs(a, b)
    if len(a) < 2:
        return float("nan")
    a_c = a - a.mean()
    b_c = b - b.mean()
    denom = math.sqrt((a_c**2).sum() * (b_c**2).sum())
    if denom < 1e-12:
        # One series is constant — correlation is undefined
        return float("nan")
    return float((a_c * b_c).sum() / denom)
