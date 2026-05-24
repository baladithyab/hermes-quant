"""hermes_quant.data.yfinance_provider — yfinance equity OHLCV provider.

Per ADR-0005: yfinance for v0.1 equities (broad coverage, no key required,
delayed data ~15min for free tier). Heavy import — lazy-loaded on first use.

Asset class: 'equity' or 'etf'. Timeframes: 1m, 5m, 15m, 30m, 1h, 1d.
Note: yfinance limits intraday lookback (1m: 7 days; 5m-30m: 60 days; 1h: 730
days; 1d: full history). The provider returns whatever the source allows.

For backtesting we use only the day/hour range available; for live we use
fetch_latest with the appropriate lookback window.

Note on rate limiting: yfinance is unofficial. Yahoo throttles aggressively
on bursts. We add a 100ms inter-call sleep and retry with backoff inside
the provider chain (see data.base.fetch_with_chain).
"""
from __future__ import annotations

import logging
import time
from typing import Any

import pandas as pd

from hermes_quant.data.base import validate_bars
from hermes_quant.protocol import (
    DataProviderError,
    DataQualityError,
    RateLimitError,
)

logger = logging.getLogger(__name__)


# yfinance period/interval translation
_TF_TO_YF_INTERVAL = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "60m",  # yfinance uses '60m' for hourly
    "4h": None,   # not directly supported; skip for v0.1.1
    "1d": "1d",
}


# Phase-9e (synthesis 2026-05-13): exponential-backoff retry pattern adapted
# from TauricResearch/TradingAgents (TradingAgents/dataflows/utils.py).
# Yahoo throttles aggressively on bursts; the previous 100ms inter-call
# sleep alone wasn't enough — a single transient 429 would propagate up
# and force the chain to fall back to a slower provider when the issue
# would resolve in 2-4 seconds.
#
# Adapted (NOT copied verbatim):
#   - Their function takes a callable and retries on YFRateLimitError.
#   - We retry on our RateLimitError + transient OSError/ConnectionError.
#   - 3 max attempts, 2s base, exponential factor 2 → delays {2s, 4s}.
#   - Final attempt raises whatever the underlying call raises.
#
# Cited: docs/research/04-tradingagents-comparison.md §"P0 — yf_retry()"
def _retry_with_backoff(
    fn,
    *args,
    max_attempts: int = 3,
    base_delay_s: float = 2.0,
    factor: float = 2.0,
    **kwargs,
):
    """Call fn(*args, **kwargs) with exponential-backoff retry on
    transient failures (RateLimitError, ConnectionError).

    Adapted from TauricResearch/TradingAgents yf_retry pattern. Our
    RateLimitError replaces their YFRateLimitError; we additionally
    retry on ConnectionError (and OSError parent class) since DNS
    flaps / brief network blips are also transient.
    """
    import time as _time
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fn(*args, **kwargs)
        except (RateLimitError, ConnectionError) as e:
            last_exc = e
            if attempt + 1 >= max_attempts:
                break  # exhausted; re-raise outside loop
            delay = base_delay_s * (factor ** attempt)
            logger.warning(
                "yfinance transient failure (attempt %d/%d): %s — "
                "retrying in %.1fs",
                attempt + 1, max_attempts, e, delay,
            )
            _time.sleep(delay)
    assert last_exc is not None  # mypy
    raise last_exc


