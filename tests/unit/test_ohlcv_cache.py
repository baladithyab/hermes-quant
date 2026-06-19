"""Tests for V03-7 OHLCV file cache."""

from __future__ import annotations

import numpy as np
import pandas as pd

from hermes_quant.data.cache import (
    OhlcvCache,
    _safe_component,
    cached_fetch,
    normalize_bars,
)


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
    # cs72: _safe_component is now INJECTIVE. Identities containing chars outside
    # [A-Za-z0-9_.-] percent-escape the offending byte rather than collapsing it
    # to "_", so "BTC/USDT" and "BTC:USDT" (and a literal "BTC_USDT") can never
    # share a cache stem. "/" -> "%2F", ":" -> "%3A".
    cache = OhlcvCache("ccxt:kraken", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=False)
    assert cache.csv_path.name == "BTC%2FUSDT-1h.csv"
    assert "ccxt%3Akraken" in str(cache.csv_path)


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


# ---------------------------------------------------------------------------
# cs66: PIT-preserving interior value-rewrite guard on OhlcvCache.append.
#
# The OHLCV cache schema [timestamp,open,high,low,close,volume] has NO
# fetched_at column, so ``normalize_bars`` deduped on timestamp with
# keep="last" and ``append`` concatenated incoming AFTER existing -> a re-fetch
# returning a REVISED value for a PAST timestamp (vendor restatement, split/div
# re-adjustment, late print, differently-sized window) silently OVERWROTE the
# cached historical bar. A backtest replayed at the same --end then read a
# DIFFERENT historical price -> non-reproducible OOS metrics. This is the OHLCV
# sibling of the fundamentals-side cs42(b)/cs53/cs59/cs61 PIT family; the fix
# mirrors cs59's ``write_sector_median`` same-day-wins / cross-day-loses guard.
# ---------------------------------------------------------------------------


def _bar_at(ts, close, *, vol=1000.0):
    """A single OHLCV bar at one timestamp with an explicit close (cs66)."""
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return pd.DataFrame(
        {
            "timestamp": [t],
            "open": [float(close)],
            "high": [float(close)],
            "low": [float(close)],
            "close": [float(close)],
            "volume": [float(vol)],
        }
    )


def _close_at(cache, ts):
    out = cache.read()
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    sub = out.loc[out["timestamp"] == t, "close"]
    assert len(sub) == 1, f"expected exactly one bar at {ts}, got {len(sub)}"
    return float(sub.iloc[0])


def test_cs66_cross_day_refetch_does_not_overwrite_historical_bar(tmp_path):
    """cs66 RED: a cached bar at T=2024-01-05 close=100, then a CROSS-DAY
    re-fetch returning close=50 for the SAME T (the test clock is 2026, so the
    re-fetch's fetched_at is a LATER calendar day than the 2024 bar date),
    currently OVERWRITES the cached historical bar -> a replay reads 50.0.
    After the fix the historical 100.0 is PRESERVED (cross-day backfill loses).
    """
    cache = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=False)
    cache.append(_bar_at("2024-01-05", 100.0))
    cache.append(_bar_at("2024-01-05", 50.0))  # cross-day re-fetch (now >> 2024)
    assert _close_at(cache, "2024-01-05") == 100.0


def test_cs66_same_day_correction_still_wins(tmp_path):
    """cs66: a SAME-DAY correction (a newer fetched_at on the SAME calendar day
    as the bar's timestamp) is the legitimate intraday-revision case and STILL
    wins, mirroring cs59's same-day-wins rule on the fundamentals side.
    """
    cache = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=False)
    today = pd.Timestamp.now(tz="UTC").normalize()
    t = today + pd.Timedelta(hours=15)  # a bar timestamped today
    cache.append(_bar_at(t, 100.0), fetched_at=today + pd.Timedelta(hours=9))
    cache.append(_bar_at(t, 99.0), fetched_at=today + pd.Timedelta(hours=15, minutes=30))
    assert _close_at(cache, t) == 99.0


def test_cs66_first_write_of_disjoint_timestamps_byte_identical(tmp_path):
    """cs66: a first-write of NEW disjoint timestamps preserves all values
    byte-identical (the guard only ever fires on a re-write of an existing T).
    """
    cache = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=False)
    cache.append(_bar_at("2024-01-05", 100.0, vol=111.0))
    cache.append(_bar_at("2024-01-06", 200.0, vol=222.0))  # disjoint T
    cache.append(_bar_at("2024-01-07", 300.0, vol=333.0))  # disjoint T
    out = cache.read()
    assert len(out) == 3
    assert _close_at(cache, "2024-01-05") == 100.0
    assert _close_at(cache, "2024-01-06") == 200.0
    assert _close_at(cache, "2024-01-07") == 300.0
    # Every OHLCV value is byte-identical to the first write.
    assert out.loc[out["timestamp"] == pd.Timestamp("2024-01-06", tz="UTC"), "volume"].iloc[0] == 222.0


def test_cs66_served_columns_have_no_fetched_at_leak(tmp_path):
    """cs66: the served read() frame stays the canonical 6 OHLCV columns after a
    cross-day re-fetch sequence -- the internal fetched_at storage column must
    NOT leak into the served frame any downstream consumer reads.
    """
    cache = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=False)
    cache.append(_bar_at("2024-01-05", 100.0))
    cache.append(_bar_at("2024-01-05", 50.0))
    cache.append(_bar_at("2024-01-06", 200.0))
    assert list(cache.read().columns) == [
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]


def test_cs66_legacy_fetched_at_less_file_appends_without_crash(tmp_path):
    """cs66 migration: a LEGACY fetched_at-less 6-col cache (written by the old
    ``write()`` path, or any pre-cs66 file) must not crash on append, must be
    idempotent, must preserve its legacy rows, and a cross-day re-fetch of a
    legacy timestamp must NOT overwrite it (the legacy sentinel fetched_at is
    backfilled strictly after every cached bar so legacy rows are historical /
    cross-day-protected).
    """
    cache = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=False)
    # Write a legacy 6-col frame directly to disk, bypassing append (this is
    # exactly what the unchanged public write() produces -- a fetched_at-less file).
    cache.write(
        pd.concat(
            [_bar_at("2024-01-05", 100.0), _bar_at("2024-01-06", 200.0)],
            ignore_index=True,
        )
    )
    # Append a new disjoint-T bar twice -> no crash, idempotent, legacy rows survive.
    cache.append(_bar_at("2024-01-07", 300.0))
    cache.append(_bar_at("2024-01-07", 300.0))
    out = cache.read()
    assert len(out) == 3  # idempotent: no duplicate of 2024-01-07
    assert _close_at(cache, "2024-01-05") == 100.0
    assert _close_at(cache, "2024-01-06") == 200.0
    assert _close_at(cache, "2024-01-07") == 300.0
    # A cross-day re-fetch of a LEGACY timestamp does NOT overwrite the legacy bar.
    cache.append(_bar_at("2024-01-05", 7.0))
    assert _close_at(cache, "2024-01-05") == 100.0


