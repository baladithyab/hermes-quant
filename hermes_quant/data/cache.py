"""OHLCV file cache for repeatable backtests.

V03-7: provider/symbol/timeframe caches under
`~/.hermes/quant/cache/<provider>/<symbol>-<timeframe>.parquet` with an
append + dedupe + atomic-rename write discipline.

CSV fallback is supported when parquet engines are unavailable, but parquet is
preferred for fidelity and speed.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

DEFAULT_CACHE_ROOT = Path.home() / ".hermes" / "quant" / "cache"

# cs66: the SERVED OHLCV schema (what read() returns and what every downstream
# consumer reads). Unchanged from the original cache.
_SERVED_COLS = ["timestamp", "open", "high", "low", "close", "volume"]

# cs66: the on-disk STORAGE schema adds a ``fetched_at`` wall-clock column so the
# append-side dedup can distinguish a legitimate SAME-DAY intraday correction
# (fetched_at on the same calendar day as the bar's timestamp -> wins) from a
# CROSS-DAY backfill / vendor restatement (fetched_at on a LATER calendar day ->
# must NOT overwrite the historical point-in-time bar). The OHLCV parquet is a
# DERIVED, regenerable cache (not state.db), and read() strips this extra column
# via ``out = out[required]`` in normalize_bars, so a storage file carrying
# fetched_at is forward+backward compatible with the served 6-col read path.
_STORAGE_COLS = ["timestamp", "open", "high", "low", "close", "volume", "fetched_at"]

# cs63: one closed-session day. A market HOLIDAY is one extra closed session day
# adjacent to a known calendar rhythm (a Monday holiday turns a Fri->Mon weekend
# into Fri->Tue; an intraday Fri-close -> next-session-open weekend gap grows by a
# full closed day). The right-edge bound tolerates the recurring rhythm PLUS this
# allowance so a holiday-bordered cache stays fresh, while a one-off multi-month
# (cs43) / interior (cs50) hole — which is many session days, not one — stays
# rejected. ``max(canonical, _HOLIDAY_ALLOWANCE)`` so an intraday holiday (a full
# closed day) and a daily holiday (one 1d step) are both covered.
_HOLIDAY_ALLOWANCE = pd.Timedelta(days=1)

# Timeframe → bar-step seconds. Used to infer the default right-edge staleness
# bound (cs43): one timeframe step is the tightest gap a fresh cache can have
# below the cutoff without indicating stale data.
_TF_SECONDS = {
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
}


def _infer_step(timeframe: str, eligible: pd.DataFrame) -> pd.Timedelta | None:
    """Infer one bar-step as a Timedelta.

    Prefers the declared ``timeframe`` (canonical, exact). Falls back to the
    median spacing of the cached bars when the timeframe is unknown. Returns
    ``None`` when neither is available (no bound can be derived).
    """
    secs = _TF_SECONDS.get(timeframe)
    if secs is not None:
        return pd.Timedelta(seconds=secs)
    if len(eligible) >= 2:
        diffs = eligible["timestamp"].diff().dropna()
        if not diffs.empty:
            step = diffs.median()
            if step > pd.Timedelta(0):
                return step
    return None


def _positive_inter_bar_gaps(timestamps: pd.Series) -> pd.Series:
    """Strictly-positive inter-bar deltas of a timestamp series (cs58 helper).

    Shared by the HIT-path calendar bound and the MISS-path abstain flag bound
    so the diff/positive-filter logic lives in one place. Returns an empty
    Series when fewer than two ordered bars are available.
    """
    if len(timestamps) < 2:
        return pd.Series([], dtype="timedelta64[ns]")
    diffs = timestamps.diff().dropna()
    return diffs[diffs > pd.Timedelta(0)]


def _max_observed_gap(timestamps: pd.Series) -> pd.Timedelta | None:
    """Largest strictly-positive inter-bar gap, or ``None`` when undefined (cs58).

    A self-calibrating ceiling: the cache's own widest observed calendar gap
    (weekend, overnight session) is the most a fresh right edge can legitimately
    sit below the cutoff. Unlike :func:`_calendar_bound` this does NOT require
    recurrence — a single observed weekend gap is enough to vouch for an
    equally-sized right-edge gap.
    """
    diffs = _positive_inter_bar_gaps(timestamps)
    if diffs.empty:
        return None
    return diffs.max()


def _calendar_bound(canonical: pd.Timedelta, timestamps: pd.Series) -> pd.Timedelta:
    """Self-calibrating CONTIGUITY bound for calendar-gapped markets (cs58).

    cs43 set the right-edge staleness bound to ONE literal ``_infer_step``
    (1d->1 day, 1h->1 hour) with zero trading-calendar awareness. Non-24/7
    markets have legitimate calendar gaps at the right edge: a DAILY equity
    cache's freshest real bar is Friday's close, but a backtest ``--end``
    anchored Monday sits ~3 calendar days above Friday > the 1-day step, so the
    HIT was wrongly rejected and the cache refetched on every run even though
    the provider had nothing newer (markets closed Sat/Sun). Same for an
    intraday cache across an overnight session gap.

    We learn the market's own rhythm from the cache: the largest inter-bar gap
    that RECURS (appears at least twice) in the observed spacing is a legitimate
    calendar gap (weekend, overnight); a one-off giant gap does NOT recur and is
    excluded, so the cs50 single interior hole stays rejected. The canonical step
    is the floor (a one-bar provider shortfall is always tolerated; a too-short
    cache with no learnable rhythm degrades to the cs43 canonical bound).

    cs63 SCOPE: this recurrence-based bound now gates ONLY the cs50 CONTIGUITY
    check of the served lookback window — it must stay conservative so a one-off
    interior hole cannot widen the contiguity tolerance. The right-EDGE freshness
    gate uses the looser :func:`_edge_bound` (this bound + one closed session day)
    so a non-recurring market HOLIDAY at the edge does not refetch forever; the
    MISS-path abstain flag uses :func:`_flag_bound`.
    """
    if len(timestamps) < 2:
        return canonical
    diffs = _positive_inter_bar_gaps(timestamps)
    if diffs.empty:
        return canonical
    # Group near-equal gaps by whole seconds; a gap that recurs (>=2) is a
    # calendar rhythm, not a one-off hole.
    secs = diffs.dt.total_seconds().round().astype("int64")
    counts = secs.value_counts()
    recurring = counts[counts >= 2].index
    if len(recurring) == 0:
        return canonical
    largest_recurring = pd.Timedelta(seconds=int(recurring.max()))
    return max(canonical, largest_recurring)


def _edge_bound(canonical: pd.Timedelta, timestamps: pd.Series) -> pd.Timedelta:
    """Right-edge freshness bound that tolerates a non-recurring HOLIDAY (cs63).

    cs58's :func:`_calendar_bound` learns only the cache's RECURRING inter-bar gap
    (the 3-day Fri->Mon weekend, the overnight session gap). A market HOLIDAY
    produces a rarer, longer trailing gap that a single backtest window observes
    <2 times, so the >=2 recurrence gate never learns it: a fresh DAILY cache
    ending Friday with an ``--end`` anchored the Tuesday after a Monday holiday
    sits Fri->Tue = 4 calendar days > the learned 3-day weekend bound, so the
    fresh fully-supplied cache MISSes + refetches on EVERY run (the refetch-forever
    shape cs58 fixed for weekends), and likewise for a 2-session intraday holiday
    gap.

    A holiday is exactly ONE extra closed session day adjacent to a known calendar
    rhythm, so the edge tolerance is the recurring rhythm PLUS one closed session
    day (:data:`_HOLIDAY_ALLOWANCE`, floored to the canonical step). Crucially the
    widening is derived from the RECURRING rhythm, NOT from the cache's largest
    observed gap: an ancient one-off interior delisting hole (many session days)
    does not inflate the edge tolerance, so the cs43 multi-month stale edge and an
    ancient-interior-hole + smaller-stale-edge cache both stay rejected. The cs50
    contiguity check keeps the tighter :func:`_calendar_bound` so a one-off interior
    hole still fails contiguity even though the edge tolerance is widened.
    """
    cal = _calendar_bound(canonical, timestamps)
    return max(canonical, cal + max(canonical, _HOLIDAY_ALLOWANCE))


def _flag_bound(canonical: pd.Timedelta, timestamps: pd.Series) -> pd.Timedelta:
    """MISS-path abstain-flag bound (cs58 Layer-2 + cs63).

    The ``right_edge_stale_days`` flag is an HONESTY signal (not a data-serving
    gate), so it may be looser than the HIT-path :func:`_edge_bound`. cs58 Layer-2
    widened it to the cache's own largest observed inter-bar gap (a single observed
    weekend/overnight gap is enough to vouch for an equal-sized right-edge gap). cs63
    additionally tolerates one closed session day so a holiday-bordered cache that
    happens to MISS for another reason does not false-abstain. A genuinely stale
    cache (cs49: dense hourly, newest months below the cutoff) has only a tiny
    observed gap, far below the multi-month edge gap -> it STILL flags.
    """
    observed = _max_observed_gap(timestamps)
    if observed is None:
        return canonical
    return max(canonical, observed + max(canonical, _HOLIDAY_ALLOWANCE))


@dataclass(frozen=True)
class OhlcvCache:
    """Provider/symbol/timeframe OHLCV cache."""

    provider: str
    symbol: str
    timeframe: str
    root: Path = DEFAULT_CACHE_ROOT
    prefer_parquet: bool = True

    @property
    def directory(self) -> Path:
        return self.root / _safe_component(self.provider)

    @property
    def stem(self) -> str:
        return f"{_safe_component(self.symbol)}-{_safe_component(self.timeframe)}"

    @property
    def parquet_path(self) -> Path:
        return self.directory / f"{self.stem}.parquet"

    @property
    def csv_path(self) -> Path:
        return self.directory / f"{self.stem}.csv"

    @property
    def path(self) -> Path:
        if self.prefer_parquet and self.parquet_path.exists():
            return self.parquet_path
        if self.csv_path.exists():
            return self.csv_path
        return self.parquet_path if self.prefer_parquet else self.csv_path

    def read(self) -> pd.DataFrame:
        path = self.path
        if not path.exists():
            return _empty_bars()
        if path.suffix == ".parquet":
            try:
                df = pd.read_parquet(path)
            except Exception:
                # If parquet engine is absent/corrupt, fall back to CSV if present.
                if self.csv_path.exists():
                    df = pd.read_csv(self.csv_path)
                else:
                    raise
        else:
            df = pd.read_csv(path)
        return normalize_bars(df)

    def write(self, bars: pd.DataFrame) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        df = normalize_bars(bars)
        target = self.parquet_path if self.prefer_parquet else self.csv_path
        try:
            _atomic_write(df, target)
        except Exception:
            # Parquet engine missing is common in minimal installs; degrade to CSV.
            if target.suffix == ".parquet":
                target = self.csv_path
                _atomic_write(df, target)
            else:
                raise
        return target

    def _read_storage(self) -> pd.DataFrame:
        """Read the RAW on-disk 7-col STORAGE frame (cs66), NOT the served frame.

        Unlike :meth:`read` (which routes through ``normalize_bars`` and strips
        any extra column via ``out = out[required]``), this preserves the
        ``fetched_at`` column the append-side PIT dedup needs. It coerces the 6
        OHLCV value columns + drops NaN-keyed rows inline (so it never depends on
        normalize_bars stripping fetched_at), and BACKFILLS a sentinel
        ``fetched_at`` for legacy rows that predate the cs66 storage schema.

        MIGRATION (cs66): a pre-cs66 cache file has no fetched_at column. We
        backfill it with the file mtime (UTC), which is by construction strictly
        AFTER every cached bar's timestamp -> the dedup classifies legacy rows as
        CROSS-DAY (historical / first-write), so a later re-fetch of a legacy
        timestamp can never overwrite it. (No file -> empty frame.) This makes the
        migration idempotent and crash-free: a fetched_at-less old parquet is
        readable, never raises, and worst case the cache simply regenerates on the
        next fetch.
        """
        path = self.path
        if not path.exists():
            return pd.DataFrame(columns=_STORAGE_COLS)
        if path.suffix == ".parquet":
            try:
                df = pd.read_parquet(path)
            except Exception:
                # Mirror read()'s parquet->CSV degrade-on-corrupt-engine fallback.
                if self.csv_path.exists():
                    df = pd.read_csv(self.csv_path)
                else:
                    raise
        else:
            df = pd.read_csv(path)
        if df is None or len(df) == 0:
            return pd.DataFrame(columns=_STORAGE_COLS)
        out = df.copy()
        missing = [c for c in _SERVED_COLS if c not in out.columns]
        if missing:
            raise ValueError(f"OHLCV storage missing required columns: {missing}")
        out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
        for col in ["open", "high", "low", "close", "volume"]:
            out[col] = pd.to_numeric(out[col], errors="coerce")
        # cs66 migration: backfill a sentinel fetched_at for legacy rows. The file
        # mtime is strictly after every cached bar (the file was written no earlier
        # than its newest bar), so legacy rows read as cross-day / historical and
        # are immutable under re-fetch. now() is the fallback when no file/mtime.
        sentinel = _file_mtime_utc(path)
        if "fetched_at" not in out.columns:
            out["fetched_at"] = sentinel
        else:
            out["fetched_at"] = pd.to_datetime(out["fetched_at"], utc=True, errors="coerce")
            out["fetched_at"] = out["fetched_at"].fillna(sentinel)
        out = out[_STORAGE_COLS]
        out = out.dropna(subset=_SERVED_COLS)
        return out.reset_index(drop=True)

    def _write_storage(self, storage_df: pd.DataFrame) -> Path:
        """Persist ALL 7 storage cols (incl fetched_at), mirroring :meth:`write`.

        Does NOT route through ``normalize_bars`` (which would strip fetched_at).
        Keeps :meth:`write`'s mkdir + parquet-target + atomic-write +
        parquet->CSV degrade-on-exception fallback. Public ``write`` is left
        UNCHANGED (it still persists the served 6-col frame); a subsequent
        append's :meth:`_read_storage` backfills the sentinel for that
        fetched_at-less file, exercising + validating the migration path.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        df = storage_df[_STORAGE_COLS].copy()
        target = self.parquet_path if self.prefer_parquet else self.csv_path
        try:
            _atomic_write(df, target)
        except Exception:
            if target.suffix == ".parquet":
                target = self.csv_path
                _atomic_write(df, target)
            else:
                raise
        return target

    def append(self, bars: pd.DataFrame, *, fetched_at: pd.Timestamp | None = None) -> Path:
        """Append bars with a PIT-preserving interior value-rewrite guard (cs66).

        The original 3-line append concatenated incoming AFTER existing and let
        ``normalize_bars`` dedup on timestamp with keep="last", so a re-fetch
        returning a REVISED value for a PAST timestamp (vendor restatement, split
        / dividend re-adjustment, late print, differently-sized window) silently
        OVERWROTE the cached historical bar -> a backtest replayed at the same
        --end read a DIFFERENT historical price -> non-reproducible OOS metrics.
        This is the OHLCV sibling of the fundamentals-side cs42(b)/cs53/cs59/cs61
        point-in-time family.

        The fix mirrors cs59's ``write_sector_median`` dedup, keyed on
        ``timestamp`` (the PIT key, analog of fundamentals' ``as_of_date``): a
        SAME-DAY correction (incoming fetched_at on the same calendar day as the
        bar's timestamp) is the legitimate intraday revision and WINS; a CROSS-DAY
        backfill (fetched_at on a LATER calendar day) does NOT overwrite the
        already-stored historical bar. A first-write of a new timestamp is
        byte-identical (the guard only fires on a re-write of an existing T).

        ``fetched_at`` (default now, UTC) stamps the incoming bars; it is a test
        seam so the same-day vs cross-day cases can be exercised deterministically
        (production always passes now).

        CRITICAL DIVERGENCE FROM cs59 (do NOT "simplify" this back) — cs59 ranks
        rows ``[as_of_date, _same_day, fetched_at]`` then keep="last", i.e. within
        a key it always keeps the LATEST fetched_at. That is correct on the
        fundamentals side because a re-fetch's fetched_at is same-day with its own
        as_of_date. On the OHLCV side a CROSS-DAY re-fetch of a 2024 bar carries a
        2026 fetched_at — under plain keep="last" the LATER (cross-day) re-fetch
        would win and re-introduce the exact overwrite this guards against. So we
        split the fetched_at tie-break BY same-day class: among rows for one
        timestamp, SAME-DAY rows keep the LATEST fetched_at (intraday correction
        wins) and CROSS-DAY rows keep the EARLIEST fetched_at (the original
        first-write historical bar wins), and a same-day row outranks any
        cross-day row for the same timestamp.
        """
        if fetched_at is None:
            fetched_at = pd.Timestamp.now(tz="UTC")
        else:
            fetched_at = pd.Timestamp(fetched_at)
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.tz_localize("UTC")

        existing = self._read_storage()  # 7-col (legacy sentinel already backfilled)
        incoming = normalize_bars(bars).copy()  # 6-col served-normalized
        incoming["fetched_at"] = fetched_at
        incoming = incoming[_STORAGE_COLS]

        # Skip an empty (all-NA columns) existing frame from the concat: it adds
        # nothing and tripped a pandas all-NA-concat FutureWarning. A first-write
        # (empty cache) then flows through incoming alone.
        frames = [f for f in (existing, incoming) if not f.empty]
        merged = pd.concat(frames, ignore_index=True) if frames else incoming
        merged["timestamp"] = pd.to_datetime(merged["timestamp"], utc=True)
        merged["fetched_at"] = pd.to_datetime(merged["fetched_at"], utc=True)

        # cs66 PIT-preserving dedup keyed on timestamp. A row is SAME-DAY when its
        # fetched_at falls on (or before) the bar's own calendar day; otherwise it
        # is a CROSS-DAY backfill. See the docstring for why the tie-break diverges
        # from cs59 (cross-day rows must keep EARLIEST, not LATEST, fetched_at).
        same_day = merged["fetched_at"].dt.normalize() <= merged["timestamp"].dt.normalize()
        sd = merged[same_day]
        cd = merged[~same_day]
        # Same-day: intraday correction wins -> keep LATEST fetched_at.
        sd = sd.sort_values("fetched_at").drop_duplicates(subset=["timestamp"], keep="last")
        # Cross-day: original historical first-write wins -> keep EARLIEST fetched_at.
        cd = cd.sort_values("fetched_at").drop_duplicates(subset=["timestamp"], keep="first")
        # A same-day row outranks a cross-day row for the same timestamp.
        combined = pd.concat([cd, sd], ignore_index=True)
        combined = combined.drop_duplicates(subset=["timestamp"], keep="last")
        combined = combined.sort_values("timestamp").reset_index(drop=True)
        return self._write_storage(combined)

    def coverage(self) -> dict:
        df = self.read()
        if df.empty:
            return {
                "path": str(self.path),
                "exists": self.path.exists(),
                "n_bars": 0,
                "start": None,
                "end": None,
            }
        return {
            "path": str(self.path),
            "exists": self.path.exists(),
            "n_bars": len(df),
            "start": df["timestamp"].iloc[0].isoformat(),
            "end": df["timestamp"].iloc[-1].isoformat(),
        }


