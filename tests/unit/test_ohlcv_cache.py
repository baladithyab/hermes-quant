"""Tests for V03-7 OHLCV file cache."""

from __future__ import annotations

import numpy as np
import pandas as pd

from hermes_quant.data.cache import OhlcvCache, cached_fetch, normalize_bars


def _bars(n=10, start="2024-01-01", *, seed=1):
    rng = np.random.default_rng(seed)
    ts = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    close = 100 + np.cumsum(rng.normal(0, 0.1, n))
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": close - 0.1,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": 1000.0,
        }
    )


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


def test_cached_fetch_hit_prunes_to_cutoff_no_lookahead(tmp_path):
    """cs38: a warm cache populated to a LATER date must not serve bars that
    post-date the backtest cutoff on a HIT.
    """
    calls = {"n": 0}
    cache = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=False)
    # Cache covers 2024-01-01 .. (200 hourly bars -> well past Jan 3).
    cache.write(_bars(200, start="2024-01-01"))

    def fetch():
        calls["n"] += 1
        return _bars(200, start="2024-01-01")

    cutoff = pd.Timestamp("2024-01-03T00:00:00Z")  # 49 bars at-or-before
    out, meta = cached_fetch(
        fetch,
        provider="ccxt",
        symbol="BTC/USDT",
        timeframe="1h",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    assert calls["n"] == 0  # enough at-or-before bars -> still a HIT
    assert meta["cache_hit"] is True
    assert len(out) == 10
    # No bar in the result may post-date the cutoff (the leak).
    assert (out["timestamp"] <= cutoff).all()
    assert out["timestamp"].iloc[-1] <= cutoff


def test_cached_fetch_hit_gate_counts_only_at_or_before_cutoff(tmp_path):
    """cs38: a cache full of FUTURE bars must not falsely satisfy the hit
    threshold. With too few at-or-before bars, fall through to a MISS/fetch
    rather than serving future bars.
    """
    calls = {"n": 0}
    cache = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=False)
    # 100 bars total, but only 3 fall at-or-before the cutoff.
    cache.write(_bars(100, start="2024-01-01"))

    def fetch():
        calls["n"] += 1
        # Provider supplies the past window the backtest actually needs.
        return _bars(20, start="2023-12-20")

    cutoff = pd.Timestamp("2024-01-01T02:00:00Z")  # only bars 0,1,2 are <= cutoff
    out, meta = cached_fetch(
        fetch,
        provider="ccxt",
        symbol="BTC/USDT",
        timeframe="1h",
        lookback_bars=20,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    # 3 at-or-before bars < min_hit_bars(=19) -> MISS, fetch invoked.
    assert calls["n"] == 1
    assert meta["cache_hit"] is False
    # Result still respects the cutoff after the fetch+merge.
    assert (out["timestamp"] <= cutoff).all()


def test_cached_fetch_cutoff_none_byte_identical(tmp_path):
    """cs38: cutoff=None (live/up-to-now caller) prunes nothing -> identical to
    the prior behaviour (returns the most-recent lookback_bars).
    """
    calls = {"n": 0}
    cache = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=False)
    cache.write(_bars(20, start="2024-01-01"))

    def fetch():
        calls["n"] += 1
        return _bars(20, start="2024-01-01")

    out, meta = cached_fetch(
        fetch,
        provider="ccxt",
        symbol="BTC/USDT",
        timeframe="1h",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=None,
    )
    assert calls["n"] == 0
    assert meta["cache_hit"] is True
    assert len(out) == 10
    # Most-recent 10 bars (tail), unchanged from pre-cs38 behaviour.
    full = cache.read()
    assert out["timestamp"].iloc[-1] == full["timestamp"].iloc[-1]


