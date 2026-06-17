"""hermes_quant.risk.exit_strategy — tranche + trailing exit decision cores (ADR-0099 Part A).

These are the PURE decision cores for the leg-level equity exit STRATEGIES that build ON
the all-at-once take-profit already enforced (per_position_stop.evaluate_take_profit). Per
ADR-0099:

  * TRANCHE scale-out: exit ONE 0.05 NAV-fraction ladder rung at +1R, move the hard stop to
    BREAKEVEN on the residual, exit the residual at +2R OR when the trailing stop fires.
    "R" = the risk distance = the stop threshold (so +1R gain == stop_pct, +2R == 2*stop_pct;
    this is why the 16% TP default is +2R against the 8% stop — they compose).
  * TRAILING stop: a chandelier/ATR-style ratchet that ACTIVATES ONLY after a profit cushion
    (+activation_gain, default +3%) so it does NOT liquidate early (the operator's explicit
    ask), then trails the peak by trail_distance; once the pullback from the peak exceeds
    the distance, take the residual.

POSTURE: PURE + stateless GIVEN the inputs. The caller (the tick's monitor) supplies the
position's current gain, its prior peak gain, and how many tranches are already taken — this
module returns the next action. State PERSISTENCE (peak gain, tranches taken) lives in the
watched-position registry (aegis-eq3), NOT here. Every numeric input is finite-guarded
(NaN/inf -> HOLD, never a fabricated exit). All thresholds are EVAL-GATE-PENDING starting
points (ADR-0099 open_calibrations); the ranges here are conservative upward adjustments off
crypto-derived literature (Li et al. 2026) that is too tight for 30-min equity bars.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

# Tranche R-multiples (gain thresholds expressed as multiples of the stop = R).
TRANCHE_1_R = 1.0   # exit the first rung at +1R
TRANCHE_2_R = 2.0   # exit the residual at +2R (== the 16% TP default vs an 8% stop)
TRANCHE_RUNG = 0.05  # one discrete NAV-fraction ladder rung per tranche step (never fractional)

# Trailing-stop params (EVAL-GATE-PENDING; conservative vs Li et al. 2026 crypto values).
DEFAULT_TRAIL_ACTIVATION_GAIN = 0.03  # the trail ARMS only after +3% gain (no early liquidation)
DEFAULT_TRAIL_DISTANCE = 0.06         # trail the peak by 6% (full); tighten on the residual


ExitAction = Literal["hold", "tranche_1", "tranche_2", "trail_exit"]


@dataclass(frozen=True)
class TrancheDecision:
    """The next tranche/trailing action for ONE open winning position.

    ``action`` is one of: hold | tranche_1 (exit one rung, move residual stop to BE) |
    tranche_2 (exit the residual at +2R) | trail_exit (the trailing stop fired on the
    residual). ``exit_fraction`` is how much NAV-fraction to close NOW (0 for hold).
    ``move_stop_to_breakeven`` signals the caller to reseat the residual's hard stop at
    entry. None-computable inputs -> hold (silence-by-default).
    """

    symbol: str
    action: ExitAction
    exit_fraction: float
    move_stop_to_breakeven: bool
    reason: str


def _finite(*xs: float) -> bool:
    return all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in xs)


def evaluate_tranche(
    *,
    symbol: str,
    gain_pct: float,
    held_fraction: float,
    tranches_taken: int,
    stop_pct: float,
    peak_gain_pct: float | None = None,
    trail_activation_gain: float = DEFAULT_TRAIL_ACTIVATION_GAIN,
    trail_distance: float = DEFAULT_TRAIL_DISTANCE,
) -> TrancheDecision:
    """Decide the next scale-out / trailing action for an open WINNING position.

    Args:
      gain_pct: current unrealized gain as a positive fraction (use -loss from the stop
        primitive so the sign convention matches; a LOSING position has gain_pct < 0).
      held_fraction: current signed NAV-fraction held.
      tranches_taken: how many tranche steps already executed (0, 1, or 2). The registry
        (aegis-eq3) persists this across ticks; this core is stateless given it.
      stop_pct: the position's stop threshold = 1R (e.g. 0.08). +1R == stop_pct.
      peak_gain_pct: the highest gain_pct seen so far (for the trailing ratchet); None ==
        no peak recorded yet (treated as the current gain).

    Logic (ADR-0099 leg-level): tranche_1 at +1R (exit one rung, move residual stop to BE);
    after tranche_1, the residual exits at +2R (tranche_2) OR via the trailing stop
    (trail_exit) — whichever the gain path hits. The trailing stop ARMS only after
    +trail_activation_gain so it cannot liquidate early. Fail-closed: non-finite inputs,
    a losing/flat position, or all tranches taken -> hold.
    """
    if not _finite(gain_pct, held_fraction, stop_pct, trail_activation_gain, trail_distance):
        return TrancheDecision(symbol, "hold", 0.0, False, "non_finite_input -> HOLD")
    if not _finite(stop_pct) or stop_pct <= 0.0:
        return TrancheDecision(symbol, "hold", 0.0, False, "non_finite_or_nonpositive_stop -> HOLD")
    if held_fraction == 0.0 or gain_pct <= 0.0:
        return TrancheDecision(symbol, "hold", 0.0, False, "flat or losing position -> HOLD (no scale-out)")
    if tranches_taken >= 2:
        return TrancheDecision(symbol, "hold", 0.0, False, "all tranches taken -> HOLD")

    r1 = TRANCHE_1_R * abs(stop_pct)   # +1R gain
    r2 = TRANCHE_2_R * abs(stop_pct)   # +2R gain
    held_abs = abs(held_fraction)
    rung = min(TRANCHE_RUNG, held_abs)  # never exit more than is held

    # Tranche 1: first scale-out at +1R, move the residual stop to breakeven.
    if tranches_taken == 0:
        if gain_pct >= r1:
            return TrancheDecision(
                symbol, "tranche_1", rung, True,
                f"gain {gain_pct*100:.2f}% >= +1R ({r1*100:.2f}%): exit {rung:.2f} rung, residual stop -> breakeven",
            )
        return TrancheDecision(symbol, "hold", 0.0, False, f"gain {gain_pct*100:.2f}% < +1R ({r1*100:.2f}%) -> HOLD")

    # tranches_taken == 1: residual exits at +2R OR on the trailing stop.
    if gain_pct >= r2:
        return TrancheDecision(
            symbol, "tranche_2", held_abs, False,
            f"gain {gain_pct*100:.2f}% >= +2R ({r2*100:.2f}%): exit residual {held_abs:.2f}",
        )
    # Trailing stop on the residual — ARMS only after the activation cushion.
    peak = peak_gain_pct if (peak_gain_pct is not None and _finite(peak_gain_pct)) else gain_pct
    peak = max(peak, gain_pct)
    if peak >= abs(trail_activation_gain):
        pullback = peak - gain_pct  # how far we've given back from the peak
        if pullback >= abs(trail_distance):
            return TrancheDecision(
                symbol, "trail_exit", held_abs, False,
                f"trailing stop: peak {peak*100:.2f}% - gain {gain_pct*100:.2f}% = pullback "
                f"{pullback*100:.2f}% >= trail {abs(trail_distance)*100:.2f}% (armed at +{abs(trail_activation_gain)*100:.1f}%)",
            )
    return TrancheDecision(
        symbol, "hold", 0.0, False,
        f"residual held: gain {gain_pct*100:.2f}% < +2R, trailing not triggered (peak {peak*100:.2f}%)",
    )
