"""Unit tests for hermes_quant.training.bootstrap_calibrator.

These tests use a synthetic OHLCV generator with a deliberate forward-bias
(uptrend with mean-reversion noise) so the analysts will emit views and the
realized N-bar-forward closes resolve to a non-trivial direction_correct
distribution. No live Alpaca calls.

ADR refs: ADR-0009 §P0-2 (calibration), AGENTS.md §Testing discipline
(deterministic, no network).
"""

from __future__ import annotations

import pickle
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from hermes_quant.aggregators.bma import BMAAggregator
from hermes_quant.calibrators import ColdStartCalibrator, IsotonicCalibrator
from hermes_quant.training.bootstrap_calibrator import (
    DEFAULT_CALIBRATOR_PATH,
    bootstrap_calibrator,
)

# --------------------------------------------------------------------------- #
# Synthetic Alpaca mock                                                       #
# --------------------------------------------------------------------------- #


@dataclass
class _MockBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class _MockBarSet:
    def __init__(self, data: dict[str, list[_MockBar]]):
        self.data = data

    def __getitem__(self, sym):
        return self.data[sym]


class _MockDataClient:
    """Drop-in replacement for StockHistoricalDataClient.

    Generates synthetic daily bars with controllable trend so the analysts
    have something to chew on. Ignores the request's start/end (fine for
    tests — the bootstrap caller passes the symbols list, the mock returns
    bars for whichever symbol it sees).
    """

    def __init__(self, n_bars: int = 400, trend: float = 0.0008, seed: int = 1729):
        self.n_bars = n_bars
        self.trend = trend
        self.seed = seed

    def get_stock_bars(self, req):
        symbols = req.symbol_or_symbols
        if isinstance(symbols, str):
            symbols = [symbols]
        rng = np.random.default_rng(self.seed)
        end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        out: dict[str, list[_MockBar]] = {}
        for sym in symbols:
            bars: list[_MockBar] = []
            close = 100.0
            for i in range(self.n_bars):
                # Drift + noise. Trend makes long views correct on average,
                # giving the IsotonicCalibrator an actual signal to fit.
                ret = self.trend + rng.normal(0, 0.012)
                close = max(0.5, close * (1.0 + ret))
                # Day-bar OHLC: tight envelope around close
                o = close * (1.0 + rng.normal(0, 0.002))
                h = max(o, close) * (1.0 + abs(rng.normal(0, 0.003)))
                low = min(o, close) * (1.0 - abs(rng.normal(0, 0.003)))
                vol = float(rng.integers(1_000_000, 10_000_000))
                bars.append(
                    _MockBar(
                        timestamp=end - timedelta(days=self.n_bars - i),
                        open=float(o),
                        high=float(h),
                        low=float(low),
                        close=float(close),
                        volume=vol,
                    )
                )
            out[sym] = bars
        return _MockBarSet(out)


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #


class TestBootstrap:
    def test_yields_positive_samples_under_synthetic_trend(self, tmp_path: Path):
        """A persistent uptrend should produce ≥1 (raw, correct) pair."""
        out = tmp_path / "isotonic.pkl"
        # 5 symbols × 400 bars × walk from t=200 → ~200 t-steps × 2 analysts
        # gives plenty of upside on sample count, but min_samples=10 keeps
        # the test tolerant of lighter days.
        result = bootstrap_calibrator(
            symbols=["AAA", "BBB", "CCC", "DDD", "EEE"],
            days=365,
            timeframe="1d",
            horizon_bars=4,
            output_path=out,
            min_samples=10,
            data_client=_MockDataClient(n_bars=400, trend=0.001),
        )
        assert result["n_samples"] > 0, f"expected >0 samples, got {result}"
        assert result["symbols_processed"] >= 1
        assert result["fitted"] is True
        assert out.exists()

    def test_persisted_pickle_is_valid_isotonic(self, tmp_path: Path):
        """The pickled object must unpickle to an is_calibrated IsotonicCalibrator."""
        out = tmp_path / "isotonic.pkl"
        result = bootstrap_calibrator(
            symbols=["XYZ", "PDQ", "FOO", "BAR", "BAZ"],
            days=365,
            timeframe="1d",
            horizon_bars=4,
            output_path=out,
            min_samples=10,
            data_client=_MockDataClient(n_bars=400, trend=0.0008),
        )
        assert result["fitted"]
        with open(out, "rb") as f:
            loaded = pickle.load(f)
        assert isinstance(loaded, IsotonicCalibrator)
        assert loaded.is_calibrated
        assert loaded.n_samples > 0
        # calibrated values are bounded in [0, 1] (sklearn IsotonicRegression
        # with y_min/y_max + clip)
        for raw in (0.0, 0.25, 0.5, 0.75, 1.0):
            v = loaded.calibrate(raw)
            assert 0.0 <= v <= 1.0

    def test_below_min_samples_does_not_persist(self, tmp_path: Path):
        """When n_samples < min_samples, no pickle is written and fitted=False."""
        out = tmp_path / "isotonic.pkl"
        # With min_samples set artificially high we should get fitted=False
        # even though synthetic bars will yield some pairs.
        result = bootstrap_calibrator(
            symbols=["AAA"],
            days=365,
            timeframe="1d",
            horizon_bars=4,
            output_path=out,
            min_samples=10**9,
            data_client=_MockDataClient(n_bars=400, trend=0.001),
        )
        assert result["fitted"] is False
        assert not out.exists()

    def test_rejects_non_daily_timeframe(self, tmp_path: Path):
        with pytest.raises(ValueError, match="only timeframe='1d'"):
            bootstrap_calibrator(
                symbols=["AAA"],
                days=365,
                timeframe="1h",
                output_path=tmp_path / "x.pkl",
                data_client=_MockDataClient(),
            )

    def test_analyst_breakdown_keys_match_emitting_analysts(self, tmp_path: Path):
        out = tmp_path / "isotonic.pkl"
        result = bootstrap_calibrator(
            symbols=["AAA", "BBB", "CCC"],
            days=365,
            timeframe="1d",
            horizon_bars=4,
            output_path=out,
            min_samples=10,
            data_client=_MockDataClient(n_bars=400, trend=0.001),
        )
        breakdown = result["analyst_breakdown"]
        assert len(breakdown) >= 1
        # Sum equals total
        assert sum(breakdown.values()) == result["n_samples"]


class TestBMAAggregatorLoadsCalibrator:
    def test_loads_fitted_calibrator_from_path(self, tmp_path: Path):
        """BMAAggregator(calibrator_path=<path>) loads a fitted IsotonicCalibrator."""
        out = tmp_path / "isotonic.pkl"
        result = bootstrap_calibrator(
            symbols=["AAA", "BBB", "CCC"],
            days=365,
            timeframe="1d",
            horizon_bars=4,
            output_path=out,
            min_samples=10,
            data_client=_MockDataClient(n_bars=400, trend=0.001),
        )
        assert result["fitted"]
        agg = BMAAggregator(calibrator_path=out)
        assert isinstance(agg.calibrator, IsotonicCalibrator)
        assert agg.calibrator.is_calibrated
        # And it actually maps things
        v = agg.calibrator.calibrate(0.5)
        assert 0.0 <= v <= 1.0

    def test_falls_back_to_cold_start_when_path_missing(self, tmp_path: Path):
        missing = tmp_path / "nope.pkl"
        agg = BMAAggregator(calibrator_path=missing)
        assert isinstance(agg.calibrator, ColdStartCalibrator)

    def test_falls_back_to_cold_start_on_corrupt_pickle(self, tmp_path: Path):
        bad = tmp_path / "bad.pkl"
        bad.write_bytes(b"not a pickle, just garbage \x00\x01\x02")
        agg = BMAAggregator(calibrator_path=bad)
        assert isinstance(agg.calibrator, ColdStartCalibrator)

    def test_falls_back_to_cold_start_on_wrong_type(self, tmp_path: Path):
        wrong = tmp_path / "wrong.pkl"
        with open(wrong, "wb") as f:
            pickle.dump({"not": "a calibrator"}, f)
        agg = BMAAggregator(calibrator_path=wrong)
        assert isinstance(agg.calibrator, ColdStartCalibrator)

    def test_default_path_constant_matches_module(self):
        """The bootstrap module's DEFAULT_CALIBRATOR_PATH must match BMA's
        default — drift here causes the live aggregator to silently miss the
        bootstrapped calibrator."""
        from hermes_quant.aggregators import bma as bma_mod

        assert DEFAULT_CALIBRATOR_PATH == bma_mod._DEFAULT_CALIBRATOR_PATH