def cached_fetch(
    fetch_fn,
    *,
    provider: str,
    symbol: str,
    timeframe: str,
    lookback_bars: int,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    prefer_parquet: bool = True,
    min_hit_ratio: float = 0.95,
    cutoff: pd.Timestamp | None = None,
    max_staleness: pd.Timedelta | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Read-through cache for provider fetches.

    fetch_fn is called only when cached coverage is materially short of
    lookback_bars. A small tolerance is intentional: exchanges can return fewer
    bars than requested because of gaps, maintenance, or closed-bar filtering;
    without tolerance repeated backtests refetch forever even though the cache
    already contains all the provider will return.

    It must return a normalized-ish OHLCV DataFrame; this function normalizes,
    appends/dedupes, and returns the last lookback_bars rows (or all cached rows
    if the provider never supplies that many).

    NO-LOOKAHEAD (cs38): ``cutoff`` is the as_of/end anchor of the caller
    (a backtest derives it from ``--end``). The cache file accumulates bars up
    to each prior fetch's wall-clock, so a warm cache populated to a LATER date
    must never serve bars that post-date the current backtest anchor. When
    ``cutoff`` is set:

    * the cache-HIT path prunes the returned bars to ``timestamp <= cutoff``;
    * the HIT is gated on having enough bars AT-OR-BEFORE the cutoff
      (``len(cached[timestamp <= cutoff]) >= min_hit_bars``), not just enough
      bars total — otherwise a cache full of future bars would falsely satisfy
      the hit threshold and then return too few past bars.

    ``cutoff=None`` (a live/up-to-now caller) prunes nothing and is
    byte-identical to the prior behaviour.

    RIGHT-EDGE STALENESS (cs43): cs38 made the HIT count only at-or-before bars
    but never bounded how FAR the newest at-or-before bar sits below ``cutoff``.
    A cache whose newest bar ends months before ``--end`` (yet still has
    ``>=min_hit_bars`` below the cutoff) would HIT and serve stale right-edge
    data the promotion gate then trusts. So when ``cutoff`` is set we
    additionally require the newest eligible bar to be within ``max_staleness``
    of the cutoff::

        (cutoff - eligible["timestamp"].max()) <= max_staleness

    ``max_staleness`` defaults to one timeframe step (inferred from
    ``timeframe``, or the cached bars' median spacing if the timeframe is
    unknown) — the tightest gap a fresh cache can have below the cutoff. When
    the bound can't be derived (unknown timeframe, <2 bars), the gate degrades
    to count-only (cs38 behaviour). A too-stale right edge fails the HIT and
    falls through to the MISS/fetch path so the provider supplies fresh bars.
    ``cutoff=None`` imposes no staleness bound.

    CONTIGUITY (cs50): cs38+cs43 gate the COUNT and the right EDGE but not the
    interior of the served window. A cache of 200 ancient bars plus a single
    fresh bar AT the cutoff satisfies both gates (201 eligible; newest ==
    cutoff) yet the served lookback tail glues ancient bars to the lone fresh
    bar across a multi-month INTERIOR hole, and the backtest then computes a
    spurious giant return across the seam. So when ``cutoff`` is set we also
    require the SERVED lookback window to be contiguous: its max inter-bar gap
    must be ``<= bound * 1.5`` (one missing closed bar tolerated; a multi-step
    hole not). The bound scales with an explicit ``max_staleness``. A
    discontiguous served tail fails the HIT and falls through to MISS/fetch.
    ``cutoff=None`` imposes no contiguity check.

    CALENDAR + HOLIDAY EDGE (cs58/cs63): cs43's literal one-step bound over-tightens
    for non-24/7 markets and refetches a fresh cache forever across a weekend or an
    overnight session gap. cs58 widened the edge bound to the cache's own RECURRING
    inter-bar gap (the weekend/overnight rhythm). cs63 additionally tolerates one
    closed session day: a market HOLIDAY is a rarer, longer trailing gap a single
    backtest window observes <2 times (a Friday close before a Monday holiday, with
    ``--end`` anchored the Tuesday after, is Fri->Tue = 4 calendar days > the learned
    3-day weekend bound), so the recurrence-only bound never learns it and the fresh
    cache refetched forever. The HIT-path EDGE gate uses ``_edge_bound`` (recurring
    rhythm + one closed session day) while the cs50 CONTIGUITY check keeps the tighter
    recurrence-only ``_calendar_bound``, so a non-recurring holiday at the EDGE stays
    fresh but a one-off multi-month (cs43) / interior (cs50) hole — derived from the
    recurring rhythm, NOT the largest observed gap, so an ancient interior delisting
    hole never inflates the edge tolerance — stays rejected.

    MISS RIGHT-EDGE STALENESS (cs49): on a MISS the provider may ALSO be unable
    to supply bars up to ``cutoff`` (delisted symbol / provider lagging / short
    window). Two harms follow: (1) the served result keeps a stale right edge
    with NO signal, so the gate trusts it as fresh, and (2) re-fetching +
    re-appending the same stale window on every run is a refetch-forever /
    cache-churn loop. When ``cutoff`` is set we therefore: skip the cache append
    when the just-fetched window does not ADVANCE the eligible right edge (no
    churn; cs43's legitimate stale-cache + fresh-provider refresh still appends
    + advances); and emit ``meta['right_edge_stale_days']`` when the merged
    right edge remains beyond ``bound`` of the cutoff so the caller can ABSTAIN.
    ``cutoff=None`` adds no flag and never skips the append.
    """
    cache = OhlcvCache(
        provider=provider,
        symbol=symbol,
        timeframe=timeframe,
        root=cache_root,
        prefer_parquet=prefer_parquet,
    )
    cached = cache.read()
    min_hit_bars = max(1, int(lookback_bars * min_hit_ratio))
    eligible = cached if cutoff is None else cached[cached["timestamp"] <= cutoff]
    enough_bars = len(eligible) >= min_hit_bars
    # cs43: a count-satisfying cache can still have a multi-month gap below the
    # cutoff. Reject the HIT when the newest eligible bar is staler than the
    # bound. cutoff=None imposes no bound (live caller); the bound is also a
    # no-op when it can't be derived (degrades to cs38 count-only).
    fresh_right_edge = True
    # cs50: a count + right-edge satisfying HIT can still glue ancient bars to a
    # single fresh bar across a multi-month INTERIOR hole (200 ancient bars + 1
    # bar AT the cutoff -> 201 eligible, newest == cutoff). Serving that tail
    # makes the backtest compute a spurious giant return across the seam.
    # Require the SERVED lookback window to be contiguous (max inter-bar gap
    # within the tail <= one step * tolerance) else fall through to MISS/fetch.
    # cutoff=None imposes no contiguity check; no-op when the bound can't be
    # derived (degrades to cs38/cs43 behaviour).
    contiguous = True
    if cutoff is not None and enough_bars:
        bound = max_staleness if max_staleness is not None else _infer_step(timeframe, eligible)
        if bound is not None:
            # cs58: a literal one-step bound over-tightens for calendar-gapped
            # markets (a Fri->Mon weekend, an overnight session gap) and
            # refetches a fresh cache forever. The HIT path uses TWO bounds:
            #   * cs63 edge bound: the cache's recurring rhythm PLUS one closed
            #     session day, so a non-recurring market HOLIDAY at the edge
            #     (Fri-close + Mon-holiday, Tuesday anchor) stays fresh and does
            #     not refetch forever, while a one-off cs43 multi-month stale edge
            #     (many session days) stays rejected;
            #   * cs50 contiguity bound: the TIGHTER recurrence-only bound (NOT
            #     holiday-widened) so a one-off multi-step interior hole still
            #     fails contiguity even though the edge tolerance is widened.
            edge_bound = _edge_bound(bound, eligible["timestamp"])
            contig_bound = _calendar_bound(bound, eligible["timestamp"])
            newest_eligible = eligible["timestamp"].max()
            fresh_right_edge = (cutoff - newest_eligible) <= edge_bound
            served = eligible.tail(min(lookback_bars, len(eligible)))
            if len(served) >= 2:
                max_gap = served["timestamp"].diff().dropna().max()
                # One missing closed bar (or one recurring calendar gap) is
                # tolerated; a one-off multi-step interior hole is not.
                contiguous = max_gap <= contig_bound * 1.5
    if enough_bars and fresh_right_edge and contiguous:
        out = eligible.tail(min(lookback_bars, len(eligible))).reset_index(drop=True)
        return out, {
            "cache_hit": True,
            "cache": cache.coverage(),
            "requested_lookback_bars": lookback_bars,
            "min_hit_bars": min_hit_bars,
        }

    # cs49: on a MISS, capture the pre-fetch eligible right edge so we can detect
    # a provider that cannot advance it (delisted symbol / provider also lagging
    # / short window). Re-fetching + re-appending the SAME stale window on every
    # run is a refetch-forever / cache-churn loop, and the served result keeps a
    # stale right edge with no signal the gate could ABSTAIN on.
    pre_fetch_max = None
    if cutoff is not None and not eligible.empty:
        pre_fetch_max = eligible["timestamp"].max()
    fetched = normalize_bars(fetch_fn())
    fetched_advances = True
    if pre_fetch_max is not None:
        fetched_eligible = fetched[fetched["timestamp"] <= cutoff]
        fetched_advances = (
            not fetched_eligible.empty
            and fetched_eligible["timestamp"].max() > pre_fetch_max
        )
    if fetched_advances:
        # cs43's legitimate refresh fetch (stale cache + FRESH provider) lands here.
        path = cache.append(fetched)
        merged = cache.read()
    else:
        # Provider did not advance the right edge: skip the append (no churn) and
        # serve the existing merged cache.
        path = cache.path
        merged = cached
    if cutoff is not None:
        merged = merged[merged["timestamp"] <= cutoff]
    out = merged.tail(min(lookback_bars, len(merged))).reset_index(drop=True)
    meta = {
        "cache_hit": False,
        "cache_path": str(path),
        "cache": cache.coverage(),
        "fetched_bars": len(fetched),
        "requested_lookback_bars": lookback_bars,
        "min_hit_bars": min_hit_bars,
    }
    # cs49: surface an honest right-edge staleness signal so the caller can
    # ABSTAIN rather than silently trust a stale window. cutoff=None -> no flag.
    #
    # cs58 Layer-2 + cs63: the bare canonical one-step bound over-flags a fresh
    # calendar-gapped cache. A 2-trading-week DAILY cache (Friday close) with a
    # Monday cutoff sits 3 calendar days above its newest bar > the 1-day step,
    # so the canonical bound wrongly emits ``right_edge_stale_days`` and the
    # caller ABSTAINS on a demonstrably-fresh cache. The flag is an honesty
    # signal (not a data-serving gate), so it may be looser than the HIT gate's
    # ``_edge_bound``: :func:`_flag_bound` widens it to the cache's own largest
    # observed inter-bar gap (cs58 L2) plus one closed session day (cs63 holiday).
    # A genuinely stale cache (cs49: dense hourly, newest months below the cutoff)
    # has only a 1h largest observed gap, far below the multi-month edge gap -> it
    # STILL flags.
    if cutoff is not None and not merged.empty:
        bound = max_staleness if max_staleness is not None else _infer_step(timeframe, merged)
        if bound is not None:
            flag_bound = _flag_bound(bound, merged["timestamp"])
            gap = cutoff - merged["timestamp"].max()
            if gap > flag_bound:
                meta["right_edge_stale_days"] = int(gap / pd.Timedelta(days=1))
    return out, meta


def normalize_bars(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return _empty_bars()
    out = df.copy()
    required = ["timestamp", "open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in out.columns]
    if missing:
        raise ValueError(f"OHLCV bars missing required columns: {missing}")
    out = out[required]
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=required)
    out = out.drop_duplicates(subset=["timestamp"], keep="last")
    out = out.sort_values("timestamp").reset_index(drop=True)
    return out


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])


def _file_mtime_utc(path: Path) -> pd.Timestamp:
    """cs66 migration sentinel: the file's last-modified time as a UTC Timestamp.

    Used to backfill ``fetched_at`` for legacy rows in a pre-cs66 cache file. A
    file is written no earlier than its newest bar, so the mtime is by
    construction strictly after every cached bar -> the dedup classifies legacy
    rows as cross-day / historical and a re-fetch can never overwrite them. Falls
    back to now() when the file is missing or its mtime can't be read.
    """
    try:
        return pd.Timestamp(path.stat().st_mtime, unit="s", tz="UTC")
    except (OSError, ValueError, OverflowError):
        return pd.Timestamp.now(tz="UTC")


def _safe_component(value: str) -> str:
    value = value.strip().replace("/", "_")
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return value.strip("._") or "unknown"


def _atomic_write(df: pd.DataFrame, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        if target.suffix == ".parquet":
            df.to_parquet(tmp, index=False)
        else:
            df.to_csv(tmp, index=False)
        tmp.replace(target)
    finally:
        if tmp.exists():
            tmp.unlink()
