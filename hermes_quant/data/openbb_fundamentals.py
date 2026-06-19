"""hermes_quant.data.openbb_fundamentals — OpenBB fundamentals + estimates (aegis-ob2).

ADR-0100 (ob2): extend the OpenBB perception coverage from OHLCV (ob1) to
FUNDAMENTALS and forward ANALYST ESTIMATES. Two providers live here:

  * ``OpenBBFundamentals`` — a 2nd fundamentals VENDOR behind yfinance
    (``FundamentalsProvider``). Sourced from ``obb.equity.fundamental.*``
    (metrics / balance / income via ``provider='fmp'``). It is a
    FALLBACK / 2nd-tier: yfinance stays primary; OpenBB serves when yfinance
    is missing/stale (or as configured). Rows are mapped to the SAME
    fundamentals columns ``FundamentalsProvider`` already uses, so the
    ``FundamentalsAnalyst`` can consume either vendor.

  * ``OpenBBEstimates`` — a NEW data type: forward analyst estimates from
    ``obb.equity.estimates.historical`` / ``price_target``. Each estimate row
    carries its own publish/as-of date.

FILTER-AT-SOURCE date honesty (NO-LOOKAHEAD — the cardinal rail)
----------------------------------------------------------------
Both providers require an explicit ``as_of`` (a point-in-time read) and apply
the date filter AT SOURCE before returning any row:

  * Fundamentals: keep a row iff ``period_ending <= as_of`` AND
    ``filing_date (accepted_date) <= as_of``. A RESTATEMENT filed AFTER asof —
    even for a fiscal period that ended before asof — was NOT publicly knowable
    at asof and MUST be dropped. Filtering on period_ending alone leaks the
    late restatement.
  * Estimates: keep a row iff its publish ``date <= as_of``. A consensus
    revised after asof is a leak.

LATEST-ONLY HARD-REJECT (ADR-0100 no-lookahead rail)
----------------------------------------------------
yfinance's ``info`` consensus and OpenBB's ``estimates.consensus`` are
latest-only snapshots with NO as-of honesty — they always reflect "now".
Returning one in an asof context is a no-lookahead violation, so it is
HARD-REJECTED at the boundary: ``read_consensus`` RAISES (never silently
returns a latest-only snapshot), and a read with ``as_of=None`` also raises
(an asof-less read of point-in-time data is latest-only semantics).

DEFAULT-OFF
-----------
Both providers are gated on ``HERMES_QUANT_OPENBB`` (default ``'0'``), reusing
ob1's flag. The ``openbb`` SDK is lazy-imported ONLY inside the ``obb``
property (never at module top), so a venv lacking openbb is unaffected when
the flag is off — byte-identical no-op. A clear ImportError-with-guidance
fires only if a fetch is attempted (flag on) and openbb isn't installed.

FINITE-GUARD
------------
Every numeric column is coerced through ``_coerce_float`` (NaN for missing /
non-finite). An inf/NaN value can never be laundered into a finite number that
defeats a downstream ``<=`` sanity gate.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import numpy as np
import pandas as pd

from hermes_quant.protocol import DataProviderError

logger = logging.getLogger(__name__)

# Default-OFF capability flag (ADR-0100). Reuse ob1's OpenBB vendor flag — the
# fundamentals/estimates coverage rides the SAME OpenBB enablement toggle. A
# quoted-literal default so the flag-inventory scanner counts it.
OPENBB_ENABLE_FLAG = "HERMES_QUANT_OPENBB"

# FMP (Financial Modeling Prep) is the concrete OpenBB backend the ob1 seed
# named; it exposes fundamentals + estimates with filing/publish dates.
_DEFAULT_PROVIDER = "fmp"

# The canonical fundamentals numeric columns (a subset of FundamentalsProvider's
# _SNAPSHOT_COLUMNS) the FundamentalsAnalyst scores. Mapped 1:1 so either vendor
# is consumable. OpenBB / FMP casing drift is tolerated case-insensitively.
_FUNDAMENTAL_NUMERIC_COLS = (
    "pe_trailing",
    "pe_forward",
    "debt_to_equity",
    "free_cash_flow",
    "revenue_ttm",
    "eps_trailing",
    "eps_forward",
    "revenue_yoy",
    "fcf_yoy",
)
_FUNDAMENTAL_STR_COLS = ("sector", "currency", "quote_type")

# The estimates numeric columns (forward analyst consensus). eps_avg is the
# load-bearing one (revision direction); the others are context.
_ESTIMATE_NUMERIC_COLS = ("eps_avg", "eps_prior", "revenue_avg", "analyst_count")


def _coerce_float(x: Any) -> float:
    """Coerce a value to float; NaN if missing / non-finite (finite-guard)."""
    if x is None:
        return float("nan")
    try:
        f = float(x)
    except (TypeError, ValueError, OverflowError):
        return float("nan")
    return f if np.isfinite(f) else float("nan")


def _normalize_asof(as_of: pd.Timestamp) -> pd.Timestamp:
    """Normalize an as_of cutoff to a tz-naive Timestamp for safe comparison.

    The OpenBB date columns are date-like (no tz); comparing tz-aware vs
    tz-naive raises under pandas 2.x. Strip tz to compare against the naive
    period_ending / filing_date / publish-date columns.
    """
    ts = pd.Timestamp(as_of)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts


class _OpenBBBase:
    """Shared flag-gate + lazy-SDK + OBBject-coercion plumbing (ob2)."""

    def __init__(
        self,
        *,
        obb: Any = None,
        provider: str = _DEFAULT_PROVIDER,
        require_flag: bool = True,
    ):
        # Injected seam (None -> lazy real import). NEVER import openbb here.
        self._obb: Any = obb
        self._provider = provider
        self._require_flag = require_flag

    @staticmethod
    def _flag_enabled() -> bool:
        return os.environ.get(OPENBB_ENABLE_FLAG, "0") not in ("", "0", "false", "False")

    @property
    def obb(self) -> Any:
        """Lazy-resolve the OpenBB SDK client (fail-closed when flag off).

        Mirrors ob1's OpenBBProvider.obb: enforce the flag BEFORE the heavy
        import, then lazy-import with a guided ImportError if the flag is on
        but the SDK isn't installed.
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

    @staticmethod
    def _to_dataframe(resp: Any) -> pd.DataFrame:
        """Coerce an OpenBB OBBject (or already-a-DataFrame) to a DataFrame."""
        if resp is None:
            return pd.DataFrame()
        if isinstance(resp, pd.DataFrame):
            return resp
        to_df = getattr(resp, "to_dataframe", None)
        if callable(to_df):
            return to_df()
        results = getattr(resp, "results", None)
        if results is not None:
            return pd.DataFrame([dict(r) for r in results])
        raise DataProviderError(
            "openbb response is neither a DataFrame nor an OBBject with "
            ".to_dataframe()/.results"
        )

    @staticmethod
    def _lower_map(df: pd.DataFrame) -> dict[str, Any]:
        return {str(c).lower(): c for c in df.columns}

    @staticmethod
    def _resolve_col(lower_map: dict[str, Any], *candidates: str) -> Any | None:
        for cand in candidates:
            if cand in lower_map:
                return lower_map[cand]
        return None