def test_cs66_served_window_behavior_byte_identical_cs38(tmp_path):
    """cs66 anti-regression: this fix is about the STORED interior value, not the
    SERVED WINDOW. Re-affirm a representative cs38 served-window test still
    passes (no bar post-dates the cutoff on a HIT).
    """
    calls = {"n": 0}
    cache = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=False)
    cache.write(_bars(200, start="2024-01-01"))

    def fetch():
        calls["n"] += 1
        return _bars(200, start="2024-01-01")

    cutoff = pd.Timestamp("2024-01-03T00:00:00Z")
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
    assert (out["timestamp"] <= cutoff).all()


# ---------------------------------------------------------------------------
# cs70: .csv/.parquet dual-format SOURCE-OF-TRUTH divergence.
#
# OhlcvCache.path PREFERS the .parquet when prefer_parquet and it exists, and
# read() reads the .parquet first (CSV only as a parquet-engine-absent fallback).
# But write() can DEGRADE a re-fetch to .csv (parquet engine unavailable at write
# time) WITHOUT invalidating a pre-existing stale .parquet for the same stem. So a
# backtest replayed in an env where the parquet engine IS available silently
# serves the OLD .parquet bars and DROPS the fresh CSV re-fetch -- environment
# -dependent non-reproducibility (the same --end yields different bars depending
# on which format the runtime can read). The OHLCV sibling of cs66 (PIT dedup),
# but a DISTINCT root cause: a single stem must have a SINGLE source of truth.
#
# FIX SHAPE: write() / _write_storage() write to the preferred format and DELETE
# the stale sibling-format file so only one format ever persists per stem; and
# path()/read() prefer the NEWEST-mtime format when (a pre-fix on-disk cache
# already has) BOTH present, so read() always returns the most-recently-written
# bars regardless of which format the runtime can read. A single-format cache (the
# common case) is byte-identical; cs66 fetched_at + served-window behaviour intact.
# ---------------------------------------------------------------------------


def _atomic_write_csv(cache, bars):
    """Write the served 6-col frame to the .csv path, mirroring write()'s
    parquet->CSV degrade branch (the exact bytes write() would emit on a minimal
    install with no parquet engine). Bypasses write() so the test can leave a
    stale sibling .parquet in place."""
    from hermes_quant.data.cache import _atomic_write

    _atomic_write(normalize_bars(bars), cache.csv_path)


def test_cs70_csv_refetch_supersedes_stale_parquet(tmp_path):
    """cs70 RED: a stale .parquet (close=100) lingers while a FRESH degraded-to
    -CSV re-fetch (close=50) is written for the SAME stem. In a parquet-capable
    env read() currently prefers the .parquet and serves the STALE 100, DROPPING
    the fresh CSV re-fetch. After the fix read() returns the FRESH 50.
    """
    cache = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=True)
    # Step 1: write stale bars to .parquet (engine available here).
    cache.write(_bar_at("2024-01-05", 100.0))
    assert cache.parquet_path.exists()
    # Step 2: a re-fetch that DEGRADED to CSV writes FRESH bars to .csv while the
    # stale .parquet lingers (exactly what write()'s degrade branch leaves behind
    # if it does not invalidate the sibling).
    import os as _os

    _atomic_write_csv(cache, _bar_at("2024-01-05", 50.0))
    # Force the .csv mtime strictly newer than the .parquet DETERMINISTICALLY. A
    # bare time.sleep() is not enough on coarse-granularity filesystems (WSL2 /
    # drvfs under load can collapse two writes into the same mtime tick), which
    # would flake this PIT-reproducibility test. os.utime makes the newest-mtime
    # ordering exact regardless of filesystem clock resolution.
    p_mtime = cache.parquet_path.stat().st_mtime
    _os.utime(cache.csv_path, (p_mtime + 1.0, p_mtime + 1.0))
    # Step 3: read() in a parquet-capable env must return the FRESH bars.
    assert _close_at(cache, "2024-01-05") == 50.0


def test_cs70_write_invalidates_stale_sibling_format(tmp_path):
    """cs70: after the fix a successful write to the PREFERRED format invalidates
    the stale sibling-format file, so only ONE format persists per stem (a single
    source of truth). Here a legacy .csv exists; a fresh parquet write must leave
    only the .parquet and remove the now-superseded .csv.
    """
    cache = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=True)
    _atomic_write_csv(cache, _bar_at("2024-01-05", 100.0))  # legacy .csv
    assert cache.csv_path.exists()
    cache.write(_bar_at("2024-01-05", 50.0))  # fresh parquet write
    assert cache.parquet_path.exists()
    assert not cache.csv_path.exists()  # stale sibling invalidated
    assert _close_at(cache, "2024-01-05") == 50.0


def test_cs70_append_through_csv_supersedes_stale_parquet(tmp_path):
    """cs70: the PIT append path (cs66) round-trips through _read_storage /
    _write_storage. A stale .parquet must not shadow a fresher CSV-degraded
    storage file. After the fix an append reads the freshest format and writes a
    single source of truth, so the cross-day PIT dedup operates on fresh data.
    """
    cache = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=True)
    # Seed a parquet via append (the cs66 storage path).
    cache.append(_bar_at("2024-01-05", 100.0))
    assert cache.parquet_path.exists()
    import os as _os

    # A degraded-to-CSV storage re-write lands a FRESH disjoint bar in .csv while
    # the .parquet lingers (a different stem-state in the two formats).
    storage = cache._read_storage()
    fresh_row = storage.iloc[[0]].copy()
    fresh_row["timestamp"] = pd.Timestamp("2024-01-06", tz="UTC")
    fresh_row["close"] = 200.0
    fresh = pd.concat([storage, fresh_row], ignore_index=True)
    from hermes_quant.data.cache import _atomic_write

    _atomic_write(fresh, cache.csv_path)
    # Force the .csv mtime strictly newer than the .parquet DETERMINISTICALLY
    # (coarse-granularity filesystems can collapse two writes into one mtime tick;
    # see test_cs70_csv_refetch_supersedes_stale_parquet for rationale).
    p_mtime = cache.parquet_path.stat().st_mtime
    _os.utime(cache.csv_path, (p_mtime + 1.0, p_mtime + 1.0))
    # read() must see the freshest format (the .csv with 2 bars), not the stale
    # 1-bar .parquet.
    out = cache.read()
    assert len(out) == 2
    assert _close_at(cache, "2024-01-06") == 200.0


