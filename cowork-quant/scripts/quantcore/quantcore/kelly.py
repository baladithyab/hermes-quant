"""quantcore.kelly — Kelly-fraction sizing with calibrated probability.

NOTE (port finding, 2026-06-09): hermes-quant's docstring examples claim
expected_log_return(0.5, 0.01) ~= 0; the EXACT formula its code (and ours)
implements gives -5.0e-05 (variance drag) — first-order values, never-run
doctests. We keep the exact formula and test against its TRUE values.

Verbatim math port of hermes_quant.risk.kelly (v0.6.4):

    edge = p*log(1+m) + (1-p)*log(1-m)    (exact for small m)
    edge = (2p - 1) * m                   (first-order fallback, m >= 1)

The Kelly sizer divides edge by sigma^2 (variance, NOT stdev). Quarter-Kelly
(0.25) is the conservative literature-consensus default. Both the cost gate
AND the sizer use the SAME expected_signed_edge (single source of truth).
"""

from __future__ import annotations

import math


def expected_log_return(probability: float, magnitude: float) -> float:
    """Expected log return of a directional bet.

    probability: calibrated P(directional correctness), in [0, 1].
    magnitude:   per-period absolute return fraction (0.012 = 1.2%).
    """
    if magnitude <= 0:
        return 0.0
    if magnitude >= 1:
        # Outside small-return regime; first-order avoids log(0)
        return (2 * probability - 1) * magnitude
    p, m = probability, magnitude
    if p <= 0:
        return math.log1p(-m)
    if p >= 1:
        return math.log1p(m)
    return p * math.log1p(m) + (1 - p) * math.log1p(-m)


def expected_signed_edge(direction: int, probability: float, magnitude: float) -> float:
    """Tradable edge in directional sign. Used by BOTH cost gate and sizer."""
    if direction == 0:
        return 0.0
    return float(direction) * expected_log_return(probability, abs(magnitude))


def round_to_step(value: float, step: float) -> float:
    """Snap a signed position fraction to the nearest discrete-ladder step."""
    if step <= 0:
        return value
    sign = 1.0 if value >= 0 else -1.0
    abs_val = abs(value)
    n_steps = round(abs_val / step)
    # round(...) kills float dust so ladder membership checks are exact
    return round(sign * n_steps * step, 10)


def quarter_kelly_size(
    edge: float,
    variance: float,
    *,
    quarter_kelly: float = 0.25,
    max_position_pct: float = 0.20,
    action_step: float = 0.05,
    direction: int = 1,
) -> float:
    """f* = quarter_kelly * (edge / variance), clipped to cap, snapped to step."""
    if direction == 0 or edge == 0.0:
        return 0.0
    if not math.isfinite(edge) or not math.isfinite(variance):
        return 0.0
    safe_var = max(variance, 1e-8)
    raw_size = quarter_kelly * (edge / safe_var)
    clipped = max(-max_position_pct, min(max_position_pct, raw_size))
    snapped = round_to_step(clipped, action_step)
    return max(-max_position_pct, min(max_position_pct, snapped))


def cost_gate_threshold(
    market_commission: float,
    market_spread: float,
    market_slippage: float,
    cost_multiple: float = 2.0,
) -> float:
    """Round-trip transaction cost x cost_multiple.

    round_trip = commission + 0.5*spread + slippage (spread/slippage are
    round-trip per hermes-quant convention; commission 1x for ADR-0004
    backwards-compat).
    """
    round_trip = market_commission + 0.5 * market_spread + market_slippage
    return cost_multiple * round_trip
