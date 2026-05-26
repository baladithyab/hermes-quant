"""Tests for cross-horizon BMA weighting (ADR-0036)."""

from __future__ import annotations

import pandas as pd

from hermes_quant.aggregators.bma import BMAAggregator, BMAConfig
from hermes_quant.protocol import AnalystView, MarketContext


def _ctx() -> MarketContext:
    ts = pd.date_range("2026-05-13", periods=2, freq="1h")
    bars = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [1000.0, 1000.0],
        }
    )
    return MarketContext(
        asset="AAPL",
        timeframe="1d",
        asset_class="equity",
        exchange=None,
        bars=bars,
        last_close=101.5,
        last_volume=1000.0,
        asof=ts[-1],
    )


def _v(name: str, direction: int, horizon: str, conf: float = 0.7) -> AnalystView:
    return AnalystView(
        analyst=name,
        direction=direction,
        magnitude=0.01,
        confidence=conf,
        confidence_raw=conf,
        horizon=horizon,
    )


class TestHorizonWeights:
    def test_default_horizon_weights_present(self):
        cfg = BMAConfig()
        assert cfg.horizon_weights["1d"] == 1.00
        assert cfg.horizon_weights["1w"] == 1.20
        assert cfg.horizon_weights["1M"] == 0.80
        assert cfg.horizon_weights["1Q"] == 0.60

    def test_aggregator_accepts_config(self):
        agg = BMAAggregator(config=BMAConfig(horizon_weights={"1d": 2.0, "1w": 0.5}))
        assert agg.horizon_weights["1d"] == 2.0
        assert agg.horizon_weights["1w"] == 0.5

    def test_unknown_horizon_falls_back_to_one(self):
        agg = BMAAggregator()
        # An "exotic" horizon not in the table defaults to weight 1.0
        # so behavior is monotone & non-suppressing.
        assert agg._horizon_weight("4h") == 1.0

    def test_higher_horizon_weight_dominates_direction(self):
        """1d bullish view fights 1w bearish view; 1w should win at default weights."""
        agg = BMAAggregator()
        ctx = _ctx()
        # Equal confidence/magnitude — only horizon weight differentiates them
        views = [
            _v("a1", direction=1, horizon="1d", conf=0.6),  # 1d weight 1.00
            _v("a2", direction=-1, horizon="1w", conf=0.6),  # 1w weight 1.20
        ]
        sig = agg.aggregate(views, ctx)
        # 1w bear (1.20 weight) > 1d bull (1.00 weight) → composite bearish
        assert sig.direction == -1, (
            f"expected bearish (1w weight beats 1d), got direction={sig.direction}"
        )

    def test_horizon_weights_inverted_inverts_direction(self):
        """Same view set, but horizon_weights inverted should flip direction."""
        agg = BMAAggregator(
            config=BMAConfig(horizon_weights={"1d": 1.20, "1w": 1.00}),
        )
        ctx = _ctx()
        views = [
            _v("a1", direction=1, horizon="1d", conf=0.6),
            _v("a2", direction=-1, horizon="1w", conf=0.6),
        ]
        sig = agg.aggregate(views, ctx)
        # Flipped: 1d (1.20) beats 1w (1.00) → composite bullish
        assert sig.direction == 1, (
            f"expected bullish under inverted weights, got direction={sig.direction}"
        )

    def test_metadata_records_horizons_present(self):
        agg = BMAAggregator()
        ctx = _ctx()
        views = [
            _v("a1", direction=1, horizon="1d"),
            _v("a2", direction=1, horizon="1w"),
        ]
        sig = agg.aggregate(views, ctx)
        assert "horizons_present" in sig.metadata
        assert sorted(sig.metadata["horizons_present"]) == ["1d", "1w"]