def test_cs70_write_degrade_to_csv_invalidates_stale_parquet(tmp_path, monkeypatch):
    """cs70 (the production shape): a re-fetch whose parquet write FAILS (no
    parquet engine on a minimal install) degrades to .csv THROUGH ``write``. The
    pre-existing stale .parquet must be invalidated so a single source of truth
    remains = the fresh CSV, and read() (even in a parquet-capable env, where an
    unconditional .parquet preference would serve the stale bars) returns fresh.

    This proves the fix does not depend on filesystem mtime resolution: a degraded
    write and the stale parquet could share an mtime tick, but sibling
    invalidation makes the result deterministic regardless.
    """
    import hermes_quant.data.cache as cache_mod

    cache = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=True)
    cache.write(_bar_at("2024-01-05", 100.0))  # parquet engine works here
    assert cache.parquet_path.exists() and not cache.csv_path.exists()

    real_atomic_write = cache_mod._atomic_write

    def flaky_atomic_write(df, target):
        if target.suffix == ".parquet":
            raise RuntimeError("no parquet engine (minimal install)")
        return real_atomic_write(df, target)

    monkeypatch.setattr(cache_mod, "_atomic_write", flaky_atomic_write)
    written = cache.write(_bar_at("2024-01-05", 50.0))  # degrades to .csv
    monkeypatch.undo()

    assert written == cache.csv_path
    # Single source of truth: the stale .parquet is invalidated.
    assert cache.csv_path.exists()
    assert not cache.parquet_path.exists()
    # read() returns the FRESH bars regardless of which format the runtime prefers.
    assert _close_at(cache, "2024-01-05") == 50.0
    assert list(cache.read().columns) == ["timestamp", "open", "high", "low", "close", "volume"]


def test_cs70_single_format_cache_byte_identical(tmp_path):
    """cs70 anti-regression: a cache with only ONE format present (the common
    case) is byte-identical -- no sibling to invalidate, no behaviour change.
    """
    # parquet-only
    p = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=True)
    p.write(_bars(10, start="2024-01-01"))
    assert p.parquet_path.exists()
    assert not p.csv_path.exists()
    assert len(p.read()) == 10
    # csv-only
    c = OhlcvCache("ccxt", "ETH/USDT", "1h", root=tmp_path, prefer_parquet=False)
    c.write(_bars(10, start="2024-01-01"))
    assert c.csv_path.exists()
    assert not c.parquet_path.exists()
    assert len(c.read()) == 10


def test_cs70_legacy_single_format_does_not_crash(tmp_path):
    """cs70: a legacy single-format cache (only a .csv, no .parquet) must read
    without crashing and must not be invalidated by a read.
    """
    cache = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=True)
    _atomic_write_csv(cache, _bars(5, start="2024-01-01"))
    assert not cache.parquet_path.exists()
    out = cache.read()  # no crash, no parquet to prefer
    assert len(out) == 5
    assert cache.csv_path.exists()  # a read does not delete the only file


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


# ---------------------------------------------------------------------------
# cs71: a MISS that BACKFILLS an INTERIOR hole below the cutoff (new bars, but
# none past the existing right edge) sets the cs49 fetched_advances churn-skip
# to False -> the interior backfill is DISCARDED and never persisted. The cs50
# HIT contiguity gate then keeps rejecting the still-discontiguous cache, so
# fetch_fn is called on EVERY replay (refetch-forever). A cs49 x cs50
# interaction bug. The fix: also append when the fetch contains ANY new bar the
# cache lacked (edge OR interior), while STILL skipping a pure no-new-data
# re-serve (the cs49 churn case the provider re-serves only cached timestamps).
# ---------------------------------------------------------------------------


def _daily_bars_from(closes_by_date):
    """Daily OHLCV frame from a {date_str: close} mapping (cs71)."""
    return pd.concat(
        [_bar_at(d, c) for d, c in closes_by_date.items()], ignore_index=True
    )


