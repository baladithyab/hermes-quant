"""hermes_quant.data.openbb_provider — OpenBB perception provider (aegis-ob1).

ADR-0100: OpenBB joins the data layer as a DEFAULT-OFF, asof-pinned,
host-blind OHLCV provider — the 2nd OHLCV tier behind yfinance in
``hermes_quant.data.base.fetch_with_chain``. It is the highest-value
OpenBB child (broad equity coverage via the OpenBB Platform's pluggable
provider backends).

Design (per ADR-0100 + the DataProvider Protocol seam):
- Source endpoint: ``obb.equity.price.historical(symbol, start_date,
  end_date, provider='fmp')`` — Financial Modeling Prep is the concrete
  2nd-tier OHLCV source the seed names. The OpenBB Platform returns an
  ``OBBject`` whose ``.to_dataframe()`` yields a date-indexed OHLCV frame;
  we map it to the canonical ``['timestamp','open','high','low','close',
  'volume']`` (timestamp a COLUMN, UTC) and run ``validate_bars``.
- NO-LOOKAHEAD (cardinal rail): the leaf-level ``as_of`` filter is applied
  AFTER ``validate_bars`` (which normalizes to tz-naive UTC), byte-identical
  to ``yfinance_provider`` / ``alphavantage_provider``. A bar with
  ``timestamp > as_of`` MUST be filtered out before return.
- LATEST-ONLY REJECTION: ``obb.equity.price.quote`` is a latest-only endpoint
  with NO asof honesty. It is HARD-REJECTED at the boundary — it is never
  wired into ``fetch_bars``, and ``fetch_quote`` raises in any asof context
  rather than silently returning a point-in-time-unsafe snapshot.
- >10-DAY STALENESS GUARD (TradingAgents parity, per ob00): if the newest
  returned bar is more than 10 days before the asof horizon (``as_of`` when
  set, else ``end``), that is a staleness signal. yfinance_provider has NO
  staleness precedent (it relies on ``validate_bars`` only), so per the
  fail-closed posture we WARN (never fabricate fresh bars) and return the
  stale-but-real bars; the warning is observable on the data log.
- DEFAULT-OFF: gated on ``HERMES_QUANT_OPENBB`` (default ``'0'``). The
  module imports without ``openbb`` installed (heavy/optional dep). The
  ``openbb`` SDK is lazy-imported ONLY inside the ``obb`` property — never
  at module top — so a venv lacking openbb is unaffected when the flag is
  off. A clear ImportError-with-guidance fires only if a fetch is actually
  attempted (flag on) and openbb isn't installed.
- Determinism: the returned DataFrame is a pure function of the OBBject
  body (no wall-clock in output). Health counters are side-channel only.

Optional-extras: install with ``pip install 'hermes-quant[openbb]'``.
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
)

logger = logging.getLogger(__name__)

# Default-OFF capability flag (ADR-0100). Read at construction-INTENT time
# (the `obb` property) so the module + class load with the flag unset and
# without openbb installed. A quoted-literal default so the flag-inventory
# scanner (ops/scripts/quant-flag-inventory.py) counts it.
OPENBB_ENABLE_FLAG = "HERMES_QUANT_OPENBB"

# FMP (Financial Modeling Prep) is the concrete 2nd-tier OHLCV source the
# seed names. OpenBB routes the historical call to it via provider='fmp'.
_DEFAULT_PROVIDER = "fmp"
_LATEST_ONLY_OPTIONS_CHAIN_PROVIDERS = frozenset({"cboe", "yfinance"})

# TradingAgents parity (ob00): a newest-bar more than this many days behind
# the asof horizon is a staleness signal. WARN, never fabricate.
_MAX_STALENESS_DAYS = 10

# OpenBB equity.price.historical supports daily + intraday; we expose the
# canonical equity timeframe set (daily is the FMP free-tier sweet spot).
_VALID_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "1d"]

# OpenBB interval translation. The Platform takes an `interval` kwarg; FMP's
# daily is the default. We pass through the canonical timeframe → OpenBB
# interval string.
_TF_TO_OBB_INTERVAL = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "1d": "1d",
}


class OpenBBProvider:
    """OpenBB Platform equity/ETF OHLCV provider (aegis-ob1, ADR-0100).

    Implements the canonical DataProvider Protocol signature
    ``fetch_bars(asset, timeframe, start, end, *, use_cache, as_of)`` so it
    drops straight into ``fetch_with_chain`` as the 2nd OHLCV tier behind
    yfinance.

    DEFAULT-OFF: gated on ``HERMES_QUANT_OPENBB``. With the flag unset the
    ``obb`` SDK is never imported — constructing the class and registering it
    in the entry-point/vendor table is byte-identical no-op (no openbb import
    attempted). The flag is enforced at the ``obb`` property (fetch time),
    NOT at construction, so discovery/registration never crash.

    Args:
        obb: test seam — inject an object exposing
            ``.equity.price.historical(symbol, start_date, end_date,
            provider, interval)`` (returns an OBBject with ``.to_dataframe()``
            or already a DataFrame) and ``.equity.price.quote(...)``. When
            None, the real ``openbb.obb`` is lazy-imported on first fetch
            (and only if the flag is on).
        provider: OpenBB backend provider routing key (default ``'fmp'``).
        require_flag: if True (default), fetches require
            ``HERMES_QUANT_OPENBB`` truthy. Set False only in tests that
            inject ``obb`` directly and don't want to touch the env.
    """

    name = "openbb"
    asset_classes = ["equity", "etf"]
    timeframes = list(_VALID_TIMEFRAMES)
    requires_credentials = True  # FMP backend needs an API key (resolved by OpenBB)

    def __init__(
        self,
        *,
        obb: Any = None,
        provider: str = _DEFAULT_PROVIDER,
        require_flag: bool = True,
    ):
        # Injected seam (None → lazy real import). NEVER import openbb here.
        self._obb: Any = obb
        self._provider = provider
        self._require_flag = require_flag
        self._n_fetches = 0
        self._n_errors = 0
        self._last_fetch_at: pd.Timestamp | None = None

    # ------------------------------------------------------------------
    # Flag + lazy SDK
    # ------------------------------------------------------------------
    @staticmethod
    def _flag_enabled() -> bool:
        return os.environ.get(OPENBB_ENABLE_FLAG, "0") not in ("", "0", "false", "False")

    @property
    def obb(self) -> Any:
        """Lazy-resolve the OpenBB SDK client.

        - If a seam was injected, return it (tests, never touches the env
          unless ``require_flag``).
        - Else enforce ``HERMES_QUANT_OPENBB`` (fail-closed: a clear
          DataProviderError when off) BEFORE attempting the heavy import.
        - Then lazy-import ``openbb`` with a clear ImportError-with-guidance
          if the flag is on but the SDK isn't installed (do NOT crash at
          module import time — only here, at actual fetch).
        """
        if self._require_flag and not self._flag_enabled():
            raise DataProviderError(
                f"OpenBB provider is disabled; set {OPENBB_ENABLE_FLAG}=1 to enable "
                "(default-OFF per ADR-0100)."
            )
        if self._obb is None:
            try:
                from openbb import obb as _obb  # lazy: optional heavy dep
            except ImportError as e:
                raise DataProviderError(
                    "openbb not installed but HERMES_QUANT_OPENBB is set; "
                    "install hermes-quant[openbb] (pip install 'hermes-quant[openbb]')."
                ) from e
            self._obb = _obb
        return self._obb

    # ------------------------------------------------------------------
    # fetch_bars (the canonical, asof-honest path)
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
        """Fetch OHLCV bars in [start, end] for ``asset`` at ``timeframe``.

        Routes through ``obb.equity.price.historical(provider='fmp')``, maps
        the OBBject to the canonical OHLCV frame, validates, then applies the
        leaf-level ``as_of`` no-lookahead filter AFTER validation (mirroring
        yfinance_provider exactly).

        Raises:
            DataProviderError: openbb missing / flag off / transient API error.
            DataQualityError: bars fail validation gates.
        """
        if timeframe not in self.timeframes:
            raise DataProviderError(
                f"timeframe {timeframe!r} not supported by openbb; options: {self.timeframes}"
            )
        obb_interval = _TF_TO_OBB_INTERVAL[timeframe]

        start_date = self._as_date_str(start)
        end_date = self._as_date_str(end)

        try:
            resp = self.obb.equity.price.historical(
                symbol=asset,
                start_date=start_date,
                end_date=end_date,
                provider=self._provider,
                interval=obb_interval,
            )
        except DataProviderError:
            raise
        except Exception as e:  # noqa: BLE001
            self._n_errors += 1
            msg = str(e).lower()
            if "rate" in msg or "429" in msg or "too many" in msg:
                from hermes_quant.protocol import RateLimitError

                raise RateLimitError(f"openbb throttled: {e}") from e
            raise DataProviderError(f"openbb fetch failed: {e}") from e

        raw = self._to_dataframe(resp)
        if raw is None or len(raw) == 0:
            raise DataQualityError(
                f"openbb returned empty bars for {asset} {timeframe} {start}..{end}"
            )

        out = self._map_canonical(raw)
        # validate_bars: tz-normalize to tz-naive UTC, drop NaN OHLC,
        # drop zero-volume, dedupe, sort, min-bars gate.
        validated = validate_bars(out)

        self._last_fetch_at = pd.Timestamp.utcnow()
        self._n_fetches += 1

        # ADR-0005 amendment (Wave C.1) — leaf-level lookahead enforcement.
        # Filter to as_of AFTER validation, comparing against the same
        # canonical tz-naive UTC frame an analyst would see. Identical
        # handling to yfinance_provider / alphavantage_provider.
        if as_of is not None:
            cutoff_naive = self._cutoff_naive(as_of)
            validated = validated[validated["timestamp"] <= cutoff_naive].reset_index(
                drop=True
            )

        # >10-day staleness guard (TradingAgents parity, ob00). yfinance has
        # no staleness precedent, so per the fail-closed posture we WARN and
        # return the real (stale) bars — we never fabricate fresh ones.
        self._staleness_warn(validated, as_of=as_of, end=end, asset=asset)

        return validated

    # ------------------------------------------------------------------
    # fetch_latest — daily/most-recent N bars (NO latest-only quote)
    # ------------------------------------------------------------------
    def fetch_latest(
        self,
        asset: str,
        timeframe: str,
        lookback: int = 500,
    ) -> pd.DataFrame:
        """Fetch the most recent N bars at ``timeframe``.

        Uses the asof-honest ``historical`` path (NOT ``quote``). The
        latest-only ``quote`` endpoint is hard-rejected (see ``fetch_quote``).
        """
        if timeframe not in self.timeframes:
            raise DataProviderError(f"unknown timeframe {timeframe!r}")
        tf_to_seconds = {
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "1d": 86400,
        }
        secs = tf_to_seconds.get(timeframe)
        if secs is None:
            raise DataProviderError(f"unknown timeframe {timeframe!r}")
        end = pd.Timestamp.utcnow()
        start = end - pd.Timedelta(seconds=secs * lookback * 2)
        bars = self.fetch_bars(asset, timeframe, start, end, use_cache=False)
        return bars.tail(lookback).reset_index(drop=True)

    # ------------------------------------------------------------------
    # LATEST-ONLY REJECTION (ADR-0100 no-lookahead rail)
    # ------------------------------------------------------------------
    def fetch_quote(self, asset: str, *, as_of: pd.Timestamp | None = None) -> Any:
        """HARD-REJECTED: ``obb.equity.price.quote`` is latest-only.

        The quote endpoint returns a point-in-time-UNSAFE snapshot with no
        asof honesty (it always reflects "now"). Returning it in any asof
        context would be a no-lookahead violation, so it is hard-rejected at
        the boundary — never silently returned. ADR-0100 latest-only
        HARD-REJECT.
        """
        raise DataProviderError(
            "openbb equity.price.quote is a latest-only endpoint with no asof "
            "honesty — HARD-REJECTED (ADR-0100 no-lookahead rail). Use fetch_bars "
            "(historical) for point-in-time-safe OHLCV."
        )

    def fetch_options_chain(
        self,
        asset: str,
        *,
        as_of: pd.Timestamp | None = None,
        provider: str = "cboe",
    ) -> Any:
        """HARD-REJECT unsafe OpenBB options-chain endpoints.

        ADR-0100 explicitly distinguishes asof-capable historical chain sources
        from latest-only chain sources. OpenBB's CBOE/yfinance chain routes are
        latest-only snapshots, so this boundary rejects them before any SDK call
        can leak current chain state into replay/eval. A future asof-capable
        implementation must be added as a dated provider path that writes or reads
        recorded snapshots.
        """
        provider_key = str(provider or "").strip().lower()
        if as_of is None:
            raise DataProviderError(
                "openbb options chain read requires an explicit as_of; an asof-less "
                "chain read is latest-only semantics and is HARD-REJECTED (ADR-0100)."
            )
        if provider_key in _LATEST_ONLY_OPTIONS_CHAIN_PROVIDERS:
            raise DataProviderError(
                f"openbb derivatives.options.chains via provider={provider_key!r} is "
                "latest-only with no asof honesty; use recorded ChainSnapshotReader "
                "data or an explicitly dated historical chain provider instead. "
                "ADR-0100 forbids latest-only options-chain reads in replay/backtest contexts."
            )
        raise DataProviderError(
            "OpenBB asof-capable options-chain ingestion is not wired yet; use the "
            "recorded ChainSnapshotReader path until a dated provider boundary is "
            "implemented and tested."
        )

    # ------------------------------------------------------------------
    # health
    # ------------------------------------------------------------------
    def health(self) -> dict:
        return {
            "provider": self.name,
            "enabled": self._flag_enabled(),
            "n_fetches": self._n_fetches,
            "n_errors": self._n_errors,
            "last_fetch_at": (
                self._last_fetch_at.isoformat() if self._last_fetch_at else None
            ),
            "openbb_loaded": self._obb is not None,
            "backend_provider": self._provider,
        }

    # ------------------------------------------------------------------
    # Mapping / helper internals
    # ------------------------------------------------------------------
    @staticmethod
    def _as_date_str(ts: pd.Timestamp) -> str:
        """OpenBB historical takes ISO date strings (YYYY-MM-DD)."""
        t = pd.Timestamp(ts)
        return t.strftime("%Y-%m-%d")

    @staticmethod
    def _cutoff_naive(as_of: pd.Timestamp) -> pd.Timestamp:
        """Normalize an as_of cutoff to tz-naive UTC to match validate_bars.

        validate_bars stores timestamps tz-naive UTC; comparing tz-aware vs
        tz-naive raises under pandas 2.x. Identical to yfinance_provider.
        """
        cutoff = pd.Timestamp(as_of)
        if cutoff.tzinfo is None:
            cutoff = cutoff.tz_localize("UTC")
        return cutoff.tz_convert("UTC").tz_localize(None)

    @staticmethod
    def _to_dataframe(resp: Any) -> pd.DataFrame:
        """Coerce an OpenBB OBBject (or already-a-DataFrame) to a DataFrame.

        The OpenBB Platform returns an ``OBBject`` exposing ``.to_dataframe()``.
        Tests may inject a bare DataFrame; accept both.
        """
        if resp is None:
            return pd.DataFrame()
        if isinstance(resp, pd.DataFrame):
            return resp
        to_df = getattr(resp, "to_dataframe", None)
        if callable(to_df):
            return to_df()
        # Some OBBject builds expose `.results` (list of pydantic rows).
        results = getattr(resp, "results", None)
        if results is not None:
            return pd.DataFrame([dict(r) for r in results])
        raise DataProviderError(
            "openbb response is neither a DataFrame nor an OBBject with "
            ".to_dataframe()/.results"
        )

    @staticmethod
    def _map_canonical(raw: pd.DataFrame) -> pd.DataFrame:
        """Map an OpenBB OHLCV frame to the canonical column set.

        OpenBB returns a date-INDEXED frame with lowercase columns
        ['open','high','low','close','volume'] (date is the index, named
        'date' or unnamed). We lift the index to a 'timestamp' COLUMN
        (canonical contract: timestamp is a column, not the index) and select
        the OHLCV columns. Column lookup is case-insensitive to tolerate
        provider casing drift.
        """
        df = raw.copy()

        # Build a case-insensitive column map.
        lower_to_actual = {str(c).lower(): c for c in df.columns}

        # Resolve the timestamp source: a 'date'/'timestamp' column if
        # present, else the index.
        ts_col = None
        for cand in ("timestamp", "date", "datetime"):
            if cand in lower_to_actual:
                ts_col = lower_to_actual[cand]
                break
        if ts_col is not None:
            timestamps = df[ts_col]
        else:
            # Date-indexed frame (the OpenBB default).
            timestamps = df.index

        def _col(name: str) -> Any:
            actual = lower_to_actual.get(name)
            if actual is None:
                raise DataProviderError(
                    f"openbb response missing OHLCV column {name!r}; "
                    f"columns={list(df.columns)}"
                )
            return df[actual].to_numpy()

        out = pd.DataFrame(
            {
                "timestamp": pd.Series(list(timestamps)),
                "open": _col("open"),
                "high": _col("high"),
                "low": _col("low"),
                "close": _col("close"),
                "volume": _col("volume"),
            }
        )
        return out

    def _staleness_warn(
        self,
        bars: pd.DataFrame,
        *,
        as_of: pd.Timestamp | None,
        end: pd.Timestamp,
        asset: str,
    ) -> None:
        """WARN (never fabricate) if the newest bar is >10 days stale.

        TradingAgents parity (ob00). The asof horizon is ``as_of`` when set,
        else ``end``. yfinance_provider has no staleness precedent, so per the
        fail-closed posture we surface a warning on the data log and return
        the real (stale) bars; we never synthesize fresh data.
        """
        if bars is None or len(bars) == 0:
            return
        horizon = as_of if as_of is not None else end
        try:
            horizon_naive = self._cutoff_naive(pd.Timestamp(horizon))
            newest = pd.Timestamp(bars["timestamp"].iloc[-1])
            age_days = (horizon_naive - newest).total_seconds() / 86400.0
        except Exception:  # noqa: BLE001 — never let a warn path raise
            return
        if age_days > _MAX_STALENESS_DAYS:
            logger.warning(
                "openbb staleness: newest bar for %s is %.1f days before asof "
                "horizon (>%d-day guard, ob00) — returning real stale bars, NOT "
                "fabricating fresh ones",
                asset,
                age_days,
                _MAX_STALENESS_DAYS,
            )