class OpenBBFundamentals(_OpenBBBase):
    """OpenBB fundamentals provider — the 2nd vendor behind yfinance (ob2).

    ``read_fundamentals(ticker, as_of)`` returns a DataFrame of fundamentals
    rows (one per fiscal period), filtered AT SOURCE to only rows knowable by
    ``as_of`` (``period_ending <= as_of`` AND ``filing_date <= as_of``), mapped
    to the canonical FundamentalsProvider column set. This is the asof-honest
    read the FundamentalsAnalyst can consume as a fallback for yfinance.

    DEFAULT-OFF (``HERMES_QUANT_OPENBB``): with the flag unset ``read_*`` fails
    closed at the ``obb`` property before the lazy import — byte-identical no-op.
    """

    name = "openbb_fundamentals"

    def read_fundamentals(
        self, ticker: str, *, as_of: pd.Timestamp | None = None
    ) -> pd.DataFrame:
        """Read asof-honest fundamentals rows for ``ticker``.

        Raises:
            DataProviderError: as_of is None (latest-only otherwise), flag off,
                openbb missing, or transient API error.
        """
        if as_of is None:
            raise DataProviderError(
                "OpenBBFundamentals.read_fundamentals requires an explicit as_of "
                "(an asof-less read is latest-only — HARD-REJECTED, ADR-0100 "
                "no-lookahead rail)."
            )
        cutoff = _normalize_asof(as_of)

        try:
            resp = self.obb.equity.fundamental.metrics(
                symbol=ticker,
                provider=self._provider,
            )
        except DataProviderError:
            raise
        except Exception as e:  # noqa: BLE001
            raise DataProviderError(f"openbb fundamentals fetch failed: {e}") from e

        raw = self._to_dataframe(resp)
        if raw is None or len(raw) == 0:
            return self._empty_fundamentals()

        mapped = self._map_canonical(raw)
        # FILTER-AT-SOURCE no-lookahead: BOTH period_ending <= asof AND
        # filing_date <= asof must hold. A row whose filing_date is NaT is NOT
        # provably public-by-asof -> drop it (fail-closed: a missing filing date
        # is treated as not-yet-knowable, never silently admitted).
        keep = (mapped["period_ending"] <= cutoff) & (mapped["filing_date"] <= cutoff)
        out = mapped[keep].reset_index(drop=True)
        return out

    def _map_canonical(self, raw: pd.DataFrame) -> pd.DataFrame:
        """Map an OpenBB fundamentals frame to the canonical column set.

        The point-in-time honesty columns ``period_ending`` and ``filing_date``
        are resolved case-insensitively across OpenBB/FMP casing drift
        (``filing_date`` / ``accepted_date`` / ``date``). Numeric columns are
        finite-guarded (inf/NaN -> NaN). Missing numeric columns become NaN.
        """
        df = raw.copy()
        lm = self._lower_map(df)

        period_col = self._resolve_col(
            lm, "period_ending", "period_end", "calendar_year", "fiscal_period_end"
        )
        filing_col = self._resolve_col(
            lm, "filing_date", "accepted_date", "filling_date", "date"
        )
        if period_col is None:
            raise DataProviderError(
                f"openbb fundamentals missing a period_ending column; "
                f"columns={list(df.columns)}"
            )
        if filing_col is None:
            raise DataProviderError(
                f"openbb fundamentals missing a filing_date/accepted_date column; "
                f"columns={list(df.columns)}"
            )

        cols: dict[str, Any] = {
            "period_ending": pd.to_datetime(df[period_col], errors="coerce"),
            "filing_date": pd.to_datetime(df[filing_col], errors="coerce"),
        }
        # Strip any tz on the date columns so comparison against the tz-naive
        # cutoff never raises.
        for c in ("period_ending", "filing_date"):
            ser = cols[c]
            if getattr(ser.dt, "tz", None) is not None:
                cols[c] = ser.dt.tz_convert("UTC").dt.tz_localize(None)

        n = len(df)
        for col in _FUNDAMENTAL_NUMERIC_COLS:
            actual = lm.get(col)
            if actual is None:
                cols[col] = [float("nan")] * n
            else:
                cols[col] = [_coerce_float(v) for v in df[actual].tolist()]
        for col in _FUNDAMENTAL_STR_COLS:
            actual = lm.get(col)
            if actual is None:
                cols[col] = [""] * n
            else:
                cols[col] = [str(v) if v is not None else "" for v in df[actual].tolist()]

        return pd.DataFrame(cols)

    @staticmethod
    def _empty_fundamentals() -> pd.DataFrame:
        data: dict[str, Any] = {
            "period_ending": pd.to_datetime([]),
            "filing_date": pd.to_datetime([]),
        }
        for col in _FUNDAMENTAL_NUMERIC_COLS:
            data[col] = []
        for col in _FUNDAMENTAL_STR_COLS:
            data[col] = []
        return pd.DataFrame(data)


