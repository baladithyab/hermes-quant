"""tests/regime/test_regime_uncertainty.py — ADR-0096 Gate 4 regime uncertainty propagation tests.

RED->GREEN contract:
    - peaked posterior -> factor near 1 (high confidence, low entropy)
    - uniform / max-entropy posterior -> factor near 0 (silence)
    - NaN/inf posterior -> 0.0 (fail-CLOSED, silence)
    - all-zero / empty posterior -> 0.0 (degenerate -> silence)
    - negative probability mass -> 0.0 (degenerate -> silence)
    - entropy math is correct (compare directly against numpy reference)
    - hard-label helper (one-hot) -> 1.0 (zero entropy, fully confident)
    - hard-label with invalid args -> 0.0 (degenerate -> silence)
    - factor is monotonically decreasing in entropy (more peaked = higher factor)
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from hermes_quant.regime.regime_uncertainty import (
    _DEGENERATE_FACTOR,
    compute_regime_factor,
    regime_factor_from_hard_label,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uniform(n: int) -> np.ndarray:
    """Return a uniform distribution over n states."""
    return np.ones(n, dtype=float) / n


def _peaked(n: int, hot: int = 0, mass: float = 0.99) -> np.ndarray:
    """Return a near-one-hot distribution: mass on hot, rest spread equally."""
    p = np.full(n, (1.0 - mass) / max(n - 1, 1), dtype=float)
    p[hot] = mass
    return p


def _reference_entropy(p: np.ndarray) -> float:
    """Shannon entropy via numpy (reference implementation)."""
    p = p / p.sum()
    safe = np.maximum(p, 1e-15)
    return float(-np.sum(safe * np.log(safe)))


# ---------------------------------------------------------------------------
# Core: peaked posterior -> factor near 1
# ---------------------------------------------------------------------------


def test_peaked_3_state_near_one():
    """A near-one-hot 3-state posterior yields a factor well above 0.5.

    With mass=0.98 across 3 states the normalized entropy is ~0.10, so
    factor ~ 0.90. The threshold is set conservatively at 0.80 to be robust
    to floating-point differences while still verifying the qualitative
    contract (peaked -> high factor, well above uniform's ~0.0).
    """
    p = _peaked(3, hot=0, mass=0.98)
    factor = compute_regime_factor(p)
    assert factor > 0.80, f"expected factor > 0.80 for peaked posterior; got {factor}"
    assert 0.0 <= factor <= 1.0


def test_one_hot_3_state_exact_one():
    """A perfect one-hot posterior has zero entropy -> factor = 1.0."""
    p = np.array([0.0, 1.0, 0.0], dtype=float)
    factor = compute_regime_factor(p)
    assert math.isclose(factor, 1.0, abs_tol=1e-9), f"expected 1.0; got {factor}"


def test_peaked_5_state_near_one():
    """Peak over 5 states also yields high factor.

    With mass=0.95 across 5 states the normalized entropy is ~0.17, so
    factor ~ 0.83. Threshold set conservatively at 0.75.
    """
    p = _peaked(5, hot=2, mass=0.95)
    factor = compute_regime_factor(p)
    assert factor > 0.75


# ---------------------------------------------------------------------------
# Core: uniform (max-entropy) posterior -> factor near 0
# ---------------------------------------------------------------------------


def test_uniform_3_state_near_zero():
    """A uniform 3-state posterior (max entropy) yields factor near 0."""
    factor = compute_regime_factor(_uniform(3))
    assert factor < 0.05, f"expected factor < 0.05 for uniform posterior; got {factor}"
    assert factor >= 0.0


def test_uniform_2_state_near_zero():
    """A uniform 2-state posterior (max entropy) yields factor near 0."""
    factor = compute_regime_factor(_uniform(2))
    assert factor < 0.05, f"expected factor < 0.05 for uniform 2-state; got {factor}"


def test_uniform_5_state_near_zero():
    """A uniform 5-state posterior (max entropy) yields factor near 0."""
    factor = compute_regime_factor(_uniform(5))
    assert factor < 0.05


def test_exact_uniform_factor_zero():
    """Exact uniform posterior should yield factor = 0.0 (within float tolerance)."""
    p = np.ones(3) / 3.0
    factor = compute_regime_factor(p)
    # Allow small floating-point slack (entropy can be marginally below log(n))
    assert math.isclose(factor, 0.0, abs_tol=1e-9), f"expected ~0; got {factor}"


# ---------------------------------------------------------------------------
# Core: entropy math correctness
# ---------------------------------------------------------------------------


def test_entropy_math_3_state_peaked():
    """Verify factor = 1 - H/H_max exactly against reference implementation."""
    p = np.array([0.7, 0.2, 0.1], dtype=float)
    h = _reference_entropy(p)
    h_max = math.log(3)
    expected_factor = max(0.0, min(1.0, 1.0 - h / h_max))

    computed = compute_regime_factor(p)
    assert math.isclose(computed, expected_factor, abs_tol=1e-9), (
        f"expected {expected_factor}; got {computed}"
    )


def test_entropy_math_4_state_moderate():
    """Verify factor against reference for a moderate 4-state posterior."""
    p = np.array([0.5, 0.3, 0.15, 0.05], dtype=float)
    h = _reference_entropy(p)
    h_max = math.log(4)
    expected_factor = max(0.0, min(1.0, 1.0 - h / h_max))

    computed = compute_regime_factor(p)
    assert math.isclose(computed, expected_factor, abs_tol=1e-9)


def test_entropy_math_unnormalized_input():
    """Unnormalized posterior (sums to 2.0) must give same factor as normalized."""
    p_norm = np.array([0.6, 0.3, 0.1], dtype=float)
    p_unnorm = p_norm * 2.0  # same shape, different scale

    f_norm = compute_regime_factor(p_norm)
    f_unnorm = compute_regime_factor(p_unnorm)
    assert math.isclose(f_norm, f_unnorm, abs_tol=1e-9), (
        "normalization should not affect factor"
    )


# ---------------------------------------------------------------------------
# Monotonicity: more peaked = higher factor
# ---------------------------------------------------------------------------


def test_monotone_in_entropy_3_state():
    """Factor is monotonically decreasing in entropy (more peaked -> higher factor)."""
    posteriors = [
        np.array([0.97, 0.02, 0.01]),   # very peaked
        np.array([0.8, 0.15, 0.05]),    # moderately peaked
        np.array([0.6, 0.25, 0.15]),    # weakly peaked
        np.array([0.4, 0.35, 0.25]),    # near-uniform
        np.array([1.0 / 3, 1.0 / 3, 1.0 / 3]),  # uniform
    ]
    factors = [compute_regime_factor(p) for p in posteriors]
    for i in range(len(factors) - 1):
        assert factors[i] >= factors[i + 1] - 1e-12, (
            f"expected factor[{i}]={factors[i]:.6f} >= factor[{i+1}]={factors[i+1]:.6f}"
        )


# ---------------------------------------------------------------------------
# Fail-CLOSED: NaN / inf -> 0.0 (silence)
# ---------------------------------------------------------------------------


def test_nan_posterior_returns_silence():
    """NaN in posterior -> factor = 0.0 (fail-CLOSED)."""
    p = np.array([0.5, float("nan"), 0.3], dtype=float)
    assert compute_regime_factor(p) == _DEGENERATE_FACTOR


def test_inf_posterior_returns_silence():
    """Inf in posterior -> factor = 0.0 (fail-CLOSED)."""
    p = np.array([0.5, float("inf"), 0.3], dtype=float)
    assert compute_regime_factor(p) == _DEGENERATE_FACTOR


def test_negative_inf_posterior_returns_silence():
    """-Inf in posterior -> factor = 0.0 (fail-CLOSED)."""
    p = np.array([0.5, float("-inf"), 0.3], dtype=float)
    assert compute_regime_factor(p) == _DEGENERATE_FACTOR


def test_all_nan_posterior_returns_silence():
    """All-NaN posterior -> factor = 0.0 (fail-CLOSED)."""
    p = np.full(3, float("nan"))
    assert compute_regime_factor(p) == _DEGENERATE_FACTOR


# ---------------------------------------------------------------------------
# Fail-CLOSED: degenerate inputs -> 0.0 (silence)
# ---------------------------------------------------------------------------


def test_all_zero_posterior_returns_silence():
    """All-zero posterior (cannot normalize) -> 0.0."""
    p = np.zeros(3, dtype=float)
    assert compute_regime_factor(p) == _DEGENERATE_FACTOR


def test_empty_array_returns_silence():
    """Empty array -> 0.0."""
    p = np.array([], dtype=float)
    assert compute_regime_factor(p) == _DEGENERATE_FACTOR


def test_negative_mass_returns_silence():
    """Any negative probability mass -> 0.0 (not a valid distribution)."""
    p = np.array([0.6, -0.1, 0.5], dtype=float)
    assert compute_regime_factor(p) == _DEGENERATE_FACTOR


def test_single_state_finite_returns_one():
    """A 1-state posterior has zero ambiguity -> factor = 1.0."""
    assert math.isclose(compute_regime_factor(np.array([1.0])), 1.0, abs_tol=1e-9)
    assert math.isclose(compute_regime_factor(np.array([42.0])), 1.0, abs_tol=1e-9)


def test_single_state_zero_returns_silence():
    """A 1-state posterior of [0.0] cannot be normalized -> 0.0."""
    assert compute_regime_factor(np.array([0.0])) == _DEGENERATE_FACTOR


# ---------------------------------------------------------------------------
# Hard-label helper
# ---------------------------------------------------------------------------


def test_hard_label_one_hot_returns_one():
    """regime_factor_from_hard_label: hard label -> factor = 1.0."""
    assert math.isclose(regime_factor_from_hard_label(3, 0), 1.0, abs_tol=1e-9)
    assert math.isclose(regime_factor_from_hard_label(3, 2), 1.0, abs_tol=1e-9)
    assert math.isclose(regime_factor_from_hard_label(1, 0), 1.0, abs_tol=1e-9)


def test_hard_label_invalid_state_index_returns_silence():
    """regime_factor_from_hard_label: invalid state_index -> 0.0."""
    assert regime_factor_from_hard_label(3, 3) == _DEGENERATE_FACTOR   # out of range
    assert regime_factor_from_hard_label(3, -1) == _DEGENERATE_FACTOR  # negative


def test_hard_label_invalid_n_states_returns_silence():
    """regime_factor_from_hard_label: n_states < 1 -> 0.0."""
    assert regime_factor_from_hard_label(0, 0) == _DEGENERATE_FACTOR


# ---------------------------------------------------------------------------
# Output range invariant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "posterior",
    [
        np.array([1.0, 0.0, 0.0]),
        np.array([0.5, 0.5]),
        np.array([0.3, 0.3, 0.4]),
        np.array([0.25, 0.25, 0.25, 0.25]),
        np.array([0.9, 0.05, 0.05]),
        np.ones(10) / 10.0,
    ],
)
def test_factor_in_unit_interval(posterior):
    """For any valid input, factor is always in [0, 1]."""
    factor = compute_regime_factor(posterior)
    assert 0.0 <= factor <= 1.0, f"factor={factor} out of [0,1] for {posterior}"


# ---------------------------------------------------------------------------
# RED-proof reversal: verify the test actually measures the module
# ---------------------------------------------------------------------------


def test_peaked_vs_uniform_ordering():
    """Peaked posterior has strictly higher factor than uniform (main RED proof).

    This is the core behavior test: if compute_regime_factor always returned 0.5
    (a constant), this test would fail.
    """
    peaked = compute_regime_factor(np.array([0.95, 0.03, 0.02]))
    uniform = compute_regime_factor(np.array([1.0 / 3, 1.0 / 3, 1.0 / 3]))
    assert peaked > uniform + 0.5, (
        f"peaked={peaked:.4f} should be >> uniform={uniform:.4f}; "
        "a correct 1-H/Hmax implementation should show this gap"
    )
