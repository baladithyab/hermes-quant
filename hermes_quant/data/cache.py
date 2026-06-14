"""OHLCV file cache for repeatable backtests.

V03-7: provider/symbol/timeframe caches under
`~/.hermes/quant/cache/<provider>/<symbol>-<timeframe>.parquet` with an
append + dedupe + atomic-rename write discipline.

CSV fallback is supported when parquet engines are unavailable, but parquet is
preferred for fidelity and speed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
import tempfile

import pandas as pd


DEFAULT_CACHE_ROOT = Path.home() / ".hermes" / "quant" / "cache"

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

    def append(self, bars: pd.DataFrame) -> Path:
        existing = self.read()
        incoming = normalize_bars(bars)
        merged = pd.concat([existing, incoming], ignore_index=True)
        merged = normalize_bars(merged)
        return self.write(merged)

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
            newest_eligible = eligible["timestamp"].max()
            fresh_right_edge = (cutoff - newest_eligible) <= bound
            served = eligible.tail(min(lookback_bars, len(eligible)))
            if len(served) >= 2:
                max_gap = served["timestamp"].diff().dropna().max()
                # One missing closed bar is tolerated; a multi-step hole is not.
                contiguous = max_gap <= bound * 1.5
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
    if cutoff is not None and not merged.empty:
        bound = max_staleness if max_staleness is not None else _infer_step(timeframe, merged)
        if bound is not None:
            gap = cutoff - merged["timestamp"].max()
            if gap > bound:
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
