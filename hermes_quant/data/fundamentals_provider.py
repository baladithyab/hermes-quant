"""hermes_quant.data.fundamentals_provider — yfinance fundamentals snapshot provider.

Per ADR-0064 + design docs/design/v0.6.1-fundamentals-analyst.md §3-4:

  * Hot path = parquet read + arithmetic. ~5-20 ms / call.
  * Cold path (cron) = yfinance fetch + append. Called by
    scripts/quant-fundamentals-prewarm-daily.py once per day, never on
    the analyst hot path.

Cache layout::

    ~/.hermes/quant/cache/fundamentals/
        yfinance/
            AAPL.parquet     # one row per as_of_date snapshot
            MSFT.parquet
            ...
        sector_medians/
            Technology.parquet   # one row per as_of_date snapshot
            Healthcare.parquet
            ...

Per-ticker schema (one parquet file per ticker, append-only)::

    as_of_date           date          snapshot key (UTC, day-truncated)
    fetched_at           datetime[UTC] wall-clock fetch time
    report_date          datetime[UTC] filing/report date the datum was first
                                       publicly knowable (NULLable; backfilled
                                       additively — older snapshots leave NaT)
    period_end           datetime[UTC] fiscal period end the datum describes
                                       (NULLable; coarse report_date fallback)
    source               str           "yfinance" / "yfinance_balance_sheet"
    pe_trailing          float64       info["trailingPE"]
    pe_forward           float64       info["forwardPE"]
    debt_to_equity       float64       info["debtToEquity"] / 100  (yfinance scales)
    free_cash_flow       float64       info["freeCashflow"]
    revenue_ttm          float64       info["totalRevenue"]
    eps_trailing         float64       info["trailingEps"]
    eps_forward          float64       info["forwardEps"]
    gross_margin_ttm     float64       derived from income_stmt
    gross_margin_prior   float64       derived from income_stmt y-1
    revenue_yoy          float64       derived from quarterly income_stmt
    fcf_yoy              float64       derived from cashflow YoY
    sector               str           info["sector"]
    currency             str           info["currency"]
    quote_type           str           info["quoteType"]

Append discipline mirrors `OhlcvCache.write` (data/cache.py): read existing,
append, dedupe on as_of_date keeping latest fetched_at, write to temp, atomic
rename.

Sector-median sibling cache holds rolling sector P/E benchmarks refreshed
weekly via `refresh_sector_medians`.

Reporting-lag-adjusted as_of (B34, no-lookahead)
------------------------------------------------
A fundamental datum is NOT knowable as of its period end — it is only
knowable after its filing/report date PLUS the typical reporting lag (e.g.
a Q4 closing 31-Dec is filed ~mid-Feb). Filtering a backtest read on the
period end (or even on the cache snapshot date) can therefore leak a
fundamental into the past before it was actually reported.

When ``HERMES_QUANT_FUNDAMENTALS_REPORTING_LAG`` is truthy (default ON; cs12 /
no-lookahead), the hot-path reads (`read_latest`, `read_sector_median_pe`)
require, in addition to the existing ``as_of_date <= as_of`` snapshot filter,
that the row's effective-knowable date satisfies::

    effective_knowable = (report_date or period_end) + reporting_lag_days
    keep row iff effective_knowable <= as_of

The lag (``reporting_lag_days``, default 45d — a safe ~quarterly filing
window) only ever TIGHTENS what is visible: a row that passes the snapshot
filter may still be dropped because its report_date+lag is after as_of, but
no row that the OFF path would have excluded is ever admitted (the original
``as_of_date <= as_of`` predicate is still ANDed in). Rows with neither
report_date nor period_end fall back to the snapshot ``as_of_date`` (already
a knowable date — it is when the datum entered the cache), so missing
backfill never loosens visibility.

The flag is read at call time and is ON by default (cs12). An explicit
falsey value (``HERMES_QUANT_FUNDAMENTALS_REPORTING_LAG=0`` / ``false`` /
``no`` / ``off`` / empty) reverts the read path to the byte-identical
pre-B34 ``as_of_date <= as_of`` behavior — the instant operator kill switch
for this live-analyst-input change.

Operator note — old/stale cache + default-ON
---------------------------------------------
Against an OLD-SCHEMA or STALE cache the default-ON filter is intentionally
conservative to the point of going dark. Parquets that predate the B34
``period_end`` / ``report_date`` columns (backfilled NaT), or a cache the
prewarm cron has not yet repopulated, carry no point-in-time stamp, so every
row falls back to ``as_of_date + reporting_lag_days``. A freshly-cached
snapshot read at ~``as_of`` is then DROPPED (``as_of_date + 45d > as_of``) and
``read_latest`` returns None. With the whole universe on that fallback the
``FundamentalsAnalyst`` abstains across the board (full dark — silence-by-
default, safety-conservative) until (i) the prewarm cron rewrites new-schema
rows carrying a real ``period_end`` / ``report_date`` AND (ii) the 45d lag has
elapsed for those rows. The byte-identical revert / instant kill switch is
``HERMES_QUANT_FUNDAMENTALS_REPORTING_LAG=0`` (or ``false`` / ``no`` / ``off`` /
empty), which restores the pre-B34 ``as_of_date <= as_of`` read path exactly.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


DEFAULT_CACHE_ROOT = Path.home() / ".hermes" / "quant" / "cache" / "fundamentals"

# B34 reporting-lag-adjusted as_of. Default-ON (cs12 / no-lookahead); read at
# call time so the explicit-OFF revert path is byte-identical to pre-B34.
REPORTING_LAG_ENV_FLAG = "HERMES_QUANT_FUNDAMENTALS_REPORTING_LAG"
# Conservative default for quarterly fundamentals: a 10-K/10-Q can land ~40d
# (large accelerated filer) to ~75d after period end. 45d is a safe, jitter-
# stable middle of the quarterly filing window — it never loosens visibility.
DEFAULT_REPORTING_LAG_DAYS: int = 45


def _reporting_lag_flag_on() -> bool:
    """True iff the reporting-lag-adjusted as_of filter is enabled.

    Default-ON (cs12 / no-lookahead): an ``as_of``-bounded read is by
    definition a point-in-time read, and a fundamental is NOT knowable as of
    its cache date — only after ``period_end`` (or ``report_date``) plus the
    typical reporting lag. Filtering only on ``as_of_date <= as_of`` leaks a
    Q4 datum (period_end 31-Dec, cached mid-Jan) into a backtest deciding in
    January, ~45d before the 10-K is actually filed. The lag filter closes
    that leak, so it is ON by default.

    Reversibility: the operator can opt OUT with an explicit falsey value
    (``HERMES_QUANT_FUNDAMENTALS_REPORTING_LAG=0`` / ``false`` / ``off`` / ...).
    On that OFF path the read is byte-identical to pre-B34 behavior — the
    instant kill switch required for a live analyst-input change.

    Read at call time so flipping the env var takes effect without re-import.
    """
    return os.environ.get(REPORTING_LAG_ENV_FLAG, "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
        "",
    )

# Per-ticker snapshot schema (column -> dtype)
_SNAPSHOT_COLUMNS: dict[str, str] = {
    "as_of_date": "datetime64[ns, UTC]",
    "fetched_at": "datetime64[ns, UTC]",
    # B34 reporting-lag-adjusted as_of: NULLable point-in-time columns. Older
    # parquets predate them and are backfilled with NaT by the normalizer; the
    # reporting-lag read filter is no-op while these are NaT unless the flag is
    # ON and a period_end exists.
    "report_date": "datetime64[ns, UTC]",
    "period_end": "datetime64[ns, UTC]",
    "source": "string",
    "pe_trailing": "float64",
    "pe_forward": "float64",
    "debt_to_equity": "float64",
    "free_cash_flow": "float64",
    "revenue_ttm": "float64",
    "eps_trailing": "float64",
    "eps_forward": "float64",
    "gross_margin_ttm": "float64",
    "gross_margin_prior": "float64",
    "revenue_yoy": "float64",
    "fcf_yoy": "float64",
    "sector": "string",
    "currency": "string",
    "quote_type": "string",
}

# Sector-median snapshot schema
_SECTOR_MEDIAN_COLUMNS: dict[str, str] = {
    "as_of_date": "datetime64[ns, UTC]",
    "fetched_at": "datetime64[ns, UTC]",
    "sector": "string",
    "median_pe_trailing": "float64",
    "n_constituents": "int64",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_component(value: str) -> str:
    """Sanitize a path component (mirror of data.cache._safe_component)."""
    import re

    value = (value or "").strip().replace("/", "_")
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return value.strip("._") or "unknown"


def _atomic_write_parquet(df: pd.DataFrame, target: Path) -> None:
    """Atomic-rename parquet write."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        df.to_parquet(tmp, index=False)
        tmp.replace(target)
    finally:
        if tmp.exists():
            tmp.unlink()


