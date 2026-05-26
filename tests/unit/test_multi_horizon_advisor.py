"""Tests for advisor.recommend_multi_horizon fan-out (ADR-0036)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from hermes_quant.advisor import recommend_multi_horizon
from hermes_quant.protocol import AnalystView, MarketContext


# --- Helpers -----------------------------------------------------------------


def _make_daily_df(n: int = 600, start: str = "2023-01-04") -> pd.DataFrame:
    idx = pd.bdate_range(start=start, periods=n)
    rng = np.random.default_rng(seed=7)
    close = 100.0 + rng.normal(0, 1, size=n).cumsum()
    return pd.DataFrame(
        {
            "timestamp": idx,
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(n, 1_000_000.0),
        }
    )


class _StubProvider:
    """Returns the same daily DataFrame every fetch_bars call."""

    name = "stub"
    asset_classes = ["equity", "etf"]
    timeframes = ["1d"]

    def __init__(self, df: pd.DataFrame):
        self._df = df
        self.calls: list[tuple[str, str]] = []

    def fetch_bars(
        self,
        asset: str,
        timeframe: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
        *,
        as_of: pd.Timestamp | None = None,
        **_: Any,
    ) -> pd.DataFrame:
        self.calls.append((asset, timeframe))
        df = self._df.copy()
        if as_of is not None:
            cutoff = as_of.tz_convert("UTC").tz_localize(None) if as_of.tzinfo else as_of
            df = df[df["timestamp"] <= cutoff].reset_index(drop=True)
        return df


class _StubAnalyst:
    """Records which timeframes it was called with; emits one view each call."""

    def __init__(self, name: str, direction: int = 1, conf: float = 0.7):
        self.name = name
        self._direction = direction
        self._conf = conf
        self.observed_timeframes: list[str] = []

    def analyze(self, ctx: MarketContext) -> AnalystView | None:
        self.observed_timeframes.append(ctx.timeframe)
        return AnalystView(
            analyst=self.name,
            direction=self._direction,
            magnitude=0.01,
            confidence=self._conf,
            confidence_raw=self._conf,
            # Set horizon as the analyst's intrinsic horizon ("1d" baseline);
            # the fan-out wrapper retags this to ctx.timeframe per ADR-0036.
            horizon="1d",
        )


class _SilentAnalyst:
    def __init__(self, name: str = "silent"):
        self.name = name

    def analyze(self, ctx: MarketContext) -> AnalystView | None:
        return None


# --- Tests -------------------------------------------------------------------


class TestRecommendMultiHorizon:
    def test_two_analysts_two_horizons_yields_four_views(self):
        df = _make_daily_df()
        provider = _StubProvider(df)
        a1 = _StubAnalyst("a1")
        a2 = _StubAnalyst("a2")

        views = recommend_multi_horizon(
            "AAPL",
            horizons=["1d", "1w"],
            asset_class="equity",
            provider=provider,
            analysts=[a1, a2],
            as_of="2024-12-31",
        )

        assert len(views) == 4, f"expected 4 views (2 analysts × 2 horizons), got {len(views)}"
        # Each (analyst, horizon) pair must appear exactly once
        pairs = {(v.analyst, v.horizon) for v in views}
        assert pairs == {
            ("a1", "1d"),
            ("a1", "1w"),
            ("a2", "1d"),
            ("a2", "1w"),
        }

    def test_view_horizon_field_matches_requested_horizon(self):
        df = _make_daily_df()
        provider = _StubProvider(df)
        a1 = _StubAnalyst("a1")
        views = recommend_multi_horizon(
            "AAPL",
            horizons=["1d", "1w", "1M"],
            asset_class="equity",
            provider=provider,
            analysts=[a1],
            as_of="2024-12-31",
        )
        # Even though the stub analyst hardcodes horizon="1d" on its view,
        # the fan-out wrapper retags it to the ctx.timeframe.
        assert sorted(v.horizon for v in views) == ["1M", "1d", "1w"]

    def test_silent_analyst_skipped_no_penalty(self):
        df = _make_daily_df()
        provider = _StubProvider(df)
        loud = _StubAnalyst("loud")
        silent = _SilentAnalyst()

        views = recommend_multi_horizon(
            "AAPL",
            horizons=["1d", "1w"],
            asset_class="equity",
            provider=provider,
            analysts=[loud, silent],
            as_of="2024-12-31",
        )
        # Only the loud analyst contributes — 2 views (one per horizon)
        assert len(views) == 2
        assert all(v.analyst == "loud" for v in views)

    def test_single_horizon_default_passthrough(self):
        """recommend() shim must keep working with timeframe='1d' single-horizon."""
        from hermes_quant.advisor import recommend

        df = _make_daily_df()
        provider = _StubProvider(df)
        a1 = _StubAnalyst("a1")

        out = recommend(
            "AAPL",
            asset_class="equity",
            timeframe="1d",
            provider=provider,
            analysts=[a1],
            as_of="2024-12-31",
        )
        # The legacy recommend() returns a dict, not a list of views.
        assert isinstance(out, dict)
        assert out["symbol"] == "AAPL"
        assert out["timeframe"] == "1d"
        # One analyst view in the result (one horizon)
        assert len(out["analyst_views"]) == 1
