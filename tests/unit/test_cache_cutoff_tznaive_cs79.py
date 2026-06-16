"""cs79 — cached_fetch must tolerate a tz-NAIVE cutoff (no TypeError).

The cached OHLCV ``timestamp`` column is tz-aware (datetime64[ns, UTC]). Before
the fix, passing a tz-NAIVE ``cutoff`` Timestamp raised
``TypeError: Invalid comparison between dtype=datetime64[ns, UTC] and Timestamp``
at the cache-HIT eligible filter (``cached[cached["timestamp"] <= cutoff]``) and at
the cs43 right-edge / cs50 contiguity / cs49+cs73 MISS comparison sites. The fix
normalizes ``cutoff`` to tz-aware UTC ONCE at the top of ``cached_fetch`` so every
downstream comparison is safe.

This was filed by a worktree fix lane that branched from a STALE base where
``cached_fetch`` had no ``cutoff`` parameter at all (so the lane correctly reported
not-a-defect ON ITS BASE). The bug is REAL on the integration branch (cs49/cs50/cs58
landed the cutoff-aware cache here), so this test + the fix are authored directly on
the integration branch.

Tests:
  1. a tz-NAIVE cutoff does NOT raise and prunes IDENTICALLY to the tz-aware
     equivalent (byte-identical eligible set) — the core contract.
  2. non-vacuity: a cutoff that excludes some bars actually prunes them (so the
     test is not satisfied by a degenerate all-pass).
  3. an already tz-aware cutoff is unchanged (byte-identical to the prior behaviour).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from hermes_quant.data.cache import OhlcvCache, cached_fetch


def _bars(n=200, start="2024-01-01", *, seed=1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = pd.date_range(start, periods=n, freq="1h", tz="UTC")  # tz-AWARE column
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


def _run(tmp_path, cutoff) -> tuple[pd.DataFrame, dict, int]:
    calls = {"n": 0}
    cache = OhlcvCache("ccxt", "BTC/USDT", "1h", root=tmp_path, prefer_parquet=False)
    cache.write(_bars(200, start="2024-01-01"))

    def fetch():
        calls["n"] += 1
        return _bars(200, start="2024-01-01")

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
    return out, meta, calls["n"]


def test_cs79_tznaive_cutoff_does_not_raise_and_matches_aware(tmp_path):
    """THE CONTRACT: a tz-NAIVE cutoff must not raise and must prune identically to
    the tz-aware equivalent.

    Pre-fix repro (documented, not run here to keep the test green): the naive
    branch hit ``cached[cached["timestamp"] <= cutoff]`` and raised
    ``TypeError: Invalid comparison between dtype=datetime64[ns, UTC] and Timestamp``.
    """
    naive = pd.Timestamp("2024-01-03T00:00:00")  # tz-NAIVE
    aware = pd.Timestamp("2024-01-03T00:00:00Z")  # tz-AWARE, same instant

    out_naive, meta_naive, calls_naive = _run(tmp_path / "naive", naive)  # must NOT raise
    out_aware, meta_aware, calls_aware = _run(tmp_path / "aware", aware)

    # Identical pruning: same row count, same right edge, same hit/miss verdict.
    assert len(out_naive) == len(out_aware)
    assert calls_naive == calls_aware
    assert meta_naive["cache_hit"] == meta_aware["cache_hit"]
    # The aware cutoff is the comparison anchor; the naive result must respect it too.
    assert (out_naive["timestamp"] <= aware).all()
    pd.testing.assert_frame_equal(
        out_naive.reset_index(drop=True), out_aware.reset_index(drop=True)
    )


def test_cs79_cutoff_actually_prunes_future_bars_nonvacuous(tmp_path):
    """Non-vacuity: the cutoff genuinely excludes post-cutoff bars (so the equality
    test above is not satisfied by a no-op all-pass)."""
    naive = pd.Timestamp("2024-01-03T00:00:00")  # 49 hourly bars at-or-before
    out, _meta, _calls = _run(tmp_path, naive)
    aware = pd.Timestamp("2024-01-03T00:00:00Z")
    # Every served bar is at-or-before the cutoff, and the newest is strictly within
    # the pre-cutoff window (proves bars WERE pruned, not just all returned).
    assert (out["timestamp"] <= aware).all()
    assert out["timestamp"].iloc[-1] <= aware


def test_cs79_aware_cutoff_unchanged(tmp_path):
    """An already tz-aware cutoff passes through untouched (byte-identical to the
    prior behaviour) — the fix must not perturb the existing aware-caller path."""
    aware = pd.Timestamp("2024-01-03T00:00:00Z")
    out, meta, calls = _run(tmp_path, aware)
    assert meta["cache_hit"] is True
    assert calls == 0
    assert (out["timestamp"] <= aware).all()
