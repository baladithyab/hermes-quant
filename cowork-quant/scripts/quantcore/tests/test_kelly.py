"""Math fidelity vs hermes_quant.risk.kelly (values from its doctests)."""

from __future__ import annotations

from quantcore.kelly import (
    cost_gate_threshold,
    expected_log_return,
    expected_signed_edge,
    quarter_kelly_size,
    round_to_step,
)


def test_expected_log_return_variance_drag_at_half():
    # Exact formula: at p=0.5 the expected LOG return is slightly NEGATIVE
    v = expected_log_return(0.5, 0.01)
    assert -1e-4 < v < 0


def test_expected_log_return_doctest_value():
    # exact formula: p=0.6, m=0.01 -> 0.00195006 (hermes doctest said
    # 0.001990 = first-order value; those doctests never ran)
    assert abs(expected_log_return(0.6, 0.01) - 0.00195006) < 1e-7


def test_expected_log_return_edges():
    assert expected_log_return(0.7, 0.0) == 0.0
    assert expected_log_return(0.7, -0.1) == 0.0
    # m >= 1 falls back to first order
    assert expected_log_return(0.6, 1.5) == (2 * 0.6 - 1) * 1.5


def test_signed_edge_flat_is_zero():
    assert expected_signed_edge(0, 0.7, 0.01) == 0.0
    # p=0.5 long: small negative edge (variance drag) -> sign guard silences
    assert -1e-4 < expected_signed_edge(1, 0.5, 0.01) < 0


def test_signed_edge_negative_when_p_below_half():
    # Long signal with p<0.5: edge must be NEGATIVE (drives the sign guard)
    assert expected_signed_edge(1, 0.35, 0.01) < 0
    assert expected_signed_edge(-1, 0.35, 0.01) > 0


def test_round_to_step_ladder():
    assert round_to_step(0.07, 0.05) == 0.05
    assert round_to_step(0.08, 0.05) == 0.10
    assert round_to_step(-0.13, 0.05) == -0.15
    assert round_to_step(0.0, 0.05) == 0.0


def test_quarter_kelly_respects_cap_and_ladder():
    # Huge edge, tiny variance -> must clip to cap exactly
    size = quarter_kelly_size(edge=0.5, variance=1e-6, direction=1)
    assert size == 0.20
    size = quarter_kelly_size(edge=-0.5, variance=1e-6, direction=-1)
    assert size == -0.20


def test_quarter_kelly_zero_cases():
    assert quarter_kelly_size(edge=0.0, variance=0.01, direction=1) == 0.0
    assert quarter_kelly_size(edge=0.01, variance=0.01, direction=0) == 0.0
    assert quarter_kelly_size(edge=float("nan"), variance=0.01, direction=1) == 0.0


def test_cost_gate_threshold_formula():
    # round_trip = commission + 0.5*spread + slippage
    t = cost_gate_threshold(0.001, 0.002, 0.0005, cost_multiple=2.0)
    assert abs(t - 2.0 * (0.001 + 0.001 + 0.0005)) < 1e-12