def _quarantine_corrupt_parquet(path: Path) -> Path:
    """Rename a corrupt/unreadable parquet to a timestamped ``.corrupt`` sidecar.

    cs61 (money-software, fail-CLOSED): when ``pd.read_parquet`` RAISES on an
    existing cache file (transient/torn/genuinely-corrupt read) the write paths
    used to reset ``existing`` to an EMPTY frame and ``_atomic_write_parquet``
    the single new row over the file — irrecoverably destroying the ENTIRE
    historical point-in-time series for that ticker / sector (same write-side
    PIT-history-mutation class as cs42(b) / cs59, but triggered by a READ ERROR
    rather than a re-fetch, and a SILENT fail-open data-loss).

    Quarantine instead: MOVE the corrupt bytes aside (so the history is
    recoverable for a human / a repair job) BEFORE the caller starts fresh, and
    return the sidecar path so the caller can LOUDLY warn. Preserves forward
    progress (the new row is still written) AND recoverability, vs. a bare
    ``raise`` that would also block the cron.

    The sidecar name carries a UTC timestamp + a uniqueness counter so repeated
    corrupt reads of the same path never clobber an earlier quarantine. On the
    (unlikely) event the rename itself fails the corrupt bytes are left in place
    and the original exception path is preserved — we never silently delete.
    """
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%S")
    base = path.with_suffix(path.suffix + f".{stamp}.corrupt")
    sidecar = base
    counter = 0
    # Never clobber an earlier quarantine of the same path within the same second.
    while sidecar.exists():
        counter += 1
        sidecar = path.with_suffix(path.suffix + f".{stamp}-{counter}.corrupt")
    path.replace(sidecar)
    return sidecar


