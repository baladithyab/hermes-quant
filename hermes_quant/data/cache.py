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
    if len(eligible) >= min_hit_bars:
        out = eligible.tail(min(lookback_bars, len(eligible))).reset_index(drop=True)
        return out, {
            "cache_hit": True,
            "cache": cache.coverage(),
            "requested_lookback_bars": lookback_bars,
            "min_hit_bars": min_hit_bars,
        }

    fetched = normalize_bars(fetch_fn())
    path = cache.append(fetched)
    merged = cache.read()
    if cutoff is not None:
        merged = merged[merged["timestamp"] <= cutoff]
    out = merged.tail(min(lookback_bars, len(merged))).reset_index(drop=True)
    return out, {
        "cache_hit": False,
        "cache_path": str(path),
        "cache": cache.coverage(),
        "fetched_bars": len(fetched),
        "requested_lookback_bars": lookback_bars,
        "min_hit_bars": min_hit_bars,
    }


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
