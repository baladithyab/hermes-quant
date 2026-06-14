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


def _bars_from_ts(ts, *, seed=1):
    """OHLCV frame for an explicit (calendar-gapped) timestamp index."""
    ts = pd.DatetimeIndex(ts)
    n = len(ts)
    rng = np.random.default_rng(seed)
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


def _daily_business_bars(start, end):
    """Mon-Fri daily bars (legitimate Fri->Mon weekend gaps at the right edge)."""
    return _bars_from_ts(pd.bdate_range(start, end, tz="UTC"))


def _intraday_rth_bars(start, end, *, open_h=14, close_h=20, holidays=()):
    """1h Mon-Fri RTH-window bars with overnight AND weekend session gaps.

    ``holidays`` (date strings) are dropped so a closed-holiday session produces a
    longer-than-overnight/weekend trailing gap (cs63).
    """
    skip = {pd.Timestamp(d, tz="UTC") for d in holidays}
    ts = []
    for day in pd.bdate_range(start, end, tz="UTC"):
        if day in skip:
            continue
        for h in range(open_h, close_h + 1):
            ts.append(day + pd.Timedelta(hours=h))
    return _bars_from_ts(pd.DatetimeIndex(ts), seed=2)


def _daily_business_bars_with_holidays(start, end, holidays):
    """Mon-Fri daily bars with the given ``holidays`` (date strings) removed.

    A removed Monday/Friday holiday produces a 4-calendar-day trailing gap
    (Fri->Tue or Thu->Mon) that exceeds the 3-day weekend rhythm (cs63).
    """
    days = pd.bdate_range(start, end, tz="UTC")
    skip = {pd.Timestamp(d, tz="UTC") for d in holidays}
    return _bars_from_ts(pd.DatetimeIndex([d for d in days if d not in skip]))


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


