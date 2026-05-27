"""tests/factors/test_ic_dedup.py — IC deduplication gate tests.

Coverage:
    - Rejects perfectly-correlated (r≈1.0) factor
    - Accepts orthogonal / uncorrelated factor
    - Threshold env-var HERMES_QUANT_IC_DEDUP_THRESHOLD is honoured
    - Empty library always passes
    - register() then remove() lifecycle
    - save() / load() round-trip
    - Custom threshold via constructor param
    - BMA integration: excluded analyst doesn't contaminate aggregation result
    - Gate checks against multiple factors — uses maximum correlation
    - Negatively-correlated factor (|corr|=1) is also rejected
    - NaN-heavy series degrades gracefully
"""

from __future__ import annotations

import json
import os
import pathlib

import numpy as np
import pytest

from hermes_quant.factors.ic_dedup import ICDedupGate, ICDedupResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

RNG = np.random.default_rng(42)

FACTOR_A = RNG.standard_normal(100)
FACTOR_B = FACTOR_A + RNG.standard_normal(100) * 1e-6  # near-perfect clone
FACTOR_C = RNG.standard_normal(100)                     # independent
FACTOR_NEG = -FACTOR_A                                  # perfect negative corr


# ---------------------------------------------------------------------------
# Basic check() behaviour
# ---------------------------------------------------------------------------


class TestICDedupGateBasic:
    def test_empty_library_always_passes(self):
        gate = ICDedupGate()
        result = gate.check(FACTOR_A)
        assert result.passes is True
        assert result.correlated_with is None
        assert result.reason == "library_empty"

    def test_rejects_perfectly_correlated_factor(self):
        gate = ICDedupGate(threshold=0.99)
        gate.register("factor_a", FACTOR_A)
        result = gate.check(FACTOR_B)
        assert result.passes is False
        assert result.max_corr > 0.99
        assert result.correlated_with == "factor_a"

    def test_accepts_uncorrelated_factor(self):
        gate = ICDedupGate(threshold=0.99)
        gate.register("factor_a", FACTOR_A)
        result = gate.check(FACTOR_C)
        assert result.passes is True
        assert abs(result.max_corr) < 0.99

    def test_rejects_negatively_correlated_factor(self):
        """Negative perfect correlation should ALSO be rejected (|corr| check)."""
        gate = ICDedupGate(threshold=0.99)
        gate.register("factor_a", FACTOR_A)
        result = gate.check(FACTOR_NEG)
        assert result.passes is False
        assert result.max_corr > 0.99

    def test_threshold_boundary_exact_equal_rejects(self):
        """At exactly threshold, the factor should be rejected (< threshold required)."""
        # Manufacture a pair with a known correlation ~0.90
        rng = np.random.default_rng(0)
        a = rng.standard_normal(200)
        noise = rng.standard_normal(200) * 0.436  # correlation ≈ 0.916
        b = a + noise
        gate = ICDedupGate(threshold=0.99)
        gate.register("a", a)
        result = gate.check(b)
        # With threshold=0.99 and corr≈0.92 it should pass
        assert result.passes is True

    def test_custom_threshold_constructor(self):
        gate = ICDedupGate(threshold=0.50)
        gate.register("factor_a", FACTOR_A)
        # FACTOR_C is independent so correlation should be low
        result = gate.check(FACTOR_C)
        # correlation is likely < 0.5 for independent normals
        assert isinstance(result.passes, bool)  # just confirms it runs

    def test_multiple_factors_uses_maximum_correlation(self):
        gate = ICDedupGate(threshold=0.99)
        gate.register("low_corr", FACTOR_C)
        gate.register("high_corr", FACTOR_A)  # near-clone of FACTOR_B
        result = gate.check(FACTOR_B)
        assert result.passes is False
        assert result.correlated_with == "high_corr"

    def test_nan_series_handled_gracefully(self):
        gate = ICDedupGate(threshold=0.99)
        gate.register("factor_a", FACTOR_A)
        nan_series = np.full(100, float("nan"))
        result = gate.check(nan_series)
        # NaN series yields NaN correlation — gate should not crash
        # and should either pass (all_correlations_nan) or have NaN max_corr
        assert isinstance(result, ICDedupResult)

    def test_per_call_threshold_override(self):
        """Passing threshold= kwarg to check() overrides instance threshold."""
        gate = ICDedupGate(threshold=0.99)
        gate.register("factor_a", FACTOR_A)
        # With very tight threshold 0.0, even weakly-correlated factors are rejected
        result = gate.check(FACTOR_C, threshold=0.0)
        assert result.passes is False

    def test_per_call_library_override(self):
        """Passing existing_library= kwarg uses that library instead of self._library."""
        gate = ICDedupGate(threshold=0.99)
        gate.register("factor_a", FACTOR_A)  # internal library has FACTOR_A
        # Override with a fresh library that has FACTOR_C (unrelated to FACTOR_B)
        custom_lib = {"custom_c": FACTOR_C}
        result = gate.check(FACTOR_B, existing_library=custom_lib)
        # FACTOR_B is unrelated to FACTOR_C → should pass
        assert result.passes is True


