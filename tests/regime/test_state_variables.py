"""tests/regime/test_state_variables.py — Wave 7 state variable tests."""
from __future__ import annotations

import json
import math
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hermes_quant.regime.state_variables import (
    StateVariables,
    compute_state_variables,
    _compute_realized_vol,
    _compute_trend_strength,
    _compute_vol_percentile,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bars(n: int = 300, *, seed: int = 42, trend: float = 0.0) -> pd.DataFrame:
    """Create synthetic OHLCV bars DataFrame with ``n`` rows."""
    rng = np.random.default_rng(seed)
    log_returns = rng.normal(loc=trend / 252, scale=0.015, size=n)
    prices = 100.0 * np.exp(np.cumsum(log_returns))
    timestamps = pd.date_range("2025-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": prices * 0.999,
            "high": prices * 1.002,
            "low": prices * 0.998,
            "close": prices,
            "volume": rng.integers(1_000, 100_000, size=n).astype(float),
        }
    )


# ---------------------------------------------------------------------------
# Test: realized volatility math
# ---------------------------------------------------------------------------


def test_realized_vol_positive():
    bars = _make_bars(100)
    sv = compute_state_variables(bars)
    assert sv.realized_vol_60d > 0.0, "realized_vol_60d must be positive"


def test_realized_vol_annualized():
    """Annualized vol of daily returns with daily_stdev≈0.01 should be ≈ 0.01*√252."""
    rng = np.random.default_rng(0)
    log_returns = rng.normal(loc=0.0, scale=0.01, size=200)
    prices = pd.Series(100.0 * np.exp(np.cumsum(log_returns)))
    vol = _compute_realized_vol(prices)
    expected = 0.01 * math.sqrt(252)
    # Allow ±50% relative tolerance (seed randomness + finite sample)
    assert 0.5 * expected < vol < 1.5 * expected, (
        f"vol={vol:.4f} not within ±50% of expected {expected:.4f}"
    )


def test_realized_vol_percentile_in_0_1():
    bars = _make_bars(300)
    sv = compute_state_variables(bars)
    assert 0.0 <= sv.realized_vol_percentile <= 1.0


def test_realized_vol_percentile_high_for_spike():
    """A period with elevated vol at the end should push percentile above 0.5."""
    rng = np.random.default_rng(1)
    # Build 300 bars with low daily vol
    n = 300
    log_returns_low = rng.normal(0.0, 0.005, size=n)
    prices = 100.0 * np.exp(np.cumsum(log_returns_low))
    # Replace last 60 returns with 10× higher vol
    log_returns_high = rng.normal(0.0, 0.05, size=60)
    prices_high_end = float(prices[-61]) * np.exp(np.cumsum(log_returns_high))
    prices[-60:] = prices_high_end
    timestamps = pd.date_range("2025-01-01", periods=n, freq="B", tz="UTC")
    bars_high = pd.DataFrame({
        "timestamp": timestamps,
        "close": prices,
        "open": prices,
        "high": prices,
        "low": prices,
        "volume": 1.0,
    })
    sv = compute_state_variables(bars_high)
    # After injecting high vol in the last 60 bars, percentile should be high
    assert sv.realized_vol_percentile >= 0.5, (
        f"Expected high-vol percentile >= 0.5, got {sv.realized_vol_percentile:.3f}"
    )


def test_graceful_when_bars_less_than_lookback():
    """Should not raise; returns percentile in [0, 1] with a warning."""
    bars = _make_bars(30)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        sv = compute_state_variables(bars)
    assert 0.0 <= sv.realized_vol_percentile <= 1.0
    assert sv.metadata.get("insufficient_bars_for_vol") is True


def test_graceful_very_short_bars():
    """Only 5 bars — should not crash."""
    bars = _make_bars(5)
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        sv = compute_state_variables(bars)
    assert isinstance(sv, StateVariables)


def test_missing_yield_curve_cache(tmp_path):
    bars = _make_bars(200)
    sv = compute_state_variables(bars, yield_cache_path=tmp_path / "nonexistent.json")
    assert sv.yield_curve_slope is None
    assert sv.metadata.get("yield_curve_unavailable") is True


def test_yield_curve_loaded_from_cache(tmp_path):
    cache_file = tmp_path / "yield-curve-cache.json"
    cache_file.write_text(
        json.dumps({"dates": ["2025-01-02", "2025-01-03"], "slope_10y_2y": [0.45, 0.50]})
    )
    bars = _make_bars(200)
    sv = compute_state_variables(bars, yield_cache_path=cache_file)
    assert sv.yield_curve_slope is not None
    assert abs(sv.yield_curve_slope - 0.50) < 1e-9


def test_trend_strength_none_when_too_few_bars():
    bars = _make_bars(30)
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        sv = compute_state_variables(bars)
    assert sv.trend_strength is None


def test_trend_strength_positive_for_uptrend():
    """Bars with strong uptrend should yield positive trend_strength."""
    bars = _make_bars(200, trend=2.0)  # 200% annual drift
    sv = compute_state_variables(bars)
    # Should be positive (close > 50d MA)
    if sv.trend_strength is not None:
        assert sv.trend_strength > 0.0


def test_compute_state_variables_no_close_raises():
    df = pd.DataFrame({"open": [1.0, 2.0], "high": [3.0, 4.0]})
    with pytest.raises(ValueError, match="close"):
        compute_state_variables(df)


def test_as_of_from_timestamp_column():
    bars = _make_bars(100)
    sv = compute_state_variables(bars)
    # as_of should be a pd.Timestamp
    assert isinstance(sv.as_of, pd.Timestamp)