def test_cached_fetch_miss_persists_interior_backfill_and_heals(tmp_path):
    """cs71 RED->GREEN: a cache with an INTERIOR hole + a fetch that backfills it
    (no bar past the existing right edge). Today the cs49 fetched_advances guard
    is False (the fetched window does not advance the edge) so the append is
    SKIPPED -> the interior backfill is discarded, the served window stays
    discontiguous, and the cs50 HIT gate rejects every replay -> fetch called 3x
    (refetch-forever). After the fix the backfill is persisted on the first MISS,
    healing the hole, so replays 2 and 3 are HITs (1 fetch total, contiguous).
    """
    calls = {"n": 0}
    cache = OhlcvCache("yfinance", "SPY", "1d", root=tmp_path, prefer_parquet=False)
    # Cache holds 2026-01-01, -02, and a fresh edge bar at -10: a multi-day
    # INTERIOR hole (03..09) below the cutoff.
    cache.write(_daily_bars_from({"2026-01-01": 100, "2026-01-02": 101, "2026-01-10": 110}))
    cutoff = pd.Timestamp("2026-01-10", tz="UTC")

    def fetch():
        calls["n"] += 1
        # Interior backfill 03..09 ONLY (none past the existing edge day 10).
        days = pd.date_range("2026-01-03", "2026-01-09", freq="1d", tz="UTC")
        return pd.DataFrame(
            {
                "timestamp": days,
                "open": 102.0,
                "high": 102.0,
                "low": 102.0,
                "close": 102.0,
                "volume": 1000.0,
            }
        )

    # lookback_bars=10 so the healed 10-bar contiguous window satisfies the count
    # gate (min_hit_bars = int(10 * 0.95) = 9); the point under test is that the
    # interior backfill is PERSISTED so the cs50 contiguity HIT gate stops
    # rejecting, not the count gate.
    kwargs = dict(
        provider="yfinance",
        symbol="SPY",
        timeframe="1d",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    out1, m1 = cached_fetch(fetch, **kwargs)
    out2, m2 = cached_fetch(fetch, **kwargs)
    out3, m3 = cached_fetch(fetch, **kwargs)

    # One fetch heals the hole; the next two replays are HITs (no refetch loop).
    assert calls["n"] == 1
    assert m1["cache_hit"] is False  # the healing MISS
    assert m2["cache_hit"] is True
    assert m3["cache_hit"] is True
    # The interior backfill is PERSISTED: the cache is now the contiguous 01..10.
    persisted = cache.read()["timestamp"].dt.strftime("%Y-%m-%d").tolist()
    assert persisted == [f"2026-01-{d:02d}" for d in range(1, 11)]
    # The served window is contiguous (max inter-bar gap == one day).
    assert out3["timestamp"].diff().dropna().max() == pd.Timedelta(days=1)


def test_cached_fetch_miss_no_new_data_still_skips_append(tmp_path):
    """cs71 must NOT reopen the cs49 refetch-forever fix. A warm cache that holds
    the provider's FULL stale window, with the provider re-serving the SAME bars
    (zero new timestamps), is a pure no-new-data re-serve: fetched_has_new must be
    False so the append is still SKIPPED (no churn) and the honest abstain flag is
    still emitted. Spies the real append + the cache file mtime across 2 replays.
    """
    cache = OhlcvCache("ccxt", "DEAD", "1h", root=tmp_path, prefer_parquet=False)
    cache.write(_bars(300, start="2024-01-01"))  # provider's full stale window

    def fetch():
        return _bars(300, start="2024-01-01")  # re-serves the SAME 300 bars

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
    append_calls = {"n": 0}
    orig_append = OhlcvCache.append

    def spy_append(self, b):
        append_calls["n"] += 1
        return orig_append(self, b)

    OhlcvCache.append = spy_append
    cache_path = cache.path
    mtime_before = cache_path.stat().st_mtime_ns if cache_path.exists() else None
    try:
        _out1, m1 = cached_fetch(fetch, **kwargs)
        _out2, m2 = cached_fetch(fetch, **kwargs)
    finally:
        OhlcvCache.append = orig_append
    mtime_after = cache_path.stat().st_mtime_ns if cache_path.exists() else None

    assert m1["cache_hit"] is False
    assert m2["cache_hit"] is False
    # No new timestamp in the fetch -> no append, no cache rewrite (cs49 intact).
    assert append_calls["n"] == 0
    assert mtime_after == mtime_before
    # The honest staleness signal still fires so the caller can ABSTAIN.
    assert m2["right_edge_stale_days"] >= 139


# ---------------------------------------------------------------------------
# cs72: _safe_component collapsed every non-[A-Za-z0-9_.-] char to "_", so
# DISTINCT identities shared one cache stem ("BTC/USDT" == "BTC:USDT" ==
# "BTC_USDT" -> "BTC_USDT"). In production the provider is "ccxt:<exchange>" and
# the symbol a ccxt BASE/QUOTE pair, so two instruments differing only by
# separator silently MERGED into one cache file -> blended/wrong OHLCV bars
# served with no signal (cross-contamination + PIT hazard). The fix makes the
# sanitizer INJECTIVE via percent-escaping while keeping already-safe components
# byte-identical (existing all-safe caches are not orphaned).
# ---------------------------------------------------------------------------


def test_safe_component_is_injective_across_separators(tmp_path):
    """cs72 RED->GREEN: the three distinct identities map to DISTINCT stems and
    only the literal all-safe "BTC_USDT" stays unchanged. RED today: all three
    collapse to "BTC_USDT".
    """
    slash = _safe_component("BTC/USDT")
    colon = _safe_component("BTC:USDT")
    underscore = _safe_component("BTC_USDT")
    # The already-safe literal is byte-identical.
    assert underscore == "BTC_USDT"
    # The three are pairwise distinct (no cross-contamination).
    assert slash != colon
    assert slash != underscore
    assert colon != underscore
    assert len({slash, colon, underscore}) == 3


def test_safe_component_safe_values_byte_identical():
    """cs72: a component already inside [A-Za-z0-9_.-] is returned byte-identical
    (no stem change -> existing all-safe caches are NOT orphaned). Empty / all
    -whitespace degrade to the legacy "unknown" sentinel.
    """
    for v in ["AAPL", "1h", "yfinance", "BTC_USDT", "SPY", "ccxt", "a.b-c_d"]:
        assert _safe_component(v) == v
    assert _safe_component("") == "unknown"
    assert _safe_component("  ") == "unknown"


# ---------------------------------------------------------------------------
# cs73: MISS-path INTERIOR discontiguity (no flag). cs49 flags a stale right
# EDGE on the MISS path; cs50 gates INTERIOR contiguity on the HIT path ONLY.
# Neither covers a MISS that serves a window with a genuinely-UNFILLABLE
# interior hole: the provider ADVANCES the right edge (cs71's append runs) but
# CANNOT backfill a delisted stretch / provider short-window / exchange-downtime
# gap, so the served lookback tail glues bars across the hole and a backtest
# computes a spurious seam return with NO signal the caller could ABSTAIN on.
# cs49's right_edge_stale_days flags a stale EDGE; there was no analogous
# INTERIOR-discontiguity flag on the MISS path. The fix emits
# ``meta['interior_gap_days']`` + ``meta['served_window_discontiguous']`` using
# the SAME cs50 contiguity bound, symmetric with cs49. A FILLABLE hole is still
# HEALED by cs71 (no flag once contiguous); a contiguous window / cutoff=None is
# byte-identical (flag absent). The fix does NOT refetch (the edge advanced, so
# cs71's append already ran -- the cs71 boundary).
# ---------------------------------------------------------------------------


def test_cached_fetch_miss_flags_unfillable_interior_hole(tmp_path):
    """cs73 RED->GREEN: a cache contiguous 01..05 then an UNFILLABLE interior hole
    (06..14) then edge bars 15,16; a MISS whose provider ADVANCES the edge (adds
    day 17 == cutoff) but CANNOT backfill 06..14. The served lookback tail glues
    01..05 to 15,16,17 across the ~10-day interior hole. The right edge is FRESH
    (advanced to the cutoff) so cs49's right_edge_stale_days correctly does NOT
    fire -- yet the served window is internally discontiguous with NO signal.

    RED today: no interior flag, caller silently trusts a discontiguous window.
    GREEN: ``meta['interior_gap_days']`` + ``meta['served_window_discontiguous']``
    surface the hole so the caller can ABSTAIN.
    """
    cache = OhlcvCache("yfinance", "DELISTED", "1d", root=tmp_path, prefer_parquet=False)
    seg1 = {f"2026-01-{d:02d}": 100 + d for d in range(1, 6)}  # 01..05 contiguous
    seg2 = {f"2026-01-{d:02d}": 100 + d for d in (15, 16)}  # edge after a 06..14 hole
    cache.write(_daily_bars_from({**seg1, **seg2}))
    cutoff = pd.Timestamp("2026-01-17", tz="UTC")

    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        # Provider ADVANCES the edge (day 17) but cannot fill 06..14 (delisted).
        return _daily_bars_from({"2026-01-17": 117})

    out, meta = cached_fetch(
        fetch,
        provider="yfinance",
        symbol="DELISTED",
        timeframe="1d",
        lookback_bars=8,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    assert meta["cache_hit"] is False
    # The served window glues 05 -> 15 across the unfillable interior hole.
    served = out["timestamp"].dt.strftime("%Y-%m-%d").tolist()
    assert served == ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04",
                      "2026-01-05", "2026-01-15", "2026-01-16", "2026-01-17"]
    # The interior-discontiguity honesty flag is present (~10-day hole).
    assert meta["served_window_discontiguous"] is True
    assert meta["interior_gap_days"] >= 9
    # The right EDGE advanced to the cutoff -> cs49's edge flag must NOT fire.
    assert "right_edge_stale_days" not in meta
    assert (out["timestamp"] <= cutoff).all()


def test_cached_fetch_miss_no_interior_flag_when_hole_is_fillable_cs71(tmp_path):
    """cs73 boundary vs cs71: a FILLABLE interior hole must be HEALED by cs71 (the
    backfill is appended -> the served window becomes contiguous) and therefore
    carry NO interior flag. cs73 flags ONLY a hole that REMAINS after the
    append+dedup. RED-trap: a naive cs73 that flagged on the PRE-append window
    would false-flag here even though cs71 healed the hole.
    """
    cache = OhlcvCache("yfinance", "SPY", "1d", root=tmp_path, prefer_parquet=False)
    cache.write(_daily_bars_from({"2026-01-01": 100, "2026-01-02": 101, "2026-01-10": 110}))
    cutoff = pd.Timestamp("2026-01-10", tz="UTC")

    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        # cs71 fillable backfill: interior 03..09 (none past the existing edge).
        days = pd.date_range("2026-01-03", "2026-01-09", freq="1d", tz="UTC")
        return pd.DataFrame(
            {
                "timestamp": days,
                "open": 102.0,
                "high": 102.0,
                "low": 102.0,
                "close": 102.0,
                "volume": 1000.0,
            }
        )

    out, meta = cached_fetch(
        fetch,
        provider="yfinance",
        symbol="SPY",
        timeframe="1d",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    assert meta["cache_hit"] is False  # the healing MISS
    # cs71 healed the hole -> the served window is contiguous (01..10) -> no flag.
    served = out["timestamp"].dt.strftime("%Y-%m-%d").tolist()
    assert served == [f"2026-01-{d:02d}" for d in range(1, 11)]
    assert "interior_gap_days" not in meta
    assert "served_window_discontiguous" not in meta


def test_cached_fetch_miss_no_interior_flag_on_contiguous_window(tmp_path):
    """cs73 anti-regression: a CONTIGUOUS served MISS window (a dense hourly cache,
    provider lagging) carries NO interior flag even though cs49's edge flag fires.
    A legitimate calendar gap inside the window would not flag either (the bound is
    the cs50 recurrence-aware ``_calendar_bound``).
    """
    cache = OhlcvCache("ccxt", "DEAD", "1h", root=tmp_path, prefer_parquet=False)
    cache.write(_bars(5, start="2024-01-01"))  # short warm cache -> MISS by count

    def fetch():
        return _bars(300, start="2024-01-01")  # dense contiguous, provider lagging

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
    # Contiguous served window -> no interior flag.
    assert "interior_gap_days" not in meta
    assert "served_window_discontiguous" not in meta
    # cs49 right-edge staleness still fires (the edge is months below the cutoff).
    assert meta["right_edge_stale_days"] >= 139


def test_cached_fetch_miss_interior_flag_respects_calendar_gap(tmp_path):
    """cs73: a legitimate RECURRING calendar gap (a Fri->Mon weekend) inside the
    served MISS window must NOT false-flag as a discontiguity. A daily Mon-Fri
    cache served on a MISS has recurring 3-day weekend gaps; the cs50
    ``_calendar_bound`` learns the rhythm so the served window is treated as
    contiguous (no interior flag).
    """
    cache = OhlcvCache("alpaca", "DEAD", "1d", root=tmp_path, prefer_parquet=False)
    # 6 weeks of Mon-Fri daily bars (recurring 3-day weekend gaps); short of count.
    cache.write(_daily_business_bars("2024-01-01", "2024-01-05"))  # one short week

    def fetch():
        # Provider supplies a longer Mon-Fri window (recurring weekend gaps), but
        # its right edge stays months below the cutoff -> MISS persists.
        return _daily_business_bars("2024-01-01", "2024-02-16")

    cutoff = pd.Timestamp("2024-06-03T00:00:00Z")
    out, meta = cached_fetch(
        fetch,
        provider="alpaca",
        symbol="DEAD",
        timeframe="1d",
        lookback_bars=20,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    assert meta["cache_hit"] is False
    # Recurring weekend gaps are NOT a discontiguity -> no interior flag.
    assert "interior_gap_days" not in meta
    assert "served_window_discontiguous" not in meta


def test_cached_fetch_miss_interior_flag_short_tail_single_weekend_no_false_flag(
    tmp_path,
):
    """cs73 calendar-symmetry (mirror cs50): a SHORT served MISS tail that straddles
    exactly ONE weekend (e.g. ``lookback_bars=3`` -> [Thu, Fri, Mon]) must NOT
    false-flag as discontiguous.

    The recurrence gate in ``_calendar_bound`` requires a calendar gap to appear
    >=2 times. A 3-bar served tail contains the Fri->Mon weekend gap only ONCE, so
    learning the contiguity bound from the served tail ``out`` alone degrades to the
    canonical 1-day floor and (3-day weekend > 1d*1.5) FALSE-FLAGS a legitimate
    single-weekend window — a false-abstain, the exact symmetric failure cs58 fixed
    for the EDGE flag. The fix learns the bound from the BROAD served-source set
    ``merged`` (the MISS analogue of the cs50 HIT gate's ``eligible``), which carries
    the recurring rhythm, so the weekend gap is tolerated. The cs50 HIT path serves
    the identical [Thu, Fri, Mon] shape cleanly; this asserts the MISS path is
    symmetric.

    RED (pre-fix, bound learned from ``out``): ``served_window_discontiguous`` is
    wrongly True with ``interior_gap_days == 3``. GREEN (bound from ``merged``): the
    flag is absent.
    """
    cache = OhlcvCache("alpaca", "WKND", "1d", root=tmp_path, prefer_parquet=False)
    # Two full Mon-Fri weeks already cached (the broad merged set will carry >=2
    # recurring weekend gaps); short of the requested edge so a MISS is forced.
    cache.write(_daily_business_bars("2026-01-05", "2026-01-16"))

    def fetch():
        # Provider ADVANCES the right edge to the next Monday (a fresh weekend gap)
        # but supplies no genuinely-new interior bars -> the served lookback tail of
        # 3 bars is [Thu, Fri, Mon], a legitimate single-weekend window.
        return _daily_business_bars("2026-01-05", "2026-01-19")

    cutoff = pd.Timestamp("2026-01-19T00:00:00Z")
    out, meta = cached_fetch(
        fetch,
        provider="alpaca",
        symbol="WKND",
        timeframe="1d",
        lookback_bars=3,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    assert meta["cache_hit"] is False
    # Served tail straddles exactly one weekend: [Thu, Fri, Mon].
    days = list(out["timestamp"].dt.day_name())
    assert days == ["Thursday", "Friday", "Monday"], days
    # A single legitimate weekend gap is NOT an interior discontiguity.
    assert "interior_gap_days" not in meta
    assert "served_window_discontiguous" not in meta
    # The edge advanced to the cutoff Monday -> the cs49 edge flag must not fire.
    assert "right_edge_stale_days" not in meta


# cs76: the MISS-path twin of cs74. cs73 moved the interior-flag contiguity bound
# learning-source from the short served tail ``out`` to the BROAD ``merged`` set to
# dodge a false-flag — but that only works when ``merged`` ITSELF contains the
# weekend gap >=2x (the recurrence gate in ``_calendar_bound``). For a SHORT cache
# whose ENTIRE merged set straddles exactly ONE weekend (or one overnight session),
# the gap appears ONCE even in ``merged`` -> ``_calendar_bound`` degrades to the
# 1-step floor -> a 3d weekend > bound*1.5 -> ``served_window_discontiguous`` fires
# on a window that is CONTIGUOUS-modulo-weekends -> the caller may wrongly ABSTAIN
# on a legitimate short cache. cs74 fixed the symmetric HIT-GATE by switching its
# contig_bound from ``_calendar_bound`` to the holiday-tolerant ``_edge_bound``;
# cs74's HIT-path-only scope left the MISS flag with the bug. The fix mirrors cs74
# EXACTLY: switch the MISS-path flag bound to ``_edge_bound``.


def test_cached_fetch_miss_interior_flag_single_weekend_merged_no_false_flag(tmp_path):
    """cs76 RED->GREEN (MISS-side twin of cs74): a SHORT MISS whose ENTIRE merged set
    straddles exactly ONE weekend must NOT false-flag interior discontiguity.

    The cache holds Thu+Fri only (short of the lookback -> MISS by count). The
    provider supplies Thu,Fri,Mon and ADVANCES the right edge to Monday == cutoff
    (FRESH edge, so cs49's right_edge_stale_days correctly does NOT fire). The merged
    set is exactly [Thu, Fri, Mon] -> the single Fri->Mon weekend gap appears ONCE,
    so the cs73 recurrence-only ``_calendar_bound`` degrades to the canonical 1-day
    floor and (3d weekend > 1d*1.5) FALSE-FLAGS this contiguous-modulo-weekends
    window. The cs76 fix learns the flag bound from the holiday-tolerant ``_edge_bound``
    (recurring rhythm + one closed session day), so a single legitimate weekend gap is
    tolerated (3d <= 3d) and no flag fires.

    RED today: ``served_window_discontiguous`` is wrongly True with
    ``interior_gap_days == 3``. GREEN after: both flags absent.
    """
    cache = OhlcvCache("alpaca", "WK1", "1d", root=tmp_path, prefer_parquet=False)
    # Thu+Fri only (2 bars), short of lookback=3 -> MISS by count.
    cache.write(_bars_from_ts(["2026-01-08", "2026-01-09"]))
    cutoff = pd.Timestamp("2026-01-12", tz="UTC")  # Monday == provider edge

    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        # Provider ADVANCES the edge to Monday; merged = [Thu, Fri, Mon] straddles
        # exactly ONE weekend -> the gap appears ONCE even in merged (the cs76 trigger).
        return _bars_from_ts(["2026-01-08", "2026-01-09", "2026-01-12"])

    out, meta = cached_fetch(
        fetch,
        provider="alpaca",
        symbol="WK1",
        timeframe="1d",
        lookback_bars=3,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    assert meta["cache_hit"] is False
    # The served window is the whole single-weekend merged set: [Thu, Fri, Mon].
    days = list(out["timestamp"].dt.day_name())
    assert days == ["Thursday", "Friday", "Monday"], days
    # A single legitimate weekend gap is NOT an interior discontiguity (cs76 fix).
    assert "interior_gap_days" not in meta
    assert "served_window_discontiguous" not in meta
    # The edge advanced to the cutoff Monday -> the cs49 edge flag must not fire.
    assert "right_edge_stale_days" not in meta


def test_cached_fetch_miss_interior_flag_intraday_single_overnight_no_false_flag(
    tmp_path,
):
    """cs76 (intraday variant): a SHORT INTRADAY MISS whose ENTIRE merged set spans
    exactly TWO sessions (ONE overnight gap) must NOT false-flag. The single overnight
    gap appears once even in merged, so the recurrence-only ``_calendar_bound`` degrades
    to the 1h floor and the ~18h overnight > 1h*1.5 wrongly flags; the ``_edge_bound``
    one-closed-session-day allowance tolerates the single overnight.
    """
    cache = OhlcvCache("alpaca", "OVN", "1h", root=tmp_path, prefer_parquet=False)
    # Day-1 RTH session only (7 bars), short of the requested lookback -> MISS.
    cache.write(_intraday_rth_bars("2024-05-22", "2024-05-22"))

    def fetch():
        # Provider ADVANCES the edge into day-2's session; merged spans exactly TWO
        # sessions -> the single overnight gap appears ONCE in merged.
        return _intraday_rth_bars("2024-05-22", "2024-05-23")

    bars2 = _intraday_rth_bars("2024-05-22", "2024-05-23")
    cutoff = bars2["timestamp"].max()  # FRESH edge: cutoff == newest bar (day-2 close)
    out, meta = cached_fetch(
        fetch,
        provider="alpaca",
        symbol="OVN",
        timeframe="1h",
        lookback_bars=14,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    assert meta["cache_hit"] is False
    # A single legitimate overnight session gap is NOT an interior discontiguity.
    assert "interior_gap_days" not in meta
    assert "served_window_discontiguous" not in meta
    assert "right_edge_stale_days" not in meta


def test_cached_fetch_cs76_miss_flag_still_flags_unfillable_hole(tmp_path):
    """cs76 must NOT silence the cs73 genuinely-unfillable interior hole. Widening the
    MISS-flag bound to ``_edge_bound`` only adds ONE closed session day of tolerance; a
    multi-step unfillable hole (delisting stretch ~10 session-days wide) is far above
    ``_edge_bound * 1.5`` -> ``served_window_discontiguous`` STILL fires. This is the
    exact cs73 original case; the cs76 fix must leave it flagged.
    """
    cache = OhlcvCache("yfinance", "DELISTED2", "1d", root=tmp_path, prefer_parquet=False)
    seg1 = {f"2026-01-{d:02d}": 100 + d for d in range(1, 6)}  # 01..05 contiguous
    seg2 = {f"2026-01-{d:02d}": 100 + d for d in (15, 16)}  # edge after a 06..14 hole
    cache.write(_daily_bars_from({**seg1, **seg2}))
    cutoff = pd.Timestamp("2026-01-17", tz="UTC")

    def fetch():
        return _daily_bars_from({"2026-01-17": 117})  # advances edge, cannot fill hole

    out, meta = cached_fetch(
        fetch,
        provider="yfinance",
        symbol="DELISTED2",
        timeframe="1d",
        lookback_bars=8,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    assert meta["cache_hit"] is False
    # The genuinely-unfillable ~10-day interior hole STILL flags (cs73 intact).
    assert meta["served_window_discontiguous"] is True
    assert meta["interior_gap_days"] >= 9


def test_cached_fetch_cs76_multiweek_recurring_weekend_miss_byte_identical(tmp_path):
    """cs76 anti-regression: a long MISS window whose Fri->Mon weekend RECURS (>=2x in
    merged) was ALREADY non-flagging under the recurrence-only ``_calendar_bound`` (3d
    learned -> 3d <= 4.5d). Widening to ``_edge_bound`` (4d) only LOOSENS the bound, so
    the flag decision cannot flip -> the outcome is byte-identical (no interior flag),
    while cs49's right-edge flag still fires (the provider stays months below cutoff).
    """
    cache = OhlcvCache("alpaca", "DEAD2", "1d", root=tmp_path, prefer_parquet=False)
    cache.write(_daily_business_bars("2024-01-01", "2024-01-05"))  # one short week

    def fetch():
        # Multi-week Mon-Fri window (recurring weekends), right edge months below cutoff.
        return _daily_business_bars("2024-01-01", "2024-02-16")

    cutoff = pd.Timestamp("2024-06-03T00:00:00Z")
    out, meta = cached_fetch(
        fetch,
        provider="alpaca",
        symbol="DEAD2",
        timeframe="1d",
        lookback_bars=20,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    assert meta["cache_hit"] is False
    # Recurring weekends -> no interior flag (byte-identical to cs73).
    assert "interior_gap_days" not in meta
    assert "served_window_discontiguous" not in meta
    # The right edge stays months below cutoff -> cs49 edge flag still fires.
    assert meta["right_edge_stale_days"] >= 100


def test_cached_fetch_miss_interior_flag_cutoff_none_byte_identical(tmp_path):
    """cs73: cutoff=None (live caller) imposes NO interior-discontiguity flag, even
    when the served window has an interior hole -> byte-identical to the prior
    behaviour (no flag).
    """
    cache = OhlcvCache("yfinance", "X", "1d", root=tmp_path, prefer_parquet=False)
    # Short warm cache (2 bars) << lookback so the count gate forces a MISS/fetch.
    cache.write(_daily_bars_from({"2026-01-01": 101, "2026-01-02": 102}))

    def fetch():
        # Provider supplies a window with an interior hole (03..14 missing): the
        # merged served tail is 01,02 then 15,16,17 -> internally discontiguous.
        return _daily_bars_from(
            {"2026-01-15": 115, "2026-01-16": 116, "2026-01-17": 117}
        )

    out, meta = cached_fetch(
        fetch,
        provider="yfinance",
        symbol="X",
        timeframe="1d",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=None,
    )
    assert meta["cache_hit"] is False
    # cutoff=None -> the interior flag is never computed (byte-identical).
    assert "interior_gap_days" not in meta
    assert "served_window_discontiguous" not in meta


# cs74: the HIT-path cs50 CONTIGUITY bound used the RECURRENCE-ONLY
# ``_calendar_bound`` (a gap must appear >=2x to be learned). For a SHORT cache
# whose entire eligible set straddles exactly ONE legitimate weekend (or one
# overnight session gap), that gap occurs only ONCE -> _calendar_bound degrades to
# the canonical 1-step floor, so the served tail's max_gap (the single weekend /
# overnight) exceeds contig_bound*1.5 and the HIT is wrongly rejected EVEN THOUGH
# the right edge is perfectly fresh (cutoff==newest, passed by the looser
# ``_edge_bound``). The MISS path re-serves the identical bars (cs49/cs71 skip the
# append) BUT fetch_fn is called on every replay = refetch-forever (the calls==0
# contract). The fix mirrors cs63 onto the HIT contiguity gate: contig_bound now
# uses ``_edge_bound`` (recurring rhythm + one closed session day) so ONE
# non-recurring session gap in a short cache passes, while a cs50 multi-step
# interior hole (many session-days wide) still fails contiguity.


def test_cached_fetch_daily_one_weekend_fresh_edge_hits_no_refetch_loop(tmp_path):
    """cs74 (REGRESSION cs58): a SHORT, fresh, contiguous DAILY cache that straddles
    exactly ONE weekend (Mon..Fri..Mon..Fri = 10 bdays) with an --end anchored AT the
    newest Friday (FRESH edge, cutoff==newest) was HIT-rejected because the single
    Fri->Mon weekend gap appears only ONCE -> the recurrence-only contig_bound degrades
    to the 1-day floor and the 3-day weekend in the served tail fails contiguity. The
    fresh edge passes ``_edge_bound`` so the cache then refetched on EVERY run (calls=3)
    even though the provider has nothing newer = a refetch-forever loop on a demonstrably
    fresh short cache. The fix gives the contiguity bound the cs63 one-closed-session-day
    allowance so the single weekend is tolerated and the cache HITs.

    RED today: hits=[F,F,F], calls=3. GREEN after: HITs each run, calls==0.
    """
    calls = {"n": 0}
    # 10 Mon-Fri daily bars, ONE weekend (Fri 2024-05-10 -> Mon 2024-05-13).
    bars = _daily_business_bars("2024-05-06", "2024-05-17")
    cache = OhlcvCache("alpaca", "AAPL", "1d", root=tmp_path, prefer_parquet=False)
    cache.write(bars)

    def fetch():
        calls["n"] += 1
        return bars  # provider has nothing newer than the newest Friday

    cutoff = bars["timestamp"].max()  # FRESH edge: cutoff == newest bar (Fri)
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
    assert calls["n"] == 0  # no refetch-forever loop
    assert (out["timestamp"] <= cutoff).all()


def test_cached_fetch_intraday_one_overnight_fresh_edge_hits_no_refetch_loop(tmp_path):
    """cs74: a SHORT, fresh INTRADAY 1h cache spanning exactly TWO RTH sessions (one
    18h overnight gap) with --end anchored AT the newest bar (FRESH edge) was wrongly
    HIT-rejected — the single overnight gap appears once so contig_bound degrades to
    the 1h floor and the 18h gap fails contiguity, then refetched every run. The cs63
    holiday allowance applied to the contiguity bound tolerates the single overnight.

    RED today: hits=[F,F,F], calls=3. GREEN after: HITs each run, calls==0.
    """
    calls = {"n": 0}
    # Two RTH sessions (14:00-20:00Z) -> 7 bars each = 14 bars, ONE 18h overnight gap.
    bars = _intraday_rth_bars("2024-05-22", "2024-05-23")
    assert len(bars) == 14
    cache = OhlcvCache("alpaca", "MSFT", "1h", root=tmp_path, prefer_parquet=False)
    cache.write(bars)

    def fetch():
        calls["n"] += 1
        return bars

    cutoff = bars["timestamp"].max()  # FRESH edge
    kwargs = dict(
        provider="alpaca",
        symbol="MSFT",
        timeframe="1h",
        lookback_bars=14,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    for _ in range(3):
        out, meta = cached_fetch(fetch, **kwargs)
        assert meta["cache_hit"] is True
    assert calls["n"] == 0
    assert (out["timestamp"] <= cutoff).all()


def test_cached_fetch_cs74_edge_bound_still_rejects_cs50_interior_hole(tmp_path):
    """cs74 must NOT reopen the cs50 interior-hole rejection. Even with the contiguity
    bound widened to ``_edge_bound`` (recurring rhythm + one closed session day, ~1d1h
    for a 1h cache), a single multi-month INTERIOR hole (200 contiguous hourly bars
    glued to one fresh bar AT the cutoff, ~143 days wide) is MANY session-days, far
    above ``_edge_bound * 1.5`` (~37.5h) -> still fails contiguity -> MISS/fetch.
    """
    calls = {"n": 0}
    ancient = _bars(200, start="2024-01-01")  # contiguous hourly
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


def test_cached_fetch_cs74_edge_bound_still_rejects_cs43_multimonth(tmp_path):
    """cs74 must NOT reopen the cs43 multi-month-stale rejection. A DAILY Mon-Fri cache
    ending months before the anchor fails on the EDGE gate (``fresh_right_edge`` False
    via ``_edge_bound``), which is independent of the contiguity bound the cs74 fix
    touches -> the HIT is still REJECTED and the fresh provider refreshes.
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


def test_cached_fetch_cs74_multiweek_recurring_weekend_byte_identical(tmp_path):
    """cs74: a 4-week DAILY cache whose Fri->Mon weekend gap RECURS (>=2x) was ALREADY
    contiguous under the recurrence-only ``_calendar_bound`` (3d learned -> 3d <= 4.5d).
    Widening the contiguity bound to ``_edge_bound`` (4d) only LOOSENS the test, so the
    HIT decision cannot flip -> the outcome is byte-identical: HITs every run, calls==0,
    and the served tail is stable run-to-run (no loop-induced drift).
    """
    calls = {"n": 0}
    bars = _daily_business_bars("2024-05-06", "2024-05-31")  # 4 trading weeks
    cache = OhlcvCache("alpaca", "AAPL", "1d", root=tmp_path, prefer_parquet=False)
    cache.write(bars)

    def fetch():
        calls["n"] += 1
        return bars

    cutoff = bars["timestamp"].max()  # FRESH edge
    kwargs = dict(
        provider="alpaca",
        symbol="AAPL",
        timeframe="1d",
        lookback_bars=10,
        cache_root=tmp_path,
        prefer_parquet=False,
        cutoff=cutoff,
    )
    outs = []
    for _ in range(3):
        out, meta = cached_fetch(fetch, **kwargs)
        assert meta["cache_hit"] is True
        outs.append(out)
    assert calls["n"] == 0
    # Served tail byte-identical run-to-run (no refetch-forever drift).
    assert list(outs[0]["timestamp"]) == list(outs[1]["timestamp"])
    assert list(outs[1]["timestamp"]) == list(outs[2]["timestamp"])
    assert (outs[0]["timestamp"] <= cutoff).all()
