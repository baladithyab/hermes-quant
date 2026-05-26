"""Unit test for the Bayesian beta-prior warm-start fallback in ClassicalTAAnalyst.

Regression guard for the silent-symbol bug: before this fix, the calibrator
fallback used `max(0.0, raw - 0.20)` which drove every typical 2/4-agreement
signal (raw≈0.20) to exactly 0.0 confidence. The replacement is a Beta(2, 5)
weak prior:

    posterior = (raw * 1.0 + alpha) / (1.0 + alpha + beta)   # alpha=2, beta=5

centered at 2/(2+5) ≈ 0.286, which lets agreement signals pass through
without claiming the precision of a calibrated estimate.

This test exercises the fallback branch by injecting an unfitted
IsotonicCalibrator (which raises CalibratorNotReady on calibrate()), then
forcing the analyst's sub-signals to a known 2/4-agreement state via
monkeypatching so the assertion is deterministic and independent of the
indicator math.
"""

from __future__ import annotations

import pandas as pd
import pytest

from hermes_quant.analysts.classical_ta import ClassicalTAAnalyst, _SubSignal
from hermes_quant.calibrators import IsotonicCalibrator
from hermes_quant.protocol import MarketContext


def _make_context(close_series: list[float]) -> MarketContext:
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
        asof=ts[-1],
    )


def _force_two_of_four_agreement(analyst: ClassicalTAAnalyst) -> None:
    """Override sub-signals so exactly 2/4 vote direction=1 with sub-conf 0.4.

    rsi → long (sub_conf=0.4)
    macd → long (sub_conf=0.4)
    bollinger → flat
    ma_cross → flat

    Composite raw = agreement * mean_sub_conf = (2/4) * 0.4 = 0.20.
    """
    analyst._rsi_signal = lambda close: _SubSignal(1, 0.01, 0.4, "rsi")  # type: ignore[method-assign]
    analyst._macd_signal = lambda close: _SubSignal(1, 0.01, 0.4, "macd")  # type: ignore[method-assign]
    analyst._bollinger_signal = lambda close: _SubSignal(0, 0.0, 0.0, "bollinger")  # type: ignore[method-assign]
    analyst._ma_cross_signal = lambda close: _SubSignal(0, 0.0, 0.0, "ma_cross")  # type: ignore[method-assign]


class TestWarmStartFallback:
    def test_two_of_four_agreement_is_not_punished_to_zero(self):
        """A 2/4-agreement signal (raw≈0.20) must emit confidence > 0.05.

        Before the fix this case yielded confidence = max(0, 0.20 - 0.20) = 0.0
        and the symbol went silent.
        """
        analyst = ClassicalTAAnalyst()
        # Inject an unfitted calibrator → calibrate() raises CalibratorNotReady,
        # forcing the warm-start fallback path.
        analyst.calibrator = IsotonicCalibrator()
        assert not analyst.calibrator.is_calibrated
        _force_two_of_four_agreement(analyst)

        prices = [100.0 + i * 0.1 for i in range(80)]
        ctx = _make_context(prices)
        view = analyst.analyze(ctx)

        assert view is not None
        assert view.direction == 1
        # Raw should be ~0.20 from forced 2/4 agreement at sub_conf=0.4.
        assert view.confidence_raw == pytest.approx(0.20, abs=1e-6)
        # Lower bound: warm-start must NOT punish to zero.
        assert view.confidence > 0.05
        # Upper bound: warm-start is intentionally less confident than a
        # fully-calibrated 2/4 signal would be.
        assert view.confidence < 0.50

    def test_warm_start_matches_beta_prior_formula(self):
        """The fallback must emit (raw + alpha) / (1 + alpha + beta) exactly."""
        analyst = ClassicalTAAnalyst()
        analyst.calibrator = IsotonicCalibrator()
        _force_two_of_four_agreement(analyst)

        prices = [100.0 + i * 0.1 for i in range(80)]
        ctx = _make_context(prices)
        view = analyst.analyze(ctx)

        assert view is not None
        alpha, beta = 2.0, 5.0
        expected = (view.confidence_raw * 1.0 + alpha) / (1.0 + alpha + beta)
        assert view.confidence == pytest.approx(expected, abs=1e-6)
        # Sanity: with raw=0.20 the posterior is 2.20/8.0 = 0.275.
        assert view.confidence == pytest.approx(0.275, abs=1e-6)