class OpenBBEstimates(_OpenBBBase):
    """OpenBB forward-analyst-estimates provider — a NEW data type (ob2).

    ``read_estimates(ticker, as_of)`` returns a DataFrame of forward estimate
    rows from ``obb.equity.estimates.historical`` (each stamped with its publish
    ``date``), filtered AT SOURCE to ``date <= as_of``. Finite-guarded: a row
    whose key numeric (eps_avg) is NaN/inf is dropped.

    ``read_consensus`` is HARD-REJECTED: the latest-only consensus endpoint has
    NO as-of honesty (always reflects "now"), so it raises in any asof context.

    DEFAULT-OFF (``HERMES_QUANT_OPENBB``).
    """

    name = "openbb_estimates"

    def read_estimates(
        self, ticker: str, *, as_of: pd.Timestamp | None = None
    ) -> pd.DataFrame:
        """Read asof-honest forward estimate rows for ``ticker``.

        Raises:
            DataProviderError: as_of is None (latest-only otherwise), flag off,
                openbb missing, or transient API error.
        """
        if as_of is None:
            raise DataProviderError(
                "OpenBBEstimates.read_estimates requires an explicit as_of (an "
                "asof-less read is latest-only — HARD-REJECTED, ADR-0100 "
                "no-lookahead rail)."
            )
        cutoff = _normalize_asof(as_of)

        try:
            resp = self.obb.equity.estimates.historical(
                symbol=ticker,
                provider=self._provider,
            )
        except DataProviderError:
            raise
        except Exception as e:  # noqa: BLE001
            raise DataProviderError(f"openbb estimates fetch failed: {e}") from e

        raw = self._to_dataframe(resp)
        if raw is None or len(raw) == 0:
            return self._empty_estimates()

        mapped = self._map_canonical(raw)
        # FILTER-AT-SOURCE no-lookahead: publish date <= asof. A NaT publish date
        # is NOT provably public-by-asof -> drop (fail-closed).
        keep_date = mapped["date"].notna() & (mapped["date"] <= cutoff)
        # FINITE-GUARD: drop any row whose load-bearing numeric (eps_avg) is
        # NaN/inf — it can never drive a revision view.
        keep_finite = mapped["eps_avg"].apply(lambda v: np.isfinite(_coerce_float(v)))
        out = mapped[keep_date & keep_finite].reset_index(drop=True)
        return out

    def read_consensus(self, ticker: str, *, as_of: pd.Timestamp | None = None) -> Any:
        """HARD-REJECTED: the consensus endpoint is latest-only.

        yfinance/OpenBB consensus is a point-in-time-UNSAFE snapshot with no
        as-of honesty (it always reflects "now"). Returning it in an asof
        context is a no-lookahead violation -> hard-rejected at the boundary.
        """
        raise DataProviderError(
            "estimates consensus is a latest-only endpoint with no asof honesty "
            "— HARD-REJECTED (ADR-0100 no-lookahead rail). Use read_estimates "
            "(historical, publish-date filtered) for point-in-time-safe estimates."
        )

    def _map_canonical(self, raw: pd.DataFrame) -> pd.DataFrame:
        """Map an OpenBB estimates frame to the canonical estimate columns.

        The publish date is resolved case-insensitively (``date`` /
        ``published_date`` / ``as_of_date``). Numeric columns are
        finite-guarded.
        """
        df = raw.copy()
        lm = self._lower_map(df)

        date_col = self._resolve_col(
            lm, "date", "published_date", "publish_date", "as_of_date"
        )
        if date_col is None:
            raise DataProviderError(
                f"openbb estimates missing a publish-date column; "
                f"columns={list(df.columns)}"
            )

        date_ser = pd.to_datetime(df[date_col], errors="coerce")
        if getattr(date_ser.dt, "tz", None) is not None:
            date_ser = date_ser.dt.tz_convert("UTC").dt.tz_localize(None)

        cols: dict[str, Any] = {"date": date_ser}
        n = len(df)
        for col in _ESTIMATE_NUMERIC_COLS:
            actual = lm.get(col)
            if actual is None:
                cols[col] = [float("nan")] * n
            else:
                cols[col] = [_coerce_float(v) for v in df[actual].tolist()]
        return pd.DataFrame(cols)

    @staticmethod
    def _empty_estimates() -> pd.DataFrame:
        data: dict[str, Any] = {"date": pd.to_datetime([])}
        for col in _ESTIMATE_NUMERIC_COLS:
            data[col] = []
        return pd.DataFrame(data)


__all__ = [
    "OpenBBFundamentals",
    "OpenBBEstimates",
    "OPENBB_ENABLE_FLAG",
]
