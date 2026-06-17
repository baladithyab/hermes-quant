"""Regression: ``fetch_with_chain`` must thread the no-lookahead ``as_of`` cutoff
down to each provider's ``fetch_bars`` (PIT / forward-bias leak).

The leaf-level no-lookahead filter (``timestamp <= as_of``) is the entire
no-lookahead mechanism for the data layer (ADR-0005 amendment, Wave C.1). For
yfinance the ``end=`` query upper bound coincidentally limits most lookahead,
but the AlphaVantage fallback tier returns the last ~100 daily bars REGARDLESS
of ``start``/``end`` (free-tier ``compact``) — its docstring states the
``as_of`` leaf filter is the ONLY no-lookahead enforcement it has.

``fetch_with_chain`` (data/base.py) calls ``provider.fetch_bars(asset, tf,
start, end, use_cache=...)`` with NO ``as_of``. So when the fallback chain is
enabled (``HERMES_QUANT_DATA_FALLBACK=1``) and a backtest/replay at a historical
``asof`` falls through to AlphaVantage, bars right up to "today" leak in: a
forward-looking-bias leak in the dangerous direction. The same path is reached
live-or-replay via ``daemon.tick_loop.run_one_tick`` (end=asof, no as_of).

This test uses a provider that mimics the AlphaVantage contract (ignores
start/end; only the ``as_of`` leaf filter bounds the future) and proves the
leak through ``fetch_with_chain``.

RED before fix: bars dated after ``as_of`` survive ``fetch_with_chain``.
"""

from __future__ import annotations

import pandas as pd

from hermes_quant.data.base import fetch_with_chain


class _AsOfOnlyProvider:
    """Mimics AlphaVantageProvider: returns its full series ignoring start/end;
    the only no-lookahead bound is the ``as_of`` leaf filter (if passed)."""

    name = "asof_only"

    def __init__(self, full_series: pd.DataFrame) -> None:
        self._full = full_series

    def fetch_bars(self, asset, timeframe, start, end, *, use_cache=True, as_of=None):
        out = self._full.copy()
        # Deliberately DO NOT window by start/end (mirrors AV compact behavior).
        if as_of is not None:
            cutoff = pd.Timestamp(as_of)
            if cutoff.tzinfo is not None:
                cutoff = cutoff.tz_convert("UTC").tz_localize(None)
            out = out[out["timestamp"] <= cutoff].reset_index(drop=True)
        return out


def _series() -> pd.DataFrame:
    dates = [
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",  # backtest as_of cutoff here
        "2026-01-08",  # FUTURE relative to as_of
        "2026-01-09",  # FUTURE relative to as_of
    ]
    n = len(dates)
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(dates),
            "open": [10.0 + i for i in range(n)],
            "high": [11.0 + i for i in range(n)],
            "low": [9.0 + i for i in range(n)],
            "close": [10.5 + i for i in range(n)],
            "volume": [100 + i for i in range(n)],
        }
    )


def test_fetch_with_chain_threads_asof_cutoff_to_provider():
    provider = _AsOfOnlyProvider(_series())
    as_of = pd.Timestamp("2026-01-07", tz="UTC")

    bars = fetch_with_chain(
        [provider],
        "IBM",
        "1d",
        start=pd.Timestamp("2026-01-01", tz="UTC"),
        end=as_of,
        as_of=as_of,
    )

    cutoff_naive = as_of.tz_convert("UTC").tz_localize(None)
    max_ts = pd.Timestamp(bars["timestamp"].max())
    assert max_ts <= cutoff_naive, (
        f"forward-bias leak: fetch_with_chain returned bar at {max_ts} > as_of "
        f"{cutoff_naive}; the as_of cutoff was not threaded to provider.fetch_bars"
    )
    # Non-vacuity: the as_of-honest result must still contain the in-window bars.
    assert pd.Timestamp("2026-01-07") in set(bars["timestamp"])
    assert len(bars) == 3


def test_fetch_with_chain_default_call_shape_does_not_leak_future_bars():
    """The production call shape (daemon.tick_loop.run_one_tick) passes
    ``end=asof`` but NO ``as_of``. With an AV-style provider that ignores
    start/end, future bars must NOT leak even when as_of is omitted: the chain
    must default ``as_of`` to ``end`` so the leaf cutoff still bounds the future.
    """
    provider = _AsOfOnlyProvider(_series())
    asof = pd.Timestamp("2026-01-07", tz="UTC")

    # Mirror tick_loop.run_one_tick: positional, no as_of kwarg.
    bars = fetch_with_chain(
        [provider],
        "IBM",
        "1d",
        pd.Timestamp("2026-01-01", tz="UTC"),
        asof,
    )

    cutoff_naive = asof.tz_convert("UTC").tz_localize(None)
    max_ts = pd.Timestamp(bars["timestamp"].max())
    assert max_ts <= cutoff_naive, (
        f"forward-bias leak (production call shape): bar at {max_ts} > end/asof "
        f"{cutoff_naive}; chain must bound the future by end when as_of is omitted"
    )