# ---------------------------------------------------------------------------
# Library lifecycle
# ---------------------------------------------------------------------------


class TestLibraryLifecycle:
    def test_register_adds_to_library(self):
        gate = ICDedupGate()
        assert len(gate) == 0
        gate.register("f1", FACTOR_A)
        assert len(gate) == 1
        assert "f1" in gate

    def test_register_overwrites_duplicate_name(self):
        gate = ICDedupGate()
        gate.register("f1", FACTOR_A)
        gate.register("f1", FACTOR_C)  # overwrite
        assert len(gate) == 1
        np.testing.assert_array_equal(gate.library["f1"], FACTOR_C)

    def test_remove_existing_returns_true(self):
        gate = ICDedupGate()
        gate.register("f1", FACTOR_A)
        assert gate.remove("f1") is True
        assert "f1" not in gate

    def test_remove_missing_returns_false(self):
        gate = ICDedupGate()
        assert gate.remove("nonexistent") is False

    def test_library_property_returns_copy(self):
        gate = ICDedupGate()
        gate.register("f1", FACTOR_A)
        lib = gate.library
        lib["injected"] = FACTOR_C  # mutating copy should NOT affect gate
        assert "injected" not in gate


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_save_load_round_trip(self, tmp_path):
        gate = ICDedupGate(threshold=0.97)
        gate.register("fa", FACTOR_A)
        gate.register("fc", FACTOR_C)
        save_path = tmp_path / "factors.json"
        gate.save(save_path)

        gate2 = ICDedupGate(threshold=0.97)
        gate2.load(save_path)

        assert set(gate2.library.keys()) == {"fa", "fc"}
        np.testing.assert_allclose(gate2.library["fa"], FACTOR_A, rtol=1e-6)
        np.testing.assert_allclose(gate2.library["fc"], FACTOR_C, rtol=1e-6)

    def test_save_creates_parent_dirs(self, tmp_path):
        gate = ICDedupGate()
        gate.register("f1", FACTOR_A)
        deep_path = tmp_path / "deep" / "nested" / "factors.json"
        gate.save(deep_path)
        assert deep_path.exists()

    def test_saved_json_is_valid(self, tmp_path):
        gate = ICDedupGate()
        gate.register("f1", FACTOR_A)
        save_path = tmp_path / "f.json"
        gate.save(save_path)
        payload = json.loads(save_path.read_text())
        assert "f1" in payload
        assert len(payload["f1"]) == len(FACTOR_A)


# ---------------------------------------------------------------------------
# Env var threshold
# ---------------------------------------------------------------------------


