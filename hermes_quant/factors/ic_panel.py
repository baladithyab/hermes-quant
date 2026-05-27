"""hermes_quant.factors.ic_panel — Walk-forward IC panel computation.

Provides the :class:`ICPanel` dataclass (per-factor metric bundle) and
:func:`compute_ic_panel` which runs a rolling walk-forward window over
factor values and forward returns to produce aggregated IC statistics.

These metrics feed directly into :class:`~hermes_quant.factors.factor_oracle.FactorOracle`
for production-readiness scoring.

Design notes
~~~~~~~~~~~~
- Walk-forward windows: 60-day rolling window stepped every 5 business days.
  For a 252-day bar input that yields ~39 windows (each shifted 5 days).
- Per-window IC: scipy.stats.spearmanr(factor_window, fwd_return_window)[0].
- ICIR = ic_mean / max(ic_std, 1e-9) — avoids divide-by-zero on
  perfectly-stable IC streams.
- Turnover: average absolute daily change in fractional rank of the factor.
  Rank normalised to [0, 1] so the metric is scale-free.
- Alignment: factor_series and fwd_returns are inner-joined on index before
  any windowing — dates present in one but not the other are silently dropped.

References
~~~~~~~~~~
    AlphaBench (CityU, 2024) — Factor Forecasting Oracle (FFO) design.
    R&D-Agent (NeurIPS 2025, arXiv:2505.15155) — §IC-gating, walk-forward eval.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

try:
    from scipy.stats import spearmanr as _scipy_spearmanr  # type: ignore[import-untyped]

    _HAS_SCIPY = True
except ImportError:  # pragma: no cover
    _HAS_SCIPY = False


# ---------------------------------------------------------------------------
# ICPanel dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ICPanel:
    """Aggregated IC statistics for a single factor over a walk-forward window.

    Attributes:
        factor_id:        Unique identifier of the factor (from AlphaZoo).
        ic_mean:          Mean Spearman rank IC over all walk-forward periods.
        ic_std:           Standard deviation of per-period IC values.
        icir:             IC Information Ratio: ic_mean / max(ic_std, 1e-9).
        hit_rate:         Fraction of walk-forward periods with positive IC.
        turnover:         Average absolute daily change in fractional rank
                          (0 = perfectly stable rank, 1 = completely reshuffled).
        n_periods:        Number of walk-forward windows evaluated.
        fwd_horizon_days: Forward-return horizon used (e.g. 5 = next-week).
    """

    factor_id: str
    ic_mean: float
    ic_std: float
    icir: float
    hit_rate: float
    turnover: float
    n_periods: int
    fwd_horizon_days: int

    def to_dict(self) -> dict:
        """Serialise to a plain dict (JSON-safe values)."""
        return {
            "factor_id": self.factor_id,
            "ic_mean": self.ic_mean,
            "ic_std": self.ic_std,
            "icir": self.icir,
            "hit_rate": self.hit_rate,
            "turnover": self.turnover,
            "n_periods": self.n_periods,
            "fwd_horizon_days": self.fwd_horizon_days,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ICPanel":
        """Deserialise from a plain dict."""
        return cls(
            factor_id=d["factor_id"],
            ic_mean=float(d["ic_mean"]),
            ic_std=float(d["ic_std"]),
            icir=float(d["icir"]),
            hit_rate=float(d["hit_rate"]),
            turnover=float(d["turnover"]),
            n_periods=int(d["n_periods"]),
            fwd_horizon_days=int(d["fwd_horizon_days"]),
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _spearman_ic(a: np.ndarray, b: np.ndarray) -> float:
    """Spearman rank IC between arrays a and b (pairwise NaN drop)."""
    mask = np.isfinite(a) & np.isfinite(b)
    a_clean, b_clean = a[mask], b[mask]
    if len(a_clean) < 3:  # need at least 3 points for a meaningful correlation
        return float("nan")
    if _HAS_SCIPY:
        result = _scipy_spearmanr(a_clean, b_clean)
        corr = result.statistic if hasattr(result, "statistic") else result[0]
        return float(corr) if math.isfinite(corr) else float("nan")
    # Manual Spearman via rank transformation
    ra = _rank_array(a_clean)
    rb = _rank_array(b_clean)
    ra_c = ra - ra.mean()
    rb_c = rb - rb.mean()
    denom = math.sqrt((ra_c**2).sum() * (rb_c**2).sum())
    if denom < 1e-12:
        return float("nan")
    return float((ra_c * rb_c).sum() / denom)


def _rank_array(arr: np.ndarray) -> np.ndarray:
    """Average-rank tie-handling without scipy."""
    n = len(arr)
    order = np.argsort(arr, kind="stable")
    ranks = np.empty(n, dtype=float)
    ranks[order] = np.arange(1, n + 1, dtype=float)
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


def _fractional_ranks(series: pd.Series) -> pd.Series:
    """Return fractional ranks in [0, 1] for a pd.Series (NaN propagated)."""
    return series.rank(pct=True)


def _compute_turnover(factor_series: pd.Series) -> float:
    """Average absolute daily change in fractional rank (scale-free 0–1)."""
    frac_ranks = _fractional_ranks(factor_series.dropna())
    if len(frac_ranks) < 2:
        return float("nan")
    diffs = frac_ranks.diff().dropna().abs()
    if diffs.empty:
        return 0.0
    return float(diffs.mean())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_ic_panel(
    factor_series: pd.Series,
    fwd_returns: pd.Series,
    *,
    factor_id: str = "",
    window: int = 60,
    fwd_horizon_days: int = 5,
    step: int = 5,
) -> ICPanel:
    """Compute a walk-forward IC panel for a single factor.

    Parameters
    ----------
    factor_series:
        Factor values indexed by date (e.g. output of ``AlphaZoo.compute()``).
    fwd_returns:
        Forward return series indexed by date.  Typically constructed as::

            fwd_returns = bars["close"].pct_change(fwd_horizon_days).shift(-fwd_horizon_days)

        which assigns the *next* ``fwd_horizon_days`` return to the bar where
        the factor signal is observed.
    factor_id:
        Identifier placed into the returned :class:`ICPanel`.  Defaults to
        the ``name`` attribute of *factor_series* if set, else empty string.
    window:
        Number of observations per walk-forward window.  Default 60.
    fwd_horizon_days:
        Metadata field — the forward horizon already baked into *fwd_returns*.
        Only stored; this function does not re-shift.  Default 5.
    step:
        Stride between successive window start indices (business-day stepping).
        Default 5 (weekly).

    Returns
    -------
    ICPanel
        Aggregated IC statistics over all valid windows.

    Raises
    ------
    ValueError
        If fewer than ``window`` aligned observations are available after
        inner-joining factor_series and fwd_returns.
    """
    if not factor_id:
        factor_id = getattr(factor_series, "name", "") or ""

    # ---- Alignment: inner join on index ----
    aligned = pd.concat(
        [factor_series.rename("factor"), fwd_returns.rename("fwd")],
        axis=1,
        join="inner",
    ).dropna()

    n_obs = len(aligned)
    if n_obs < window:
        raise ValueError(
            f"compute_ic_panel: only {n_obs} aligned observations; "
            f"need at least window={window}."
        )

    factor_vals = aligned["factor"].values
    fwd_vals = aligned["fwd"].values

    # ---- Walk-forward windows ----
    ics: list[float] = []
    starts = list(range(0, n_obs - window + 1, step))

    for start in starts:
        end = start + window
        ic = _spearman_ic(factor_vals[start:end], fwd_vals[start:end])
        if math.isfinite(ic):
            ics.append(ic)

    n_periods = len(ics)

    if n_periods == 0:
        # All windows produced NaN IC — degenerate factor
        return ICPanel(
            factor_id=factor_id,
            ic_mean=float("nan"),
            ic_std=float("nan"),
            icir=float("nan"),
            hit_rate=float("nan"),
            turnover=_compute_turnover(aligned["factor"]),
            n_periods=0,
            fwd_horizon_days=fwd_horizon_days,
        )

    ic_arr = np.array(ics, dtype=float)
    ic_mean = float(np.mean(ic_arr))
    ic_std = float(np.std(ic_arr, ddof=1)) if n_periods > 1 else 0.0
    icir = ic_mean / max(ic_std, 1e-9)
    hit_rate = float(np.mean(ic_arr > 0.0))
    turnover = _compute_turnover(aligned["factor"])

    return ICPanel(
        factor_id=factor_id,
        ic_mean=ic_mean,
        ic_std=ic_std,
        icir=icir,
        hit_rate=hit_rate,
        turnover=turnover,
        n_periods=n_periods,
        fwd_horizon_days=fwd_horizon_days,
    )
