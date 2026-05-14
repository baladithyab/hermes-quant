"""Tests for V03-7 OHLCV file cache."""
from __future__ import annotations

import numpy as np
import pandas as pd

from hermes_quant.data.cache import OhlcvCache, cached_fetch, normalize_bars


def _bars(n=10, start="2024-01-01", *, seed=1):
    rng = np.random.default_rng(seed)
    ts = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    close = 100 + np.cumsum(rng.normal(0, 0.1, n))
    return pd.DataFrame({
        "timestamp": ts,
        "open": close - 0.1,
        "high": close + 0.2,
        "low": close - 0.2,
        "close": close,
        "volume": 1000.0,
    })


def test_safe_path_uses_provider_symbol_timeframe(tmp_path):
    cache = OhlcvCache("ccxt:kraken", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=False)
    assert cache.csv_path.name == "BTC_USDT-1h.csv"
    assert "ccxt_kraken" in str(cache.csv_path)


def test_write_and_read_round_trip_csv(tmp_path):
    cache = OhlcvCache("ccxt:kraken", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=False)
    path = cache.write(_bars(10))
    assert path.exists()
    out = cache.read()
    assert len(out) == 10
    assert list(out.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert str(out["timestamp"].dt.tz) == "UTC"


def test_append_dedupes_and_sorts(tmp_path):
    cache = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=False)
    a = _bars(10)
    b = pd.concat([a.iloc[5:], _bars(5, start="2024-01-01 10:00", seed=2)], ignore_index=True)
    cache.append(a)
    cache.append(b)
    out = cache.read()
    assert len(out) == 15
    assert out["timestamp"].is_monotonic_increasing
    assert out["timestamp"].is_unique


def test_cached_fetch_hits_when_enough_bars(tmp_path):
    calls = {"n": 0}
    cache = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=False)
    cache.write(_bars(20))

    def fetch():
        calls["n"] += 1
        return _bars(20)

    out, meta = cached_fetch(
        fetch,
        provider="ccxt",
        symbol="BTC/USDT",
        timeframe="1h",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
    )
    assert calls["n"] == 0
    assert meta["cache_hit"] is True
    assert len(out) == 10


def test_cached_fetch_misses_then_writes(tmp_path):
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return _bars(20)

    out, meta = cached_fetch(
        fetch,
        provider="ccxt",
        symbol="BTC/USDT",
        timeframe="1h",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
    )
    assert calls["n"] == 1
    assert meta["cache_hit"] is False
    assert len(out) == 10
    assert meta["cache"]["n_bars"] == 20


def test_cached_fetch_hit_tolerates_exchange_shortfall(tmp_path):
    """If the cache has >=95% of requested bars, don't refetch forever.
    Exchanges often return fewer bars than requested after closed-bar filters.
    """
    calls = {"n": 0}
    cache = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=False)
    cache.write(_bars(95))

    def fetch():
        calls["n"] += 1
        return _bars(95)

    out, meta = cached_fetch(
        fetch,
        provider="ccxt",
        symbol="BTC/USDT",
        timeframe="1h",
        lookback_bars=100,
        cache_root=tmp_path,
        prefer_parquet=False,
    )
    assert calls["n"] == 0
    assert meta["cache_hit"] is True
    assert meta["min_hit_bars"] == 95
    assert len(out) == 95


def test_normalize_drops_bad_rows_and_duplicates():
    df = _bars(5)
    dup = df.iloc[[2]].copy()
    dup["close"] = 999.0
    bad = df.iloc[[3]].copy()
    bad["close"] = None
    out = normalize_bars(pd.concat([df, dup, bad], ignore_index=True))
    assert len(out) == 5
    assert out.loc[out["timestamp"] == df.iloc[2]["timestamp"], "close"].iloc[0] == 999.0
    assert out["timestamp"].is_unique


def test_coverage_empty_then_populated(tmp_path):
    cache = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=False)
    empty = cache.coverage()
    assert empty["n_bars"] == 0
    cache.write(_bars(3))
    cov = cache.coverage()
    assert cov["n_bars"] == 3
    assert cov["start"] is not None
    assert cov["end"] is not None
