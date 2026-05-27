"""tests/factors/test_ic_metrics.py — IC / ICIR / factor-correlation tests.

Coverage:
    - Spearman IC on monotonic data = 1.0 (perfect rank correlation)
    - Spearman IC on anti-monotonic data = -1.0
    - IC on independent random data is near 0 (no guarantee, but finite)
    - IC handles NaN pairwise: drops NaN pairs
    - IC on fewer than 2 elements returns NaN
    - ICIR positive on consistently correlated history
    - ICIR negative on consistently anti-correlated history
    - ICIR returns NaN when fewer than 2 IC values
    - ICIR handles NaN ICs gracefully
    - Pearson correlation: perfect positive correlation → 1.0
    - Pearson correlation: perfect negative correlation → -1.0
    - Pearson correlation on constant series → NaN (undefined)
    - Pearson on independent normals gives finite result
    - Spearman agrees with Pearson on strictly monotone input
    - compute_ic accepts list inputs (not just ndarray)
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from hermes_quant.factors.ic_metrics import (
    compute_ic,
    compute_icir,
    factor_correlation,
)

RNG = np.random.default_rng(123)


# ---------------------------------------------------------------------------
# compute_ic
# ---------------------------------------------------------------------------


class TestComputeIC:
    def test_monotone_increasing_returns_one(self):
        a = np.arange(10, dtype=float)
        b = np.arange(10, dtype=float)
        ic = compute_ic(a, b)
        assert abs(ic - 1.0) < 1e-9

    def test_monotone_decreasing_returns_minus_one(self):
        a = np.arange(10, dtype=float)
        b = a[::-1]
        ic = compute_ic(a, b)
        assert abs(ic + 1.0) < 1e-9

    def test_independent_random_is_finite(self):
        a = RNG.standard_normal(100)
        b = RNG.standard_normal(100)
        ic = compute_ic(a, b)
        assert math.isfinite(ic)

    def test_nan_pairs_dropped(self):
        a = np.array([1.0, 2.0, float("nan"), 4.0, 5.0])
        b = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        ic = compute_ic(a, b)
        # Without NaN pair: a=[1,2,4,5], b=[1,2,4,5] → monotone → IC ≈ 1
        assert abs(ic - 1.0) < 1e-9

    def test_all_nan_returns_nan(self):
        a = np.full(10, float("nan"))
        b = np.arange(10, dtype=float)
        ic = compute_ic(a, b)
        assert math.isnan(ic)

    def test_too_few_elements_returns_nan(self):
        assert math.isnan(compute_ic([1.0], [1.0]))
        assert math.isnan(compute_ic([], []))

    def test_accepts_list_input(self):
        """compute_ic must accept plain Python lists, not just ndarray."""
        a = list(range(5))
        b = list(range(5))
        ic = compute_ic(a, b)
        assert abs(ic - 1.0) < 1e-9

    def test_tie_handling_doesnt_crash(self):
        a = np.array([1.0, 1.0, 2.0, 2.0, 3.0])
        b = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        ic = compute_ic(a, b)
        assert math.isfinite(ic)


# ---------------------------------------------------------------------------
# compute_icir
# ---------------------------------------------------------------------------


class TestComputeICIR:
    def _consistent_history(self, n_periods=20, n_assets=30, positive=True):
        """Generate a history where every period has a strong positive/negative IC."""
        sign = 1 if positive else -1
        predictions = []
        realizations = []
        rng = np.random.default_rng(7)
        for _ in range(n_periods):
            pred = rng.standard_normal(n_assets)
            # Realizations highly correlated with predictions
            real = sign * pred + rng.standard_normal(n_assets) * 0.05
            predictions.append(pred)
            realizations.append(real)
        return predictions, realizations

    def test_positive_on_consistently_correlated_factor(self):
        preds, reals = self._consistent_history(positive=True)
        icir = compute_icir(preds, reals)
        assert math.isfinite(icir)
        assert icir > 0

    def test_negative_on_consistently_anti_correlated_factor(self):
        preds, reals = self._consistent_history(positive=False)
        icir = compute_icir(preds, reals)
        assert math.isfinite(icir)
        assert icir < 0

    def test_single_period_returns_nan(self):
        icir = compute_icir([[1.0, 2.0, 3.0]], [[1.0, 2.0, 3.0]])
        assert math.isnan(icir)

    def test_zero_periods_returns_nan(self):
        icir = compute_icir([], [])
        assert math.isnan(icir)

    def test_nan_ics_handled(self):
        """If some IC values are NaN (e.g. constant series), they are ignored."""
        # First cross-section: constant predictions → NaN IC
        preds = [np.ones(10)] + [np.arange(10, dtype=float) for _ in range(5)]
        reals = [np.arange(10, dtype=float) for _ in range(6)]
        icir = compute_icir(preds, reals)
        # Should not crash; will have 5 valid ICs
        assert isinstance(icir, float)

    def test_icir_magnitude_grows_with_consistency(self):
        """A noisier history should have lower |ICIR| than a clean one."""
        rng = np.random.default_rng(99)
        clean_preds, clean_reals = self._consistent_history()
        noisy_preds, noisy_reals = [], []
        for p in clean_preds:
            noisy_reals.append(rng.standard_normal(len(p)))  # random reals
            noisy_preds.append(p)
        icir_clean = abs(compute_icir(clean_preds, clean_reals))
        icir_noisy = abs(compute_icir(noisy_preds, noisy_reals))
        assert icir_clean > icir_noisy or math.isnan(icir_noisy)


# ---------------------------------------------------------------------------
# factor_correlation
# ---------------------------------------------------------------------------


class TestFactorCorrelation:
    def test_perfect_positive(self):
        a = np.arange(1.0, 11.0)
        b = a * 2.0 + 5.0  # linear transform → corr = 1
        assert abs(factor_correlation(a, b) - 1.0) < 1e-9

    def test_perfect_negative(self):
        a = np.arange(1.0, 11.0)
        b = -a
        assert abs(factor_correlation(a, b) + 1.0) < 1e-9

    def test_constant_series_returns_nan(self):
        a = np.ones(10)
        b = np.arange(10, dtype=float)
        result = factor_correlation(a, b)
        assert math.isnan(result)

    def test_both_constant_returns_nan(self):
        assert math.isnan(factor_correlation(np.ones(10), np.ones(10)))

    def test_independent_normals_finite(self):
        a = RNG.standard_normal(200)
        b = RNG.standard_normal(200)
        r = factor_correlation(a, b)
        assert math.isfinite(r)
        assert -1.0 <= r <= 1.0

    def test_accepts_list_input(self):
        r = factor_correlation(list(range(5)), list(range(5)))
        assert abs(r - 1.0) < 1e-9

    def test_nan_pairwise_dropped(self):
        a = np.array([1.0, 2.0, float("nan"), 4.0])
        b = np.array([1.0, 2.0, 3.0, 4.0])
        r = factor_correlation(a, b)
        # After dropping: a=[1,2,4], b=[1,2,4] → corr = 1
        assert abs(r - 1.0) < 1e-9

    def test_spearman_and_pearson_agree_on_monotone(self):
        """On strictly monotone data, Spearman IC and Pearson corr should both be 1."""
        a = np.arange(1.0, 21.0)
        b = a  # same ordering
        ic = compute_ic(a, b)
        corr = factor_correlation(a, b)
        assert abs(ic - 1.0) < 1e-9
        assert abs(corr - 1.0) < 1e-9

    def test_too_few_elements_returns_nan(self):
        assert math.isnan(factor_correlation([1.0], [1.0]))
        assert math.isnan(factor_correlation([], []))
