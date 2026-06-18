"""hermes_quant.data.openbb_insider — OpenBB insider + institutional ownership (aegis-ob3).

ADR-0100 (ob3): extend OpenBB perception coverage to OWNERSHIP — insider
transactions (Form-4 family) and institutional (13-F) holdings. Two providers
live here:

  * ``OpenBBInsider`` — insider transactions from
    ``obb.equity.ownership.insider_trading(symbol, provider='sec')``. ``sec`` is
    the SAME EDGAR source the existing ``evidence/adapters/form4.py`` adapter
    uses (``fmp`` / ``intrinio`` are paid alternatives). It maps each insider
    transaction to the SAME ``FilingEvidence`` evidence shape ``form4.py``
    emits, so ob3 feeds the SAME ``filing``-kind evidence series BESIDE
    form4 — it does NOT replace form4's own EDGAR ingestion.

  * ``OpenBBInstitutional`` — a NEW data type: 13-F institutional holdings from
    ``obb.equity.ownership.institutional(symbol, provider='sec')``. A 13-F is
    filed ~45 days AFTER quarter-end, so the late-filing leak is the exact
    no-lookahead hazard this provider must guard.

FILTER-AT-SOURCE date honesty (NO-LOOKAHEAD — the cardinal rail)
----------------------------------------------------------------
Both providers require an explicit ``as_of`` (a point-in-time read) and apply
the date filter AT SOURCE before returning any row:

  * Insider: keep a row iff its ``filing_date`` (the EDGAR acceptance/filing
    moment — when the Form-4 became public) ``<= as_of``. A transaction whose
    Form-4 was FILED after asof was not publicly knowable at asof and is
    dropped. Anchoring on the TRANSACTION date (``transaction_date``) instead
    would back-date public availability — exactly the leak form4.py's docstring
    warns about — so the filter is on ``filing_date`` ONLY. A NaT filing_date is
    NOT provably public-by-asof -> dropped (fail-closed).
  * Institutional (13-F): keep a row iff its ``filing_date`` (the 13-F filing
    moment) ``<= as_of``. The 13-F's ``period_ending`` (quarter-end) is metadata
    only — filtering on it would leak the holdings ~45 days before the 13-F was
    actually filed. The filter is on ``filing_date`` ONLY; NaT -> dropped.

FINITE-GUARD
------------
Share counts / value columns are coerced through ``_coerce_float`` (NaN for
missing / non-finite). A row whose load-bearing share count is NaN/inf is
DROPPED — an inf/NaN share count can never drive a net-buy/sell view and would
defeat every downstream ``<=`` sanity gate.

DEFAULT-OFF
-----------
Both providers are gated on ``HERMES_QUANT_OPENBB`` (default ``'0'``), reusing
ob1/ob2's flag. The ``openbb`` SDK is lazy-imported ONLY inside the ``obb``
property (never at module top), so a venv lacking openbb is unaffected when the
flag is off — byte-identical no-op. A clear ImportError-with-guidance fires only
if a fetch is attempted (flag on) and openbb isn't installed.

This module is the OpenBB SOURCE layer for ownership; the deterministic
``InsiderAnalyst`` (``hermes_quant.analysts.insider``) consumes it.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import numpy as np
import pandas as pd

from hermes_quant.evidence.schema import (
    FilingEvidence,
    compute_available_at,
    derive_evidence_id,
    sha256_of_json,
)
from hermes_quant.protocol import DataProviderError

logger = logging.getLogger(__name__)

# Default-OFF capability flag (ADR-0100). Reuse ob1/ob2's OpenBB vendor flag —
# the ownership coverage rides the SAME OpenBB enablement toggle. A
# quoted-literal default so the flag-inventory scanner counts it.
OPENBB_ENABLE_FLAG = "HERMES_QUANT_OPENBB"

# `sec` routes the OpenBB ownership call to EDGAR — the SAME asof-honest source
# form4.py uses. fmp / intrinio are paid alternatives.
_DEFAULT_PROVIDER = "sec"

# Source tags. The insider source is DISTINCT from form4's `sec_edgar_form4`
# tag so the deterministic FilingEvidence id (which hashes the source) never
# collides with a form4 record for the same accession — the two adapters feed
# the same evidence SERIES (kind='filing') beside one another, not the same row.
_INSIDER_SOURCE = "openbb_sec_insider"
_INSTITUTIONAL_SOURCE = "openbb_sec_13f"

# The insider share-count column that drives net buy/sell (load-bearing). A row
# whose securities_transacted is NaN/inf is dropped (finite-guard).
_INSIDER_NUMERIC_COLS = ("securities_transacted", "securities_owned", "transaction_price")
# The 13-F load-bearing columns (shares held + value).
_INSTITUTIONAL_NUMERIC_COLS = ("shares", "value", "change")


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
    filing_date / period_ending columns. Mirrors openbb_fundamentals.
    """
    ts = pd.Timestamp(as_of)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts


