"""hermes_quant.data.fred_macro — FRED macro economic SERIES (aegis-ob4).

ADR-0100 (ob4): the macro toolset TradingAgents leans on — the actual SERIES
VALUES of the policy/rate/inflation/labor series over time, point-in-time-honest:

  FEDFUNDS    effective federal funds rate
  DGS2 / DGS10 / DGS30   2y / 10y / 30y constant-maturity Treasury yields
  T10Y2Y      10y-2y term spread (the yield-curve inversion signal)
  CPIAUCSL    CPI-U, all items (headline inflation level)
  UNRATE      civilian unemployment rate
  M2SL        M2 money stock
  VIXCLS      CBOE VIX (implied volatility)

This is DISTINCT from ``catalyst.calendar.ingest_fred_releases`` — that ingests
the FRED *release CALENDAR* (when prints are scheduled). ob4 adds the SERIES
VALUES (e.g. the actual FEDFUNDS level history). We reuse the calendar's
``FRED_API_KEY`` resolution posture (env, never logged, key-absent => silent /
fail-closed) but do NOT touch the calendar path.

NO-LOOKAHEAD (the cardinal rail) — TWO date axes
--------------------------------------------------
Every FRED observation has TWO dates and BOTH must be <= asof:

  * the OBSERVATION date (``date``) — the period the value describes; and
  * the RELEASE/VINTAGE date (``realtime_start``) — when that value first
    became publicly knowable (ALFRED vintage semantics).

For PUBLISHED-DAILY series (VIX, the DGS* rates) the observation date IS
effectively the knowable date — ``date <= asof`` is the binding bound. For
LAGGED series (CPI, UNRATE, M2) the release lag is the hazard: the May CPI
*value* carries an observation date in May but is not RELEASED until mid-June,
so a read at, say, 2026-06-01 must NOT see the May print. We therefore keep a
row iff BOTH ``date <= asof`` AND ``release_date <= asof``. Dropping the
release-date half leaks the not-yet-released print — the exact lookahead the
eval gate exists to catch.

WINDOW-PINNED (documented lookback)
-----------------------------------
A series read at ``asof`` returns only observations within the documented
lookback window ``[asof - lookback, asof]`` (default 730 days). Combined with
the dual-date <= asof filter, the read is a pure function of (series, asof,
lookback) — no wall-clock leaks into the output.

DEFAULT-OFF
-----------
Gated on ``HERMES_QUANT_FRED_MACRO`` (default ``'0'``) for the macro-SERIES
source; OpenBB-sourced reads ALSO require ``HERMES_QUANT_OPENBB`` (we reuse
ob1's OpenBB flag for the ``obb.economy`` route). With the flags unset NO FRED
HTTP call and NO openbb import happen — byte-identical no-op. The ``openbb``
SDK is lazy-imported ONLY inside the ``obb`` property (never at module top).

FAIL-CLOSED
-----------
Flag ON but ``FRED_API_KEY`` absent (and no openbb route / injected client) =>
a clear ``DataProviderError`` — we NEVER fabricate a series. A dead/erroring
feed raises (a macro series silently returning empty would be read as "no
signal", which is a fail-open lie about the rate environment).

FINITE-GUARD
------------
Every observation value is coerced through ``_coerce_float`` (NaN for missing /
non-finite "." sentinel / inf). A non-finite value is DROPPED, never laundered
into a finite number that could defeat a downstream ``<=`` sanity gate.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import numpy as np
import pandas as pd

from hermes_quant.protocol import DataProviderError

logger = logging.getLogger(__name__)

# Default-OFF capability flag (ADR-0100, ob4) for the macro-SERIES source. A
# quoted-literal default so the flag-inventory scanner counts it. The constant
# name carries FLAG so the scanner's _CONST regex binds it.
FRED_MACRO_ENABLE_FLAG = "HERMES_QUANT_FRED_MACRO"

# Reuse ob1's OpenBB vendor flag for the obb.economy route (the macro series
# rides the SAME OpenBB enablement toggle when sourced via OpenBB).
OPENBB_ENABLE_FLAG = "HERMES_QUANT_OPENBB"

# FRED series/observations REST endpoint (ALFRED vintage-aware). We pass
# realtime_start/realtime_end so FRED itself returns vintage-correct values;
# we ALSO filter defensively on the per-row release date we map out.
_FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"

_DEFAULT_TIMEOUT = 15.0

# Documented default lookback window (days). A read at asof returns only
# observations in [asof - lookback, asof].
_DEFAULT_LOOKBACK_DAYS = 730

# The TradingAgents macro toolset — canonical series IDs. PUBLISHED-DAILY
# (date is the knowable bound) vs LAGGED (release date is the binding bound).
DAILY_SERIES: tuple[str, ...] = ("FEDFUNDS", "DGS2", "DGS10", "DGS30", "T10Y2Y", "VIXCLS")
LAGGED_SERIES: tuple[str, ...] = ("CPIAUCSL", "UNRATE", "M2SL")
MACRO_SERIES: tuple[str, ...] = DAILY_SERIES + LAGGED_SERIES


def _coerce_float(x: Any) -> float:
    """Coerce a value to float; NaN if missing / non-finite / FRED '.' sentinel."""
    if x is None:
        return float("nan")
    if isinstance(x, str) and x.strip() in ("", "."):
        # FRED encodes a missing observation as the string ".".
        return float("nan")
    try:
        f = float(x)
    except (TypeError, ValueError, OverflowError):
        return float("nan")
    return f if np.isfinite(f) else float("nan")


def _normalize_asof(as_of: pd.Timestamp) -> pd.Timestamp:
    """Normalize an as_of cutoff to a tz-naive Timestamp for safe comparison.

    FRED date columns are date-like (no tz); comparing tz-aware vs tz-naive
    raises under pandas 2.x. Strip tz so comparison against the naive
    observation/release date columns never raises.
    """
    ts = pd.Timestamp(as_of)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts


def _fred_api_key() -> str | None:
    """Return FRED_API_KEY from the env, or None when absent/blank.

    The key is never logged. Mirrors ``catalyst.calendar._fred_api_key`` exactly
    so the macro-series source reuses the calendar's key resolution posture.
    """
    key = (os.environ.get("FRED_API_KEY") or "").strip()
    return key or None


def _macro_flag_enabled() -> bool:
    """True iff HERMES_QUANT_FRED_MACRO is set truthy (default-OFF)."""
    return os.environ.get(FRED_MACRO_ENABLE_FLAG, "0") not in ("", "0", "false", "False")


def _openbb_flag_enabled() -> bool:
    """True iff HERMES_QUANT_OPENBB is set truthy (default-OFF)."""
    return os.environ.get(OPENBB_ENABLE_FLAG, "0") not in ("", "0", "false", "False")


def _http_fetch(url: str, timeout: float) -> bytes:
    import urllib.request

    req = urllib.request.Request(
        url, headers={"User-Agent": "hermes-quant fred-macro; research"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed FRED host
        return resp.read()


class FredMacroProvider:
    """Point-in-time-honest FRED macro economic SERIES reader (ob4, ADR-0100).

    ``read_series(series_id, as_of, lookback_days)`` returns a tz-naive,
    date-sorted DataFrame ``['date', 'value', 'release_date']`` of the
    observations of ``series_id`` that were publicly KNOWABLE by ``as_of`` and
    fall within the documented lookback window. BOTH the observation date and
    the release/vintage date must be ``<= as_of`` (the release-lag rail).

    Sourcing:
      * ``obb.economy`` route when an ``obb`` client is injected / available
        AND ``HERMES_QUANT_OPENBB`` is on (pinnable via OpenBB's FRED backend);
      * else the direct FRED ``series/observations`` REST endpoint with
        ``FRED_API_KEY`` (ALFRED vintage params ``realtime_*`` pin the read).

    DEFAULT-OFF: gated on ``HERMES_QUANT_FRED_MACRO``. With the flag unset
    ``read_series`` fails closed BEFORE any HTTP call or openbb import — a
    byte-identical no-op for a venv without openbb and without a key.

    Args:
        obb: test/route seam — an object exposing ``.economy.fred_series`` (or
            a compatible call) returning rows with date/value/realtime_start.
            When provided AND the OpenBB flag is on, the obb route is used.
        fetcher: injectable ``fetcher(url, timeout) -> bytes`` for the direct
            FRED REST route (offline tests). Defaults to a urllib GET.
        api_key: explicit FRED key (else resolved from ``FRED_API_KEY``).
        require_flag: if True (default), reads require
            ``HERMES_QUANT_FRED_MACRO`` truthy. Set False only in offline tests
            that inject a fetcher and don't want to touch the env.
    """

    name = "fred_macro"

    def __init__(
        self,
        *,
        obb: Any = None,
        fetcher: Any = None,
        api_key: str | None = None,
        require_flag: bool = True,
    ):
        # Injected seams (None -> resolve real source). NEVER import openbb here.
        self._obb: Any = obb
        self._fetcher = fetcher
        self._api_key = api_key
        self._require_flag = require_flag

    # ------------------------------------------------------------------
    # Flag gate + lazy OpenBB SDK
    # ------------------------------------------------------------------
    @property
    def obb(self) -> Any:
        """Lazy-resolve the OpenBB SDK client for the economy route.

        Enforces ``HERMES_QUANT_OPENBB`` BEFORE the heavy import (fail-closed),
        then lazy-imports ``openbb`` with a guided ImportError if the flag is on
        but the SDK isn't installed. Never imported at module top.
        """
        if not _openbb_flag_enabled():
            raise DataProviderError(
                f"OpenBB route disabled; set {OPENBB_ENABLE_FLAG}=1 to enable "
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
    # read_series — the asof-honest, window-pinned read
    # ------------------------------------------------------------------
    def read_series(
        self,
        series_id: str,
        *,
        as_of: pd.Timestamp | None = None,
        lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    ) -> pd.DataFrame:
        """Read asof-honest, window-pinned observations of ``series_id``.

        Returns a tz-naive DataFrame ``['date', 'value', 'release_date']`` sorted
        by ``date`` ascending. Only rows with BOTH ``date <= as_of`` AND
        ``release_date <= as_of`` AND ``date >= as_of - lookback_days`` AND a
        FINITE value survive.

        Raises:
            DataProviderError: ``as_of`` is None (an asof-less macro read is
                latest-only semantics — HARD-REJECTED), the macro flag is off,
                no source is resolvable (no openbb route + no FRED_API_KEY),
                openbb missing, or a transient API error.
        """
        if as_of is None:
            raise DataProviderError(
                "FredMacroProvider.read_series requires an explicit as_of (an "
                "asof-less macro read is latest-only — HARD-REJECTED, ADR-0100 "
                "no-lookahead rail)."
            )
        if self._require_flag and not _macro_flag_enabled():
            raise DataProviderError(
                f"FRED macro series source is disabled; set {FRED_MACRO_ENABLE_FLAG}=1 "
                "to enable (default-OFF per ADR-0100)."
            )

        cutoff = _normalize_asof(as_of)
        window_start = cutoff - pd.Timedelta(days=int(lookback_days))

        raw = self._fetch_raw_observations(series_id, cutoff=cutoff, window_start=window_start)
        mapped = self._map_canonical(raw)

        # NO-LOOKAHEAD (cardinal) — BOTH date axes <= asof. A NaT on EITHER axis
        # is not provably knowable-by-asof -> drop (fail-closed). The
        # release_date half is what blocks the not-yet-released lagged print.
        keep_dates = (
            mapped["date"].notna()
            & mapped["release_date"].notna()
            & (mapped["date"] <= cutoff)
            & (mapped["release_date"] <= cutoff)
        )
        # WINDOW-PINNED — within the documented lookback.
        keep_window = mapped["date"] >= window_start
        # FINITE-GUARD — a non-finite value is dropped, never laundered.
        keep_finite = mapped["value"].apply(lambda v: np.isfinite(_coerce_float(v)))

        out = mapped[keep_dates & keep_window & keep_finite].copy()
        out = out.sort_values("date").reset_index(drop=True)
        return out

    def read_macro_panel(
        self,
        *,
        as_of: pd.Timestamp | None = None,
        lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
        series: tuple[str, ...] = MACRO_SERIES,
    ) -> dict[str, pd.DataFrame]:
        """Read the full TradingAgents macro panel asof-honest.

        Returns ``{series_id: read_series(series_id, as_of, lookback_days)}``.
        A per-series failure is NOT swallowed silently — it raises (a macro
        series quietly missing is a fail-open lie about the rate environment).
        """
        return {
            sid: self.read_series(sid, as_of=as_of, lookback_days=lookback_days)
            for sid in series
        }

    # ------------------------------------------------------------------
    # Source resolution: obb.economy route OR direct FRED REST
    # ------------------------------------------------------------------
    def _fetch_raw_observations(
        self, series_id: str, *, cutoff: pd.Timestamp, window_start: pd.Timestamp
    ) -> Any:
        """Fetch raw observation rows for ``series_id`` via obb OR direct FRED.

        Prefer the OpenBB economy route when an obb client is injected/available
        AND the OpenBB flag is on; else fall back to the direct FRED REST
        endpoint keyed by ``FRED_API_KEY``. With NO route resolvable (no obb +
        no key) raise a clear fail-closed error — never fabricate.
        """
        # OpenBB route — only when the OpenBB flag is on (else the obb property
        # fails closed). An injected obb seam also requires the flag (tests that
        # want the obb route set HERMES_QUANT_OPENBB=1; tests using the direct
        # FRED route inject a fetcher and leave the OpenBB flag off).
        if self._obb is not None and _openbb_flag_enabled():
            try:
                resp = self.obb.economy.fred_series(
                    symbol=series_id,
                    start_date=window_start.strftime("%Y-%m-%d"),
                    end_date=cutoff.strftime("%Y-%m-%d"),
                )
            except DataProviderError:
                raise
            except Exception as e:  # noqa: BLE001
                raise DataProviderError(
                    f"openbb economy.fred_series failed for {series_id}: {e}"
                ) from e
            return resp

        # Direct FRED REST route — needs a key.
        key = self._api_key or _fred_api_key()
        if not key:
            raise DataProviderError(
                "FRED macro series source is enabled but FRED_API_KEY is absent "
                "and no OpenBB route is available — cannot read a series "
                "(fail-closed: we NEVER fabricate a macro series)."
            )

        import urllib.parse

        # ALFRED vintage params: realtime_end=asof pins the read to values
        # KNOWN by asof (so FRED itself withholds not-yet-released prints).
        params = urllib.parse.urlencode(
            {
                "series_id": series_id,
                "api_key": key,
                "file_type": "json",
                "observation_start": window_start.strftime("%Y-%m-%d"),
                "observation_end": cutoff.strftime("%Y-%m-%d"),
                "realtime_start": window_start.strftime("%Y-%m-%d"),
                "realtime_end": cutoff.strftime("%Y-%m-%d"),
            }
        )
        url = f"{_FRED_OBSERVATIONS_URL}?{params}"
        fetch = self._fetcher or _http_fetch
        try:
            raw = fetch(url, _DEFAULT_TIMEOUT)
        except DataProviderError:
            raise
        except Exception as e:  # noqa: BLE001
            # A dead feed must NOT silently return empty (read as "no signal" =
            # a fail-open lie about the macro environment). Raise (key redacted).
            raise DataProviderError(
                f"FRED series fetch failed for {series_id}: {e}"
            ) from e
        return raw

    # ------------------------------------------------------------------
    # Canonical mapping
    # ------------------------------------------------------------------
    @staticmethod
    def _to_rows(resp: Any) -> list[dict]:
        """Coerce a FRED REST payload / OBBject / DataFrame to a list of dict rows.

        FRED REST returns ``{"observations": [{date, value, realtime_start,
        realtime_end}, ...]}``. OpenBB returns an OBBject (``.to_dataframe()`` /
        ``.results``) or a DataFrame. Tests may inject any of these.
        """
        if resp is None:
            return []
        # bytes / str JSON payload (direct FRED REST).
        if isinstance(resp, (bytes, str)):
            try:
                obj = json.loads(
                    resp.decode("utf-8", "replace") if isinstance(resp, bytes) else resp
                )
            except (ValueError, AttributeError) as e:
                raise DataProviderError(f"FRED JSON parse error: {e}") from e
            if isinstance(obj, dict):
                obs = obj.get("observations")
                return list(obs) if isinstance(obs, list) else []
            return []
        # already a dict payload.
        if isinstance(resp, dict):
            obs = resp.get("observations")
            return list(obs) if isinstance(obs, list) else []
        # DataFrame.
        if isinstance(resp, pd.DataFrame):
            return resp.to_dict("records")
        # OBBject (.to_dataframe() / .results).
        to_df = getattr(resp, "to_dataframe", None)
        if callable(to_df):
            return to_df().to_dict("records")
        results = getattr(resp, "results", None)
        if results is not None:
            return [dict(r) for r in results]
        raise DataProviderError(
            "FRED/openbb response is neither JSON, dict, DataFrame, nor an "
            "OBBject with .to_dataframe()/.results"
        )

    @classmethod
    def _map_canonical(cls, resp: Any) -> pd.DataFrame:
        """Map raw observation rows to the canonical ['date','value','release_date'].

        The release/vintage date is resolved case-insensitively across the FRED
        REST shape (``realtime_start``) and OpenBB drift (``release_date`` /
        ``vintage_date``). A row missing a release date keeps NaT (the
        no-lookahead filter then drops it: not-provably-public -> drop).
        """
        rows = cls._to_rows(resp)
        if not rows:
            return cls._empty()

        dates: list[Any] = []
        values: list[Any] = []
        releases: list[Any] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            lm = {str(k).lower(): v for k, v in r.items()}
            dates.append(lm.get("date"))
            values.append(_coerce_float(lm.get("value")))
            rel = (
                lm.get("realtime_start")
                if lm.get("realtime_start") is not None
                else lm.get("release_date")
                if lm.get("release_date") is not None
                else lm.get("vintage_date")
            )
            releases.append(rel)

        date_ser = pd.to_datetime(pd.Series(dates), errors="coerce")
        rel_ser = pd.to_datetime(pd.Series(releases), errors="coerce")
        date_ser = cls._strip_tz(date_ser)
        rel_ser = cls._strip_tz(rel_ser)

        return pd.DataFrame(
            {"date": date_ser, "value": values, "release_date": rel_ser}
        )

    @staticmethod
    def _strip_tz(ser: pd.Series) -> pd.Series:
        if getattr(ser.dt, "tz", None) is not None:
            return ser.dt.tz_convert("UTC").dt.tz_localize(None)
        return ser

    @staticmethod
    def _empty() -> pd.DataFrame:
        return pd.DataFrame(
            {
                "date": pd.to_datetime([]),
                "value": pd.Series([], dtype="float64"),
                "release_date": pd.to_datetime([]),
            }
        )


__all__ = [
    "FredMacroProvider",
    "FRED_MACRO_ENABLE_FLAG",
    "OPENBB_ENABLE_FLAG",
    "MACRO_SERIES",
    "DAILY_SERIES",
    "LAGGED_SERIES",
]