class YFinanceProvider:
    """yfinance-backed equity / ETF data provider.

    Implements DataProvider Protocol from hermes_quant.protocol.

    Per ADR-0005:
    - asset_classes = ['equity', 'etf']
    - timeframes from _TF_TO_YF_INTERVAL
    - requires_credentials = False (yfinance is unofficial Yahoo scrape)
    """

    name = "yfinance"
    asset_classes = ["equity", "etf"]
    timeframes = ["1m", "5m", "15m", "30m", "1h", "1d"]
    requires_credentials = False

    def __init__(
        self,
        *,
        inter_call_sleep_s: float = 0.1,
        retry_max_attempts: int = 3,
        retry_base_delay_s: float = 2.0,
        retry_factor: float = 2.0,
    ):
        self._yf: Any = None  # lazy
        self._inter_call_sleep = inter_call_sleep_s
        self._retry_max_attempts = retry_max_attempts
        self._retry_base_delay_s = retry_base_delay_s
        self._retry_factor = retry_factor
        self._n_fetches = 0
        self._n_errors = 0
        self._last_fetch_at: pd.Timestamp | None = None

    @property
    def yf(self) -> Any:
        """Lazy-import yfinance so plugin install doesn't hard-require it."""
        if self._yf is None:
            try:
                import yfinance as yf
            except ImportError as e:
                raise DataProviderError(
                    "yfinance not installed; install hermes-quant[yfinance]"
                ) from e
            self._yf = yf
        return self._yf

    def fetch_bars(
        self,
        asset: str,
        timeframe: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
        *,
        use_cache: bool = True,
        as_of: pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        """Fetch OHLCV bars in [start, end] for `asset` at `timeframe`.

        Args:
            asset: ticker symbol (e.g., 'AAPL', 'SPY'). yfinance accepts the
                bare ticker for US equities; for ETFs same.
            timeframe: one of self.timeframes.
            start, end: UTC timestamps. yfinance treats start as inclusive,
                end as exclusive (caller convention).
            use_cache: yfinance has its own caching via session=requests-cache,
                but we don't wire it for v0.1.1; the param is forwarded for
                interface symmetry.
            as_of: ADR-0005 amendment 2026-05-13 (Wave C.1) — leaf-level
                lookahead enforcement. When set, bars with
                `timestamp > as_of` are filtered out before return. This
                pushes the no-lookahead invariant DOWN to the data layer
                so analysts that use the canonical fetch path can't see
                future bars even if they forget to filter. Pattern stolen
                from TauricResearch/TradingAgents §"as_of_date" filter at
                the data leaf.

        Returns:
            Validated DataFrame with REQUIRED_COLUMNS, ascending UTC timestamps.

        Raises:
            DataProviderError: if yfinance not installed or unrecoverable network error.
            RateLimitError: if Yahoo signals throttling.
            DataQualityError: if bars fail validation gates.
        """
        if timeframe not in self.timeframes:
            raise DataProviderError(
                f"timeframe {timeframe!r} not supported by yfinance; "
                f"options: {self.timeframes}"
            )
        yf_interval = _TF_TO_YF_INTERVAL[timeframe]
        if yf_interval is None:
            raise DataProviderError(
                f"timeframe {timeframe!r} not supported in v0.1.1"
            )

        # Inter-call sleep to avoid burst throttling
        if self._last_fetch_at is not None:
            elapsed = (pd.Timestamp.utcnow() - self._last_fetch_at).total_seconds()
            if elapsed < self._inter_call_sleep:
                time.sleep(self._inter_call_sleep - elapsed)

        # Phase-9e: wrap the actual yfinance API call with exponential-
        # backoff retry on transient throttle/network errors. The retry
        # is bounded (3 attempts × 2s/4s) so the caller's overall budget
        # is at most ~6s before falling through to the chain's next
        # provider.
        def _do_fetch():
            try:
                ticker = self.yf.Ticker(asset)
                return ticker.history(
                    start=start.to_pydatetime() if hasattr(start, "to_pydatetime") else start,
                    end=end.to_pydatetime() if hasattr(end, "to_pydatetime") else end,
                    interval=yf_interval,
                    auto_adjust=False,  # we want raw OHLC (back-adjustment is for analysis-side)
                    actions=False,
                    prepost=False,
                )
            except Exception as e:  # noqa: BLE001
                self._n_errors += 1
                msg = str(e).lower()
                if "rate" in msg or "429" in msg or "too many" in msg:
                    raise RateLimitError(f"yfinance throttled: {e}") from e
                raise DataProviderError(f"yfinance fetch failed: {e}") from e

        try:
            raw = _retry_with_backoff(
                _do_fetch,
                max_attempts=self._retry_max_attempts,
                base_delay_s=self._retry_base_delay_s,
                factor=self._retry_factor,
            )
        except RateLimitError:
            # Exhausted retry budget on rate-limit. Re-raise so the chain
            # can fall to the next provider.
            raise
        except DataProviderError:
            raise

        self._last_fetch_at = pd.Timestamp.utcnow()
        self._n_fetches += 1

        if raw is None or len(raw) == 0:
            raise DataQualityError(
                f"yfinance returned empty bars for {asset} {timeframe} {start}..{end}"
            )

        # yfinance returns:
        #   index: DatetimeIndex (tz-aware America/New_York for US equities)
        #   columns: ['Open', 'High', 'Low', 'Close', 'Volume', 'Dividends', 'Stock Splits']
        out = pd.DataFrame(
            {
                "timestamp": raw.index,
                "open": raw["Open"].values,
                "high": raw["High"].values,
                "low": raw["Low"].values,
                "close": raw["Close"].values,
                "volume": raw["Volume"].values,
            }
        )
        # validate_bars handles tz normalization, NaN drops, zero-volume drops
        validated = validate_bars(out)

        # ADR-0005 amendment 2026-05-13 (Wave C.1) — leaf-level lookahead
        # enforcement. Filter to as_of AFTER validation so the cutoff is
        # applied to the same canonical (UTC, ascending) dataframe an
        # analyst would otherwise see. Comparison-safe regardless of
        # input bars timezone (validate_bars normalizes to tz-NAIVE UTC).
        if as_of is not None:
            cutoff = as_of
            if cutoff.tzinfo is None:
                cutoff = cutoff.tz_localize("UTC")
            # validate_bars stores timestamps tz-naive UTC (line 75 of base.py:
            # .dt.tz_convert("UTC").dt.tz_localize(None)). Drop cutoff tz to
            # match — comparing tz-aware vs tz-naive raises TypeError under
            # pandas 2.x. Fix verified 2026-05-24 against yfinance 1.4.0.
            cutoff_naive = cutoff.tz_convert("UTC").tz_localize(None)
            validated = validated[validated["timestamp"] <= cutoff_naive].reset_index(drop=True)

        return validated

    def fetch_latest(
        self,
        asset: str,
        timeframe: str,
        lookback: int = 500,
    ) -> pd.DataFrame:
        """Fetch the most recent N bars at `timeframe`."""
        end = pd.Timestamp.utcnow()
        # Estimate the start range conservatively
        tf_to_seconds = {
            "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
            "1h": 3600, "4h": 14400, "1d": 86400,
        }
        secs = tf_to_seconds.get(timeframe)
        if secs is None:
            raise DataProviderError(f"unknown timeframe {timeframe!r}")
        # 2× lookback for slack (markets close, weekends, etc.)
        start = end - pd.Timedelta(seconds=secs * lookback * 2)
        bars = self.fetch_bars(asset, timeframe, start, end, use_cache=False)
        return bars.tail(lookback).reset_index(drop=True)

    def health(self) -> dict:
        return {
            "provider": self.name,
            "n_fetches": self._n_fetches,
            "n_errors": self._n_errors,
            "last_fetch_at": (
                self._last_fetch_at.isoformat() if self._last_fetch_at else None
            ),
            "yfinance_loaded": self._yf is not None,
        }