class _OpenBBOwnershipBase:
    """Shared flag-gate + lazy-SDK + OBBject-coercion plumbing (ob3).

    Mirrors ``hermes_quant.data.openbb_fundamentals._OpenBBBase`` exactly so the
    lazy-import + default-OFF invariant is identical across the OpenBB vendors.
    """

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

        Mirrors ob1/ob2: enforce the flag BEFORE the heavy import, then
        lazy-import with a guided ImportError if the flag is on but the SDK
        isn't installed.
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

    @staticmethod
    def _strip_tz(ser: pd.Series) -> pd.Series:
        """Strip any tz on a datetime series so comparison against the tz-naive
        cutoff never raises (pandas 2.x)."""
        if getattr(ser.dt, "tz", None) is not None:
            return ser.dt.tz_convert("UTC").dt.tz_localize(None)
        return ser


class OpenBBInsider(_OpenBBOwnershipBase):
    """OpenBB insider-transactions provider — feeds the form4 evidence series (ob3).

    ``read_insider(ticker, as_of)`` returns a DataFrame of insider transaction
    rows (one per Form-4 transaction line), filtered AT SOURCE to only rows
    knowable by ``as_of`` (``filing_date <= as_of``) and finite-guarded. This
    sits BESIDE ``evidence/adapters/form4.py`` — it maps to the SAME
    ``FilingEvidence`` shape (via :meth:`to_filing_evidence`) so it feeds the
    SAME ``filing``-kind evidence series. It does NOT replace form4's own EDGAR
    ingestion.

    DEFAULT-OFF (``HERMES_QUANT_OPENBB``): with the flag unset ``read_insider``
    fails closed at the ``obb`` property before the lazy import — byte-identical
    no-op.
    """

    name = "openbb_insider"

    def read_insider(
        self, ticker: str, *, as_of: pd.Timestamp | None = None
    ) -> pd.DataFrame:
        """Read asof-honest insider transaction rows for ``ticker``.

        Raises:
            DataProviderError: as_of is None (latest-only otherwise), flag off,
                openbb missing, or transient API error.
        """
        if as_of is None:
            raise DataProviderError(
                "OpenBBInsider.read_insider requires an explicit as_of (an "
                "asof-less read is latest-only — HARD-REJECTED, ADR-0100 "
                "no-lookahead rail)."
            )
        cutoff = _normalize_asof(as_of)

        try:
            resp = self.obb.equity.ownership.insider_trading(
                symbol=ticker,
                provider=self._provider,
            )
        except DataProviderError:
            raise
        except Exception as e:  # noqa: BLE001
            raise DataProviderError(f"openbb insider fetch failed: {e}") from e

        raw = self._to_dataframe(resp)
        if raw is None or len(raw) == 0:
            return self._empty_insider()

        mapped = self._map_canonical(raw)
        # FILTER-AT-SOURCE no-lookahead: filing_date (the public moment) <= asof.
        # A NaT filing_date is NOT provably public-by-asof -> drop (fail-closed).
        # We DO NOT filter on transaction_date — anchoring public availability on
        # the trade date would back-date it (form4.py §WHY EDGAR).
        keep_date = mapped["filing_date"].notna() & (mapped["filing_date"] <= cutoff)
        # FINITE-GUARD: drop any row whose load-bearing share count
        # (securities_transacted) is NaN/inf — it can never drive a net buy/sell.
        keep_finite = mapped["securities_transacted"].apply(
            lambda v: np.isfinite(_coerce_float(v))
        )
        out = mapped[keep_date & keep_finite].reset_index(drop=True)
        return out

    def _map_canonical(self, raw: pd.DataFrame) -> pd.DataFrame:
        """Map an OpenBB insider frame to the canonical insider column set.

        The point-in-time honesty column ``filing_date`` is resolved
        case-insensitively across OpenBB/FMP/SEC casing drift (``filing_date`` /
        ``filed_date`` / ``date`` / ``filling_date``). The transaction date
        (``transaction_date``) is metadata only. Numeric columns are
        finite-guarded; ``acquisition_or_disposal`` ('A'/'D') gives the
        buy/sell sign. ``accession_number`` (when present) is carried for the
        deterministic FilingEvidence identity.
        """
        df = raw.copy()
        lm = self._lower_map(df)

        filing_col = self._resolve_col(
            lm, "filing_date", "filed_date", "filling_date", "date"
        )
        if filing_col is None:
            raise DataProviderError(
                f"openbb insider missing a filing_date column; "
                f"columns={list(df.columns)}"
            )
        txn_col = self._resolve_col(lm, "transaction_date", "trade_date")

        cols: dict[str, Any] = {
            "filing_date": self._strip_tz(
                pd.to_datetime(df[filing_col], errors="coerce")
            ),
        }
        if txn_col is not None:
            cols["transaction_date"] = self._strip_tz(
                pd.to_datetime(df[txn_col], errors="coerce")
            )
        else:
            cols["transaction_date"] = pd.Series([pd.NaT] * len(df))

        n = len(df)
        for col in _INSIDER_NUMERIC_COLS:
            actual = lm.get(col)
            if actual is None:
                cols[col] = [float("nan")] * n
            else:
                cols[col] = [_coerce_float(v) for v in df[actual].tolist()]

        # Buy/sell sign: SEC 'acquisition_or_disposal' is 'A' (acquire/buy) or
        # 'D' (dispose/sell). Default '' (unknown) when absent.
        ad_col = self._resolve_col(
            lm, "acquisition_or_disposal", "transaction_type", "acquisition_disposition"
        )
        if ad_col is not None:
            cols["acquisition_or_disposal"] = [
                str(v).strip().upper()[:1] if v is not None else ""
                for v in df[ad_col].tolist()
            ]
        else:
            cols["acquisition_or_disposal"] = [""] * n

        # Accession number (Form-4 identity) when present — carried for the
        # deterministic FilingEvidence id beside form4.
        acc_col = self._resolve_col(lm, "accession_number", "accession")
        if acc_col is not None:
            cols["accession_number"] = [
                str(v) if v is not None else "" for v in df[acc_col].tolist()
            ]
        else:
            cols["accession_number"] = [""] * n

        # Owner / form_type metadata (best-effort).
        form_col = self._resolve_col(lm, "form_type", "form")
        cols["form_type"] = (
            [str(v) if v is not None else "4" for v in df[form_col].tolist()]
            if form_col is not None
            else ["4"] * n
        )

        cols["symbol"] = [
            str(v) if v is not None else None
            for v in (
                df[self._resolve_col(lm, "symbol", "ticker")].tolist()
                if self._resolve_col(lm, "symbol", "ticker") is not None
                else [None] * n
            )
        ]
        return pd.DataFrame(cols)

    def to_filing_evidence(
        self, row: pd.Series, *, ticker: str | None = None
    ) -> FilingEvidence:
        """Build a ``FilingEvidence`` from one mapped insider row (deterministic).

        Mirrors ``form4.to_filing_evidence`` so ob3 feeds the SAME ``filing``-kind
        evidence series BESIDE form4. asof honesty:
          * ``published_at`` = ``filing_date`` (the EDGAR public moment, NEVER
            the transaction date) localized to UTC.
          * ``available_at`` = ``compute_available_at("filing", published_at)``
            (the ``filing`` ingest-lag floor is 0s, so equal to published_at).
          * Identity = ``derive_evidence_id("filing", source, payload_hash)`` over
            a canonical payload hash — same row -> same UUID (idempotent append).
            The source tag (``openbb_sec_insider``) is DISTINCT from form4's
            (``sec_edgar_form4``) so the two adapters never collide on identity.
        """
        filed = pd.Timestamp(row["filing_date"])
        if filed.tzinfo is None:
            filed = filed.tz_localize("UTC")
        published = filed.to_pydatetime()
        txn = row.get("transaction_date")
        txn_iso = (
            pd.Timestamp(txn).date().isoformat()
            if txn is not None and pd.notna(txn)
            else None
        )
        accession = str(row.get("accession_number") or "")
        payload = {
            "source": _INSIDER_SOURCE,
            "symbol": ticker or row.get("symbol"),
            "accession_number": accession,
            "form_type": str(row.get("form_type") or "4"),
            "filed_at": published.isoformat(),
            "transaction_date": txn_iso,
            "securities_transacted": _coerce_float(row.get("securities_transacted")),
            "acquisition_or_disposal": str(row.get("acquisition_or_disposal") or ""),
        }
        phash = sha256_of_json(payload)
        available = compute_available_at("filing", published)
        return FilingEvidence(
            id=derive_evidence_id("filing", _INSIDER_SOURCE, phash),
            kind="filing",
            symbol=(ticker or row.get("symbol")),
            source=_INSIDER_SOURCE,
            published_at=published,
            ingested_at=datetime.now(UTC),
            available_at=available,
            payload_ref=f"openbb:insider:{accession or phash[:16]}",
            payload_hash=phash,
            accession_number=accession,
            form_type=str(row.get("form_type") or "4"),
        )

    @staticmethod
    def _empty_insider() -> pd.DataFrame:
        data: dict[str, Any] = {
            "filing_date": pd.to_datetime([]),
            "transaction_date": pd.to_datetime([]),
        }
        for col in _INSIDER_NUMERIC_COLS:
            data[col] = []
        data["acquisition_or_disposal"] = []
        data["accession_number"] = []
        data["form_type"] = []
        data["symbol"] = []
        return pd.DataFrame(data)


