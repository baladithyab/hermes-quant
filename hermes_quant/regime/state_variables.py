"""hermes_quant.regime.state_variables — Wave 7 market state variable computation.

StateVariables captures the quantitative inputs that feed the regime detector:
  - realized_vol_60d: 60-day realized annualized volatility (stdev of log-returns × √252)
  - realized_vol_percentile: empirical CDF of current vol vs trailing lookback window
  - yield_curve_slope: 10y minus 2y treasury spread (optional — None when unavailable)
  - trend_strength: (close - 50d_MA) / 50d_stdev (optional — None when insufficient bars)
  - as_of: timestamp of the computation
  - metadata: dict for extra diagnostic flags (yield_curve_unavailable, warnings, etc.)

compute_state_variables(bars, lookback_days=252) is the main entry point.
bars must be a pd.DataFrame with at least a 'close' column (and optionally a
'timestamp' column for as_of extraction).

Reference: Mantshimuli & Mwamba, Springer 2026.
"""

from __future__ import annotations

import json
import logging
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Default cache path for the yield-curve daily series (set by the data layer).
_DEFAULT_YIELD_CACHE_PATH = (
    Path.home() / ".hermes" / "quant" / "cache" / "yield-curve-cache.json"
)

# Minimum bars required to compute realized vol (60-day window).
_MIN_BARS_FOR_VOL = 61  # need 61 closes for 60 log-returns

# Window for realized-vol percentile ranking.
_DEFAULT_LOOKBACK_DAYS = 252

# Trend-strength window (50-day MA and 50-day stdev).
_TREND_MA_WINDOW = 50


@dataclass
class StateVariables:
    """Quantitative regime-conditioning inputs for the BMA detector.

    All float fields are annualized / normalized to dimensionless [0, 1] where
    applicable.  Optional fields are None when data is insufficient.

    Attributes:
        realized_vol_60d: Annualized realized volatility over the trailing 60
            trading days.  ``stdev(log_returns) * sqrt(252)``.
        realized_vol_percentile: Empirical CDF rank of realized_vol_60d vs the
            trailing ``lookback_days`` window.  Scalar in [0, 1].
        yield_curve_slope: 10y minus 2y US treasury spread in percentage points.
            None when the cache file is absent or the asset is non-equity.
        trend_strength: (close - 50d_MA) / 50d_stdev.  None when fewer than 50
            bars are available.
        as_of: Timestamp at which these state variables were computed (UTC).
        metadata: Diagnostic extras, e.g. ``{'yield_curve_unavailable': True}``.
    """

    realized_vol_60d: float
    realized_vol_percentile: float  # [0, 1]
    yield_curve_slope: float | None
    trend_strength: float | None
    as_of: pd.Timestamp
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _compute_realized_vol(close: pd.Series) -> float:
    """Annualized realized vol from the trailing 60 closes (60 log-returns)."""
    close_arr = np.asarray(close, dtype=float)
    # log-returns: log(p[t] / p[t-1])
    log_returns = np.log(close_arr[1:] / close_arr[:-1])
    # Use the last 60 returns
    window = log_returns[-60:]
    return float(np.std(window, ddof=1) * np.sqrt(252))


def _compute_vol_percentile(close: pd.Series, lookback_days: int) -> tuple[float, float]:
    """Return (realized_vol_60d, percentile_in_0_1).

    We compute vol at each point in the lookback window using a rolling 60-bar
    window, then rank today's vol against that distribution.
    """
    close_arr = np.asarray(close, dtype=float)
    # Drop any NaN/Inf prices before computing log-returns
    close_arr = close_arr[np.isfinite(close_arr) & (close_arr > 0)]

    if len(close_arr) < 2:
        return 0.0, 0.5

    log_returns = np.log(close_arr[1:] / close_arr[:-1])

    # Current vol: trailing 60 returns
    if len(log_returns) < 60:
        current_vol = float(np.std(log_returns, ddof=1) * np.sqrt(252))
        return current_vol, 0.5  # insufficient data — midpoint fallback

    current_vol = float(np.std(log_returns[-60:], ddof=1) * np.sqrt(252))

    # Rolling 60-day vol across the lookback window
    rolling_vols: list[float] = []
    for i in range(60, len(log_returns) + 1):
        window = log_returns[max(0, i - 60) : i]
        if len(window) >= 30:  # tolerate small tail
            v = float(np.std(window, ddof=1) * np.sqrt(252))
            if np.isfinite(v):
                rolling_vols.append(v)

    if not rolling_vols:
        return current_vol, 0.5

    # Empirical CDF: fraction of historical vols strictly below current_vol
    arr = np.array(rolling_vols)
    percentile = float(np.mean(arr < current_vol))
    return current_vol, float(np.clip(percentile, 0.0, 1.0))


