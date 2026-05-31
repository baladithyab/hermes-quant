"""PDR-3 asof honesty / no-lookahead (plan §5.4).

A future-dated source must NOT manufacture convergence. The validator has no clock
of its own; the CALLER filters the item set to published_at <= decision asof
(load_packets_for validates packets <= asof, synthesize.py). Convergence over the
filtered set must drop the future family. This makes the no-lookahead rail
executable for the cross-SOURCE require_ensemble.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from hermes_quant.catalyst.ingest import CatalystItem
from hermes_quant.perception.convergence import validate_convergence


def _item(symbol: str, source: str, published_at: str) -> CatalystItem:
    return CatalystItem(
        title=f"{symbol} trend headline",
        published_at=dt.datetime.fromisoformat(published_at),
        source=source,
        link="n/a",
        query="asof-test",
    )


def _reddit_item(symbol: str, *, published_at: str) -> CatalystItem:
    return _item(symbol, "reddit/r/stocks (score=10 c=2)", published_at)


def _trends_item(symbol: str, *, published_at: str) -> CatalystItem:
    return _item(symbol, "google_trends/US", published_at)


def test_convergence_only_sees_items_at_or_before_asof():
    """A future-dated trends source must NOT contribute. The caller filters items to
    <= decision asof; convergence over the filtered set drops the future family."""
    past_reddit = _reddit_item("CELH", published_at="2024-01-01T00:00:00+00:00")
    future_trends = _trends_item("CELH", published_at="2099-01-01T00:00:00+00:00")
    asof = pd.Timestamp("2024-06-01T00:00:00Z")
    visible = [it for it in [past_reddit, future_trends] if it.published_at <= asof]
    r = validate_convergence(visible)
    assert r.n_independent == 1 and not r.validated  # future trends excluded => single


def test_with_both_visible_it_validates():
    """When both sources are at/before asof, convergence validates — proves the
    drop above is the asof filter, not a degenerate single-source set."""
    past_reddit = _reddit_item("CELH", published_at="2024-01-01T00:00:00+00:00")
    past_trends = _trends_item("CELH", published_at="2024-02-01T00:00:00+00:00")
    asof = pd.Timestamp("2024-06-01T00:00:00Z")
    visible = [it for it in [past_reddit, past_trends] if it.published_at <= asof]
    r = validate_convergence(visible)
    assert r.n_independent == 2 and r.validated


def test_validator_has_no_clock():
    """The validator scores EXACTLY the handed-in set — it never re-admits a future
    item. Handing it the future item directly still counts it (the FILTER is the
    caller's job), proving the validator is pure over its input (no hidden clock)."""
    past_reddit = _reddit_item("CELH", published_at="2024-01-01T00:00:00+00:00")
    future_trends = _trends_item("CELH", published_at="2099-01-01T00:00:00+00:00")
    # both handed in unfiltered -> validator counts both (no clock of its own)
    r = validate_convergence([past_reddit, future_trends])
    assert r.n_independent == 2
    # ...but the no-lookahead gate is the caller's <=asof filter, asserted above.
