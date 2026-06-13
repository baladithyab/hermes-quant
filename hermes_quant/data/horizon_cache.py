"""hermes_quant.data.horizon_cache — Multi-horizon OHLCV cache (ADR-0036).

The advisor's multi-timeframe fan-out (`recommend_multi_horizon`) needs OHLCV
bars at multiple horizons (`1d`, `1w`, `1M`, `1Q`). Naively this means N
yfinance round-trips per symbol — burning rate-limit budget and doubling
latency.

This module fetches the **longest required history once** (10y daily for any
multi-horizon set that includes `1Q`) and resamples in-memory for the shorter
horizons. The cache is keyed by `(symbol, asof_date_str)` and stored under
``~/.hermes/quant/cache/horizon-history/<asof_date>/<symbol>.parquet`` so a
new calendar day naturally invalidates yesterday's cache (filesystem listing
will skip the previous day's directory).

Failure mode: yfinance errors → log warning, return empty DataFrame. The
advisor's silence-by-default invariant means an empty frame just produces no
analyst views for that horizon, not an exception.

Per ADR-0036 §"Implementation notes":
- Cache key: (symbol, asof_date, longest_horizon_lookback)
- Resampling: pandas `resample` with `'W-FRI'`, `'BME'`, `'BQE-DEC'`
- Aggregations: open=first, high=max, low=min, close=last, volume=sum
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Per-horizon resample rules (pandas frequency aliases).
# ADR-0036 specifies W-FRI (week ending Friday), BME (business month end),
# BQE-DEC (business quarter end, Dec-anchored).
_RESAMPLE_RULES: dict[str, str] = {
    "1w": "W-FRI",
    "1M": "BME",
    "1Q": "BQE-DEC",
}

# How far back to fetch daily bars for a horizon set including this horizon.
# Per ADR-0036 §"Default horizon set": 1d=1y, 1w=3y, 1M=5y, 1Q=10y.
_HORIZON_LOOKBACK_YEARS: dict[str, int] = {
    "1d": 1,
    "1w": 3,
    "1M": 5,
    "1Q": 10,
}

# Standard OHLCV columns we resample.
_OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]

# Cache root: ~/.hermes/quant/cache/horizon-history
_CACHE_ROOT = Path.home() / ".hermes" / "quant" / "cache" / "horizon-history"


# ---------------------------------------------------------------------------
# Resample logic — pure function, easy to test
# ---------------------------------------------------------------------------


def resample_to_horizon(df: pd.DataFrame, horizon: str) -> pd.DataFrame:
    """Resample a daily OHLCV DataFrame to the requested horizon.

    Args:
        df: canonical OHLCV with columns
            ['timestamp', 'open', 'high', 'low', 'close', 'volume'].
            `timestamp` must be ascending and may be tz-naive or tz-aware.
        horizon: one of '1d', '1w', '1M', '1Q'. '1d' is a passthrough.

    Returns:
        New DataFrame with the same columns, resampled to `horizon`.
        Empty input → empty output (with the standard columns).

    Raises:
        ValueError: if `horizon` is not in {'1d', '1w', '1M', '1Q'}.
    """
    if horizon == "1d":
        # Passthrough — but normalize columns and copy so callers can mutate.
        if len(df) == 0:
            return pd.DataFrame(columns=_OHLCV_COLUMNS)
        return df[_OHLCV_COLUMNS].copy()

    if horizon not in _RESAMPLE_RULES:
        raise ValueError(
            f"unsupported horizon {horizon!r}; supported: {sorted(['1d', *_RESAMPLE_RULES])}"
        )

    if len(df) == 0:
        return pd.DataFrame(columns=_OHLCV_COLUMNS)

    rule = _RESAMPLE_RULES[horizon]
    indexed = df.set_index("timestamp")
    # Ensure the index is a proper DatetimeIndex for resample()
    if not isinstance(indexed.index, pd.DatetimeIndex):
        indexed.index = pd.to_datetime(indexed.index)

    resampled = indexed.resample(rule).agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    # Drop fully-NaN rows produced when the rule yields a bucket containing
    # zero source bars (e.g., trailing bucket on a partial week).
    resampled = resampled.dropna(subset=["open", "high", "low", "close"])
    resampled = resampled.reset_index()
    return resampled[_OHLCV_COLUMNS]


# ---------------------------------------------------------------------------
# Lookback resolution
# ---------------------------------------------------------------------------


def longest_lookback_years(horizons: Iterable[str]) -> int:
    """Return the max lookback (in years) required to satisfy `horizons`.

    Unknown horizons default to 1 year (the 1d baseline) so the cache stays
    useful even when an exotic horizon is requested — the resample step
    will fail loudly in that case.
    """
    horizons = list(horizons) or ["1d"]
    return max(_HORIZON_LOOKBACK_YEARS.get(h, 1) for h in horizons)


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------


def _cache_path(symbol: str, asof_date_str: str) -> Path:
    """Path to the cached daily parquet for this (symbol, asof_date)."""
    return _CACHE_ROOT / asof_date_str / f"{symbol}.parquet"


def _load_from_cache(symbol: str, asof_date_str: str) -> pd.DataFrame | None:
    path = _cache_path(symbol, asof_date_str)
    if not path.exists():
        return None
    try:
        return pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001 — corrupt cache is non-fatal
        logger.warning("horizon_cache: failed to read %s (%s); refetching", path, exc)
        return None


def _save_to_cache(symbol: str, asof_date_str: str, df: pd.DataFrame) -> None:
    if len(df) == 0:
        return
    path = _cache_path(symbol, asof_date_str)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path, index=False)
    except Exception as exc:  # noqa: BLE001 — caching is opportunistic
        logger.warning("horizon_cache: failed to write %s (%s); skipping cache", path, exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_resampled_history(
    symbol: str,
    timeframe: str,
    *,
    asof: pd.Timestamp | None = None,
    horizons_in_set: Iterable[str] | None = None,
    provider: Any = None,
) -> pd.DataFrame:
    """Return OHLCV bars at ``timeframe`` for ``symbol``, fetching once per day.

    The longest-required daily history is fetched via the underlying provider
    (default: yfinance), cached on disk under
    ``~/.hermes/quant/cache/horizon-history/<asof_date>/<symbol>.parquet``,
    then resampled in-memory to the requested ``timeframe``.

    Args:
        symbol: ticker (e.g. "AAPL").
        timeframe: one of '1d', '1w', '1M', '1Q'.
        asof: anchor timestamp; defaults to now (UTC). The cache key uses the
            calendar date of this timestamp.
        horizons_in_set: hint about the full multi-horizon request so the
            cache fetches the longest lookback once. Default: just the
            requested timeframe.
        provider: optional DataProvider override (test seam). Defaults to
            yfinance via the advisor's lazy resolution path.

    Returns:
        Resampled OHLCV DataFrame. Empty on any provider failure (silence-
        by-default; the advisor's pipeline drops the horizon's analyst views).
    """
    asof_ts: pd.Timestamp
    if asof is None:
        asof_ts = pd.Timestamp.now(tz="UTC")
    else:
        asof_ts = asof
        if asof_ts.tzinfo is None:
            asof_ts = asof_ts.tz_localize("UTC")

    asof_date_str = asof_ts.strftime("%Y-%m-%d")

    # Try cache first
    daily = _load_from_cache(symbol, asof_date_str)
    if daily is None:
        # Cache miss — fetch fresh daily history at the longest required lookback
        horizons = list(horizons_in_set) if horizons_in_set else [timeframe]
        years = longest_lookback_years(horizons)
        start = asof_ts - pd.Timedelta(days=years * 366)  # leap-year safe
        end = asof_ts

        if provider is None:
            try:
                from hermes_quant.data.yfinance_provider import YFinanceProvider

                provider = YFinanceProvider()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "horizon_cache: failed to construct yfinance provider (%s); "
                    "returning empty frame for %s",
                    exc,
                    symbol,
                )
                return pd.DataFrame(columns=_OHLCV_COLUMNS)

        try:
            daily = provider.fetch_bars(symbol, "1d", start, end, as_of=asof_ts)
        except TypeError as exc:
            # Older provider without as_of kwarg
            if "as_of" in str(exc) or "unexpected keyword" in str(exc):
                try:
                    daily = provider.fetch_bars(symbol, "1d", start, end)
                except Exception as exc2:  # noqa: BLE001
                    logger.warning(
                        "horizon_cache: provider fetch failed for %s (%s); "
                        "returning empty frame",
                        symbol,
                        exc2,
                    )
                    return pd.DataFrame(columns=_OHLCV_COLUMNS)
            else:
                logger.warning(
                    "horizon_cache: provider TypeError for %s (%s); returning empty frame",
                    symbol,
                    exc,
                )
                return pd.DataFrame(columns=_OHLCV_COLUMNS)
        except Exception as exc:  # noqa: BLE001 — fail-soft per ADR-0036
            logger.warning(
                "horizon_cache: provider fetch failed for %s (%s); returning empty frame",
                symbol,
                exc,
            )
            return pd.DataFrame(columns=_OHLCV_COLUMNS)

        if daily is None or len(daily) == 0:
            return pd.DataFrame(columns=_OHLCV_COLUMNS)

        _save_to_cache(symbol, asof_date_str, daily)

    # cs05 no-lookahead clip: clip the daily frame to bars at/under the decision
    # asof BEFORE resampling. This is load-bearing on BOTH paths: (1) the cache
    # is keyed only by calendar date, so a hit can return a frame populated later
    # in the day (or by a provider that ignored as_of) containing bars past asof;
    # (2) resampling an unclipped frame aggregates a partial weekly/monthly/
    # quarterly bucket whose period-end label is after asof into a
    # completed-looking bar that contains future daily bars. Clipping the daily
    # INPUT means partial future buckets never form and the intraday-hit leak is
    # removed in one place, keeping resample_to_horizon a pure transform.
    # Rail: asof = publication time always (no look-ahead).
    if daily is not None and len(daily) > 0:
        ts = pd.to_datetime(daily["timestamp"], utc=True)
        daily = daily.loc[ts <= asof_ts]
        if len(daily) == 0:
            return pd.DataFrame(columns=_OHLCV_COLUMNS)

    # Resample to the target horizon (1d is a passthrough copy)
    try:
        resampled = resample_to_horizon(daily, timeframe)
        # cs05 no-lookahead clip (part 2): resample labels each bucket by its
        # period-END (e.g. W-FRI), so a Mon-Wed partial week clipped correctly at
        # the daily level still emits a bar labelled the coming Friday — a
        # STILL-FORMING bucket presented as a completed bar. Drop any bucket whose
        # period-end label is after asof: a partial week/month/quarter is not a
        # realized bar (silence-by-default — better no data point than an
        # incomplete one mislabelled complete). 1d is a passthrough already clipped
        # above, so this is a no-op there.
        if len(resampled) > 0:
            rts = pd.to_datetime(resampled["timestamp"], utc=True)
            resampled = resampled.loc[rts <= asof_ts].reset_index(drop=True)
        return resampled
    except ValueError:
        # Unknown horizon — surface to caller
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "horizon_cache: resample to %s failed for %s (%s); returning empty frame",
            timeframe,
            symbol,
            exc,
        )
        return pd.DataFrame(columns=_OHLCV_COLUMNS)
