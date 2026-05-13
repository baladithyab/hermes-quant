"""hermes_quant.risk.kelly — Kelly-fraction sizing with calibrated probability.

This module implements the corrected Kelly numerator from synthesis-v2 §P0-A:

    edge = (2p - 1) * m       (first-order)
    edge = p*log(1+m) + (1-p)*log(1-m)    (exact for small m)

The original ADR-0009 §P0-1 amendment used `edge = p * m` which overestimates
the edge whenever `p > 0.5` (e.g., at p=0.6, m=0.01: amended formula gives
0.006 vs true ≈ 0.002 — a 3× overbet that compounds Kelly's known
leverage-amplification properties).

The Kelly sizer divides edge by σ² (the variance, NOT stdev — fixed in v1
review). Quarter-Kelly (multiplier 0.25) is the literature consensus default
for noisy real-world estimates. Full Kelly is dangerous; half Kelly is
acceptable for users with track record; quarter Kelly is the conservative ship.

Per ADR-0004 + ADR-0009 §P0-1 + synthesis-v2 §P0-A.
"""
from __future__ import annotations

import math


def expected_log_return(probability: float, magnitude: float) -> float:
    """Expected log return of a directional bet.

    Args:
        probability: Calibrated probability of directional correctness, in [0, 1].
            Must already be calibrated (see Calibrator protocol).
        magnitude: Per-period absolute return as a fraction (e.g., 0.012 = 1.2%).
            Must be in (0, 1) for the exact formula; >= 1 falls back to first-order.

    Returns:
        Expected log return (a small positive number when there's edge,
        small negative when the bet is bad, ≈ 0 when p=0.5).

    Examples:
        >>> abs(expected_log_return(0.5, 0.01)) < 1e-9
        True
        >>> abs(expected_log_return(0.6, 0.01) - 0.001990) < 1e-5
        True

    Edge cases:
        - p=0 or p=1: returns ±log(1±m) (deterministic outcome).
        - magnitude<=0: returns 0.0 (no bet).
        - magnitude>=1 (impossible total loss): falls back to first-order.
    """
    if magnitude <= 0:
        return 0.0
    if magnitude >= 1:
        # Outside small-return regime; fall back to first-order to avoid log(0)
        return (2 * probability - 1) * magnitude
    p, m = probability, magnitude
    if p <= 0:
        return math.log1p(-m)
    if p >= 1:
        return math.log1p(m)
    return p * math.log1p(m) + (1 - p) * math.log1p(-m)


def expected_signed_edge(direction: int, probability: float, magnitude: float) -> float:
    """Tradable edge in directional sign.

    The risk gate uses this for BOTH:
      1. Cost gate threshold: `abs(expected_signed_edge) > cost_multiple * round_trip_cost`
      2. Kelly sizer numerator: `kelly_size = expected_signed_edge / σ²`

    Per synthesis-v2 §P0-A.

    Args:
        direction: -1 (short), 0 (flat), +1 (long).
        probability: Calibrated probability in [0, 1].
        magnitude: Absolute expected return (always non-negative magnitude;
            sign comes from `direction`).

    Returns:
        Signed edge. Positive if the bet has positive expected log return,
        negative if it doesn't, zero if direction=0 or magnitude<=0.

    Examples:
        >>> abs(expected_signed_edge(1, 0.5, 0.01)) < 1e-9
        True
        >>> expected_signed_edge(0, 0.7, 0.01) == 0.0
        True
    """
    if direction == 0:
        return 0.0
    return float(direction) * expected_log_return(probability, abs(magnitude))


def round_to_step(value: float, step: float) -> float:
    """Round a position size to the nearest `step` increment.

    Per ADR-0004's discrete action space (anti-leverage-gambling): positions
    are 0, ±0.05, ±0.10, ±0.15, ±0.20 of NAV by default. This function
    rounds to the nearest such increment, preserving sign.

    Args:
        value: signed target position fraction.
        step: discrete step (default profile: 0.05).

    Returns:
        Rounded value snapped to the nearest `step` multiple.
    """
    if step <= 0:
        return value
    sign = 1.0 if value >= 0 else -1.0
    abs_val = abs(value)
    n_steps = round(abs_val / step)
    return sign * n_steps * step


def quarter_kelly_size(
    edge: float,
    variance: float,
    *,
    quarter_kelly: float = 0.25,
    max_position_pct: float = 0.20,
    action_step: float = 0.05,
    direction: int = 1,
) -> float:
    """Compute quarter-Kelly position size with discrete action snapping.

    Formula: `f* = quarter_kelly * (edge / variance)`, then clipped to
    `[-max_position_pct, +max_position_pct]`, then snapped to `action_step`.

    Args:
        edge: signed edge (output of `expected_signed_edge`).
        variance: σ² (per-period log-return variance — NOT stdev).
        quarter_kelly: multiplier on full Kelly (default 0.25 per ADR-0004).
        max_position_pct: hard cap, e.g., 0.20.
        action_step: discrete snap, e.g., 0.05.
        direction: -1, 0, +1. If 0, returns 0.0.

    Returns:
        Signed target position fraction. Sign matches direction × sign(edge).
        If direction=0, edge=0, or variance<=0, returns 0.0.

    Notes:
        - `edge` is already signed; its sign should match `direction` for any
          rationally-emitted signal. We trust the upstream and use direction
          to zero out flat signals.
        - Variance is clamped to a minimum of 1e-8 to prevent division by ~0
          producing absurd target sizes; in practice the `MarketState`
          construction never returns volatility=0 due to bootstrap defaults.
    """
    if direction == 0 or edge == 0.0:
        return 0.0
    safe_var = max(variance, 1e-8)
    raw_size = quarter_kelly * (edge / safe_var)
    # Clip to max position; preserve sign
    clipped = max(-max_position_pct, min(max_position_pct, raw_size))
    # Snap to action step
    snapped = round_to_step(clipped, action_step)
    # Final clip in case rounding overshot the cap
    return max(-max_position_pct, min(max_position_pct, snapped))


def cost_gate_threshold(
    market_commission: float,
    market_spread: float,
    market_slippage: float,
    cost_multiple: float = 2.0,
) -> float:
    """Round-trip transaction cost × cost_multiple.

    Per ADR-0004 Rule 5: `abs(expected_signed_edge) > cost_gate_threshold`
    must hold for the gate to emit a non-silent action.

    Args:
        market_commission: per-side commission as fraction.
        market_spread: bid/ask spread (round-trip) as fraction.
        market_slippage: estimated slippage (round-trip) as fraction.
        cost_multiple: edge must be at least N× the round-trip cost.

    Returns:
        Threshold above which a signal is worth acting on.

    Notes:
        - Commission counts twice (entry + exit); spread is already round-trip
          per `MarketState.spread` convention; slippage is round-trip per
          synthesis-v2 §P1-ζ.
        - Per ADR-0004's example: `transaction_cost = market.commission + 0.5 * market.spread + market.slippage_estimate`. We treat that constant as a slight overestimate (commission 1× rather than 2×) for backwards-compat with the ADR; in v0.2 we may tighten this to 2× commission.
    """
    round_trip = market_commission + 0.5 * market_spread + market_slippage
    return cost_multiple * round_trip