class OpenBBInstitutional(_OpenBBOwnershipBase):
    """OpenBB 13-F institutional-holdings provider — a NEW data type (ob3).

    ``read_institutional(ticker, as_of)`` returns a DataFrame of 13-F holding
    rows from ``obb.equity.ownership.institutional(provider='sec')``, filtered
    AT SOURCE to ``filing_date <= as_of`` and finite-guarded. A 13-F is filed
    ~45 days AFTER quarter-end, so the late-filing leak is the exact hazard: we
    filter on ``filing_date`` (the public moment), NOT ``period_ending`` (the
    quarter-end the holdings refer to).

    DEFAULT-OFF (``HERMES_QUANT_OPENBB``).
    """

    name = "openbb_institutional"

    def read_institutional(
        self, ticker: str, *, as_of: pd.Timestamp | None = None
    ) -> pd.DataFrame:
        """Read asof-honest 13-F holding rows for ``ticker``.

        Raises:
            DataProviderError: as_of is None (latest-only otherwise), flag off,
                openbb missing, or transient API error.
        """
        if as_of is None:
            raise DataProviderError(
                "OpenBBInstitutional.read_institutional requires an explicit as_of "
                "(an asof-less read is latest-only — HARD-REJECTED, ADR-0100 "
                "no-lookahead rail)."
            )
        cutoff = _normalize_asof(as_of)

        try:
            resp = self.obb.equity.ownership.institutional(
                symbol=ticker,
                provider=self._provider,
            )
        except DataProviderError:
            raise
        except Exception as e:  # noqa: BLE001
            raise DataProviderError(f"openbb institutional fetch failed: {e}") from e

        raw = self._to_dataframe(resp)
        if raw is None or len(raw) == 0:
            return self._empty_institutional()

        mapped = self._map_canonical(raw)
        # FILTER-AT-SOURCE no-lookahead: filing_date (the 13-F public moment)
        # <= asof. The period_ending (quarter-end) is metadata ONLY — filtering
        # on it would leak the holdings ~45 days before the 13-F was filed. NaT
        # filing_date -> drop (fail-closed).
        keep_date = mapped["filing_date"].notna() & (mapped["filing_date"] <= cutoff)
        # FINITE-GUARD: drop a row whose load-bearing shares count is NaN/inf.
        keep_finite = mapped["shares"].apply(
            lambda v: np.isfinite(_coerce_float(v))
        )
        out = mapped[keep_date & keep_finite].reset_index(drop=True)
        return out

    def _map_canonical(self, raw: pd.DataFrame) -> pd.DataFrame:
        """Map an OpenBB 13-F frame to the canonical institutional column set.

        The public-moment column ``filing_date`` is resolved case-insensitively
        (``filing_date`` / ``filed_date`` / ``date``). ``period_ending``
        (quarter-end / ``report_date``) is carried as metadata ONLY (never the
        filter axis). Numeric columns are finite-guarded.
        """
        df = raw.copy()
        lm = self._lower_map(df)

        filing_col = self._resolve_col(
            lm, "filing_date", "filed_date", "filling_date", "date"
        )
        if filing_col is None:
            raise DataProviderError(
                f"openbb institutional missing a filing_date column; "
                f"columns={list(df.columns)}"
            )
        period_col = self._resolve_col(
            lm, "period_ending", "report_date", "period_end", "calendar_quarter"
        )

        cols: dict[str, Any] = {
            "filing_date": self._strip_tz(
                pd.to_datetime(df[filing_col], errors="coerce")
            ),
        }
        if period_col is not None:
            cols["period_ending"] = self._strip_tz(
                pd.to_datetime(df[period_col], errors="coerce")
            )
        else:
            cols["period_ending"] = pd.Series([pd.NaT] * len(df))

        n = len(df)
        for col in _INSTITUTIONAL_NUMERIC_COLS:
            actual = lm.get(col)
            if actual is None:
                cols[col] = [float("nan")] * n
            else:
                cols[col] = [_coerce_float(v) for v in df[actual].tolist()]

        holder_col = self._resolve_col(lm, "name", "holder", "investor_name")
        cols["holder"] = (
            [str(v) if v is not None else "" for v in df[holder_col].tolist()]
            if holder_col is not None
            else [""] * n
        )
        return pd.DataFrame(cols)

    @staticmethod
    def _empty_institutional() -> pd.DataFrame:
        data: dict[str, Any] = {
            "filing_date": pd.to_datetime([]),
            "period_ending": pd.to_datetime([]),
        }
        for col in _INSTITUTIONAL_NUMERIC_COLS:
            data[col] = []
        data["holder"] = []
        return pd.DataFrame(data)


__all__ = [
    "OpenBBInsider",
    "OpenBBInstitutional",
    "OPENBB_ENABLE_FLAG",
]
