"""Unit tests for hermes_quant.analysts.classical_ta."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hermes_quant.analysts.classical_ta import (
    ClassicalTAAnalyst,
    bollinger_bands,
    macd_histogram,
    rsi,
    sma_cross_signal,
)
from hermes_quant.protocol import (
    Analyst,
    MarketContext,
)


def _make_context(close_series: list[float], asof_offset: int = 0) -> MarketContext:
    """Build a synthetic MarketContext with a given close series."""
    n = len(close_series)
    ts = pd.date_range("2026-05-01T00:00:00", periods=n, freq="1h")
    bars = pd.DataFrame(
        {
            "timestamp": ts,
            "open": close_series,
            "high": [c * 1.01 for c in close_series],
            "low": [c * 0.99 for c in close_series],
            "close": close_series,
            "volume": [1000.0] * n,
        }
    )
    return MarketContext(
        asset="BTC/USDT",
        timeframe="1h",
        asset_class="crypto",
        exchange="binance",
        bars=bars,
        last_close=close_series[-1],
        last_volume=1000.0,
        asof=ts[-1] + pd.Timedelta(hours=asof_offset),
    )


# ---------------------------------------------------------------------------
# Pure indicator math
# ---------------------------------------------------------------------------


class TestIndicators:
    def test_rsi_at_neutral_around_50(self):
        # Random walk → RSI ~ 50
        rng = np.random.default_rng(1)
        steps = rng.normal(0, 1, 100).cumsum() + 100
        r = rsi(pd.Series(steps), period=14)
        assert 30 < r < 70

    def test_rsi_uptrend_above_70(self):
        prices = pd.Series([100 + i * 0.5 for i in range(50)])
        r = rsi(prices, period=14)
        assert r > 70

    def test_rsi_downtrend_below_30(self):
        prices = pd.Series([100 - i * 0.5 for i in range(50)])
        r = rsi(prices, period=14)
        assert r < 30

    def test_rsi_insufficient_data_nan(self):
        r = rsi(pd.Series([100, 101]), period=14)
        assert np.isnan(r)

    def test_macd_uptrend_positive(self):
        prices = pd.Series([100 + i * 0.5 for i in range(50)])
        h = macd_histogram(prices)
        assert h > 0

    def test_macd_downtrend_negative(self):
        prices = pd.Series([100 - i * 0.5 for i in range(50)])
        h = macd_histogram(prices)
        assert h < 0

    def test_bollinger_returns_three_floats(self):
        prices = pd.Series([100 + i for i in range(30)])
        u, m, l = bollinger_bands(prices, period=20)
        assert l < m < u

    def test_sma_cross_uptrend_long(self):
        # Long uptrend → SMA20 > SMA50
        prices = pd.Series([100 + i * 0.5 for i in range(60)])
        d, mag = sma_cross_signal(prices)
        assert d == 1
        assert mag > 0

    def test_sma_cross_downtrend_short(self):
        prices = pd.Series([100 - i * 0.5 for i in range(60)])
        d, mag = sma_cross_signal(prices)
        assert d == -1
        assert mag > 0


# ---------------------------------------------------------------------------
# Analyst protocol & basic emit
# ---------------------------------------------------------------------------


class TestAnalystContract:
    def test_satisfies_analyst_protocol(self):
        a = ClassicalTAAnalyst()
        assert isinstance(a, Analyst)

    def test_metadata(self):
        a = ClassicalTAAnalyst()
        assert a.name == "classical-ta"
        assert "1h" in a.timeframes
        assert "crypto" in a.asset_classes
        assert a.enabled is True


class TestAnalyze:
    def test_insufficient_history_returns_none(self):
        a = ClassicalTAAnalyst(min_history_bars=60)
        ctx = _make_context([100.0] * 30)  # only 30 bars, need 60
        assert a.analyze(ctx) is None

    def test_uptrend_emits_long_view(self):
        a = ClassicalTAAnalyst()
        # Strong uptrend
        prices = [100 + i * 0.3 for i in range(80)]
        ctx = _make_context(prices)
        view = a.analyze(ctx)
        assert view is not None
        assert view.analyst == "classical-ta"
        assert view.direction == 1
        assert view.magnitude > 0
        # Confidence is calibrated. Cold-start (Beta(2,5)) amplifies low raws
        # toward the prior mean ~0.286, so calibrated CAN exceed raw — the
        # invariant we keep is that calibrated stays inside [0.25, 0.375] with
        # an unfitted calibrator.
        assert 0.25 <= view.confidence <= 0.375
        assert view.confidence > 0
        assert view.confidence_raw > 0

    def test_downtrend_emits_short_view(self):
        a = ClassicalTAAnalyst()
        prices = [100 - i * 0.3 for i in range(80)]
        ctx = _make_context(prices)
        view = a.analyze(ctx)
        assert view is not None
        assert view.direction == -1

    def test_flat_input_returns_none(self):
        a = ClassicalTAAnalyst()
        # Constant price → all sub-signals flat
        prices = [100.0] * 80
        ctx = _make_context(prices)
        view = a.analyze(ctx)
        # All sub-signals flat → composite returns None
        assert view is None

    def test_view_has_required_fields(self):
        a = ClassicalTAAnalyst()
        prices = [100 + i * 0.3 for i in range(80)]
        ctx = _make_context(prices)
        view = a.analyze(ctx)
        assert view is not None
        # All required AnalystView fields populated
        assert view.analyst
        assert view.direction in (-1, 0, 1)
        assert isinstance(view.magnitude, float)
        assert 0.0 <= view.confidence <= 1.0
        assert 0.0 <= view.confidence_raw <= 1.0
        assert view.horizon
        assert view.metadata is not None
        assert "sub_signals" in view.metadata

    def test_calibration_beta_prior_applied(self):
        """ColdStartCalibrator applies Beta(2,5) posterior to raw confidence.

        ADR-0009 §P0-2 amendment 2026-05-26 (see
        docs/diagnostics/2026-05-26-no-conviction-bimodal-pattern.md):
        cold-start is now (raw + alpha) / (1 + alpha + beta), alpha=2 beta=5.
        """
        a = ClassicalTAAnalyst()
        prices = [100 + i * 0.3 for i in range(80)]
        ctx = _make_context(prices)
        view = a.analyze(ctx)
        assert view is not None
        # cold-start: (raw + 2.0) / (1 + 2.0 + 5.0) = (raw + 2.0) / 8.0
        expected = (view.confidence_raw + 2.0) / 8.0
        assert view.confidence == pytest.approx(expected, abs=1e-6)
        # Sanity: no longer punished to zero on typical agreement.
        assert view.confidence > 0.0


class TestHealth:
    def test_health_initial(self):
        a = ClassicalTAAnalyst()
        h = a.health()
        assert h["name"] == "classical-ta"
        assert h["n_views_emitted"] == 0
        assert h["last_view_at"] is None
        assert h["error_count"] == 0
        assert "calibrator_status" in h

    def test_health_after_emit(self):
        a = ClassicalTAAnalyst()
        prices = [100 + i * 0.3 for i in range(80)]
        ctx = _make_context(prices)
        a.analyze(ctx)
        h = a.health()
        assert h["n_views_emitted"] == 1
        assert h["last_view_at"] is not None


class TestErrorHandling:
    def test_malformed_bars_does_not_crash(self):
        a = ClassicalTAAnalyst()
        # Build a context with NaN-filled bars
        ts = pd.date_range("2026-05-01", periods=80, freq="1h")
        bars = pd.DataFrame(
            {
                "timestamp": ts,
                "open": [float("nan")] * 80,
                "high": [float("nan")] * 80,
                "low": [float("nan")] * 80,
                "close": [float("nan")] * 80,
                "volume": [1000.0] * 80,
            }
        )
        ctx = MarketContext(
            asset="BTC/USDT",
            timeframe="1h",
            asset_class="crypto",
            exchange="binance",
            bars=bars,
            last_close=100.0,
            last_volume=1000.0,
            asof=ts[-1],
        )
        # Should not crash; returns None or sane handling
        view = a.analyze(ctx)
        # Most likely None due to NaN handling
        assert view is None or view.confidence_raw == 0
