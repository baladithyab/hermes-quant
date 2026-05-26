"""Tests for multi-timeframe agreement bonus / penalty (ADR-0036)."""

from __future__ import annotations

import pandas as pd
import pytest

from hermes_quant.aggregators.bma import BMAAggregator
from hermes_quant.calibrators import ColdStartCalibrator
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


def _agg() -> BMAAggregator:
    # Force ColdStartCalibrator so the test is independent of any disk-fitted
    # calibrator and so confidence has a deterministic non-zero baseline
    # (the cold-start prior is Beta(2,5) = 2/(2+5) ≈ 0.286 for a maximal raw 1.0).
    agg = BMAAggregator()
    agg.calibrator = ColdStartCalibrator()
    return agg


class TestMultiHorizonAgreement:
    def test_all_agree_three_horizons_applies_bonus(self):
        agg_bonus = _agg()
        agg_baseline = _agg()
        ctx = _ctx()

        # All-agree multi-horizon set
        views_multi = [
            _v("a1", direction=1, horizon="1d"),
            _v("a2", direction=1, horizon="1w"),
            _v("a3", direction=1, horizon="1M"),
        ]
        # Same total information but single-horizon (no MTF effect)
        views_single = [
            _v("a1", direction=1, horizon="1d"),
            _v("a2", direction=1, horizon="1d"),
            _v("a3", direction=1, horizon="1d"),
        ]
        sig_multi = agg_bonus.aggregate(views_multi, ctx)
        sig_single = agg_baseline.aggregate(views_single, ctx)

        # All three views agree on direction in both cases → bullish
        assert sig_multi.direction == 1
        assert sig_single.direction == 1

        # The multi-horizon all-agree confidence must be strictly greater
        # than the single-horizon equivalent (the +10% bonus path).
        assert sig_multi.confidence > sig_single.confidence, (
            f"multi-horizon all-agree should boost confidence "
            f"(multi={sig_multi.confidence}, single={sig_single.confidence})"
        )
        assert sig_multi.metadata["horizon_agreement"] == "all_agree"
        assert sorted(sig_multi.metadata["horizons_present"]) == ["1M", "1d", "1w"]

    def test_mixed_directions_applies_penalty(self):
        agg = _agg()
        ctx = _ctx()
        views = [
            _v("a1", direction=1, horizon="1d"),
            _v("a2", direction=1, horizon="1w"),
            _v("a3", direction=-1, horizon="1M"),  # disagrees
        ]
        sig = agg.aggregate(views, ctx)
        assert sig.metadata["horizon_agreement"] == "mixed"
        # Confidence should be penalized vs the unmodified vote share.
        # Vote share before agreement adjustment must be > final confidence.
        # We can't directly read the pre-adjustment confidence; instead check
        # that the metadata flag is set and confidence is > 0 but capped.
        assert 0.0 <= sig.confidence <= 1.0
        # Compare to the same view-set with all-agree → mixed must be strictly less
        agg2 = _agg()
        views_all_agree = [
            _v("a1", direction=1, horizon="1d"),
            _v("a2", direction=1, horizon="1w"),
            _v("a3", direction=1, horizon="1M"),
        ]
        sig2 = agg2.aggregate(views_all_agree, ctx)
        assert sig.confidence < sig2.confidence, (
            f"mixed-horizon must be < all-agree (mixed={sig.confidence}, "
            f"all-agree={sig2.confidence})"
        )

    def test_single_horizon_skips_agreement_adjustment(self):
        agg = _agg()
        ctx = _ctx()
        views = [
            _v("a1", direction=1, horizon="1d"),
            _v("a2", direction=1, horizon="1d"),
        ]
        sig = agg.aggregate(views, ctx)
        assert sig.metadata["horizon_agreement"] == "single_horizon"
        # No adjustment applied — confidence comes from the standard vote share path

    def test_bonus_capped_at_one(self):
        agg = _agg()
        # Stuff the calibrator with a very high baseline so 1.10x would exceed 1.0
        # — the cap must hold. ColdStartCalibrator returns ≤ 0.286, so we patch.
        class _MaxConfCalibrator:
            def calibrate(self, x: float) -> float:
                return 0.95
            def status(self) -> dict:
                return {"name": "max_test"}

        agg.calibrator = _MaxConfCalibrator()
        ctx = _ctx()
        views = [
            _v("a1", direction=1, horizon="1d"),
            _v("a2", direction=1, horizon="1w"),
            _v("a3", direction=1, horizon="1M"),
        ]
        sig = agg.aggregate(views, ctx)
        assert sig.confidence <= 1.0
        assert sig.metadata["horizon_agreement"] == "all_agree"
