"""ADR-0099 Part A — tranche scale-out + trailing-stop decision cores (tp1/tp2).

The operator's explicit ask: the trailing stop must NOT liquidate early — it ARMS only
after a profit cushion. These tests pin that property plus the +1R/+2R scale-out and the
fail-closed guards.
"""
from __future__ import annotations

import math

import pytest

from hermes_quant.risk.exit_strategy import (
    DEFAULT_TRAIL_ACTIVATION_GAIN,
    DEFAULT_TRAIL_DISTANCE,
    TRANCHE_RUNG,
    evaluate_tranche,
)

STOP = 0.08  # 1R = 8%; +1R = 8%, +2R = 16%


def _ev(**kw):
    base = dict(symbol="X", held_fraction=0.10, tranches_taken=0, stop_pct=STOP)
    base.update(kw)
    return evaluate_tranche(**base)


# --------------------------------------------------------------------------- #
# Tranche 1 at +1R (exit one rung, move residual stop to breakeven).
# --------------------------------------------------------------------------- #
def test_tranche1_fires_at_1R():
    d = _ev(gain_pct=0.08, tranches_taken=0)  # exactly +1R
    assert d.action == "tranche_1"
    assert d.exit_fraction == pytest.approx(TRANCHE_RUNG)  # 0.05 rung
    assert d.move_stop_to_breakeven is True


def test_tranche1_holds_below_1R():
    d = _ev(gain_pct=0.05, tranches_taken=0)  # +5% < +8%
    assert d.action == "hold"
    assert d.exit_fraction == 0.0


def test_tranche1_rung_capped_at_held():
    # A 0.03 position can't exit a 0.05 rung — cap at what's held.
    d = _ev(gain_pct=0.10, held_fraction=0.03, tranches_taken=0)
    assert d.action == "tranche_1"
    assert d.exit_fraction == pytest.approx(0.03)


# --------------------------------------------------------------------------- #
# Tranche 2 at +2R (exit the residual).
# --------------------------------------------------------------------------- #
def test_tranche2_fires_at_2R():
    d = _ev(gain_pct=0.16, held_fraction=0.05, tranches_taken=1)  # +2R, residual 0.05
    assert d.action == "tranche_2"
    assert d.exit_fraction == pytest.approx(0.05)  # all of the residual


def test_residual_holds_between_1R_and_2R_without_trail():
    # +12% (between +1R and +2R), peak == gain (no pullback) -> hold the residual.
    d = _ev(gain_pct=0.12, held_fraction=0.05, tranches_taken=1, peak_gain_pct=0.12)
    assert d.action == "hold"


# --------------------------------------------------------------------------- #
# Trailing stop — the no-early-liquidation property (the operator's ask).
# --------------------------------------------------------------------------- #
def test_trailing_does_NOT_arm_below_activation():
    # Isolate the ACTIVATION gate: a tiny trail_distance (1%) so the pullback alone WOULD
    # trigger, but the peak (+2%) is below the +3% activation cushion. Only the cushion
    # holds it; remove the cushion and this trail_exits. (gain +1% positive so it passes the
    # losing-position guard; peak +2% < +3% activation; pullback 1% >= 1% distance.)
    d = _ev(
        gain_pct=0.01, held_fraction=0.05, tranches_taken=1,
        peak_gain_pct=0.02, trail_distance=0.01,
    )
    assert d.action == "hold", (
        "trailing stop must NOT arm below the +3% activation cushion even when the pullback "
        "exceeds the trail distance — the operator's no-early-liquidation requirement"
    )


def test_trailing_fires_after_armed_and_pullback():
    # Peak +12% (armed), gain pulled back to +5% => pullback 7% >= 6% trail -> exit.
    d = _ev(gain_pct=0.05, held_fraction=0.05, tranches_taken=1, peak_gain_pct=0.12)
    assert d.action == "trail_exit"
    assert d.exit_fraction == pytest.approx(0.05)


def test_trailing_holds_when_pullback_within_distance():
    # Peak +10% (armed), gain +6% => pullback 4% < 6% trail -> hold.
    d = _ev(gain_pct=0.06, held_fraction=0.05, tranches_taken=1, peak_gain_pct=0.10)
    assert d.action == "hold"


def test_activation_uses_peak_not_current():
    # Current gain tiny (+0.5%) but peak was +10% (armed) and pullback 9.5% >= 6% -> exit.
    d = _ev(gain_pct=0.005, held_fraction=0.05, tranches_taken=1, peak_gain_pct=0.10)
    assert d.action == "trail_exit"


# --------------------------------------------------------------------------- #
# Fail-closed guards.
# --------------------------------------------------------------------------- #
def test_losing_position_holds():
    d = _ev(gain_pct=-0.05, tranches_taken=0)  # a loser never scales out (the stop handles it)
    assert d.action == "hold"


def test_flat_position_holds():
    d = _ev(gain_pct=0.10, held_fraction=0.0, tranches_taken=0)
    assert d.action == "hold"


def test_all_tranches_taken_holds():
    d = _ev(gain_pct=0.30, held_fraction=0.0, tranches_taken=2)
    assert d.action == "hold"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_gain_holds(bad):
    d = _ev(gain_pct=bad, tranches_taken=0)
    assert d.action == "hold" and d.exit_fraction == 0.0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -0.08])
def test_nonfinite_or_nonpositive_stop_holds(bad):
    d = _ev(gain_pct=0.20, tranches_taken=0, stop_pct=bad)
    assert d.action == "hold"


def test_defaults_are_documented_values():
    assert DEFAULT_TRAIL_ACTIVATION_GAIN == 0.03
    assert DEFAULT_TRAIL_DISTANCE == 0.06
    assert TRANCHE_RUNG == 0.05
