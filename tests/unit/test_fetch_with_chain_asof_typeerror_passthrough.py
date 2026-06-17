"""ar103 follow-up: ``_fetch_bars_with_optional_asof`` must NOT silently swallow a
REAL ``TypeError`` raised from inside ``provider.fetch_bars`` (one unrelated to the
``as_of`` kwarg). Swallowing such an error and retrying WITHOUT ``as_of`` would
silently drop the no-lookahead leaf cutoff — the exact forward-bias leak ar103
set out to close, reintroduced through the over-broad degrade path.

The original ar103 guard was::

    if "as_of" in str(exc) or "unexpected keyword" in str(exc):
        return provider.fetch_bars(... no as_of ...)

CPython's genuine legacy signature error always NAMES the offending kwarg, e.g.
``fetch_bars() got an unexpected keyword argument 'as_of'`` — so the ``"as_of"``
clause alone catches the real legacy provider. The extra ``or "unexpected
keyword"`` disjunct is pure over-breadth: a MODERN provider that DOES accept
``as_of`` but whose body raises an unrelated unexpected-keyword ``TypeError``
(a downstream lib kwarg-typo, a pandas API change, etc.) produces a message
that contains ``"unexpected keyword"`` but NOT ``"as_of"`` -> it was misclassified
as "legacy" and retried WITHOUT the cutoff. That silently re-opens the leak the
fix exists to prevent (fail-OPEN on the no-lookahead bound).

RED before fix: the unrelated-TypeError provider's bars (including a post-cutoff
bar) are returned because the retry dropped ``as_of``.
GREEN after fix: the unrelated ``TypeError`` propagates (fails CLOSED — the chain
either tries the next provider or raises, never silently un-bounds the future).
"""

from __future__ import annotations

import pandas as pd
import pytest

from hermes_quant.data.base import _fetch_bars_with_optional_asof, fetch_with_chain


def _series_with_future() -> pd.DataFrame:
    dates = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
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


class _ModernProviderWithUnrelatedBug:
    """A modern provider that DOES accept ``as_of`` (no legacy gap), but whose body
    raises an unrelated unexpected-keyword TypeError on the FIRST (as_of-carrying)
    call. If the degrade path swallows it and retries WITHOUT ``as_of``, the second
    call succeeds and returns FUTURE bars (the no-lookahead cutoff was dropped)."""

    name = "modern_buggy"

    def __init__(self, full_series: pd.DataFrame) -> None:
        self._full = full_series

    def fetch_bars(self, asset, timeframe, start, end, *, use_cache=True, as_of=None):
        if as_of is not None:
            # Simulate a real internal bug surfacing only when as_of is supplied:
            # a downstream helper called with a typo'd kwarg.
            def _downstream(value, mode="x"):
                return value

            _downstream(1, moed="y")  # TypeError: unexpected keyword argument 'moed'
        # Retry-without-as_of path: ignores start/end (AV-style) -> leaks the future.
        return self._full.copy()


def test_unrelated_typeerror_is_not_swallowed_as_legacy():
    """A modern provider's unrelated unexpected-keyword TypeError must PROPAGATE
    from ``_fetch_bars_with_optional_asof`` — not be retried without ``as_of``."""
    provider = _ModernProviderWithUnrelatedBug(_series_with_future())
    as_of = pd.Timestamp("2026-01-07", tz="UTC")

    with pytest.raises(TypeError) as ei:
        _fetch_bars_with_optional_asof(
            provider,
            "IBM",
            "1d",
            pd.Timestamp("2026-01-01", tz="UTC"),
            as_of,
            True,
            as_of,
        )
    # It is the unrelated bug that surfaces, not a silent leak.
    assert "moed" in str(ei.value)


def test_chain_does_not_leak_future_when_provider_has_unrelated_typeerror():
    """End-to-end through ``fetch_with_chain``: a single modern-but-buggy provider
    must NOT silently return future bars. The unrelated TypeError must surface as a
    chain failure (DataProviderError after exhausting providers) — never a quietly
    un-bounded result."""
    provider = _ModernProviderWithUnrelatedBug(_series_with_future())
    as_of = pd.Timestamp("2026-01-07", tz="UTC")

    from hermes_quant.data.base import DataProviderError

    # The chain catches "any other exception" per-provider and records it, then
    # raises DataProviderError once all providers fail. The critical invariant is
    # that it does NOT return a future-leaking DataFrame.
    with pytest.raises((TypeError, DataProviderError)):
        fetch_with_chain(
            [provider],
            "IBM",
            "1d",
            start=pd.Timestamp("2026-01-01", tz="UTC"),
            end=as_of,
            as_of=as_of,
        )


def test_genuine_legacy_provider_still_degrades():
    """Non-vacuity / byte-identity: a genuine legacy provider WITHOUT the ``as_of``
    kwarg must still degrade gracefully (the real CPython message names 'as_of')."""

    class _LegacyProvider:
        name = "legacy"

        def __init__(self, df):
            self._df = df

        def fetch_bars(self, asset, timeframe, start, end, *, use_cache=True):
            # windows by end (its only — weaker — bound), as a real legacy provider does
            df = self._df.copy()
            end_naive = pd.Timestamp(end)
            if end_naive.tzinfo is not None:
                end_naive = end_naive.tz_convert("UTC").tz_localize(None)
            return df[df["timestamp"] <= end_naive].reset_index(drop=True)

    provider = _LegacyProvider(_series_with_future())
    as_of = pd.Timestamp("2026-01-07", tz="UTC")
    out = _fetch_bars_with_optional_asof(
        provider,
        "IBM",
        "1d",
        pd.Timestamp("2026-01-01", tz="UTC"),
        as_of,
        True,
        as_of,
    )
    # legacy degrade still works: windowed by end, no exception
    assert pd.Timestamp("2026-01-07") in set(out["timestamp"])
    assert pd.Timestamp("2026-01-09") not in set(out["timestamp"])