def _coerce_float(x: Any) -> float:
    """Coerce yfinance value to float; NaN if missing/unparseable."""
    if x is None:
        return float("nan")
    try:
        f = float(x)
        if not np.isfinite(f):
            return float("nan")
        return f
    except (TypeError, ValueError):
        return float("nan")


def _normalize_dte(raw: Any) -> float:
    """yfinance debt-to-equity is reported as a percentage (e.g. 175 means 1.75x).

    Heuristic: if value > 5 we assume yfinance percentage encoding and divide
    by 100. Otherwise return as-is (already a ratio). NaN for missing.
    """
    f = _coerce_float(raw)
    if np.isnan(f):
        return f
    if f > 5.0:
        return f / 100.0
    return f


def _coerce_epoch_to_ts(value: Any) -> pd.Timestamp:
    """Coerce a yfinance unix-epoch seconds value to a tz-aware UTC Timestamp.

    Returns ``pd.NaT`` for missing / non-positive / unparseable input. Used for
    B34 period-end stamping (info["mostRecentQuarter"] is unix seconds).
    """
    f = _coerce_float(value)
    if np.isnan(f) or f <= 0:
        return pd.NaT
    try:
        return pd.Timestamp(int(f), unit="s", tz="UTC")
    except (ValueError, OverflowError, OSError):
        return pd.NaT


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


