"""hermes_quant.data.base — DataProvider helpers (validation, provider chains).

Per ADR-0005:
- All providers return canonical OHLCV DataFrames with:
  columns ['timestamp', 'open', 'high', 'low', 'close', 'volume']
  (timestamp as a COLUMN, not index — avoids subtle iloc bugs across analysts)
- Validation gates: drop NaN OHLC rows, drop zero-volume rows, dedupe on
  timestamp, sort. If <2 valid bars remain, raise DataQualityError.
- Bars are UTC. Localization is display-only.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable

import pandas as pd

from hermes_quant.protocol import (
    DataProviderError,
    DataQualityError,
    RateLimitError,
)

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]
MIN_VALID_BARS = 2


def validate_bars(
    df: pd.DataFrame,
    *,
    drop_zero_volume: bool = True,
    drop_nan_ohlc: bool = True,
    dedupe_timestamp: bool = True,
    sort: bool = True,
    enforce_utc: bool = True,
    min_bars: int = MIN_VALID_BARS,
) -> pd.DataFrame:
    """Run validation gates on raw provider OHLCV.

    Per ADR-0005: bars are UTC, ascending, deduplicated, no NaN OHLC, no
    zero-volume rows (halted tickers). If <2 valid bars remain, raise
    DataQualityError (don't return empty).

    Args:
        df: raw provider bars. Must have at least the REQUIRED_COLUMNS.
        drop_zero_volume: drop rows with volume <= 0 (halted ticker pattern).
        drop_nan_ohlc: drop rows with any NaN in OHLC.
        dedupe_timestamp: keep last on duplicate timestamp.
        sort: sort ascending by timestamp.
        enforce_utc: convert tz-aware timestamps to UTC; tz-naive treated as UTC.
        min_bars: raise DataQualityError if fewer than this many valid bars remain.

    Returns:
        Cleaned DataFrame.

    Raises:
        DataQualityError: if validation produces <min_bars valid rows.
    """
    if df is None or len(df) == 0:
        raise DataQualityError("provider returned empty DataFrame")

    missing = set(REQUIRED_COLUMNS) - set(df.columns)
    if missing:
        raise DataQualityError(f"missing required columns: {sorted(missing)}")

    out = df.copy()

    # Normalize timestamp to UTC pandas Timestamp
    if enforce_utc:
        ts = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
        # Normalize to tz-naive UTC for downstream consistency (pandas convention)
        out["timestamp"] = ts.dt.tz_convert("UTC").dt.tz_localize(None)
        # Drop rows with unparseable timestamps
        out = out.dropna(subset=["timestamp"])

    # Drop NaN OHLC
    if drop_nan_ohlc:
        out = out.dropna(subset=["open", "high", "low", "close"])

    # Drop zero-volume (halted ticker)
    if drop_zero_volume:
        out = out[out["volume"] > 0]

    # Dedupe on timestamp (keep last)
    if dedupe_timestamp:
        out = out.drop_duplicates(subset=["timestamp"], keep="last")

    # Sort ascending
    if sort:
        out = out.sort_values("timestamp").reset_index(drop=True)

    if len(out) < min_bars:
        raise DataQualityError(
            f"only {len(out)} valid bars after validation (min {min_bars}); started with {len(df)}"
        )

    return out


def _fetch_bars_with_optional_asof(
    provider,
    asset: str,
    timeframe: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    use_cache: bool,
    as_of: pd.Timestamp | None,
) -> pd.DataFrame:
    """Call ``provider.fetch_bars`` with the leaf no-lookahead ``as_of`` cutoff,
    degrading gracefully for an older provider that predates the ``as_of`` kwarg.

    Mirrors the advisor / horizon_cache TypeError-retry idiom: pass ``as_of``
    first; on a TypeError that names the kwarg, retry without it (a legacy
    provider that windows by ``end`` keeps its existing — if weaker — bound).
    """
    try:
        return provider.fetch_bars(
            asset, timeframe, start, end, use_cache=use_cache, as_of=as_of
        )
    except TypeError as exc:
        if "as_of" in str(exc) or "unexpected keyword" in str(exc):
            return provider.fetch_bars(asset, timeframe, start, end, use_cache=use_cache)
        raise


def fetch_with_chain(
    providers: Iterable,
    asset: str,
    timeframe: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    max_retries: int = 2,
    use_cache: bool = True,
    as_of: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Try each provider in sequence; fall back on transient failures.

    Per ADR-0005 provider-chain pattern:
    - On RateLimitError: sleep + try next provider
    - On DataProviderError (transient): try next provider
    - On DataQualityError: don't retry — propagate (data isn't going to fix itself)
    - On any other exception: propagate

    Args:
        providers: ordered iterable of DataProvider instances.
        asset, timeframe, start, end: passed through to each provider.
        max_retries: per-provider retry count for transient errors.
        use_cache: forwarded to provider.fetch_bars.
        as_of: ADR-0005 amendment (Wave C.1) leaf-level no-lookahead cutoff,
            threaded through to ``provider.fetch_bars`` so the chain enforces
            the same ``timestamp <= as_of`` bound the single-provider path does.
            This is load-bearing for fallback tiers (AlphaVantage ``compact``
            returns the last ~100 bars REGARDLESS of ``start``/``end`` — its
            ONLY no-lookahead bound is this leaf filter). When omitted, defaults
            to ``end`` so a backtest/replay that already passes ``end=asof``
            (e.g. ``daemon.tick_loop.run_one_tick``) cannot leak future bars
            through a provider that ignores the ``end`` window.

    Returns:
        Validated bars from the first successful provider.

    Raises:
        DataProviderError: if all providers in the chain fail.
        DataQualityError: if a provider's data is unrecoverable.
    """
    errors: list[tuple[str, Exception]] = []
    providers_list = list(providers)
    if not providers_list:
        raise DataProviderError("no providers configured")

    # Default the no-lookahead cutoff to ``end``. ``end`` is already the upper
    # bound the caller intends (e.g. asof in run_one_tick); using it as the leaf
    # ``as_of`` makes the future bound robust against providers that ignore the
    # ``end`` window (AlphaVantage compact). Never LOOSENS visibility: end is the
    # caller's own upper bound.
    cutoff = as_of if as_of is not None else end

    for provider in providers_list:
        for attempt in range(max_retries + 1):
            try:
                bars = _fetch_bars_with_optional_asof(
                    provider, asset, timeframe, start, end, use_cache, cutoff
                )
                # Validate (will raise DataQualityError if bad)
                validated = validate_bars(bars)
                return validated
            except RateLimitError as e:
                logger.warning(
                    "rate limit on provider=%s attempt=%d: %s",
                    provider.name,
                    attempt,
                    e,
                )
                if attempt < max_retries:
                    time.sleep(2.0**attempt)  # exponential backoff
                    continue
                errors.append((provider.name, e))
                break  # try next provider
            except DataProviderError as e:
                logger.warning(
                    "transient provider error provider=%s attempt=%d: %s",
                    provider.name,
                    attempt,
                    e,
                )
                if attempt < max_retries:
                    time.sleep(0.5)
                    continue
                errors.append((provider.name, e))
                break
            except DataQualityError:
                # Don't retry; propagate immediately
                raise

    # All providers exhausted
    summary = "; ".join(f"{name}: {e}" for name, e in errors)
    raise DataProviderError(f"all providers failed: {summary}")
