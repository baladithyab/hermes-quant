"""hermes_quant.data.alphavantage_provider — Alpha Vantage equity OHLCV provider.

B22 (R-B22, 2026-05-31): a DataProvider-Protocol-conformant equity/ETF
provider, intended as the LAST-RESORT fallback tier in
``hermes_quant.data.base.fetch_with_chain`` AFTER yfinance/ccxt.

Design (per research note R-B22):
- Raw ``requests`` (lazy-imported), NOT the ``alpha_vantage`` PyPI wrapper.
  The wrapper isn't installed, pulls transitive deps, and hides the raw
  ``Note``/``Information`` body we need to classify a rate-limit.
- Endpoint: ``GET https://www.alphavantage.co/query`` with
  ``function=TIME_SERIES_DAILY`` (RAW as-traded OHLCV — matches yfinance
  ``auto_adjust=False``). ``TIME_SERIES_DAILY_ADJUSTED`` and
  ``outputsize=full`` are PREMIUM as of 2025; we default to
  ``outputsize=compact`` (last ~100 daily bars, free-tier safe).
- Free tier is 25 requests/DAY + 5 requests/MINUTE. AV is a last-resort
  fallback, never a primary; order MUST be yfinance-first, AV-last.
- HTTP 200 != success. AV returns throttle/quota messages as HTTP-200 JSON
  bodies (``Note`` / ``Information`` / ``Error Message``). We inspect the
  body BEFORE indexing ``"Time Series (Daily)"`` and map a throttle onto the
  repo's ``RateLimitError`` so ``fetch_with_chain`` backs off + falls
  through.
- Fail-closed: a missing ``ALPHA_VANTAGE_API_KEY`` raises ``DataProviderError``
  AT FETCH TIME (never at construction/discovery), which ``fetch_with_chain``
  treats as transient-fall-through — AV becomes a silent no-op tier.
- No-lookahead: the leaf-level ``as_of`` filter is applied AFTER
  ``validate_bars`` (which normalizes to tz-naive UTC), identical to
  ``yfinance_provider``.
- Determinism: the returned DataFrame is a pure function of the JSON body
  (no wall-clock in output). Health counters are side-channel only.
- No circuit breaker (out of scope per R-B22 §5): stateful, determinism
  hazard, no benefit at hermes' single-process volume.

Optional-extras: install with ``pip install 'hermes-quant[alphavantage]'``.
The ``requests`` import is lazy (inside the fetch path) so the module loads
without it; key resolution never logs the key value.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import pandas as pd

from hermes_quant.data.base import validate_bars
from hermes_quant.protocol import (
    DataProviderError,
    DataQualityError,
    RateLimitError,
)

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.alphavantage.co/query"
_API_KEY_ENV = "ALPHA_VANTAGE_API_KEY"

# Free tier: TIME_SERIES_DAILY is the only daily function; intraday is a
# separate function (premium-gated for full history). We expose 1d only.
_VALID_TIMEFRAMES = {"1d"}

# AV daily "Time Series (Daily)" payload column keys (numbered strings).
# All values arrive as STRINGS and MUST be cast.
_TS_KEY = "Time Series (Daily)"
_COL_MAP = {
    "open": "1. open",
    "high": "2. high",
    "low": "3. low",
    "close": "4. close",
    "volume": "5. volume",
}

# Substrings that, when present in an "Information"/"Note" body, indicate a
# throttle/quota rather than a successful payload. Lowercased compare.
_RATE_LIMIT_MARKERS = ("rate limit", "api key", "premium", "calls per", "requests per")


class AlphaVantageProvider:
    """Alpha Vantage TIME_SERIES_DAILY equity/ETF provider (B22).

    Implements the canonical DataProvider Protocol signature
    ``fetch_bars(asset, timeframe, start, end, *, use_cache, as_of)`` so it
    drops straight into ``fetch_with_chain`` as a fallback tier.

    FIRST credentialed provider in the repo (``requires_credentials = True``).
    Fail-closed: a missing key raises ``DataProviderError`` at fetch time, so
    the chain silently skips AV rather than crashing.

    Args:
        api_key: explicit key; if None, resolved from ``ALPHA_VANTAGE_API_KEY``
            at fetch time (NOT at construction).
        session: test seam — inject a ``requests.Session``-like object whose
            ``.get(url, params=, timeout=)`` returns a response with
            ``.json()`` and ``.raise_for_status()``. When None, a lazy
            ``requests`` session is created on first fetch.
        outputsize: ``"compact"`` (free, ~100 bars) or ``"full"`` (PREMIUM).
            Default ``"compact"``.
        retry_max_attempts/retry_base_delay_s/retry_factor: bounded
            exponential-backoff retry on transient throttle/network errors
            (mirrors yfinance_provider).
        timeout_s: per-request HTTP timeout (seconds).
    """

    name = "alphavantage"
    asset_classes = ["equity", "etf"]
    timeframes = ["1d"]
    requires_credentials = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        session: Any = None,
        outputsize: str = "compact",
        retry_max_attempts: int = 3,
        retry_base_delay_s: float = 2.0,
        retry_factor: float = 2.0,
        timeout_s: float = 30.0,
    ):
        # Do NOT fail at construction on a missing key — discovery/daemons
        # without a key must not crash. Resolution happens at fetch time.
        self._api_key = api_key
        self._session = session  # may be None -> lazy requests.Session
        self._outputsize = outputsize
        self._retry_max_attempts = retry_max_attempts
        self._retry_base_delay_s = retry_base_delay_s
        self._retry_factor = retry_factor
        self._timeout_s = timeout_s
        self._n_fetches = 0
        self._n_errors = 0
        self._last_fetch_at: pd.Timestamp | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_key(self) -> str:
        """Resolve the API key (arg > env). Fail-closed if absent.

        Raises:
            DataProviderError: if neither the constructor arg nor
                ``ALPHA_VANTAGE_API_KEY`` is set. Treated by
                ``fetch_with_chain`` as transient -> chain skips AV.
        """
        key = self._api_key or os.environ.get(_API_KEY_ENV)
        if not key:
            raise DataProviderError(
                f"{_API_KEY_ENV} not set; AlphaVantageProvider is fail-closed "
                "(chain will skip this tier)"
            )
        return key

    def _get_session(self) -> Any:
        """Lazy-create a requests.Session (or return the injected seam)."""
        if self._session is not None:
            return self._session
        try:
            import requests
        except ImportError as e:  # pragma: no cover - requests is a base dep
            raise DataProviderError(
                "requests not installed; install hermes-quant[alphavantage]"
            ) from e
        self._session = requests.Session()
        return self._session

    def _retry(self, fn):
        """Bounded exponential-backoff retry on RateLimitError /
        ConnectionError / TimeoutError. Mirrors yfinance_provider._retry_
        with_backoff.

        The 25/day quota will NOT resolve on retry (so the final RateLimitError
        propagates and the chain falls through), but a 5/min throttle resolves
        in <=12s -> a 2s/4s backoff is the right budget before fall-through.
        """
        import time as _time

        last_exc: Exception | None = None
        for attempt in range(self._retry_max_attempts):
            try:
                return fn()
            except (RateLimitError, ConnectionError, TimeoutError) as e:
                last_exc = e
                if attempt + 1 >= self._retry_max_attempts:
                    break
                delay = self._retry_base_delay_s * (self._retry_factor**attempt)
                logger.warning(
                    "alphavantage transient failure (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1,
                    self._retry_max_attempts,
                    e,
                    delay,
                )
                _time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _classify_body(body: Any, asset: str) -> dict:
        """Inspect a parsed AV JSON body BEFORE indexing the time series.

        HTTP 200 != success: AV returns throttle/quota/error as 200 JSON.

        Returns the body if it is a usable time-series payload.

        Raises:
            RateLimitError: throttle / daily-quota / premium-gate.
            DataProviderError: bad symbol/params or malformed body (transient
                class so the chain still tries the next provider; a typo'd
                equity should still reach yfinance).
        """
        if not isinstance(body, dict):
            raise DataProviderError(
                f"alphavantage returned non-object body for {asset}: {type(body).__name__}"
            )
        # Legacy per-minute throttle is keyed "Note".
        if "Note" in body:
            raise RateLimitError(f"alphavantage throttled (Note) for {asset}: {body['Note']}")
        # Daily-quota / premium-gate is keyed "Information".
        info = body.get("Information")
        if info is not None:
            info_l = str(info).lower()
            if any(m in info_l for m in _RATE_LIMIT_MARKERS):
                raise RateLimitError(
                    f"alphavantage rate-limited/premium-gated for {asset}: {info}"
                )
            # An unexpected Information body with no series -> treat as transient.
            if _TS_KEY not in body:
                raise DataProviderError(f"alphavantage Information for {asset}: {info}")
        # Bad symbol / bad params -> NOT transient throttle, but we still want
        # the chain to try the next provider (a typo'd equity may resolve on
        # yfinance). DataProviderError = transient-class fall-through.
        if "Error Message" in body:
            raise DataProviderError(
                f"alphavantage error for {asset}: {body['Error Message']}"
            )
        if _TS_KEY not in body:
            raise DataProviderError(
                f"alphavantage response missing {_TS_KEY!r} for {asset}; keys={sorted(body)}"
            )
        return body

    # ------------------------------------------------------------------
    # Public API — DataProvider Protocol (canonical signature)
    # ------------------------------------------------------------------

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
        """Fetch daily OHLCV for ``asset`` via Alpha Vantage TIME_SERIES_DAILY.

        Args:
            asset: ticker symbol (e.g. 'IBM', 'SPY').
            timeframe: must be '1d' (free tier daily only).
            start, end: UTC range. AV ``compact`` returns the last ~100 daily
                bars regardless of ``start``/``end`` (the params dict never
                forwards them to the HTTP request — documented AV behavior).
                We do NOT lower-bound prune (the chain gets the full free-tier
                window) but we DO upper-bound prune: bars later than the
                tightest of ``as_of`` and ``end`` are dropped. This closes the
                no-lookahead leak on the ``fetch_with_chain`` path, which calls
                ``fetch_bars`` WITHOUT ``as_of`` — without the ``end`` prune the
                full ~100-bar window up to wall-clock today would be returned
                regardless of the backtest's requested ``end`` anchor. ``end``
                is the implicit cutoff when ``as_of`` is absent; ``end=None``
                (genuine up-to-now request) is the only no-upper-prune case.
            use_cache: accepted for interface symmetry; the provider itself
                does not cache (a read-through cache is layered above via
                data.cache, per R-B22 quota-preservation note).
            as_of: leaf-level no-lookahead filter; drops bars with
                ``timestamp > as_of`` AFTER validation.

        Returns:
            Validated DataFrame with REQUIRED_COLUMNS, ascending tz-naive UTC.

        Raises:
            DataProviderError: missing key (fail-closed), bad symbol/params,
                network error, or unsupported timeframe.
            RateLimitError: AV throttle / daily-quota / premium-gate.
            DataQualityError: bars fail validation gates (<2 valid bars).
        """
        if timeframe not in self.timeframes:
            raise DataProviderError(
                f"timeframe {timeframe!r} not supported by alphavantage; "
                f"options: {self.timeframes} (free tier: TIME_SERIES_DAILY only)"
            )

        key = self._resolve_key()
        params = {
            "function": "TIME_SERIES_DAILY",
            "symbol": asset,
            "outputsize": self._outputsize,
            "datatype": "json",
            "apikey": key,
        }

        def _do_fetch() -> dict:
            session = self._get_session()
            try:
                resp = session.get(_BASE_URL, params=params, timeout=self._timeout_s)
                resp.raise_for_status()
                body = resp.json()
            except RateLimitError:
                raise
            except DataProviderError:
                raise
            except Exception as e:  # noqa: BLE001
                self._n_errors += 1
                msg = str(e).lower()
                if "429" in msg or "too many" in msg or "rate" in msg:
                    raise RateLimitError(f"alphavantage HTTP throttled: {e}") from e
                raise DataProviderError(f"alphavantage fetch failed: {e}") from e
            # HTTP 200 != success — classify the body (may raise).
            return self._classify_body(body, asset)

        try:
            body = self._retry(_do_fetch)
        except (RateLimitError, DataProviderError):
            self._n_errors += 1
            raise

        series = body[_TS_KEY]
        if not series:
            raise DataQualityError(f"alphavantage returned empty series for {asset}")

        # Build canonical frame. AV values are STRINGS — explicit casts. Dates
        # are US/Eastern calendar dates (no time-of-day for daily).
        timestamps: list[Any] = []
        opens: list[float] = []
        highs: list[float] = []
        lows: list[float] = []
        closes: list[float] = []
        volumes: list[int] = []
        for date_str, row in series.items():
            try:
                opens.append(float(row[_COL_MAP["open"]]))
                highs.append(float(row[_COL_MAP["high"]]))
                lows.append(float(row[_COL_MAP["low"]]))
                closes.append(float(row[_COL_MAP["close"]]))
                volumes.append(int(float(row[_COL_MAP["volume"]])))
            except (KeyError, TypeError, ValueError) as e:
                # A single malformed row shouldn't poison the whole fetch, but
                # a structurally-wrong payload should surface.
                raise DataProviderError(
                    f"alphavantage malformed row {date_str!r} for {asset}: {e}"
                ) from e
            timestamps.append(date_str)

        # Localize US/Eastern -> tz-aware; validate_bars normalizes to
        # tz-naive UTC. AV "5. Time Zone" is US/Eastern.
        ts = pd.to_datetime(pd.Series(timestamps))
        ts = ts.dt.tz_localize("US/Eastern", ambiguous="NaT", nonexistent="shift_forward")
        out = pd.DataFrame(
            {
                "timestamp": ts,
                "open": opens,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": volumes,
            }
        )

        validated = validate_bars(out)

        self._last_fetch_at = pd.Timestamp.utcnow()
        self._n_fetches += 1

        # Leaf-level no-lookahead upper-bound prune (cs11). AV ignores
        # start/end on the wire, so the only protection against returning bars
        # later than the backtest anchor is this post-filter. The cutoff is the
        # TIGHTEST (earliest) of as_of and end:
        #   - as_of provided  -> the explicit no-lookahead cutoff (unchanged
        #     behavior; the canonical advisor/builder path passes as_of==end).
        #   - end provided     -> the implicit cutoff for callers that omit
        #     as_of, e.g. fetch_with_chain (data/base.py) which never forwards
        #     as_of. Without this, the full ~100-bar window up to wall-clock
        #     today leaked past the requested `end`.
        # Taking min() means the end-prune only ADDS protection — it can never
        # return a bar later than as_of, so existing as_of behavior is never
        # weakened. end=None (genuine up-to-now) is the only no-prune case.
        cutoff_naive = self._tightest_cutoff(as_of, end)
        if cutoff_naive is not None:
            validated = validated[validated["timestamp"] <= cutoff_naive].reset_index(
                drop=True
            )

        return validated

    @staticmethod
    def _tightest_cutoff(
        as_of: pd.Timestamp | None, end: pd.Timestamp | None
    ) -> pd.Timestamp | None:
        """Return the tz-naive-UTC min() of the provided as_of / end cutoffs.

        validate_bars stores timestamps tz-naive UTC, so the cutoff must be
        normalized to tz-naive UTC for a TypeError-free comparison under pandas
        2.x. Returns None only when BOTH are None (genuine up-to-now request).
        """
        cutoffs: list[pd.Timestamp] = []
        for raw in (as_of, end):
            if raw is None:
                continue
            ts = pd.Timestamp(raw)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            cutoffs.append(ts.tz_convert("UTC").tz_localize(None))
        if not cutoffs:
            return None
        return min(cutoffs)

    def fetch_latest(
        self,
        asset: str,
        timeframe: str,
        lookback: int = 500,
    ) -> pd.DataFrame:
        """Fetch the most recent N daily bars.

        Note: AV free tier caps ``compact`` at ~100 daily bars regardless of
        ``lookback``; the tail is whatever the free window allows.
        """
        if timeframe not in self.timeframes:
            raise DataProviderError(f"unknown timeframe {timeframe!r}")
        end = pd.Timestamp.utcnow()
        # Daily only; conservative start window (2x lookback days of slack).
        start = end - pd.Timedelta(days=lookback * 2)
        bars = self.fetch_bars(asset, timeframe, start, end, use_cache=False)
        return bars.tail(lookback).reset_index(drop=True)

    def health(self) -> dict:
        # NEVER log/return the key value — only whether one resolves.
        has_key = bool(self._api_key or os.environ.get(_API_KEY_ENV))
        return {
            "provider": self.name,
            "n_fetches": self._n_fetches,
            "n_errors": self._n_errors,
            "last_fetch_at": (self._last_fetch_at.isoformat() if self._last_fetch_at else None),
            "has_key": has_key,
        }
