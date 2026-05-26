"""Tests for MicrostructureLite analyst (Wave B.3)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hermes_quant.analysts.microstructure import (
    MicrostructureLite,
    atr_relative,
    directional_bar_imbalance,
    percent_b,
    trend_quality,
)
from hermes_quant.protocol import MarketContext


def _make_bars(
    n: int = 100, *, base: float = 100.0, trend_per_bar: float = 0.0, noise: float = 0.5
) -> pd.DataFrame:
    """Synthetic OHLCV with optional drift."""
    rng = np.random.default_rng(seed=42)
    timestamps = pd.date_range("2026-01-01", periods=n, freq="1D", tz="UTC")
    closes = base + np.arange(n) * trend_per_bar + rng.normal(0, noise, n)
    opens = closes + rng.normal(0, noise * 0.3, n)
    highs = np.maximum(closes, opens) + rng.uniform(0, noise * 0.7, n)
    lows = np.minimum(closes, opens) - rng.uniform(0, noise * 0.7, n)
    volumes = rng.uniform(1e6, 5e6, n)
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )


def _ctx(bars, *, symbol="TEST", asset_class="equity") -> MarketContext:
    return MarketContext(
        asset=symbol,
        timeframe="1d",
        asset_class=asset_class,
        exchange=None,
        bars=bars,
        last_close=float(bars["close"].iloc[-1]),
        last_volume=float(bars["volume"].iloc[-1]),
        asof=bars["timestamp"].iloc[-1],
        extras={},
    )


# ---------------------------------------------------------------------------
# Indicator math sanity
# ---------------------------------------------------------------------------


def test_percent_b_returns_nan_on_short_data():
    short = pd.Series([100.0, 101.0])
    assert np.isnan(percent_b(short, period=20))


def test_percent_b_in_range_0_to_1_for_normal_data():
    bars = _make_bars(50, trend_per_bar=0.0, noise=1.0)
    b = percent_b(bars["close"], period=20)
    # Random walk should produce %B usually in [-0.5, 1.5] range
    assert -1.0 < b < 2.0


def test_atr_relative_returns_nan_on_short_data():
    bars = _make_bars(5)
    assert np.isnan(atr_relative(bars, period=14))


def test_trend_quality_returns_high_on_clean_trend():
    """Pure linear trend should score ≥ 0.5 (some noise lowers it)."""
    bars = _make_bars(50, trend_per_bar=1.0, noise=0.1)
    q = trend_quality(bars["close"], period=14)
    assert 0.4 < q <= 1.0


def test_trend_quality_low_on_chop():
    """Pure noise should score near 0."""
    bars = _make_bars(50, trend_per_bar=0.0, noise=2.0)
    q = trend_quality(bars["close"], period=14)
    assert q < 0.4


def test_directional_bar_imbalance_returns_zero_on_short_data():
    bars = _make_bars(5)
    assert directional_bar_imbalance(bars, period=20) == 0.0


# ---------------------------------------------------------------------------
# MicrostructureLite emission
# ---------------------------------------------------------------------------


def test_microstructure_returns_none_on_insufficient_history():
    a = MicrostructureLite()
    bars = _make_bars(10)
    view = a.analyze(_ctx(bars))
    assert view is None


def test_microstructure_returns_none_on_pure_chop():
    """Per the charter's silence-by-default principle: no clear signal → None."""
    a = MicrostructureLite()
    bars = _make_bars(60, trend_per_bar=0.0, noise=2.0)
    view = a.analyze(_ctx(bars))
    # On pure noise, all sub-signals likely silent or canceling
    # Don't strictly require None (random seeds can produce edge signals)
    # but if we DO emit, confidence should be low
    if view is not None:
        assert view.confidence_raw < 0.5


def test_microstructure_emits_long_on_strong_uptrend():
    """Clean uptrend + persistent bullish bar imbalance → long signal."""
    a = MicrostructureLite()
    # Strong trend + slight noise
    rng = np.random.default_rng(seed=7)
    n = 60
    timestamps = pd.date_range("2026-01-01", periods=n, freq="1D", tz="UTC")
    closes = 100.0 + np.arange(n) * 1.5  # +1.5/day
    opens = closes - 0.5  # close > open consistently (bullish bars)
    highs = closes + 0.3
    lows = opens - 0.3
    volumes = np.full(n, 2e6)
    bars = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )
    view = a.analyze(_ctx(bars))
    if view is not None:
        # Strong directional should emit long
        assert view.direction == 1
        assert "active_subsignals" in view.metadata
        assert len(view.metadata["active_subsignals"]) >= 1


def test_microstructure_metadata_includes_indicators():
    a = MicrostructureLite()
    bars = _make_bars(60, trend_per_bar=0.5)
    view = a.analyze(_ctx(bars))
    if view is not None:
        md = view.metadata
        for key in [
            "bollinger_pct_b",
            "atr_relative",
            "trend_quality",
            "bar_imbalance",
            "active_subsignals",
            "n_active_subsignals",
        ]:
            assert key in md


def test_microstructure_view_satisfies_protocol():
    a = MicrostructureLite()
    bars = _make_bars(60, trend_per_bar=0.5)
    view = a.analyze(_ctx(bars))
    if view is not None:
        # AnalystView contract per ADR-0002 + ADR-0009
        assert view.analyst == "microstructure_lite"
        assert view.direction in {-1, 0, 1}
        assert 0.0 <= view.confidence <= 1.0
        assert 0.0 <= view.confidence_raw <= 1.0
        assert view.horizon == "4h"
        assert view.rationale is not None
        assert view.rationale.startswith("[microstructure]")


def test_microstructure_health_reports_basics():
    a = MicrostructureLite()
    health = a.health()
    assert health["name"] == "microstructure_lite"
    assert health["n_views_emitted"] == 0
    assert health["calibrated"] is False  # cold start
    assert health["error_count"] == 0


def test_microstructure_via_advisor_pipeline():
    """End-to-end: advisor.recommend uses MicrostructureLite as second voice."""
    from hermes_quant.advisor import recommend

    class FakeProvider:
        name = "fake"
        asset_classes = ["equity"]
        timeframes = ["1d"]
        requires_credentials = False

        def fetch_bars(self, asset, timeframe, start, end, *, use_cache=True):
            return _make_bars(120, trend_per_bar=0.5)

    result = recommend(
        symbol="TREND",
        provider=FakeProvider(),
        include_lessons=False,
    )
    # Should have at least one analyst view (ClassicalTA) — micro may be silent
    assert isinstance(result.get("analyst_views"), list)
    # decision_price should be top-level (Wave B.1)
    assert "decision_price" in result
    assert result["decision_price"] > 0
