"""hermes_quant.data.ccxt_provider — Crypto OHLCV via ccxt (ADR-0017).

Per ADR-0017 + research/06-ccxt-provider-patterns.md:
- Default exchange: Binance (deepest BTC/USDT book, cleanest klines API)
- Symbol format: unified ('BTC/USDT' WITH slash; provider rejects no-slash)
- Lookahead-safe: bar timestamp = OPEN time; we filter `open_ts + tf_ms > as_of_ms`
  AFTER validate_bars per ADR-0009 §P0-A
- Pagination: canonical ccxt idiom `since = last_ts + 1` until empty
- Rate limiting: enableRateLimit=True (don't roll our own; Binance allots
  6000 weight/min/IP, klines cost 1-10 weight)
- Retry shape: NetworkError->retry, RateLimitExceeded->retry, ExchangeError->fatal

Optional-extras: install with `pip install 'hermes-quant[ccxt]'`. The ccxt
import is lazy (inside __init__) so the module loads without ccxt and only
fails on instantiation, not on import.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from hermes_quant.data.base import validate_bars
from hermes_quant.protocol import (
    DataProviderError,
    DataQualityError,
    RateLimitError,
)

logger = logging.getLogger(__name__)


# Timeframe canonical set (subset of ccxt's larger range; ADR-0017 §D7)
_VALID_TIMEFRAMES = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}

# Timeframe → seconds (for as_of arithmetic)
_TF_SECONDS = {
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
}


class CcxtProvider:
    """ccxt-backed OHLCV provider for crypto exchanges (ADR-0017).

    Args:
        exchange_id: ccxt exchange identifier ('binance', 'okx', 'bybit', etc.).
            Default 'binance' per ADR-0017 §D8.
        sandbox: Use exchange testnet/sandbox. Default False. Per ADR-0017 §D6
            testnet has no historical-data guarantees and should NOT be used
            for unit tests; use FakeCcxtExchange or pytest-recording instead.
        rate_limit: Enable ccxt's built-in rate limiter. Default True. Don't
            disable unless you know what you're doing.
    """

    name = "ccxt"

    def __init__(
        self,
        exchange_id: str = "binance",
        *,
        sandbox: bool = False,
        rate_limit: bool = True,
        # Test seam: inject a fake exchange instead of importing ccxt
        _exchange_factory=None,
    ):
        self.exchange_id = exchange_id
        self._sandbox = sandbox
        self._rate_limit = rate_limit

        if _exchange_factory is not None:
            self._ex = _exchange_factory()
        else:
            try:
                import ccxt  # noqa: I001
            except ImportError as exc:
                raise DataProviderError(
                    "ccxt is not installed. Install with: pip install 'hermes-quant[ccxt]'"
                ) from exc

            try:
                exchange_cls = getattr(ccxt, exchange_id)
            except AttributeError as exc:
                raise DataProviderError(
                    f"unknown exchange_id={exchange_id!r}; see ccxt.exchanges for valid identifiers"
                ) from exc

            self._ex = exchange_cls(
                {
                    "enableRateLimit": rate_limit,
                    "options": {"defaultType": "spot"},
                }
            )
            if sandbox:
                self._ex.set_sandbox_mode(True)

    # ------------------------------------------------------------------
    # Public API — DataProvider Protocol
    # ------------------------------------------------------------------

    def fetch_bars(
        self,
        symbol: str,
        asset_class: str,
        timeframe: str,
        *,
        lookback_bars: int = 200,
        as_of: pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        """Fetch OHLCV bars; lookahead-safe per ADR-0017 §D3.

        Returns DataFrame indexed 0..N-1 with columns
        ['timestamp', 'open', 'high', 'low', 'close', 'volume'].
        timestamp is tz-aware UTC, representing bar OPEN time.

        Raises:
            DataProviderError: invalid symbol/timeframe/asset_class
            RateLimitError: ccxt RateLimitExceeded after retries
            DataQualityError: validate_bars rejected the result
        """
        # Validation
        if asset_class not in {"crypto"}:
            raise DataProviderError(
                f"CcxtProvider only handles asset_class='crypto', got {asset_class!r}"
            )
        if timeframe not in _VALID_TIMEFRAMES:
            raise DataProviderError(
                f"timeframe must be one of {sorted(_VALID_TIMEFRAMES)}, got {timeframe!r}"
            )
        if "/" not in symbol:
            raise DataProviderError(
                f"symbol must be unified format like 'BTC/USDT', got {symbol!r}. "
                "ccxt rejects no-slash forms."
            )

        # Compute since_ms — leave a 25% buffer over lookback_bars to absorb
        # the "in-flight bar gets dropped by as_of filter" case
        tf_seconds = _TF_SECONDS[timeframe]
        if as_of is None:
            as_of = pd.Timestamp.now(tz="UTC")
        else:
            as_of = pd.Timestamp(as_of)
            if as_of.tzinfo is None:
                as_of = as_of.tz_localize("UTC")

        buffer_bars = int(lookback_bars * 1.25) + 5
        since_dt = as_of - pd.Timedelta(seconds=tf_seconds * buffer_bars)
        since_ms = int(since_dt.timestamp() * 1000)
        as_of_ms = int(as_of.timestamp() * 1000)

        # Pagination loop — canonical ccxt idiom
        all_bars: list[list[Any]] = []
        cur_since = since_ms
        max_iters = 20  # 20 * 1000 = 20K bars upper safety cap
        for _ in range(max_iters):
            chunk = self._fetch_with_retry(
                symbol=symbol,
                timeframe=timeframe,
                since=cur_since,
                limit=1000,
            )
            if not chunk:
                break
            all_bars.extend(chunk)
            new_since = chunk[-1][0] + 1
            if new_since <= cur_since or new_since >= as_of_ms:
                break
            cur_since = new_since
            if len(chunk) < 1000:
                # Last page is partial; we've reached "now"
                break

        if not all_bars:
            raise DataQualityError(
                f"ccxt returned no bars for {symbol} {timeframe} since={since_dt}"
            )

        # Build DataFrame
        df = pd.DataFrame(
            all_bars,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)

        # validate_bars: drop NaN, dedupe, sort, drop zero-volume
        df = validate_bars(df)

        # Normalize timestamp dtype to nanosecond UTC (validate_bars may
        # return microsecond-precision in some pandas versions, which is
        # incompatible with pd.Timedelta arithmetic in others).
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).astype("datetime64[ns, UTC]")

        # CRITICAL: as_of filter — bar must CLOSE at or before as_of (ADR-0017 §D3)
        # bar's open_time + timeframe = bar close_time
        as_of = (
            pd.Timestamp(as_of).tz_convert("UTC")
            if as_of.tzinfo
            else pd.Timestamp(as_of).tz_localize("UTC")
        )
        bar_close_time = df["timestamp"] + pd.Timedelta(seconds=tf_seconds)
        df = df[bar_close_time <= as_of].reset_index(drop=True)

        if len(df) < 2:
            raise DataQualityError(
                f"after as_of filter, only {len(df)} bars remain for {symbol} "
                f"{timeframe} as_of={as_of}; need ≥2"
            )

        return df.tail(lookback_bars).reset_index(drop=True)

    # ------------------------------------------------------------------
    # Internal — retry + error mapping
    # ------------------------------------------------------------------

    def _fetch_with_retry(
        self,
        *,
        symbol: str,
        timeframe: str,
        since: int,
        limit: int,
        max_attempts: int = 3,
        base_delay_s: float = 2.0,
    ) -> list[list[Any]]:
        """Wrap exchange.fetch_ohlcv with retry on transient errors.

        ccxt error taxonomy (ADR-0017 §D4):
            NetworkError (incl. RateLimitExceeded, OnMaintenance) -> retry
            ExchangeError (incl. BadSymbol, AuthenticationError) -> fatal
        """
        # Lazy import so the module loads without ccxt
        try:
            import ccxt

            NetworkError = ccxt.NetworkError
            RateLimitExceeded = ccxt.RateLimitExceeded
            ExchangeError = ccxt.ExchangeError
            BadSymbol = ccxt.BadSymbol
        except ImportError:
            # Fake exchange; create stub exception types
            class _Stub(Exception):
                pass

            NetworkError = RateLimitExceeded = _Stub
            ExchangeError = BadSymbol = _Stub

        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            try:
                return self._ex.fetch_ohlcv(symbol, timeframe, since, limit)
            except RateLimitExceeded as exc:
                last_exc = exc
                if attempt == max_attempts - 1:
                    raise RateLimitError(str(exc)) from exc
                delay = base_delay_s * (2**attempt)
                logger.warning(
                    "ccxt: rate-limit on %s %s; sleeping %.1fs (attempt %d/%d)",
                    symbol,
                    timeframe,
                    delay,
                    attempt + 1,
                    max_attempts,
                )
                time.sleep(delay)
            except BadSymbol as exc:
                # Don't retry; bad config
                raise DataProviderError(f"ccxt rejected symbol {symbol!r}: {exc}") from exc
            except ExchangeError as exc:
                # Most ExchangeError subclasses are fatal (auth, bad request)
                raise DataProviderError(f"ccxt exchange error: {exc}") from exc
            except NetworkError as exc:
                last_exc = exc
                if attempt == max_attempts - 1:
                    raise DataProviderError(
                        f"ccxt network error after {max_attempts} attempts: {exc}"
                    ) from exc
                delay = base_delay_s * (2**attempt)
                logger.warning(
                    "ccxt: network error on %s %s: %s; retrying in %.1fs",
                    symbol,
                    timeframe,
                    exc,
                    delay,
                )
                time.sleep(delay)
            except Exception as exc:  # noqa: BLE001
                # Unknown error class — surface, don't retry blindly
                raise DataProviderError(f"ccxt unexpected error: {exc}") from exc

        # Should not reach here, but defensively
        if last_exc:
            raise DataProviderError(f"ccxt fetch failed: {last_exc}") from last_exc
        return []

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self) -> dict:
        return {
            "name": self.name,
            "exchange_id": self.exchange_id,
            "sandbox": self._sandbox,
            "rate_limit": self._rate_limit,
        }