@dataclass
class FundamentalsProvider:
    """Parquet-cached yfinance fundamentals provider.

    Hot path methods (no network):
      - read_latest(ticker, as_of)         -> latest snapshot row or None
      - read_sector_median_pe(sector, as_of) -> float P/E median or None

    Cold path methods (network, called by cron):
      - refresh(tickers)                   -> per-ticker status dict
      - refresh_sector_medians(universe)   -> writes per-sector parquet

    Cache layout::

        cache_root/
            yfinance/<TICKER>.parquet
            sector_medians/<SECTOR>.parquet

    Sector-median staleness: 7d soft / 30d hard. read_sector_median_pe
    returns None on >30d hard staleness so the analyst's pe_relative
    sub-signal abstains while the others proceed (per ADR-0064 §D5).
    """

    cache_root: Path = field(default_factory=lambda: DEFAULT_CACHE_ROOT)
    ttl_hours: int = 24
    name: str = "yfinance_fundamentals"

    # B34: conservative reporting lag (days) added to report_date / period_end
    # when the reporting-lag-adjusted as_of filter is ON. Only TIGHTENS
    # visibility (no-lookahead). Has no effect while the flag is OFF.
    reporting_lag_days: int = DEFAULT_REPORTING_LAG_DAYS

    # Sector-median hard staleness in days (per ADR-0064 §1.2 D5)
    SECTOR_MEDIAN_STALE_HARD_DAYS: int = 30

    def __post_init__(self) -> None:
        # Cache root is only auto-created on write paths so a read-only
        # filesystem (tests with tmp_path) never sees a stray mkdir.
        self.cache_root = Path(self.cache_root)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    @property
    def yfinance_dir(self) -> Path:
        return self.cache_root / "yfinance"

    @property
    def sector_medians_dir(self) -> Path:
        return self.cache_root / "sector_medians"

    def ticker_path(self, ticker: str) -> Path:
        return self.yfinance_dir / f"{_safe_component(ticker)}.parquet"

    def sector_median_path(self, sector: str) -> Path:
        return self.sector_medians_dir / f"{_safe_component(sector)}.parquet"

    # ------------------------------------------------------------------
    # B34: reporting-lag-adjusted as_of filter (default-ON, no-lookahead)
    # ------------------------------------------------------------------

    def _apply_reporting_lag_filter(
        self, df: pd.DataFrame, asof_ts: pd.Timestamp
    ) -> pd.DataFrame:
        """Drop rows not yet knowable as of ``asof_ts`` under the reporting lag.

        No-op (returns ``df`` unchanged) when the reporting-lag flag is OFF, so
        the read path is byte-identical to pre-B34 behavior. When ON, a row is
        kept only if its effective-knowable date satisfies::

            effective_knowable = (report_date or period_end) + lag <= asof_ts

        Rows with neither ``report_date`` nor ``period_end`` fall back to the
        snapshot ``as_of_date`` (already a knowable date), so a missing backfill
        never loosens visibility. This predicate is ANDed with the caller's
        existing ``as_of_date <= as_of`` filter — it only ever TIGHTENS.
        """
        if not _reporting_lag_flag_on():
            return df
        lag = pd.Timedelta(days=int(self.reporting_lag_days))

        # Normalize the candidate point-in-time columns to tz-aware UTC. Both
        # are NULLable; missing/absent columns fall back to as_of_date.
        def _as_utc(col: str) -> pd.Series:
            if col not in df.columns:
                return pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns, UTC]")
            ser = df[col]
            if not pd.api.types.is_datetime64_any_dtype(ser):
                ser = pd.to_datetime(ser, utc=True, errors="coerce")
            elif getattr(ser.dt, "tz", None) is None:
                ser = ser.dt.tz_localize("UTC")
            return ser

        report = _as_utc("report_date")
        period = _as_utc("period_end")
        as_of_date = _as_utc("as_of_date")

        # report_date preferred; fall back to period_end; finally as_of_date.
        basis = report.fillna(period).fillna(as_of_date)
        effective_knowable = basis + lag
        keep = effective_knowable <= asof_ts
        return df[keep]

    # ------------------------------------------------------------------
    # Hot path: reads
    # ------------------------------------------------------------------

    def read_latest(self, ticker: str, *, as_of: pd.Timestamp | None = None) -> pd.Series | None:
        """Return the latest-by-fetched_at snapshot row for ticker, or None.

        as_of (optional): if provided, only rows with as_of_date <= as_of
        are considered (point-in-time semantics). Otherwise the most
        recent row is returned.

        Returns None if the parquet file does not exist or is empty.
        Raises no exceptions on read failure; logs and returns None.
        """
        path = self.ticker_path(ticker)
        if not path.exists():
            return None
        try:
            df = pd.read_parquet(path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("fundamentals: parquet read failed for %s: %s", ticker, exc)
            return None
        if df.empty:
            return None

        # Ensure timestamps are tz-aware UTC for safe comparison.
        for col in ("as_of_date", "fetched_at"):
            if col in df.columns:
                ser = df[col]
                if not pd.api.types.is_datetime64_any_dtype(ser):
                    df[col] = pd.to_datetime(ser, utc=True)
                elif getattr(ser.dt, "tz", None) is None:
                    df[col] = ser.dt.tz_localize("UTC")

        if as_of is not None:
            asof_ts = pd.Timestamp(as_of)
            if asof_ts.tzinfo is None:
                asof_ts = asof_ts.tz_localize("UTC")
            df = df[df["as_of_date"] <= asof_ts]
            if df.empty:
                return None
            # B34: ANDed reporting-lag-adjusted as_of (no-op when flag OFF).
            df = self._apply_reporting_lag_filter(df, asof_ts)
            if df.empty:
                return None
            # cs42(a): a row whose fetched_at is strictly AFTER as_of was fetched
            # in the future relative to this point-in-time read — never
            # legitimate at as_of. The day-normalized as_of_date can pass the
            # ``as_of_date <= as_of`` snapshot filter for an intraday-future
            # fetched_at (or a fabricated future timestamp), which would yield a
            # negative age that defeats the downstream staleness gate. Drop it.
            # Pure PIT correctness; touches only the as_of-bounded path.
            df = df[df["fetched_at"] <= asof_ts]
            if df.empty:
                return None

        # Latest by fetched_at.
        df = df.sort_values("fetched_at")
        return df.iloc[-1]

    def read_sector_median_pe(
        self, sector: str | None, *, as_of: pd.Timestamp | None = None
    ) -> float | None:
        """Return latest median trailing P/E for sector, or None.

        Returns None when:
          - sector is None / empty / 'unknown'
          - no parquet file
          - file is empty
          - latest row is older than SECTOR_MEDIAN_STALE_HARD_DAYS
        """
        if not sector or sector.lower() in {"unknown", "none", "—", "-", ""}:
            return None
        path = self.sector_median_path(sector)
        if not path.exists():
            return None
        try:
            df = pd.read_parquet(path)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "fundamentals: sector_median parquet read failed for %s: %s", sector, exc
            )
            return None
        if df.empty:
            return None

        for col in ("as_of_date", "fetched_at"):
            if col in df.columns:
                ser = df[col]
                if not pd.api.types.is_datetime64_any_dtype(ser):
                    df[col] = pd.to_datetime(ser, utc=True)
                elif getattr(ser.dt, "tz", None) is None:
                    df[col] = ser.dt.tz_localize("UTC")

        asof_ts: pd.Timestamp
        if as_of is None:
            asof_ts = pd.Timestamp.now(tz="UTC")
        else:
            asof_ts = pd.Timestamp(as_of)
            if asof_ts.tzinfo is None:
                asof_ts = asof_ts.tz_localize("UTC")

        df = df[df["as_of_date"] <= asof_ts]
        if df.empty:
            return None
        # cs41: ANDed reporting-lag-adjusted as_of, symmetric with read_latest
        # (no-op when the flag is OFF). The pe_relative DENOMINATOR (sector
        # median) must obey the same no-lookahead lag as the NUMERATOR
        # (read_latest); else a backtest sector median embeds not-yet-public
        # constituent fundamentals. The sector-median schema has no
        # report_date / period_end, so the filter falls back to
        # ``as_of_date + reporting_lag_days`` — conservative-tightening,
        # consistent with the old/stale-cache go-dark behavior. Flag OFF reverts
        # to the byte-identical pre-cs41 ``as_of_date <= as_of`` read.
        df = self._apply_reporting_lag_filter(df, asof_ts)
        if df.empty:
            return None
        # cs53: symmetric with read_latest:426 (cs42(a)). A sector-median row
        # whose fetched_at is strictly AFTER as_of was fetched in the future
        # relative to this point-in-time read — never legitimate at as_of. The
        # day-normalized as_of_date can pass the ``as_of_date <= as_of`` snapshot
        # filter for an intraday-future fetched_at (or a fabricated future
        # timestamp), which yields a NEGATIVE age_days below; the staleness gate
        # ``age_days > SECTOR_MEDIAN_STALE_HARD_DAYS`` is then False for any
        # negative value and the future-fetched median is silently ACCEPTED. The
        # pe_relative DENOMINATOR (sector median) must obey the same no-lookahead
        # discipline as the read_latest NUMERATOR. Drop it. Pure PIT correctness;
        # touches only the as_of-bounded path.
        if as_of is not None:
            df = df[df["fetched_at"] <= asof_ts]
            if df.empty:
                return None
        df = df.sort_values("fetched_at")
        latest = df.iloc[-1]
        age_days = (asof_ts - pd.Timestamp(latest["fetched_at"])).days
        if age_days > self.SECTOR_MEDIAN_STALE_HARD_DAYS:
            return None
        val = _coerce_float(latest.get("median_pe_trailing"))
        if np.isnan(val) or val <= 0:
            return None
        return val

    # ------------------------------------------------------------------
    # Cold path: writes (cron-driven)
    # ------------------------------------------------------------------

    def write_snapshot(self, ticker: str, snapshot: dict) -> Path:
        """Append a snapshot dict to the per-ticker parquet (atomic).

        Snapshot must include the columns in _SNAPSHOT_COLUMNS. Missing
        columns are filled with NaN / pd.NA. Dedupe is on as_of_date,
        keep latest fetched_at.

        Used by the cron and by tests for fixture construction.
        """
        path = self.ticker_path(ticker)
        new_row = self._normalize_snapshot_row(snapshot)
        new_df = pd.DataFrame([new_row])

        if path.exists():
            try:
                existing = pd.read_parquet(path)
            except Exception as exc:  # noqa: BLE001
                # cs61 (money-software, fail-CLOSED): do NOT silently overwrite a
                # corrupt/unreadable cache with the single new row — that would
                # irrecoverably destroy the ticker's entire historical
                # point-in-time series (same write-side PIT-history-mutation
                # class as cs42(b) / cs59, here triggered by a READ ERROR).
                # QUARANTINE the corrupt bytes to a recoverable .corrupt sidecar,
                # LOUDLY warn, then start fresh so forward progress is preserved.
                sidecar = _quarantine_corrupt_parquet(path)
                logger.warning(
                    "fundamentals: corrupt parquet for %s, QUARANTINED to %s "
                    "(history preserved for recovery, NOT overwritten); "
                    "starting fresh: %s",
                    ticker,
                    sidecar.name,
                    exc,
                )
                existing = pd.DataFrame(columns=list(_SNAPSHOT_COLUMNS.keys()))
        else:
            existing = pd.DataFrame(columns=list(_SNAPSHOT_COLUMNS.keys()))

        merged = pd.concat([existing, new_df], ignore_index=True)
        merged = self._normalize_snapshot_frame(merged)
        # cs42(b): point-in-time-preserving dedupe. A SAME-DAY correction (a
        # newer fetched_at on the SAME calendar day as the row's as_of_date) is
        # the legitimate intraday-revision case and still wins. A CROSS-DAY
        # backfill (fetched_at on a LATER calendar day than as_of_date) must NOT
        # overwrite a row already recorded same-day-correct for that as_of_date,
        # else a re-fetch silently rewrites a historical point-in-time value.
        # Among rows sharing an as_of_date, rank same-day rows above cross-day
        # ones, then by fetched_at, and keep="last" -> the PIT-correct row.
        merged["_same_day"] = merged["fetched_at"].dt.normalize() <= merged["as_of_date"]
        merged = merged.sort_values(["as_of_date", "_same_day", "fetched_at"])
        merged = merged.drop_duplicates(subset=["as_of_date"], keep="last")
        merged = merged.drop(columns=["_same_day"])
        merged = merged.sort_values("fetched_at").reset_index(drop=True)
        _atomic_write_parquet(merged, path)
        return path

    def write_sector_median(self, sector: str, snapshot: dict) -> Path:
        """Append a sector-median row (atomic, dedupe on as_of_date)."""
        path = self.sector_median_path(sector)
        row = self._normalize_sector_median_row(snapshot)
        new_df = pd.DataFrame([row])

        if path.exists():
            try:
                existing = pd.read_parquet(path)
            except Exception as exc:  # noqa: BLE001
                # cs61 (money-software, fail-CLOSED): symmetric with
                # write_snapshot. A corrupt-read here used to silently overwrite
                # the entire historical sector-median series (the pe_relative
                # DENOMINATOR) with the single new row — and without even a
                # warning. QUARANTINE the corrupt bytes to a recoverable .corrupt
                # sidecar, LOUDLY warn, then start fresh.
                sidecar = _quarantine_corrupt_parquet(path)
                logger.warning(
                    "fundamentals: corrupt sector_median parquet for %s, "
                    "QUARANTINED to %s (history preserved for recovery, NOT "
                    "overwritten); starting fresh: %s",
                    sector,
                    sidecar.name,
                    exc,
                )
                existing = pd.DataFrame(columns=list(_SECTOR_MEDIAN_COLUMNS.keys()))
        else:
            existing = pd.DataFrame(columns=list(_SECTOR_MEDIAN_COLUMNS.keys()))

        merged = pd.concat([existing, new_df], ignore_index=True)
        merged = self._normalize_sector_median_frame(merged)
        # cs59: point-in-time-preserving dedupe, mirroring write_snapshot's
        # cs42(b) guard. The sector median is the pe_relative DENOMINATOR; a
        # past-as-of read (read_sector_median_pe) must return the SAME value
        # across re-fetches, else a backtest replayed after a refresh sees a
        # mutated denominator — the same PIT-integrity class cs42(b) closed on
        # the per-ticker write side. A SAME-DAY correction (a newer fetched_at on
        # the SAME calendar day as the row's as_of_date) is the legitimate
        # intraday-revision case and still wins. A CROSS-DAY backfill (fetched_at
        # on a LATER calendar day than as_of_date) must NOT overwrite a row
        # already recorded same-day-correct for that as_of_date, else a re-fetch
        # silently rewrites a historical point-in-time median. Among rows sharing
        # an as_of_date, rank same-day rows above cross-day ones, then by
        # fetched_at, and keep="last" -> the PIT-correct row. A first-write of a
        # new as_of_date is byte-identical (the guard only fires on a re-write).
        merged["_same_day"] = merged["fetched_at"].dt.normalize() <= merged["as_of_date"]
        merged = merged.sort_values(["as_of_date", "_same_day", "fetched_at"])
        merged = merged.drop_duplicates(subset=["as_of_date"], keep="last")
        merged = merged.drop(columns=["_same_day"])
        merged = merged.sort_values("fetched_at").reset_index(drop=True)
        _atomic_write_parquet(merged, path)
        return path

    def refresh(self, tickers: list[str]) -> dict[str, str]:
        """Cron entry: fetch yfinance fundamentals for each ticker; append.

        Returns per-ticker status string for cron logging:
          - "ok"            : refresh succeeded
          - "skipped:fresh" : within ttl_hours of last fetch (no-op)
          - "skipped:no_yf" : yfinance not installed
          - "error:..."     : exception class + message snippet
        """
        result: dict[str, str] = {}
        try:
            import yfinance as yf  # noqa: F401
        except ImportError:
            for t in tickers:
                result[t] = "skipped:no_yf"
            return result

        now = pd.Timestamp.now(tz="UTC")
        for ticker in tickers:
            try:
                latest = self.read_latest(ticker)
                if latest is not None:
                    age_h = (now - pd.Timestamp(latest["fetched_at"])).total_seconds() / 3600.0
                    # cs67: a FUTURE fetched_at (age_h < 0) must NOT count as
                    # fresh. read_latest(as_of=None) returns the MAX-fetched_at
                    # row; a row stamped in the future relative to the cron
                    # wall-clock (NTP/clock-skew regression, container clock
                    # jump, a fabricated/corrupt parquet, or a backfill tool
                    # stamping a future timestamp) makes age_h negative, so the
                    # legacy ``age_h < ttl_hours`` would skip:fresh FOREVER and
                    # the ticker's fundamentals silently go stale and are never
                    # re-fetched (until wall-clock passes that stamp). The
                    # cs42(a)/cs53 future-fetch guards are scoped to the
                    # as_of-is-not-None READ path; this cold REFRESH path
                    # (as_of=None) had no guard. "Is the cache fresh enough to
                    # SKIP re-fetching?" — a future stamp answers NO: treat it
                    # max-stale and force-refresh. A genuinely-fresh past stamp
                    # (0 <= age_h < ttl_hours) still skips:fresh, byte-identical.
                    if 0 <= age_h < self.ttl_hours:
                        result[ticker] = "skipped:fresh"
                        continue
                snapshot = self._fetch_yfinance_snapshot(ticker, now)
                if snapshot is None:
                    result[ticker] = "error:empty_info"
                    continue
                self.write_snapshot(ticker, snapshot)
                result[ticker] = "ok"
            except Exception as exc:  # noqa: BLE001
                logger.warning("fundamentals refresh: %s -> %r", ticker, exc)
                result[ticker] = f"error:{type(exc).__name__}:{str(exc)[:80]}"
        return result

    def refresh_sector_medians(self, universe: list[str]) -> dict[str, Any]:
        """Compute sector-median trailing P/E across cached snapshots.

        Reads the latest snapshot for each ticker in `universe` from the
        per-ticker parquet (does NOT call yfinance), groups by sector,
        writes one row per sector to sector_medians/<SECTOR>.parquet.

        Returns {sector: {"n": int, "median_pe": float}} for logging.
        """
        now = pd.Timestamp.now(tz="UTC")
        rows: list[dict] = []
        for ticker in universe:
            snap = self.read_latest(ticker)
            if snap is None:
                continue
            pe = _coerce_float(snap.get("pe_trailing"))
            sector = snap.get("sector")
            if not sector or pd.isna(sector):
                continue
            if np.isnan(pe) or pe <= 0 or pe > 1000:
                continue
            rows.append({"sector": str(sector), "pe": pe})
        if not rows:
            return {}
        df = pd.DataFrame(rows)
        out: dict[str, Any] = {}
        # Day-truncate the snapshot date so any read with `as_of` later
        # within the same UTC day can find the row (filter is `<=`).
        as_of_date = now.normalize()
        for sector, sub in df.groupby("sector"):
            median = float(sub["pe"].median())
            n = int(len(sub))
            self.write_sector_median(
                sector,
                {
                    "as_of_date": as_of_date,
                    "fetched_at": now,
                    "sector": sector,
                    "median_pe_trailing": median,
                    "n_constituents": n,
                },
            )
            out[str(sector)] = {"n": n, "median_pe": median}
        return out

    # ------------------------------------------------------------------
    # yfinance fetch (cold path internals)
    # ------------------------------------------------------------------

    def _fetch_yfinance_snapshot(
        self, ticker: str, asof: pd.Timestamp
    ) -> dict | None:
        """Pull yfinance.Ticker fields and reduce to one snapshot row.

        Returns None if yfinance returns an empty info dict (delisted or
        thinly covered).
        """
        import yfinance as yf  # local import; yfinance is heavy

        yt = yf.Ticker(ticker)
        try:
            info = dict(yt.info or {})
        except Exception as exc:  # noqa: BLE001
            logger.debug("fundamentals: ticker.info failed for %s: %s", ticker, exc)
            info = {}
        if not info:
            return None

        # Derived YoY metrics from quarterly frames; tolerate missing data.
        revenue_yoy = float("nan")
        fcf_yoy = float("nan")
        gross_margin_ttm = float("nan")
        gross_margin_prior = float("nan")
        try:
            qf = yt.quarterly_income_stmt
            if qf is not None and not qf.empty and "Total Revenue" in qf.index:
                rev = qf.loc["Total Revenue"].dropna().astype(float)
                if len(rev) >= 5:
                    cur4 = float(rev.iloc[:4].sum())
                    prev4 = float(rev.iloc[4:8].sum()) if len(rev) >= 8 else float("nan")
                    if prev4 and not np.isnan(prev4) and prev4 != 0:
                        revenue_yoy = (cur4 - prev4) / abs(prev4)
            af = yt.income_stmt
            if (
                af is not None
                and not af.empty
                and "Total Revenue" in af.index
                and "Gross Profit" in af.index
            ):
                rev = af.loc["Total Revenue"].dropna().astype(float)
                gp = af.loc["Gross Profit"].dropna().astype(float)
                if len(rev) >= 1 and len(gp) >= 1 and rev.iloc[0]:
                    gross_margin_ttm = float(gp.iloc[0]) / float(rev.iloc[0])
                if len(rev) >= 2 and len(gp) >= 2 and rev.iloc[1]:
                    gross_margin_prior = float(gp.iloc[1]) / float(rev.iloc[1])
        except Exception as exc:  # noqa: BLE001
            logger.debug("fundamentals: derived income_stmt failed %s: %s", ticker, exc)
        try:
            cf = yt.quarterly_cashflow
            if cf is not None and not cf.empty and "Free Cash Flow" in cf.index:
                fcf = cf.loc["Free Cash Flow"].dropna().astype(float)
                if len(fcf) >= 5:
                    cur4 = float(fcf.iloc[:4].sum())
                    prev4 = float(fcf.iloc[4:8].sum()) if len(fcf) >= 8 else float("nan")
                    if prev4 and not np.isnan(prev4) and prev4 != 0:
                        fcf_yoy = (cur4 - prev4) / abs(prev4)
        except Exception as exc:  # noqa: BLE001
            logger.debug("fundamentals: derived cashflow failed %s: %s", ticker, exc)

        # B34: best-effort point-in-time stamps. yfinance does not expose a true
        # SEC filing date, so report_date stays NaT here (the read filter then
        # uses period_end + lag, which is conservative). period_end is the most
        # recent reported quarter end (info["mostRecentQuarter"], unix-epoch).
        period_end = _coerce_epoch_to_ts(info.get("mostRecentQuarter"))

        snapshot = {
            "as_of_date": asof.normalize(),
            "fetched_at": asof,
            "report_date": pd.NaT,
            "period_end": period_end,
            "source": "yfinance",
            "pe_trailing": _coerce_float(info.get("trailingPE")),
            "pe_forward": _coerce_float(info.get("forwardPE")),
            "debt_to_equity": _normalize_dte(info.get("debtToEquity")),
            "free_cash_flow": _coerce_float(info.get("freeCashflow")),
            "revenue_ttm": _coerce_float(info.get("totalRevenue")),
            "eps_trailing": _coerce_float(info.get("trailingEps")),
            "eps_forward": _coerce_float(info.get("forwardEps")),
            "gross_margin_ttm": gross_margin_ttm,
            "gross_margin_prior": gross_margin_prior,
            "revenue_yoy": revenue_yoy,
            "fcf_yoy": fcf_yoy,
            "sector": str(info.get("sector") or ""),
            "currency": str(info.get("currency") or ""),
            "quote_type": str(info.get("quoteType") or ""),
        }
        return snapshot

    # ------------------------------------------------------------------
    # Schema normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_snapshot_row(snap: dict) -> dict:
        out: dict = {}
        for col in _SNAPSHOT_COLUMNS:
            out[col] = snap.get(col)
        return out

    @staticmethod
    def _normalize_snapshot_frame(df: pd.DataFrame) -> pd.DataFrame:
        for col in _SNAPSHOT_COLUMNS:
            if col not in df.columns:
                df[col] = pd.NA
        df = df[list(_SNAPSHOT_COLUMNS.keys())]
        # Ensure timestamps are tz-aware UTC (schema-driven so B34's NULLable
        # report_date / period_end are coerced to NaT-friendly datetime too).
        for col, dtype in _SNAPSHOT_COLUMNS.items():
            if dtype.startswith("datetime64"):
                df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
        # Coerce floats
        for col, dtype in _SNAPSHOT_COLUMNS.items():
            if dtype == "float64":
                df[col] = pd.to_numeric(df[col], errors="coerce")
        # Strings -> string dtype
        for col, dtype in _SNAPSHOT_COLUMNS.items():
            if dtype == "string":
                df[col] = df[col].astype("string")
        return df

    @staticmethod
    def _normalize_sector_median_row(snap: dict) -> dict:
        out: dict = {}
        for col in _SECTOR_MEDIAN_COLUMNS:
            out[col] = snap.get(col)
        return out

    @staticmethod
    def _normalize_sector_median_frame(df: pd.DataFrame) -> pd.DataFrame:
        for col in _SECTOR_MEDIAN_COLUMNS:
            if col not in df.columns:
                df[col] = pd.NA
        df = df[list(_SECTOR_MEDIAN_COLUMNS.keys())]
        for col in ("as_of_date", "fetched_at"):
            df[col] = pd.to_datetime(df[col], utc=True)
        df["median_pe_trailing"] = pd.to_numeric(df["median_pe_trailing"], errors="coerce")
        df["n_constituents"] = pd.to_numeric(df["n_constituents"], errors="coerce").astype(
            "Int64"
        )
        df["sector"] = df["sector"].astype("string")
        return df


__all__ = [
    "FundamentalsProvider",
    "DEFAULT_CACHE_ROOT",
    "DEFAULT_REPORTING_LAG_DAYS",
    "REPORTING_LAG_ENV_FLAG",
]