def test_cached_fetch_hit_rejects_stale_right_edge(tmp_path):
    """cs43: post-cs38 the HIT gate counts bars at-or-before the cutoff but does
    NOT bound how far the newest at-or-before bar is from the cutoff. A cache
    whose newest bar ends MONTHS before --end (yet has >=min_hit_bars below the
    cutoff) currently HITs and serves stale right-edge data the promotion gate
    then trusts. It must instead fall through to the MISS/fetch path so the
    provider supplies fresh bars up to the anchor.
    """
    calls = {"n": 0}
    cache = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=False)
    # 300 hourly bars from 2024-01-01: newest bar ~ 2024-01-13.
    cache.write(_bars(300, start="2024-01-01"))

    def fetch():
        calls["n"] += 1
        # Provider supplies fresh bars up to the anchor.
        return _bars(300, start="2024-05-20")

    # Anchor is ~147 days past the cache's newest bar (2024-01-13) but there are
    # 300 bars at-or-before it -> count alone says HIT.
    cutoff = pd.Timestamp("2024-06-01T00:00:00Z")
    out, meta = cached_fetch(
        fetch,
        provider="ccxt",
        symbol="BTC/USDT",
        timeframe="1h",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    # The newest at-or-before bar is months below the anchor -> staleness gate
    # rejects the HIT and falls through to a fetch.
    assert calls["n"] == 1
    assert meta["cache_hit"] is False
    assert (out["timestamp"] <= cutoff).all()


def test_cached_fetch_hit_allows_fresh_right_edge(tmp_path):
    """cs43: a fresh cache whose newest at-or-before bar is within ~1 timeframe
    step of the cutoff must STILL HIT (no over-tightening). The staleness bound
    is for a multi-month gap, not a one-bar provider shortfall at the edge.
    """
    calls = {"n": 0}
    cache = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=False)
    # 200 hourly bars from 2024-01-01: bar index 50 is 2024-01-03T02:00Z.
    cache.write(_bars(200, start="2024-01-01"))

    def fetch():
        calls["n"] += 1
        return _bars(200, start="2024-01-01")

    # Anchor one hour (one 1h step) past the newest eligible bar (index 50).
    cutoff = pd.Timestamp("2024-01-03T03:00:00Z")  # 51 bars at-or-before
    out, meta = cached_fetch(
        fetch,
        provider="ccxt",
        symbol="BTC/USDT",
        timeframe="1h",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    assert calls["n"] == 0  # newest eligible bar within one step -> HIT
    assert meta["cache_hit"] is True
    assert len(out) == 10
    assert (out["timestamp"] <= cutoff).all()


def test_cached_fetch_stale_gate_disabled_when_cutoff_none(tmp_path):
    """cs43: cutoff=None (live/up-to-now caller) imposes no staleness bound, so a
    cache whose newest bar is far from "now" still HITs -> byte-identical to the
    pre-cs43 behaviour.
    """
    calls = {"n": 0}
    cache = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=False)
    # Cache ends long ago; with cutoff=None there is no anchor to be stale against.
    cache.write(_bars(50, start="2020-01-01"))

    def fetch():
        calls["n"] += 1
        return _bars(50, start="2020-01-01")

    out, meta = cached_fetch(
        fetch,
        provider="ccxt",
        symbol="BTC/USDT",
        timeframe="1h",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=None,
    )
    assert calls["n"] == 0
    assert meta["cache_hit"] is True
    assert len(out) == 10


def test_cached_fetch_max_staleness_explicit_override(tmp_path):
    """cs43: an explicit max_staleness widens/narrows the bound. A generous
    override lets an otherwise-stale cache HIT (operator opt-in).
    """
    calls = {"n": 0}
    cache = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=False)
    cache.write(_bars(300, start="2024-01-01"))  # newest ~ 2024-01-13

    def fetch():
        calls["n"] += 1
        return _bars(300, start="2024-05-20")

    cutoff = pd.Timestamp("2024-06-01T00:00:00Z")
    out, meta = cached_fetch(
        fetch,
        provider="ccxt",
        symbol="BTC/USDT",
        timeframe="1h",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
        max_staleness=pd.Timedelta(days=365),  # operator opts into a wide bound
    )
    assert calls["n"] == 0
    assert meta["cache_hit"] is True


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