def test_cached_fetch_miss_flags_stale_right_edge(tmp_path):
    """cs49: on a MISS where the provider ALSO cannot supply bars up to the
    cutoff (delisted symbol / provider lagging / short window), the served
    result keeps a stale right edge but emits NO staleness signal in meta, so
    the promotion gate trusts it as fresh. The MISS path must re-apply the cs43
    right-edge check to the MERGED window and surface ``right_edge_stale_days``
    so the caller can ABSTAIN rather than silently trust the stale data.
    """
    cache = OhlcvCache("ccxt", "DEAD", "1h", root=tmp_path, prefer_parquet=False)
    # Short warm cache below min_hit_bars -> guarantees a MISS by count.
    cache.write(_bars(5, start="2024-01-01"))

    def fetch():
        # Provider is also lagging -> never reaches the cutoff (2024-06-01).
        return _bars(300, start="2024-01-01")  # newest ~ 2024-01-13

    cutoff = pd.Timestamp("2024-06-01T00:00:00Z")
    out, meta = cached_fetch(
        fetch,
        provider="ccxt",
        symbol="DEAD",
        timeframe="1h",
        lookback_bars=100,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    assert meta["cache_hit"] is False
    # The merged right edge is ~139 days below the cutoff: surface it.
    assert meta["right_edge_stale_days"] >= 139
    assert (out["timestamp"] <= cutoff).all()


def test_cached_fetch_miss_caps_refetch_once_window_is_complete(tmp_path):
    """cs49: a warm cache holding the provider's FULL stale window is
    count-satisfied, so the cs43 right-edge gate rejects the HIT and falls to a
    MISS. Today the MISS re-fetches the SAME stale window and re-appends it to
    the cache on EVERY run -> a refetch-forever / cache-churn loop for any
    symbol the provider cannot supply up to the cutoff. The fix: when the just
    -fetched window does not ADVANCE the right edge, skip the cache append (no
    churn) and serve the existing merged tail; still emit the abstain flag.
    """
    cache = OhlcvCache("ccxt", "DEAD", "1h", root=tmp_path, prefer_parquet=False)
    # Warm cache already holds the provider's full STALE window (count-satisfied
    # at-or-before the cutoff) but its right edge is far below the anchor.
    cache.write(_bars(300, start="2024-01-01"))  # newest ~ 2024-01-13

    def fetch():
        return _bars(300, start="2024-01-01")  # provider capped: same stale window

    cutoff = pd.Timestamp("2024-06-01T00:00:00Z")
    kwargs = dict(
        provider="ccxt",
        symbol="DEAD",
        timeframe="1h",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    # Spy on the real append to lock in the no-churn property directly. Asserting
    # only n_bars equality is a tautology here (OhlcvCache.append dedupes on
    # timestamp, so n_bars stays constant even on the churning baseline). The
    # actual fix is that append is NOT called when the fetch can't advance the
    # right edge, and the cache file is NOT rewritten -> spy on both.
    append_calls = {"n": 0}
    orig_append = OhlcvCache.append

    def spy_append(self, b):
        append_calls["n"] += 1
        return orig_append(self, b)

    OhlcvCache.append = spy_append
    cache_path = cache.path
    mtime_before = cache_path.stat().st_mtime_ns if cache_path.exists() else None
    try:
        out1, m1 = cached_fetch(fetch, **kwargs)
        out2, m2 = cached_fetch(fetch, **kwargs)
    finally:
        OhlcvCache.append = orig_append
    mtime_after = cache_path.stat().st_mtime_ns if cache_path.exists() else None
    # cs43 right-edge rejects the count-satisfied HIT -> both runs MISS.
    assert m1["cache_hit"] is False
    assert m2["cache_hit"] is False
    # The fetch did not advance the right edge -> the cache must NOT churn:
    # no append call and the cache file is never rewritten across both runs.
    assert append_calls["n"] == 0
    assert mtime_after == mtime_before
    assert m1["cache"]["n_bars"] == m2["cache"]["n_bars"]
    # Served output is deterministic run1 -> run2 (same stale tail).
    assert list(out1["timestamp"]) == list(out2["timestamp"])
    # The honest staleness signal is present so the caller can ABSTAIN.
    assert m2["right_edge_stale_days"] >= 139


def test_cached_fetch_hit_rejects_discontiguous_interior_hole(tmp_path):
    """cs50: 200 ancient hourly bars + a SINGLE fresh bar AT the cutoff. The
    count gate passes (201 eligible >= min_hit_bars) AND the cs43 right-edge
    gate passes (newest_eligible == cutoff, gap 0), so the HIT serves the
    lookback tail = ancient bars glued to the lone fresh bar across a ~143-day
    INTERIOR hole as if one timeframe step. The backtest then computes returns
    across the hole -> a spurious giant return at the seam. The served window
    must be CONTIGUOUS or the HIT falls through to a MISS/fetch.
    """
    calls = {"n": 0}
    cache = OhlcvCache("ccxt", "HOLE", "1h", root=tmp_path, prefer_parquet=False)
    ancient = _bars(200, start="2024-01-01")  # ends ~ 2024-01-09
    fresh1 = _bars(1, start="2024-06-01T00:00:00Z", seed=9)  # single bar AT cutoff
    cache.write(pd.concat([ancient, fresh1], ignore_index=True))

    def fetch():
        calls["n"] += 1
        return _bars(300, start="2024-05-20")  # provider fills the contiguous window

    cutoff = pd.Timestamp("2024-06-01T00:00:00Z")
    out, meta = cached_fetch(
        fetch,
        provider="ccxt",
        symbol="HOLE",
        timeframe="1h",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    assert calls["n"] == 1  # discontiguous served tail -> MISS/fetch
    assert meta["cache_hit"] is False


def test_cached_fetch_hit_allows_contiguous_window(tmp_path):
    """cs50: a fully contiguous fresh cache (no interior hole, newest == cutoff)
    must STILL HIT. The contiguity check rejects multi-step interior holes, not
    a sound dense window (no over-tightening).
    """
    calls = {"n": 0}
    cache = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=False)
    cache.write(_bars(200, start="2024-01-01"))  # contiguous hourly bars

    def fetch():
        calls["n"] += 1
        return _bars(200, start="2024-01-01")

    # Anchor exactly on the newest cached bar: 199 hours past 2024-01-01.
    cutoff = pd.Timestamp("2024-01-01T00:00:00Z") + pd.Timedelta(hours=199)
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
    assert calls["n"] == 0
    assert meta["cache_hit"] is True
    assert len(out) == 10


def test_cached_fetch_discontiguous_cutoff_none_still_hits(tmp_path):
    """cs50: cutoff=None (live/up-to-now caller) imposes NO contiguity check.
    A discontiguous cache still HITs and serves a plain tail with no staleness
    flag -> byte-identical to the pre-cs49/cs50 behaviour.
    """
    calls = {"n": 0}
    cache = OhlcvCache("ccxt", "HOLE", "1h", root=tmp_path, prefer_parquet=False)
    ancient = _bars(200, start="2024-01-01")
    fresh1 = _bars(1, start="2024-06-01T00:00:00Z", seed=9)
    cache.write(pd.concat([ancient, fresh1], ignore_index=True))

    def fetch():
        calls["n"] += 1
        return _bars(200, start="2024-01-01")

    out, meta = cached_fetch(
        fetch,
        provider="ccxt",
        symbol="HOLE",
        timeframe="1h",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=None,
    )
    assert calls["n"] == 0
    assert meta["cache_hit"] is True
    assert "right_edge_stale_days" not in meta


def test_cached_fetch_daily_weekend_anchor_hits_no_refetch_loop(tmp_path):
    """cs58 (REGRESSION cs43): a fresh, contiguous DAILY equity cache whose
    freshest real bar is FRIDAY's close, with a backtest --end anchored MONDAY,
    sits ~3 calendar days above Friday. cs43's literal one-step (1 day) bound
    REJECTED the HIT -> the cache refetched on EVERY run even though the provider
    has nothing newer (markets closed Sat/Sun) = a refetch-forever loop on the
    most common backtest case. The cache's OWN recurring weekend gap (3 days)
    must self-calibrate the bound so the fresh cache HITs and never loops.
    """
    calls = {"n": 0}
    # ~4 trading weeks of Mon-Fri daily bars; newest is Friday 2024-05-31.
    bars = _daily_business_bars("2024-05-06", "2024-05-31")
    cache = OhlcvCache("alpaca", "AAPL", "1d", root=tmp_path, prefer_parquet=False)
    cache.write(bars)

    def fetch():
        calls["n"] += 1
        return bars  # provider has nothing newer than Friday

    cutoff = pd.Timestamp("2024-06-03T00:00:00Z")  # Monday --end anchor
    kwargs = dict(
        provider="alpaca",
        symbol="AAPL",
        timeframe="1d",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    # Run three times: a correct fresh cache must HIT and never increment fetch.
    for _ in range(3):
        out, meta = cached_fetch(fetch, **kwargs)
        assert meta["cache_hit"] is True
    assert calls["n"] == 0  # no refetch-forever loop
    assert (out["timestamp"] <= cutoff).all()


def test_cached_fetch_intraday_session_gap_anchor_hits(tmp_path):
    """cs58: an INTRADAY 1h RTH cache (14:00-20:00Z, Mon-Fri) has legitimate
    overnight AND weekend session gaps. A backtest anchored at the next session's
    open sits an overnight (or weekend) gap above the cache's freshest bar, which
    cs43's literal 1-hour bound rejected -> refetch loop. The cache's recurring
    session gap must self-calibrate the bound so it HITs.
    """
    calls = {"n": 0}
    # 3 trading weeks: the cache has observed both overnight (18h) and weekend
    # (Fri 20:00 -> Mon 14:00 = 66h) gaps multiple times.
    bars = _intraday_rth_bars("2024-05-13", "2024-05-31")
    cache = OhlcvCache("alpaca", "MSFT", "1h", root=tmp_path, prefer_parquet=False)
    cache.write(bars)

    def fetch():
        calls["n"] += 1
        return bars

    # Monday-open anchor: newest is prior Friday 20:00Z, a 66h weekend gap below.
    cutoff = pd.Timestamp("2024-06-03T14:00:00Z")
    kwargs = dict(
        provider="alpaca",
        symbol="MSFT",
        timeframe="1h",
        lookback_bars=20,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    for _ in range(3):
        out, meta = cached_fetch(fetch, **kwargs)
        assert meta["cache_hit"] is True
    assert calls["n"] == 0
    assert (out["timestamp"] <= cutoff).all()


def test_cached_fetch_calendar_bound_still_rejects_cs43_multimonth(tmp_path):
    """cs58 must NOT reopen the cs43 multi-month-stale hole. A DAILY Mon-Fri cache
    ending months before the anchor has only a 3-day recurring weekend gap; the
    multi-month edge gap is a ONE-OFF (does not recur) and is excluded from the
    self-calibrated bound -> the HIT is still REJECTED and the provider refreshes.
    """
    calls = {"n": 0}
    stale = _daily_business_bars("2024-01-01", "2024-01-31")  # ends ~2024-01-31

    def fetch():
        calls["n"] += 1
        return _daily_business_bars("2024-05-01", "2024-05-31")  # fresh provider bars

    cache = OhlcvCache("alpaca", "OLD", "1d", root=tmp_path, prefer_parquet=False)
    cache.write(stale)

    cutoff = pd.Timestamp("2024-06-03T00:00:00Z")  # ~4 months past the cache edge
    out, meta = cached_fetch(
        fetch,
        provider="alpaca",
        symbol="OLD",
        timeframe="1d",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    assert calls["n"] == 1  # multi-month stale edge -> MISS/fetch (cs43 intact)
    assert meta["cache_hit"] is False
    assert (out["timestamp"] <= cutoff).all()


def test_cached_fetch_calendar_bound_still_rejects_cs50_interior_hole(tmp_path):
    """cs58 must NOT reopen the cs50 interior-hole hole. The lone giant interior
    gap appears ONCE -> not recurring -> excluded from the self-calibrated bound,
    so the contiguity check still rejects the discontiguous served tail.
    """
    calls = {"n": 0}
    ancient = _bars(200, start="2024-01-01")  # contiguous hourly, ends ~2024-01-09
    fresh1 = _bars(1, start="2024-06-01T00:00:00Z", seed=9)  # single bar AT cutoff
    cache = OhlcvCache("ccxt", "HOLE", "1h", root=tmp_path, prefer_parquet=False)
    cache.write(pd.concat([ancient, fresh1], ignore_index=True))

    def fetch():
        calls["n"] += 1
        return _bars(300, start="2024-05-20")

    cutoff = pd.Timestamp("2024-06-01T00:00:00Z")
    out, meta = cached_fetch(
        fetch,
        provider="ccxt",
        symbol="HOLE",
        timeframe="1h",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    assert calls["n"] == 1  # discontiguous interior hole -> MISS/fetch (cs50 intact)
    assert meta["cache_hit"] is False


def test_cached_fetch_calendar_bound_cutoff_none_byte_identical(tmp_path):
    """cs58: cutoff=None (live caller) imposes NO calendar bound and is unchanged
    from the prior behaviour (HITs, no staleness flag, most-recent tail).
    """
    calls = {"n": 0}
    bars = _daily_business_bars("2024-05-06", "2024-05-31")
    cache = OhlcvCache("alpaca", "AAPL", "1d", root=tmp_path, prefer_parquet=False)
    cache.write(bars)

    def fetch():
        calls["n"] += 1
        return bars

    out, meta = cached_fetch(
        fetch,
        provider="alpaca",
        symbol="AAPL",
        timeframe="1d",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=None,
    )
    assert calls["n"] == 0
    assert meta["cache_hit"] is True
    assert "right_edge_stale_days" not in meta
    full = cache.read()
    assert out["timestamp"].iloc[-1] == full["timestamp"].iloc[-1]


def test_cached_fetch_calendar_bound_short_cache_degrades_to_canonical(tmp_path):
    """cs58: a cache too short to learn a recurring rhythm (only one occurrence of
    a gap) degrades to the cs43 canonical one-step bound (no regression, no
    spurious widening). A single Fri->Mon gap that appears once is NOT learned,
    so a 3-day stale daily edge still rejects.
    """
    calls = {"n": 0}
    # Only Fri + Mon: the single 3-day gap appears once -> not recurring.
    bars = _bars_from_ts(pd.DatetimeIndex(["2024-05-24", "2024-05-31"]).tz_localize("UTC"))

    def fetch():
        calls["n"] += 1
        return bars

    cache = OhlcvCache("alpaca", "TINY", "1d", root=tmp_path, prefer_parquet=False)
    cache.write(bars)
    # Anchor 3 days past the newest bar; with no learnable rhythm the canonical
    # 1-day bound applies -> still MISS (matches cs43 conservative default).
    cutoff = pd.Timestamp("2024-06-03T00:00:00Z")
    out, meta = cached_fetch(
        fetch,
        provider="alpaca",
        symbol="TINY",
        timeframe="1d",
        lookback_bars=2,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    assert meta["cache_hit"] is False  # canonical bound, no spurious widening


def test_cached_fetch_daily_two_week_monday_no_refetch_loop_or_abstain(tmp_path):
    """cs58 Layer-2 (MISS-path false-abstain): a fresh, contiguous 2-trading-week
    DAILY cache (Friday close) has only ONE weekend (Fri->Mon) gap, so the gap
    does NOT recur (>=2) and the HIT-path self-calibrated bound degrades to the
    cs43 canonical 1-day step -> a Monday --end anchor (3 calendar days above
    Friday) MISSes. That is acceptable per the conservative HIT default; the cs49
    short-circuit already prevents cache churn (no append, mtime stable, served
    deterministically). But the MISS-path staleness FLAG still uses the bare
    canonical one-step bound, so it wrongly emits ``right_edge_stale_days=3`` and
    the caller ABSTAINS on a demonstrably-fresh cache whose own largest observed
    inter-bar gap (3-day weekend) already covers the 3-day edge gap.

    RED today: across 3 runs the cache does not churn (append_calls==0) but
    ``right_edge_stale_days`` is emitted every run -> false abstain.
    GREEN after Layer 2: no churn AND no abstain flag (the flag bound widens to
    the cache's largest observed gap), served tail deterministic across runs.
    """
    # 2 trading weeks Mon-Fri; newest is Friday 2024-05-24 (ONE weekend gap).
    bars = _daily_business_bars("2024-05-13", "2024-05-24")
    cache = OhlcvCache("alpaca", "AAPL", "1d", root=tmp_path, prefer_parquet=False)
    cache.write(bars)

    def fetch():
        return bars  # provider has nothing newer than Friday (markets closed)

    cutoff = pd.Timestamp("2024-05-27T00:00:00Z")  # Monday --end anchor
    kwargs = dict(
        provider="alpaca",
        symbol="AAPL",
        timeframe="1d",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    # Spy on the real append + capture the cache mtime (mirror the cs49 churn test).
    append_calls = {"n": 0}
    orig_append = OhlcvCache.append

    def spy_append(self, b):
        append_calls["n"] += 1
        return orig_append(self, b)

    OhlcvCache.append = spy_append
    cache_path = cache.path
    mtime_before = cache_path.stat().st_mtime_ns if cache_path.exists() else None
    metas = []
    outs = []
    try:
        for _ in range(3):
            out, meta = cached_fetch(fetch, **kwargs)
            outs.append(out)
            metas.append(meta)
    finally:
        OhlcvCache.append = orig_append
    mtime_after = cache_path.stat().st_mtime_ns if cache_path.exists() else None
    # No cache churn: the provider cannot advance the right edge (cs49 intact).
    assert append_calls["n"] == 0
    assert mtime_after == mtime_before
    # Deterministic serve across runs (no loop-induced drift).
    assert list(outs[0]["timestamp"]) == list(outs[1]["timestamp"])
    assert list(outs[1]["timestamp"]) == list(outs[2]["timestamp"])
    assert (outs[0]["timestamp"] <= cutoff).all()
    # No false abstain on a demonstrably-fresh calendar-gapped cache: the
    # largest observed gap (3-day weekend) covers the 3-day edge gap.
    for meta in metas:
        assert "right_edge_stale_days" not in meta


def test_cached_fetch_miss_flag_still_fires_on_dense_stale_cache(tmp_path):
    """cs58 Layer-2 anti-regression: the looser observed-gap flag bound must NOT
    suppress the genuine cs49 abstain on a truly stale cache. A DENSE hourly
    cache (largest observed inter-bar gap = 1h) ending ~139 days below the cutoff
    still flags ``right_edge_stale_days`` because its own observed rhythm (1h)
    does NOT cover the multi-month edge gap.
    """
    cache = OhlcvCache("ccxt", "DEAD", "1h", root=tmp_path, prefer_parquet=False)
    cache.write(_bars(300, start="2024-01-01"))  # dense hourly, newest ~ 2024-01-13

    def fetch():
        return _bars(300, start="2024-01-01")  # provider capped: same stale window

    cutoff = pd.Timestamp("2024-06-01T00:00:00Z")
    out, meta = cached_fetch(
        fetch,
        provider="ccxt",
        symbol="DEAD",
        timeframe="1h",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    assert meta["cache_hit"] is False
    # The genuine multi-month stale right edge is STILL surfaced (cs49 intact).
    assert meta["right_edge_stale_days"] >= 139


def test_cached_fetch_intraday_two_session_no_abstain(tmp_path):
    """cs58 Layer-2: an INTRADAY 1h RTH cache spanning only 2 sessions has ONE
    overnight gap (appears once -> not recurring -> HIT-path bound canonical 1h),
    so a next-session-open cutoff MISSes. But the cache's largest observed gap
    (the 18h overnight session gap) covers the equal-sized edge gap, so the MISS
    path must NOT emit a false abstain flag, and cs49 short-circuit prevents churn.
    """
    # 2 weekday sessions: 2024-05-22 (Wed) and 2024-05-23 (Thu), 14:00-20:00Z each
    # -> the cache observed exactly ONE 18h overnight gap (Wed 20:00 -> Thu 14:00).
    bars = _intraday_rth_bars("2024-05-22", "2024-05-23")
    cache = OhlcvCache("alpaca", "MSFT", "1h", root=tmp_path, prefer_parquet=False)
    cache.write(bars)

    def fetch():
        return bars  # nothing newer than Thu 20:00Z

    # Anchor at the next weekday session open (Fri 2024-05-24 14:00Z): newest is
    # Thu 20:00Z, an 18h overnight gap above == the cache's largest observed gap.
    cutoff = pd.Timestamp("2024-05-24T14:00:00Z")
    kwargs = dict(
        provider="alpaca",
        symbol="MSFT",
        timeframe="1h",
        lookback_bars=14,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    append_calls = {"n": 0}
    orig_append = OhlcvCache.append

    def spy_append(self, b):
        append_calls["n"] += 1
        return orig_append(self, b)

    OhlcvCache.append = spy_append
    try:
        out, meta = cached_fetch(fetch, **kwargs)
    finally:
        OhlcvCache.append = orig_append
    assert append_calls["n"] == 0  # no churn (cs49 intact)
    assert "right_edge_stale_days" not in meta  # no false abstain
    assert (out["timestamp"] <= cutoff).all()


def test_cached_fetch_daily_monday_holiday_anchor_hits_no_refetch_loop(tmp_path):
    """cs63 (residual cs58): cs58 learns only the cache's RECURRING inter-bar gap
    (the 3-day Fri->Mon weekend, count>=2). A market HOLIDAY produces a rarer,
    longer trailing gap: a fresh, contiguous DAILY equity cache whose freshest real
    bar is FRIDAY's close, with a backtest --end anchored the TUESDAY after a Monday
    holiday (Memorial Day), sits ~4 calendar days above Friday > the learned 3-day
    weekend bound. A single holiday recurs <2x in a typical backtest window, so the
    cs58 >=2 recurrence gate never learns the 4-day gap -> the fresh fully-supplied
    cache MISSes + refetches on EVERY run (the refetch-forever shape cs58 fixed for
    weekends). The edge bound must tolerate one closed session day adjacent to the
    recurring weekend rhythm so the fresh holiday-bordered cache HITs and never loops.
    """
    calls = {"n": 0}
    # 6 trading weeks of Mon-Fri daily bars, NO in-cache holiday; newest is Friday
    # 2024-05-24 (the trading day before Memorial Day Monday 2024-05-27).
    bars = _daily_business_bars("2024-04-15", "2024-05-24")
    cache = OhlcvCache("alpaca", "AAPL", "1d", root=tmp_path, prefer_parquet=False)
    cache.write(bars)

    def fetch():
        calls["n"] += 1
        return bars  # provider has nothing newer than Friday (Mon is a holiday)

    cutoff = pd.Timestamp("2024-05-28T00:00:00Z")  # Tuesday after Memorial Day Monday
    kwargs = dict(
        provider="alpaca",
        symbol="AAPL",
        timeframe="1d",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    for _ in range(3):
        out, meta = cached_fetch(fetch, **kwargs)
        assert meta["cache_hit"] is True
    assert calls["n"] == 0  # no refetch-forever loop on the holiday boundary
    assert (out["timestamp"] <= cutoff).all()


def test_cached_fetch_intraday_holiday_session_anchor_hits(tmp_path):
    """cs63: an INTRADAY 1h RTH cache (14:00-20:00Z, Mon-Fri) anchored at the open
    of the first session AFTER a Monday holiday. The newest bar is the prior
    Friday's 20:00Z close; the next session open is the Tuesday after the holiday,
    so the trailing edge gap is Fri 20:00 -> Tue 14:00 = 90h = a weekend (66h) plus a
    full closed session DAY. cs58's recurring weekend rhythm alone (66h) does NOT
    cover it -> refetch loop. The bound must tolerate one closed session day past the
    recurring weekend so it HITs.
    """
    calls = {"n": 0}
    # 3 trading weeks so the 66h weekend gap recurs (>=2); newest is Fri 2024-05-24.
    bars = _intraday_rth_bars("2024-05-06", "2024-05-24")
    cache = OhlcvCache("alpaca", "MSFT", "1h", root=tmp_path, prefer_parquet=False)
    cache.write(bars)

    def fetch():
        calls["n"] += 1
        return bars

    # Tuesday-after-holiday open anchor (Memorial Day Monday 2024-05-27 closed).
    cutoff = pd.Timestamp("2024-05-28T14:00:00Z")
    kwargs = dict(
        provider="alpaca",
        symbol="MSFT",
        timeframe="1h",
        lookback_bars=20,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    for _ in range(3):
        out, meta = cached_fetch(fetch, **kwargs)
        assert meta["cache_hit"] is True
    assert calls["n"] == 0
    assert (out["timestamp"] <= cutoff).all()


def test_cached_fetch_holiday_bound_still_rejects_cs43_multimonth(tmp_path):
    """cs63 anti-regression: the holiday-widened edge bound must NOT reopen the cs43
    multi-month-stale hole. A DAILY Mon-Fri cache ending months before the anchor
    has at most a 3-day recurring weekend gap; widening it by one closed session day
    yields a 4-day bound, still far below a multi-month stale edge -> the HIT is
    still REJECTED and the provider refreshes.
    """
    calls = {"n": 0}
    stale = _daily_business_bars("2024-01-01", "2024-01-31")  # ends ~2024-01-31

    def fetch():
        calls["n"] += 1
        return _daily_business_bars("2024-05-01", "2024-05-31")  # fresh provider bars

    cache = OhlcvCache("alpaca", "OLD", "1d", root=tmp_path, prefer_parquet=False)
    cache.write(stale)

    cutoff = pd.Timestamp("2024-06-03T00:00:00Z")  # ~4 months past the cache edge
    out, meta = cached_fetch(
        fetch,
        provider="alpaca",
        symbol="OLD",
        timeframe="1d",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    assert calls["n"] == 1  # multi-month stale edge -> MISS/fetch (cs43 intact)
    assert meta["cache_hit"] is False
    assert (out["timestamp"] <= cutoff).all()


def test_cached_fetch_holiday_bound_still_rejects_cs50_interior_hole(tmp_path):
    """cs63 anti-regression: widening the EDGE bound for a holiday must NOT widen the
    cs50 contiguity tolerance. The contiguity check stays on the recurrence-based
    bound (NOT the holiday-widened edge bound), so a single giant INTERIOR hole still
    fails contiguity and the discontiguous served tail is rejected.
    """
    calls = {"n": 0}
    ancient = _bars(200, start="2024-01-01")  # contiguous hourly, ends ~2024-01-09
    fresh1 = _bars(1, start="2024-06-01T00:00:00Z", seed=9)  # single bar AT cutoff
    cache = OhlcvCache("ccxt", "HOLE", "1h", root=tmp_path, prefer_parquet=False)
    cache.write(pd.concat([ancient, fresh1], ignore_index=True))

    def fetch():
        calls["n"] += 1
        return _bars(300, start="2024-05-20")

    cutoff = pd.Timestamp("2024-06-01T00:00:00Z")
    out, meta = cached_fetch(
        fetch,
        provider="ccxt",
        symbol="HOLE",
        timeframe="1h",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    assert calls["n"] == 1  # discontiguous interior hole -> MISS/fetch (cs50 intact)
    assert meta["cache_hit"] is False


def test_cached_fetch_holiday_bound_rejects_ancient_interior_hole_plus_stale_edge(tmp_path):
    """cs63 critical reconciliation (flagged by the cs58 prove): a cache containing
    an ANCIENT one-off giant INTERIOR gap (a delisting hole) PLUS a stale right edge
    must NOT wrongly HIT just because the interior hole is large. The holiday edge
    bound is derived from the cache's RECURRING calendar rhythm + one closed session
    day, NOT from the largest observed gap, so a one-off interior delisting hole does
    not inflate the edge tolerance. With a stale edge SMALLER than the interior hole,
    a max-observed-gap bound would wrongly tolerate it; the recurrence-based bound
    correctly rejects it.
    """
    calls = {"n": 0}
    seg1 = _daily_business_bars("2023-01-02", "2023-02-15")  # ~32 daily bars
    seg2 = _daily_business_bars("2023-08-01", "2023-09-15")  # after a ~6-month hole
    cache = OhlcvCache("alpaca", "DELISTED", "1d", root=tmp_path, prefer_parquet=False)
    cache.write(pd.concat([seg1, seg2], ignore_index=True))

    def fetch():
        calls["n"] += 1
        return _daily_business_bars("2023-10-15", "2023-11-15")  # fresh provider bars

    # Stale edge ~2 months past seg2 (Sep 15) -> smaller than the 6-month interior hole.
    cutoff = pd.Timestamp("2023-11-15T00:00:00Z")
    out, meta = cached_fetch(
        fetch,
        provider="alpaca",
        symbol="DELISTED",
        timeframe="1d",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    assert calls["n"] == 1  # interior hole must NOT widen the edge -> MISS/fetch
    assert meta["cache_hit"] is False
    assert (out["timestamp"] <= cutoff).all()


def test_cached_fetch_holiday_bound_cutoff_none_byte_identical(tmp_path):
    """cs63: cutoff=None (live caller) imposes NO edge/holiday bound and is unchanged
    from the prior behaviour (HITs, no staleness flag, most-recent tail).
    """
    calls = {"n": 0}
    bars = _daily_business_bars("2024-04-15", "2024-05-24")
    cache = OhlcvCache("alpaca", "AAPL", "1d", root=tmp_path, prefer_parquet=False)
    cache.write(bars)

    def fetch():
        calls["n"] += 1
        return bars

    out, meta = cached_fetch(
        fetch,
        provider="alpaca",
        symbol="AAPL",
        timeframe="1d",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=None,
    )
    assert calls["n"] == 0
    assert meta["cache_hit"] is True
    assert "right_edge_stale_days" not in meta
    full = cache.read()
    assert out["timestamp"].iloc[-1] == full["timestamp"].iloc[-1]


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
