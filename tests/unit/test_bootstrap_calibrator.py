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
    _MIN_CONTEXT_BARS,
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
        # 5 symbols × 1200 bars × NON-overlapping stride-4 walk (cs08) → enough
        # effective samples to clear the IsotonicCalibrator n_min_samples=200
        # floor. (Pre-cs08 this used 400 bars and the overlapping stride-1 walk;
        # the de-overlap fix cuts the count ~4x so the bar budget is widened to
        # keep the fit reachable — the production floor is NOT lowered.)
        result = bootstrap_calibrator(
            symbols=["AAA", "BBB", "CCC", "DDD", "EEE"],
            days=365,
            timeframe="1d",
            horizon_bars=4,
            output_path=out,
            min_samples=10,
            data_client=_MockDataClient(n_bars=1200, trend=0.001),
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
            # 1200 bars so the cs08 non-overlapping stride-4 walk still clears
            # the IsotonicCalibrator n_min_samples=200 fit floor.
            data_client=_MockDataClient(n_bars=1200, trend=0.0008),
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
            # 1200 bars so the cs08 non-overlapping stride-4 walk still clears
            # the IsotonicCalibrator n_min_samples=200 fit floor.
            data_client=_MockDataClient(n_bars=1200, trend=0.001),
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


# --------------------------------------------------------------------------- #
# cs08 — non-overlapping forward-window stride                                #
# --------------------------------------------------------------------------- #


class TestNonOverlappingStride:
    """cs08: the walk must emit ONE (raw, correct) pair per NON-overlapping
    H-bar forward block, not one per bar.

    The bug: ``_walk_bars_for_symbol`` steps ``t`` by 1 while each sample
    settles against the forward window ``[t, t+horizon_bars]``. Consecutive
    samples at ``t`` and ``t+1`` share ``H-1`` of ``H`` forward bars, so their
    ``direction_correct`` outcomes are heavily autocorrelated — NOT i.i.d.
    This inflates the sample count by ~H, lets the ``min_samples`` / isotonic
    ``n_min_samples`` gate pass on pseudo-replicated evidence, and over-confidently
    fits the production calibration curve that sizes every trade
    (risk/gate.py quarter_kelly). Fix: stride the walk by ``horizon_bars`` so
    each emitted outcome is an independent forward-window observation.
    """

    def _count(self, *, n_bars: int, horizon_bars: int, symbols, trend: float = 0.001) -> int:
        """Run the bootstrap with the gate effectively disabled; return the
        raw aggregated (raw, correct) pair count actually emitted by the walk.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "iso.pkl"
            res = bootstrap_calibrator(
                symbols=symbols,
                days=365,
                timeframe="1d",
                horizon_bars=horizon_bars,
                output_path=out,
                min_samples=1,
                data_client=_MockDataClient(n_bars=n_bars, trend=trend),
            )
        return int(res["n_samples"])

    def test_horizon4_count_is_non_overlapping_not_inflated(self):
        """T1 (the bug): at H=4 the emitted count must reflect a stride-H walk
        (~1/H of the overlapping count), not one sample per bar.

        UNPATCHED (stride 1) this emits ~125 pairs for one symbol; GREEN
        (stride H) it must drop ~4x to roughly the non-overlapping count. The
        non-overlapping walk visits ``len(range(200, n-H, H))`` candidate bars
        vs the overlapping ``len(range(200, n-H))``.
        """
        n_bars, horizon = 400, 4
        overlap_candidates = len(range(_MIN_CONTEXT_BARS, n_bars - horizon))  # 196
        nonoverlap_candidates = len(range(_MIN_CONTEXT_BARS, n_bars - horizon, horizon))  # 49
        assert overlap_candidates == 196 and nonoverlap_candidates == 49  # arithmetic pin

        n = self._count(n_bars=n_bars, horizon_bars=horizon, symbols=["AAA"])

        # The emitted count is selective (analysts skip some bars) but must be
        # bounded by the NON-overlapping candidate budget per analyst, not the
        # overlapping one. With two emitting analysts the ceiling is
        # 2 * nonoverlap_candidates = 98. The unpatched stride-1 walk produces
        # ~125 (> 98), so this assertion fails RED and passes GREEN.
        max_emitting_analysts = 2  # classical-ta + microstructure_lite
        assert n <= max_emitting_analysts * nonoverlap_candidates, (
            f"emitted {n} pairs > non-overlapping ceiling "
            f"{max_emitting_analysts * nonoverlap_candidates}: walk is still "
            "striding by 1 (overlapping forward windows, cs08)"
        )

    def test_gate_refuses_pseudo_replicated_evidence(self):
        """Keystone gate: a window whose OVERLAPPING count clears min_samples
        but whose NON-overlapping count does not must NOT fit the calibrator.

        2 symbols × 400 bars × H=4 emits ~250 overlapping pairs (>= the 200
        gate) but only ~62 non-overlapping pairs (< 200). RED: stride-1 count
        250 >= 200 -> fitted=True + pickle written (gate passes on
        pseudo-evidence). GREEN: stride-4 count ~62 < 200 -> fitted=False + no
        pickle (gate refuses).
        """
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "iso.pkl"
            res = bootstrap_calibrator(
                symbols=["S0", "S1"],
                days=365,
                timeframe="1d",
                horizon_bars=4,
                output_path=out,
                min_samples=200,  # the production default
                data_client=_MockDataClient(n_bars=400, trend=0.001),
            )
            assert res["fitted"] is False, (
                f"calibrator fitted on {res['n_samples']} samples but those are "
                "overlapping/pseudo-replicated; effective non-overlapping N is "
                "< 200 and must fail the gate (cs08)"
            )
            assert not out.exists(), "pickle written despite insufficient effective N (cs08)"
            assert res["n_samples"] < 200, (
                f"effective n_samples={res['n_samples']} should be < 200 after "
                "non-overlapping subsample"
            )

    def test_horizon1_byte_identical(self):
        """T2: at horizon_bars=1 there is NO overlap, so stride-1 == stride-1.
        The non-overlapping walk must be byte-identical to the legacy walk.

        ``range(200, n-1, 1) == range(200, n-1)``, so the emitted count and
        every (raw, correct) pair are unchanged. This is the safety invariant:
        the fix is a strict no-op when there is nothing to de-overlap.
        """
        n_bars = 400
        # Pin the index-set equivalence first (pure arithmetic, no analysts).
        assert list(range(_MIN_CONTEXT_BARS, n_bars - 1, 1)) == list(
            range(_MIN_CONTEXT_BARS, n_bars - 1)
        )
        # And the emitted count is unchanged vs the historically-observed H=1
        # stride-1 baseline (127 for one symbol with this mock+seed).
        n = self._count(n_bars=n_bars, horizon_bars=1, symbols=["AAA"])
        assert n == 127, (
            f"H=1 emitted {n} pairs; must equal the stride-1 baseline 127 "
            "(no overlap at H=1 -> byte-identical walk)"
        )

    def test_stride_tracks_horizon_magnitude(self):
        """T3: increasing horizon_bars must reduce the emitted count by ~1/H
        (stride wired to horizon_bars, not hardcoded to 1).

        Under the buggy stride-1 walk the count is ~flat across H (only the
        ``n-H`` upper bound shifts it by a bar or two: ~127->123 for H 1->8),
        so a *magnitude* test fails RED. Under stride-H the H=4 count must drop
        to roughly a quarter of the H=1 count and H=8 to roughly an eighth, so
        we assert at least a 3x / 6x reduction (loose to tolerate the selective
        emission jitter) — unreachable by the flat stride-1 walk.
        """
        n_bars = 400
        counts = {
            horizon: self._count(n_bars=n_bars, horizon_bars=horizon, symbols=["AAA"])
            for horizon in (1, 2, 4, 8)
        }
        assert counts[4] <= counts[1] / 3.0, (
            f"H=4 count {counts[4]} is not ~1/4 of H=1 count {counts[1]} "
            f"(all counts={counts}); the walk is not striding by horizon_bars (cs08)"
        )
        assert counts[8] <= counts[1] / 6.0, (
            f"H=8 count {counts[8]} is not ~1/8 of H=1 count {counts[1]} "
            f"(all counts={counts}); the walk is not striding by horizon_bars (cs08)"
        )