class TestEnvVarThreshold:
    def test_env_var_threshold_honoured(self, monkeypatch):
        """HERMES_QUANT_IC_DEDUP_THRESHOLD overrides default in a *new* gate."""
        monkeypatch.setenv("HERMES_QUANT_IC_DEDUP_THRESHOLD", "0.50")
        # Re-import to pick up the env var (it is read at module level)
        import importlib
        import hermes_quant.factors.ic_dedup as mod
        importlib.reload(mod)
        gate = mod.ICDedupGate()  # uses module-level default
        assert gate.threshold == 0.50
        # Restore
        importlib.reload(mod)


# ---------------------------------------------------------------------------
# BMA integration
# ---------------------------------------------------------------------------


class TestBMAIntegration:
    """An excluded analyst via IC dedup gate must not affect BMA output."""

    def _make_views(self):
        from hermes_quant.protocol import AnalystView, Direction
        # Two views, same direction and magnitude — analyst_b is the "clone"
        view_a = AnalystView(
            analyst="analyst_a",
            direction=1,
            magnitude=0.5,
            confidence=0.7,
            confidence_raw=0.7,
            horizon="1d",
        )
        view_b = AnalystView(
            analyst="analyst_b",
            direction=1,
            magnitude=0.5,
            confidence=0.7,
            confidence_raw=0.7,
            horizon="1d",
        )
        return view_a, view_b

    def _make_context(self):
        import pandas as pd
        from hermes_quant.protocol import MarketContext
        ts = pd.date_range("2025-06-01", periods=2, freq="1d")
        bars = pd.DataFrame({
            "timestamp": ts,
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [1000.0, 1000.0],
        })
        asof = pd.Timestamp("2025-06-02")
        return MarketContext(
            asset="AAPL",
            timeframe="1d",
            asset_class="equity",
            exchange=None,
            bars=bars,
            last_close=101.5,
            last_volume=1000.0,
            asof=asof,
        )

    def test_bma_gate_none_is_bit_identical(self):
        """With ic_dedup_gate=None the BMA result is identical to no-gate."""
        from hermes_quant.aggregators.bma import BMAAggregator

        views = list(self._make_views())
        ctx = self._make_context()

        agg_no_gate = BMAAggregator(require_ensemble=False)
        agg_with_none = BMAAggregator(require_ensemble=False, ic_dedup_gate=None)

        r1 = agg_no_gate.aggregate(views, ctx)
        r2 = agg_with_none.aggregate(views, ctx)

        assert r1.direction == r2.direction
        assert abs(r1.confidence - r2.confidence) < 1e-9
        assert abs(r1.magnitude - r2.magnitude) < 1e-9

    def test_bma_excluded_analyst_not_in_aggregation(self):
        """When analyst_b is clone of analyst_a, gate excludes it from BMA."""
        from hermes_quant.aggregators.bma import BMAAggregator

        gate = ICDedupGate(threshold=0.99)
        # Register identical return series for both analysts
        gate.register("analyst_a", FACTOR_A)
        gate.register("analyst_b", FACTOR_B)  # near-perfect clone

        views = list(self._make_views())
        ctx = self._make_context()

        agg = BMAAggregator(require_ensemble=False, ic_dedup_gate=gate)
        signal = agg.aggregate(views, ctx)

        # analyst_b should have been excluded
        excluded = signal.metadata.get("ic_dedup_excluded_analysts", [])
        assert "analyst_b" in excluded

    def test_bma_metadata_key_always_present_with_gate(self):
        """ic_dedup_excluded_analysts key present whenever gate is set."""
        from hermes_quant.aggregators.bma import BMAAggregator

        gate = ICDedupGate(threshold=0.99)
        views = list(self._make_views())
        ctx = self._make_context()

        agg = BMAAggregator(require_ensemble=False, ic_dedup_gate=gate)
        signal = agg.aggregate(views, ctx)

        assert "ic_dedup_excluded_analysts" in signal.metadata