def _compute_trend_strength(close: pd.Series) -> float | None:
    """(close[-1] - 50d_MA) / 50d_stdev.  None if fewer than 50 bars."""
    if len(close) < _TREND_MA_WINDOW:
        return None
    window = close.values[-_TREND_MA_WINDOW:]
    ma = float(np.mean(window))
    std = float(np.std(window, ddof=1))
    if std < 1e-12:
        return 0.0
    return float((close.values[-1] - ma) / std)


def _load_yield_curve_slope(
    path: Path = _DEFAULT_YIELD_CACHE_PATH,
    as_of: pd.Timestamp | None = None,
) -> float | None:
    """Load yield-curve slope from the JSON cache file.

    Cache format (expected):
        {"dates": ["2026-01-02", ...], "slope_10y_2y": [0.45, ...]}

    Returns the most recent value at or before ``as_of`` (or the last entry if
    as_of is None).  Returns None on any error.
    """
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        slopes: list[float] = data.get("slope_10y_2y") or data.get("slopes") or []
        dates: list[str] = data.get("dates") or []
        if not slopes:
            return None
        if as_of is None or not dates:
            return float(slopes[-1])
        # Find the most recent date <= as_of
        target = as_of.date()
        chosen_slope = None
        for date_str, slope in zip(dates, slopes):
            try:
                d = pd.Timestamp(date_str).date()
            except Exception:  # noqa: BLE001
                continue
            if d <= target:
                chosen_slope = float(slope)
        return chosen_slope
    except Exception as exc:  # noqa: BLE001
        logger.warning("regime: failed to load yield-curve cache from %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compute_state_variables(
    bars: pd.DataFrame,
    *,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    yield_cache_path: Path | None = None,
) -> StateVariables:
    """Compute regime state variables from an OHLCV bars DataFrame.

    Args:
        bars: DataFrame with at least a ``close`` column (case-insensitive).
              Optionally a ``timestamp`` column for ``as_of`` extraction.
        lookback_days: Length of the rolling window used for percentile ranking
              (default 252 trading days ≈ 1 year).
        yield_cache_path: Override the default yield-curve cache path.

    Returns:
        StateVariables dataclass.  ``yield_curve_slope`` is None when the cache
        file is missing; ``trend_strength`` is None when fewer than 50 bars are
        available.  Both cases are flagged in ``metadata``.
    """
    metadata: dict[str, Any] = {}

    # ---- normalise column names ----
    cols = {c.lower(): c for c in bars.columns}
    if "close" not in cols:
        raise ValueError(
            "compute_state_variables: bars DataFrame must have a 'close' column. "
            f"Found columns: {list(bars.columns)}"
        )
    close = bars[cols["close"]].astype(float).reset_index(drop=True)

    # ---- as_of ----
    if "timestamp" in cols:
        ts = bars[cols["timestamp"]].iloc[-1]
        try:
            as_of = pd.Timestamp(ts, tz="UTC") if not isinstance(ts, pd.Timestamp) else ts
            if as_of.tzinfo is None:
                as_of = as_of.tz_localize("UTC")
        except Exception:  # noqa: BLE001
            as_of = pd.Timestamp.utcnow()
    else:
        as_of = pd.Timestamp.utcnow()

    # ---- realized vol + percentile ----
    if len(close) < _MIN_BARS_FOR_VOL:
        warnings.warn(
            f"compute_state_variables: only {len(close)} bars available "
            f"(need {_MIN_BARS_FOR_VOL} for 60-day realized vol). "
            "Using all available data.",
            stacklevel=2,
        )
        metadata["insufficient_bars_for_vol"] = True
        metadata["n_bars"] = len(close)

    # Use whatever we have — _compute_vol_percentile handles short series.
    realized_vol_60d, realized_vol_percentile = _compute_vol_percentile(
        close, lookback_days=lookback_days
    )
    if realized_vol_60d < 0:
        realized_vol_60d = 0.0
    realized_vol_percentile = float(np.clip(realized_vol_percentile, 0.0, 1.0))

    # ---- trend strength ----
    trend_strength = _compute_trend_strength(close)
    if trend_strength is None:
        metadata["insufficient_bars_for_trend"] = True

    # ---- yield curve slope ----
    cache_path = yield_cache_path or _DEFAULT_YIELD_CACHE_PATH
    yield_curve_slope = _load_yield_curve_slope(cache_path, as_of=as_of)
    if yield_curve_slope is None:
        metadata["yield_curve_unavailable"] = True
        logger.debug(
            "regime: yield-curve cache not found at %s; yield_curve_slope=None",
            cache_path,
        )

    return StateVariables(
        realized_vol_60d=realized_vol_60d,
        realized_vol_percentile=realized_vol_percentile,
        yield_curve_slope=yield_curve_slope,
        trend_strength=trend_strength,
        as_of=as_of,
        metadata=metadata,
    )
