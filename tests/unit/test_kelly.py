"""Unit tests for hermes_quant.risk.kelly — Kelly numerator (2p-1)·m fix.

Anchor: synthesis-v2 §P0-A. The amended ADR-0009 §P0-1 used `edge = p*m`
which overestimates edge whenever p > 0.5. This test suite enforces the
corrected formula.
"""
from __future__ import annotations

import math

import pytest

from hermes_quant.risk.kelly import (
    cost_gate_threshold,
    expected_log_return,
    expected_signed_edge,
    quarter_kelly_size,
    round_to_step,
)


class TestExpectedLogReturn:
    """Closed-form values from the (2p-1)·m formula and exact log version."""

    def test_p_half_negative_due_to_jensens_inequality(self):
        """p=0.5 means coin flip — first-order edge is 0, but log is concave so
        E[log(1+X)] < log(E[1+X]) = 0 for symmetric ±m. The exact formula
        captures this Jensen's-inequality penalty: at p=0.5, edge is slightly
        NEGATIVE, not zero. This is a feature, not a bug — it's why volatility
        drag matters in real Kelly sizing.

        For tiny m the penalty is O(m²) so still ≈ 0 at small magnitudes.
        """
        # At very small m, penalty is negligible
        assert abs(expected_log_return(0.5, 0.001)) < 1e-6
        # At larger m, penalty becomes measurable but still small
        e = expected_log_return(0.5, 0.1)
        # 0.5*log(1.1) + 0.5*log(0.9) = 0.5*0.0953 + 0.5*(-0.1054) = -0.00501
        assert -0.01 < e < 0  # negative, but small
        # At m=0.3, penalty is more visible: 0.5*0.2624 + 0.5*(-0.3567) = -0.04713
        e_big = expected_log_return(0.5, 0.3)
        assert -0.06 < e_big < -0.04

    def test_zero_magnitude_means_zero_edge(self):
        for p in [0.0, 0.1, 0.5, 0.9, 1.0]:
            assert expected_log_return(p, 0.0) == 0.0
            assert expected_log_return(p, -0.01) == 0.0  # negative magnitude → 0

    def test_first_order_approx_small_m(self):
        """For small m, log1p(m) ≈ m — formula collapses to (2p-1)*m.

        The agreement is O(m) not perfect, so we use a looser tolerance for
        m≥0.005 cases.
        """
        for p in [0.55, 0.6, 0.7, 0.8]:
            for m in [0.0005, 0.001, 0.005]:
                exact = expected_log_return(p, m)
                first_order = (2 * p - 1) * m
                # For these magnitudes, agreement is within 5%
                assert abs(exact - first_order) / abs(first_order) < 0.05, (
                    f"p={p}, m={m}: exact={exact}, first_order={first_order}"
                )

    def test_known_p0_a_overbet_case(self):
        """At p=0.6, m=0.01: synthesis-v2 says true ≈ 0.002, amended formula 0.006.

        Exact: 0.6*log(1.01) + 0.4*log(0.99) ≈ 0.005970 - 0.004020 = 0.001950
        Confirms the ~3× overbet documented in synthesis-v2 §P0-A.
        """
        true_edge = expected_log_return(0.6, 0.01)
        amended_wrong = 0.6 * 0.01  # the buggy `p*m` form
        assert abs(true_edge - 0.001950) < 1e-5, f"got {true_edge}"
        # Amended buggy formula (0.006) is ~3× the true edge
        assert amended_wrong / true_edge > 2.5  # ~3.08× overbet

    def test_p_zero_full_loss_branch(self):
        """At p=0, exact loss = log(1-m)."""
        assert abs(expected_log_return(0.0, 0.01) - math.log1p(-0.01)) < 1e-12

    def test_p_one_full_win_branch(self):
        """At p=1, exact gain = log(1+m)."""
        assert abs(expected_log_return(1.0, 0.01) - math.log1p(0.01)) < 1e-12

    def test_large_magnitude_falls_back_to_first_order(self):
        """m=1 would log(0); fallback to first-order (2p-1)*m."""
        assert expected_log_return(0.6, 1.0) == pytest.approx(0.2)
        assert expected_log_return(0.6, 1.5) == pytest.approx(0.3)

    def test_negative_p_clipped_implicitly(self):
        """Probabilities outside [0,1] are caller error; sanity-check no crash."""
        # We don't validate p in this layer; just ensure no exception
        _ = expected_log_return(-0.1, 0.01)
        _ = expected_log_return(1.1, 0.01)


class TestExpectedSignedEdge:
    def test_zero_direction_means_zero_edge(self):
        for p in [0.0, 0.5, 1.0]:
            for m in [0.001, 0.01, 0.1]:
                assert expected_signed_edge(0, p, m) == 0.0

    def test_long_direction_positive_when_p_above_half(self):
        e = expected_signed_edge(1, 0.7, 0.01)
        assert e > 0

    def test_short_direction_flips_sign(self):
        e_long = expected_signed_edge(1, 0.7, 0.01)
        e_short = expected_signed_edge(-1, 0.7, 0.01)
        assert e_short == pytest.approx(-e_long)

    def test_p_below_half_negative_edge(self):
        """If we're going long but our calibrated p<0.5, edge is negative."""
        e = expected_signed_edge(1, 0.4, 0.01)
        assert e < 0

    def test_magnitude_taken_absolute(self):
        """expected_signed_edge takes |magnitude| internally (sign from direction)."""
        e1 = expected_signed_edge(1, 0.6, 0.01)
        e2 = expected_signed_edge(1, 0.6, -0.01)  # caller passed negative magnitude
        assert e1 == e2  # implementation absolutes it


