"""tests/regime/test_bma_integration.py — Wave 7 BMA + regime integration tests."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hermes_quant.aggregators.bma import BMAAggregator
from hermes_quant.protocol import AnalystView, MarketContext
from hermes_quant.regime.detector import RegimeDetector, RegimeState
from hermes_quant.regime.state_variables import StateVariables


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(n_bars: int = 200, *, seed: int = 42) -> MarketContext:
    rng = np.random.default_rng(seed)
    log_returns = rng.normal(0.0, 0.01, size=n_bars)
    prices = 100.0 * np.exp(np.cumsum(log_returns))
    timestamps = pd.date_range("2025-01-01", periods=n_bars, freq="B", tz="UTC")
    bars = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": prices * 0.999,
            "high": prices * 1.002,
            "low": prices * 0.998,
            "close": prices,
            "volume": 1_000_000.0,
        }
    )
    return MarketContext(
        asset="TEST/USDT",
        timeframe="1d",
        asset_class="crypto",
        exchange="test",
        bars=bars,
        last_close=float(prices[-1]),
        last_volume=1_000_000.0,
        asof=timestamps[-1],
    )


def _make_views(analysts: list[str], direction: int = 1) -> list[AnalystView]:
    return [
        AnalystView(
            analyst=name,
            direction=direction,
            magnitude=0.02,
            confidence=0.60,
            confidence_raw=0.60,
            horizon="1d",
        )
        for name in analysts
    ]


class _AlwaysBearDetector(RegimeDetector):
    """Stub detector that always returns BEAR (for integration tests)."""

    def classify(self, state_vars: StateVariables):
        return RegimeState.BEAR, "stub_always_bear"


class _AlwaysUnknownDetector(RegimeDetector):
    """Stub detector that always returns UNKNOWN."""

    def classify(self, state_vars: StateVariables):
        return RegimeState.UNKNOWN, "stub_always_unknown"


# ---------------------------------------------------------------------------
# Bit-identical behavior when regime_detector=None
# ---------------------------------------------------------------------------


def test_no_regime_detector_metadata_keys_are_none():
    """regime_state and regime_weight_multipliers must be None when detector is not set."""
    bma = BMAAggregator(require_ensemble=False)
    ctx = _make_context()
    views = _make_views(["semantic", "sentiment"])
    signal = bma.aggregate(views, ctx)
    assert signal.metadata is not None
    assert signal.metadata.get("regime_state") is None
    assert signal.metadata.get("regime_weight_multipliers") is None


def test_no_regime_detector_output_identical():
    """Calling aggregate with regime_detector=None must produce bit-identical
    weights as the pre-Wave-7 aggregator (no regime field should differ)."""
    bma_base = BMAAggregator(require_ensemble=False)
    bma_regime = BMAAggregator(require_ensemble=False, regime_detector=None)
    ctx = _make_context()
    views = _make_views(["semantic", "sentiment", "classical_ta"])
    sig_base = bma_base.aggregate(views, ctx)
    sig_regime = bma_regime.aggregate(views, ctx)
    assert sig_base.direction == sig_regime.direction
    assert abs(sig_base.confidence - sig_regime.confidence) < 1e-9
    assert abs(sig_base.magnitude - sig_regime.magnitude) < 1e-9


# ---------------------------------------------------------------------------
# BEAR regime suppresses sentiment
# ---------------------------------------------------------------------------


def test_bear_detector_suppresses_sentiment_weight():
    """In BEAR regime, sentiment multiplier=0.6, so sentiment should get a lower
    effective weight compared to the no-regime baseline."""
    bma = BMAAggregator(require_ensemble=False, regime_detector=_AlwaysBearDetector())
    ctx = _make_context()
    views = _make_views(["semantic", "sentiment", "classical_ta"])
    signal = bma.aggregate(views, ctx)
    assert signal.metadata is not None
    assert signal.metadata.get("regime_state") == "bear"
    multipliers = signal.metadata.get("regime_weight_multipliers")
    assert multipliers is not None
    assert "sentiment" in multipliers
    assert multipliers["sentiment"] < 1.0, "BEAR should suppress sentiment multiplier"


def test_bear_detector_regime_recorded_in_metadata():
    bma = BMAAggregator(require_ensemble=False, regime_detector=_AlwaysBearDetector())
    ctx = _make_context()
    views = _make_views(["semantic", "sentiment"])
    signal = bma.aggregate(views, ctx)
    assert signal.metadata is not None
    assert signal.metadata["regime_state"] == "bear"
    assert signal.metadata["regime_weight_multipliers"] is not None


# ---------------------------------------------------------------------------
# UNKNOWN regime = no adjustment
# ---------------------------------------------------------------------------


def test_unknown_regime_multipliers_are_all_one():
    bma = BMAAggregator(require_ensemble=False, regime_detector=_AlwaysUnknownDetector())
    ctx = _make_context()
    views = _make_views(["semantic", "sentiment", "classical_ta", "fundamentals", "kronos"])
    signal = bma.aggregate(views, ctx)
    assert signal.metadata is not None
    multipliers = signal.metadata.get("regime_weight_multipliers")
    assert multipliers is not None
    for analyst, m in multipliers.items():
        assert abs(m - 1.0) < 1e-9, f"UNKNOWN multiplier for {analyst} should be 1.0, got {m}"


# ---------------------------------------------------------------------------
# Flat signal path also records regime
# ---------------------------------------------------------------------------


def test_flat_signal_does_not_crash_with_regime_detector():
    """Empty views → flat signal. Regime detection is skipped (no views to weight)."""
    bma = BMAAggregator(require_ensemble=False, regime_detector=_AlwaysBearDetector())
    ctx = _make_context()
    signal = bma.aggregate([], ctx)
    assert signal.direction == 0


# ---------------------------------------------------------------------------
# Regime metadata keys present even with IC dedup gate active
# ---------------------------------------------------------------------------


def test_regime_and_ic_dedup_coexist():
    """BMA with both ic_dedup_gate=None and regime_detector set should not crash."""
    bma = BMAAggregator(require_ensemble=False, regime_detector=_AlwaysBearDetector())
    ctx = _make_context()
    views = _make_views(["semantic", "sentiment", "kronos"])
    signal = bma.aggregate(views, ctx)
    # Both audit keys should be present
    assert "ic_dedup_excluded_analysts" in signal.metadata
    assert "regime_state" in signal.metadata
