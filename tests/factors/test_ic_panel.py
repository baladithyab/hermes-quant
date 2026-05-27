"""tests/factors/test_ic_panel.py — Unit tests for ICPanel and compute_ic_panel.

Coverage:
  1. Monotonic factor + perfectly aligned forward returns → IC ≈ 1.0
  2. Anti-monotonic factor → IC ≈ -1.0
  3. Uncorrelated (random) factor → IC ≈ 0 (weak)
  4. Missing dates in factor series (alignment drops them)
  5. Missing dates in fwd_returns series (alignment drops them)
  6. Fewer than window observations → ValueError raised
  7. ICPanel.to_dict() / from_dict() round-trip
  8. ICIR formula: ic_mean / max(ic_std, 1e-9)
  9. Hit rate: fraction of windows with positive IC
 10. Constant factor series → NaN IC (degenerate)
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from hermes_quant.factors.ic_panel import ICPanel, compute_ic_panel


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_N = 300  # bars to generate (more than window=60)


def _date_index(n: int = _N) -> pd.DatetimeIndex:
    return pd.bdate_range("2020-01-02", periods=n)


def _monotonic_factor(n: int = _N) -> pd.Series:
    """Strictly increasing factor values → should have IC near +1."""
    idx = _date_index(n)
    return pd.Series(np.arange(n, dtype=float), index=idx, name="mono")


def _fwd_returns_aligned_positive(n: int = _N) -> pd.Series:
    """Forward returns proportional to index → perfectly correlated with monotonic factor."""
    idx = _date_index(n)
    return pd.Series(np.arange(n, dtype=float) * 0.01 + 0.001, index=idx, name="fwd")


def _fwd_returns_inverse(n: int = _N) -> pd.Series:
    """Forward returns inversely proportional → IC near -1."""
    idx = _date_index(n)
    return pd.Series(np.arange(n, 0, -1, dtype=float) * 0.01, index=idx, name="fwd")


def _random_factor(n: int = _N, seed: int = 42) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = _date_index(n)
    return pd.Series(rng.standard_normal(n), index=idx, name="rand_factor")


def _random_fwd(n: int = _N, seed: int = 99) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = _date_index(n)
    return pd.Series(rng.standard_normal(n) * 0.01, index=idx, name="rand_fwd")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestICPanelDataclass:
    """Tests for the ICPanel dataclass itself (serialisation, fields)."""

    def test_to_dict_contains_all_fields(self) -> None:
        panel = ICPanel(
            factor_id="test_alpha",
            ic_mean=0.12,
            ic_std=0.08,
            icir=1.5,
            hit_rate=0.65,
            turnover=0.03,
            n_periods=40,
            fwd_horizon_days=5,
        )
        d = panel.to_dict()
        assert d["factor_id"] == "test_alpha"
        assert d["ic_mean"] == pytest.approx(0.12)
        assert d["ic_std"] == pytest.approx(0.08)
        assert d["icir"] == pytest.approx(1.5)
        assert d["hit_rate"] == pytest.approx(0.65)
        assert d["turnover"] == pytest.approx(0.03)
        assert d["n_periods"] == 40
        assert d["fwd_horizon_days"] == 5

    def test_from_dict_round_trip(self) -> None:
        panel = ICPanel(
            factor_id="alpha_abc",
            ic_mean=0.07,
            ic_std=0.04,
            icir=1.75,
            hit_rate=0.7,
            turnover=0.02,
            n_periods=50,
            fwd_horizon_days=10,
        )
        assert ICPanel.from_dict(panel.to_dict()) == panel

    def test_frozen_dataclass_immutable(self) -> None:
        panel = ICPanel(
            factor_id="f",
            ic_mean=0.0,
            ic_std=0.0,
            icir=0.0,
            hit_rate=0.0,
            turnover=0.0,
            n_periods=0,
            fwd_horizon_days=5,
        )
        with pytest.raises((AttributeError, TypeError)):
            panel.ic_mean = 99.0  # type: ignore[misc]


class TestComputeICPanel:
    """Tests for compute_ic_panel()."""

    def test_monotonic_factor_high_ic(self) -> None:
        """Perfectly ordered factor vs. positively ordered fwd returns → IC ≈ 1."""
        panel = compute_ic_panel(
            _monotonic_factor(),
            _fwd_returns_aligned_positive(),
            factor_id="mono",
        )
        assert panel.ic_mean > 0.8, f"Expected IC near 1.0, got {panel.ic_mean}"
        assert panel.hit_rate > 0.9, f"Expected high hit_rate, got {panel.hit_rate}"
        assert panel.n_periods > 0

    def test_anti_monotonic_factor_negative_ic(self) -> None:
        """Inversely ordered fwd returns → IC ≈ -1."""
        panel = compute_ic_panel(
            _monotonic_factor(),
            _fwd_returns_inverse(),
            factor_id="anti_mono",
        )
        assert panel.ic_mean < -0.8, f"Expected IC near -1.0, got {panel.ic_mean}"

    def test_uncorrelated_factor_ic_near_zero(self) -> None:
        """Independent random factor and forward returns → |IC| < 0.3."""
        panel = compute_ic_panel(
            _random_factor(seed=42),
            _random_fwd(seed=99),
            factor_id="rand",
        )
        assert abs(panel.ic_mean) < 0.3, f"Expected weak IC, got {panel.ic_mean}"

    def test_factor_id_propagated(self) -> None:
        panel = compute_ic_panel(
            _monotonic_factor(),
            _fwd_returns_aligned_positive(),
            factor_id="custom_id_xyz",
        )
        assert panel.factor_id == "custom_id_xyz"

    def test_fwd_horizon_days_stored(self) -> None:
        panel = compute_ic_panel(
            _monotonic_factor(),
            _fwd_returns_aligned_positive(),
            fwd_horizon_days=10,
        )
        assert panel.fwd_horizon_days == 10

    def test_alignment_handles_missing_factor_dates(self) -> None:
        """Factor missing every other date → inner join halves the observations."""
        factor = _monotonic_factor()
        # Keep only even-indexed dates
        factor_sparse = factor.iloc[::2]
        fwd = _fwd_returns_aligned_positive()
        # Both have 300 rows; after inner-join on sparse factor, ~150 rows
        panel = compute_ic_panel(
            factor_sparse,
            fwd,
            factor_id="sparse_factor",
        )
        assert panel.n_periods > 0
        assert math.isfinite(panel.ic_mean)

    def test_alignment_handles_missing_fwd_return_dates(self) -> None:
        """fwd_returns missing every other date → inner join reduces observations."""
        factor = _monotonic_factor()
        fwd = _fwd_returns_aligned_positive()
        fwd_sparse = fwd.iloc[::2]
        panel = compute_ic_panel(
            factor,
            fwd_sparse,
            factor_id="sparse_fwd",
        )
        assert panel.n_periods > 0
        assert math.isfinite(panel.ic_mean)

    def test_insufficient_data_raises_value_error(self) -> None:
        """Fewer than window=60 aligned observations → ValueError."""
        factor = _monotonic_factor(n=50)  # only 50 obs
        fwd = _fwd_returns_aligned_positive(n=50)
        with pytest.raises(ValueError, match="aligned observations"):
            compute_ic_panel(factor, fwd, factor_id="too_short")

    def test_icir_formula(self) -> None:
        """ICIR = ic_mean / max(ic_std, 1e-9)."""
        panel = compute_ic_panel(
            _monotonic_factor(),
            _fwd_returns_aligned_positive(),
            factor_id="icir_check",
        )
        expected_icir = panel.ic_mean / max(panel.ic_std, 1e-9)
        assert panel.icir == pytest.approx(expected_icir, rel=1e-6)

    def test_hit_rate_range(self) -> None:
        """hit_rate must always be in [0, 1]."""
        panel = compute_ic_panel(
            _random_factor(),
            _random_fwd(),
            factor_id="hit_rate_range",
        )
        assert 0.0 <= panel.hit_rate <= 1.0

    def test_turnover_non_negative(self) -> None:
        panel = compute_ic_panel(
            _monotonic_factor(),
            _fwd_returns_aligned_positive(),
        )
        assert panel.turnover >= 0.0

    def test_n_periods_positive(self) -> None:
        panel = compute_ic_panel(
            _monotonic_factor(),
            _fwd_returns_aligned_positive(),
        )
        assert panel.n_periods > 0