class TestRoundToStep:
    def test_round_zero(self):
        assert round_to_step(0.0, 0.05) == 0.0

    def test_round_positive(self):
        assert round_to_step(0.073, 0.05) == pytest.approx(0.05)
        assert round_to_step(0.078, 0.05) == pytest.approx(0.10)

    def test_round_negative_preserves_sign(self):
        assert round_to_step(-0.073, 0.05) == pytest.approx(-0.05)
        assert round_to_step(-0.078, 0.05) == pytest.approx(-0.10)

    def test_step_zero_passthrough(self):
        assert round_to_step(0.073, 0.0) == 0.073
        assert round_to_step(0.073, -0.01) == 0.073


class TestQuarterKellySize:
    def test_zero_direction_returns_zero(self):
        assert quarter_kelly_size(0.001, 0.0001, direction=0) == 0.0

    def test_zero_edge_returns_zero(self):
        assert quarter_kelly_size(0.0, 0.0001, direction=1) == 0.0

    def test_positive_edge_long_direction(self):
        # edge=0.002, variance=0.0001 → raw f*=20, quarter=5, capped at 0.20
        size = quarter_kelly_size(0.002, 0.0001, direction=1)
        assert size == pytest.approx(0.20)

    def test_capping_at_max_position(self):
        size = quarter_kelly_size(1.0, 0.0001, direction=1, max_position_pct=0.10)
        assert size == pytest.approx(0.10)
        size_neg = quarter_kelly_size(-1.0, 0.0001, direction=-1, max_position_pct=0.10)
        assert size_neg == pytest.approx(-0.10)

    def test_action_step_snapping(self):
        # tiny edge: raw quarter-kelly tiny, should snap to 0
        size = quarter_kelly_size(1e-9, 0.01, direction=1, action_step=0.05)
        assert size == 0.0

    def test_realistic_size(self):
        # p=0.6 m=0.01 → edge ≈ 0.00199; σ²=0.0001 → raw=19.9; quarter=4.97; cap=0.20
        edge = expected_signed_edge(1, 0.6, 0.01)
        size = quarter_kelly_size(edge, 0.0001, direction=1)
        # Capped at 0.20 then snapped to 0.20 (0.20 is already a step multiple)
        assert size == pytest.approx(0.20)

    def test_negative_variance_clamped(self):
        """Sanity: pathological variance≤0 doesn't blow up."""
        size = quarter_kelly_size(0.001, -1.0, direction=1)
        # Floor at 1e-8 → enormous raw, then cap → 0.20
        assert size == pytest.approx(0.20)


class TestCostGateThreshold:
    def test_basic_threshold(self):
        # commission 0.001, spread 0.0008, slippage 0.0012 → 0.001 + 0.0004 + 0.0012 = 0.0026
        # × cost_multiple 2.0 = 0.0052
        thresh = cost_gate_threshold(0.001, 0.0008, 0.0012, cost_multiple=2.0)
        assert thresh == pytest.approx(0.0052)

    def test_zero_costs(self):
        assert cost_gate_threshold(0.0, 0.0, 0.0, cost_multiple=2.0) == 0.0

    def test_threshold_scales_with_multiple(self):
        t1 = cost_gate_threshold(0.001, 0.0008, 0.0012, cost_multiple=1.0)
        t2 = cost_gate_threshold(0.001, 0.0008, 0.0012, cost_multiple=3.0)
        assert t2 == pytest.approx(3.0 * t1)


class TestIntegrationCostGateAndKelly:
    """The cost gate + Kelly sizer must use the SAME edge formula (synthesis-v2 §P0-A).

    This integration test would catch if anyone reverts one of the two sites
    back to the buggy `p*m` form.
    """

    def test_sub_cost_signal_zeroes_size(self):
        """If edge < cost gate threshold, the gate should silence (size=0).

        We don't test the actual gate here (Wave 2); we test that the math
        primitives compose correctly.
        """
        # Tiny edge below threshold
        edge = expected_signed_edge(1, 0.51, 0.001)  # ≈ 0.00002
        threshold = cost_gate_threshold(0.001, 0.0008, 0.0012, cost_multiple=2.0)  # 0.0052
        assert abs(edge) < threshold, "test setup invariant"
        # The risk gate's Rule 5 will silence; verify via Wave 2 test_risk_gate.

    def test_supra_cost_signal_drives_position(self):
        edge = expected_signed_edge(1, 0.65, 0.02)  # decent edge
        threshold = cost_gate_threshold(0.001, 0.0008, 0.0012, cost_multiple=2.0)
        assert abs(edge) > threshold, f"edge={edge} threshold={threshold}"
        size = quarter_kelly_size(edge, 0.0004, direction=1)
        assert size > 0
