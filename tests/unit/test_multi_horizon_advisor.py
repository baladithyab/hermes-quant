"""Tests for advisor.recommend_multi_horizon fan-out (ADR-0036)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

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


class _CapturingAnalyst:
    """Records the MarketContext it was handed (last_close + last bar ts)."""

    def __init__(self, name: str = "cap"):
        self.name = name
        self.seen: list[MarketContext] = []

    def analyze(self, ctx: MarketContext) -> AnalystView | None:
        self.seen.append(ctx)
        return AnalystView(
            analyst=self.name,
            direction=1,
            magnitude=0.01,
            confidence=0.7,
            confidence_raw=0.7,
            horizon="1d",
        )


class TestRecommendMultiHorizonStillFormingDrop:
    """cs54 — recommend_multi_horizon's NATIVE-timeframe path (1d/1h) must drop
    the still-forming trailing bar, mirroring single-horizon recommend()
    (advisor.py:923-924, a643) and the resample clip (cs05). NO-LOOKAHEAD is a
    charter invariant: an intraday-asof daily read must not feed today's
    not-yet-final OHLCV into any analyst's MarketContext.
    """

    def _daily_df_ending_today(self) -> pd.DataFrame:
        """Two settled bars (5/26, 5/27) + today's (5/28) still-forming bar.

        Today's close (200.0) is deliberately far from the last SETTLED close
        (101.0) so a leak is unmistakable.
        """
        return pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    ["2026-05-26", "2026-05-27", "2026-05-28"]
                ),
                "open": [100.0, 100.5, 150.0],
                "high": [101.5, 102.0, 205.0],
                "low": [99.5, 100.0, 149.0],
                "close": [100.5, 101.0, 200.0],
                "volume": [1e6, 1e6, 3e5],
            }
        )

    def test_native_1d_drops_today_still_forming_bar(self, monkeypatch):
        """RED before cs54: today's still-forming daily close (200.0) leaks into
        the analyst MarketContext.last_close. GREEN after: the still-forming bar
        is dropped and the analyst sees only the last SETTLED close (101.0).
        """
        from hermes_quant.data import bar_alignment

        # Freeze wall-clock to 5/28 14:00 ET (18:00 UTC, EDT) — mid-session, so
        # the 5/28 daily bar has NOT settled (ET close is 16:00 ET / 20:00 UTC).
        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 5, 28, 18, 0, tzinfo=UTC)

        monkeypatch.setattr(bar_alignment, "datetime", _FrozenDatetime)

        provider = _StubProvider(self._daily_df_ending_today())
        cap = _CapturingAnalyst()

        # as_of mid-session 5/28 — the period-OPEN-label `<= cutoff` filter at
        # advisor.py:591 KEEPS today's bar (ts 5/28 00:00 <= asof). Only the
        # still-forming drop can remove it.
        recommend_multi_horizon(
            "AAPL",
            horizons=["1d"],
            asset_class="equity",
            provider=provider,
            analysts=[cap],
            as_of="2026-05-28T18:00:00Z",
        )

        assert cap.seen, "capturing analyst was never invoked"
        ctx = cap.seen[-1]
        # The still-forming bar must be invisible: last_close is the last SETTLED
        # close (101.0), NOT today's still-forming tick (200.0).
        assert ctx.last_close == 101.0, (
            f"still-forming daily close leaked into MarketContext.last_close: "
            f"got {ctx.last_close}, expected 101.0 (last settled bar)"
        )
        # The bar frame handed to the analyst must contain only settled bars.
        last_ts = pd.Timestamp(ctx.bars["timestamp"].iloc[-1])
        if last_ts.tzinfo is not None:
            last_ts = last_ts.tz_convert("UTC").tz_localize(None)
        assert last_ts == pd.Timestamp("2026-05-27"), (
            f"still-forming 5/28 bar present in analyst bars; last ts={last_ts}"
        )

    def test_native_1d_keeps_bar_after_session_close(self, monkeypatch):
        """Symmetry / non-vacuity fence: AFTER the ET session close the 5/28 bar
        is SETTLED and must be kept (the drop is not unconditional). Guards
        against a fix that always trims the last bar.
        """
        from hermes_quant.data import bar_alignment

        # 5/28 21:00 UTC = 17:00 ET — past the 16:00 ET close, bar is settled.
        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 5, 28, 21, 0, tzinfo=UTC)

        monkeypatch.setattr(bar_alignment, "datetime", _FrozenDatetime)

        provider = _StubProvider(self._daily_df_ending_today())
        cap = _CapturingAnalyst()

        recommend_multi_horizon(
            "AAPL",
            horizons=["1d"],
            asset_class="equity",
            provider=provider,
            analysts=[cap],
            as_of="2026-05-28T21:00:00Z",
        )

        assert cap.seen, "capturing analyst was never invoked"
        ctx = cap.seen[-1]
        # Settled now -> today's bar (200.0) is a real closed bar and is kept.
        assert ctx.last_close == 200.0, (
            f"settled 5/28 bar wrongly dropped: last_close={ctx.last_close}"
        )
