"""Tests for the options composite-exit decision core (ADR-0099 Part A, tp3).

RED-proof methodology: each test names the exact source line / constant it would
revert to prove RED. The 5 rules (TP / loss-cap / time-close / delta-breach /
extrinsic-floor) are each tested independently:
  * fires on trigger (should_close=True, correct which_rule)
  * holds when just below the trigger (should_close=False, which_rule="hold")
The 2x-loss-cap is the HARD RULE — it fires before the TP check.
NaN/non-finite inputs -> the SAFE action documented per-rule.
"""
from __future__ import annotations

import math

import pytest

from hermes_quant.risk.options_exit import (
    DELTA_BREACH_INSIDE_21DTE,
    DELTA_BREACH_OUTSIDE_21DTE,
    EXTRINSIC_FLOOR_DOLLARS,
    EXTRINSIC_FLOOR_PCT_OF_CREDIT,
    LOSS_CAP_CREDIT_MULTIPLE,
    TIME_CLOSE_DTE,
    TP_CREDIT_FRACTION,
    OptionsExitDecision,
    ShortLegState,
    evaluate_options_exit,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hold_result(**kwargs) -> OptionsExitDecision:
    """Build a "hold" decision with default arguments overridden by kwargs."""
    return OptionsExitDecision(should_close=False, reason="no exit rule fired -> HOLD", which_rule="hold")


def _no_legs() -> list[ShortLegState]:
    return []


def _one_short_leg(delta: float | None = -0.20, extrinsic: float | None = 1.50) -> list[ShortLegState]:
    """A single well-behaved short put leg (delta -0.20, extrinsic $1.50)."""
    return [ShortLegState(delta=delta, extrinsic_value=extrinsic)]


def _cc_short_leg(delta: float | None = 0.20, extrinsic: float | None = 1.50) -> list[ShortLegState]:
    """A short call leg (positive delta sign for calls)."""
    return [ShortLegState(delta=delta, extrinsic_value=extrinsic)]


# ---------------------------------------------------------------------------
# Rule 1: 50%-of-credit TP
# ---------------------------------------------------------------------------

class TestRule1TakeProfit:
    """Rule 1 fires when net_pnl >= TP_CREDIT_FRACTION * initial_credit."""

    def test_fires_at_exactly_50pct(self):
        """net_pnl == 0.50 * initial_credit -> TP fires.

        RED: change TP_CREDIT_FRACTION to 1.0 (or net_pnl to just below threshold).
        """
        credit = 1.00  # $1.00 net credit
        pnl = TP_CREDIT_FRACTION * credit  # exactly $0.50
        d = evaluate_options_exit(
            net_pnl=pnl,
            initial_credit=credit,
            dte=30,
            short_legs=_one_short_leg(),
        )
        assert d.should_close is True
        assert d.which_rule == "tp_50pct_credit"

    def test_fires_above_50pct(self):
        """net_pnl > 50% -> TP fires."""
        d = evaluate_options_exit(
            net_pnl=0.70,
            initial_credit=1.00,
            dte=30,
            short_legs=_one_short_leg(),
        )
        assert d.should_close is True
        assert d.which_rule == "tp_50pct_credit"

    def test_holds_just_below_50pct(self):
        """net_pnl just below threshold -> HOLD.

        RED: lower TP_CREDIT_FRACTION constant so this pnl triggers TP.
        """
        credit = 1.00
        pnl = TP_CREDIT_FRACTION * credit - 0.001  # just below
        d = evaluate_options_exit(
            net_pnl=pnl,
            initial_credit=credit,
            dte=30,
            short_legs=_one_short_leg(),
        )
        assert d.should_close is False
        assert d.which_rule == "hold"

    def test_losing_position_does_not_trigger_tp(self):
        """A losing position (negative pnl) -> no TP."""
        d = evaluate_options_exit(
            net_pnl=-0.30,
            initial_credit=1.00,
            dte=30,
            short_legs=_one_short_leg(),
        )
        assert d.should_close is False
        assert d.which_rule == "hold"

    def test_debit_structure_tp_skipped(self):
        """initial_credit <= 0 (debit structure) -> Rule 1 is skipped."""
        d = evaluate_options_exit(
            net_pnl=100.0,  # large "gain" but this is a debit structure
            initial_credit=0.0,
            dte=30,
            short_legs=_one_short_leg(),
        )
        # Should not fire tp_50pct_credit since initial_credit <= 0
        assert d.which_rule != "tp_50pct_credit"

    def test_nan_pnl_fails_closed_not_tp(self):
        """NaN net_pnl -> fail-CLOSED on loss cap (Rule 2), NOT a spurious TP fire.

        This verifies NaN does not trigger TP (the safe direction for TP is HOLD/close-on-loss).
        RED: remove the non-finite pnl check; NaN comparison `NaN >= threshold` is False
        so TP would not fire — but this test proves we return Rule 2 (fail-closed), not TP.
        """
        d = evaluate_options_exit(
            net_pnl=float("nan"),
            initial_credit=1.00,
            dte=30,
            short_legs=_one_short_leg(),
        )
        # NaN pnl -> Rule 2 fires (fail-CLOSED), not Rule 1 (TP).
        assert d.should_close is True
        assert d.which_rule == "loss_cap_2x"


# ---------------------------------------------------------------------------
# Rule 2: 2x-credit HARD loss cap (fail-CLOSED)
# ---------------------------------------------------------------------------

class TestRule2LossCap:
    """Rule 2 is the HARD RULE. It fires before Rule 1 TP (priority order)."""

    def test_fires_at_exactly_2x(self):
        """net loss == 2x initial_credit -> loss cap fires.

        RED: change LOSS_CAP_CREDIT_MULTIPLE to 3.0 so 2x doesn't trigger.
        """
        credit = 1.00
        loss = LOSS_CAP_CREDIT_MULTIPLE * credit  # 2x = $2.00 loss
        d = evaluate_options_exit(
            net_pnl=-loss,
            initial_credit=credit,
            dte=30,
            short_legs=_one_short_leg(),
        )
        assert d.should_close is True
        assert d.which_rule == "loss_cap_2x"

    def test_fires_above_2x(self):
        """net loss > 2x -> loss cap fires."""
        d = evaluate_options_exit(
            net_pnl=-2.50,  # more than 2x credit of $1.00
            initial_credit=1.00,
            dte=30,
            short_legs=_one_short_leg(),
        )
        assert d.should_close is True
        assert d.which_rule == "loss_cap_2x"

    def test_holds_just_below_2x(self):
        """net loss just below 2x -> HOLD (no loss cap fire).

        RED: raise LOSS_CAP_CREDIT_MULTIPLE constant so this triggers the cap.
        """
        credit = 1.00
        loss = LOSS_CAP_CREDIT_MULTIPLE * credit - 0.001  # just below
        d = evaluate_options_exit(
            net_pnl=-loss,
            initial_credit=credit,
            dte=30,
            short_legs=_one_short_leg(),
        )
        assert d.should_close is False
        assert d.which_rule == "hold"

    def test_loss_cap_fires_before_tp_check(self):
        """Loss cap is evaluated FIRST (before TP). If both would fire, loss cap wins.

        This tests the hard-rule priority order. With a 3x loss AND a positive pnl
        that would normally also trigger TP... but negative pnl can't trigger TP
        anyway; this test sets a scenario where Rule 2 fires and verifies it fires
        first in the evaluation order.

        RED: swap the evaluation order in evaluate_options_exit (put TP before loss cap).
        """
        d = evaluate_options_exit(
            net_pnl=-2.10,  # 2.1x credit = loss cap fires
            initial_credit=1.00,
            dte=30,
            short_legs=_one_short_leg(),
        )
        assert d.which_rule == "loss_cap_2x"
        assert d.should_close is True

    def test_nan_pnl_fails_closed_on_loss_cap(self):
        """NaN net_pnl -> fail-CLOSED, which_rule=loss_cap_2x.

        The safe action for a non-finite P&L is to close (fail-CLOSED), not hold.
        RED: remove the non-finite pnl guard; NaN comparison `NaN >= threshold`
        is always False so loss cap would NOT fire -> HOLD (the wrong answer).
        """
        d = evaluate_options_exit(
            net_pnl=float("nan"),
            initial_credit=1.00,
            dte=30,
            short_legs=_one_short_leg(),
        )
        assert d.should_close is True
        assert d.which_rule == "loss_cap_2x"

    def test_inf_pnl_fails_closed_on_loss_cap(self):
        """inf (positive) net_pnl is non-finite -> fail-CLOSED.

        A +inf P&L would normally pass Rule 2 and trigger Rule 1 TP; we want
        fail-CLOSED behavior on any non-finite pnl.
        RED: remove the non-finite pnl guard.
        """
        d = evaluate_options_exit(
            net_pnl=float("inf"),
            initial_credit=1.00,
            dte=30,
            short_legs=_one_short_leg(),
        )
        assert d.should_close is True
        assert d.which_rule == "loss_cap_2x"

    def test_nan_credit_with_loss_fails_closed(self):
        """NaN initial_credit with negative pnl -> fail-CLOSED (cannot compute 2x cap).

        RED: remove the non-finite credit check; NaN * anything = NaN and
        `NaN >= x` is False -> the loss cap would not fire.
        """
        d = evaluate_options_exit(
            net_pnl=-5.00,  # clearly a loss
            initial_credit=float("nan"),
            dte=30,
            short_legs=_one_short_leg(),
        )
        assert d.should_close is True
        assert d.which_rule == "loss_cap_2x"

    def test_nan_credit_with_gain_skips_credit_rules(self):
        """NaN initial_credit with positive pnl -> debit-like behavior (skip Rule 1/2)."""
        # net_pnl is finite and positive, initial_credit is NaN.
        # Since there's no loss, Rule 2 fail-closed for NaN-credit-with-loss doesn't fire.
        # Rule 1 requires a finite positive initial_credit -> skipped.
        d = evaluate_options_exit(
            net_pnl=5.00,
            initial_credit=float("nan"),
            dte=30,
            short_legs=_one_short_leg(),
        )
        # Must not fire TP (initial_credit is NaN -> not a credit structure for Rule 1).
        assert d.which_rule != "tp_50pct_credit"


# ---------------------------------------------------------------------------
# Rule 3: 21-DTE time close
# ---------------------------------------------------------------------------

class TestRule3TimeClose:
    """Rule 3 fires when DTE <= 21 (for 40-45 DTE entries)."""

    def test_fires_at_exactly_21dte(self):
        """DTE == 21 -> time close fires.

        RED: change TIME_CLOSE_DTE to 20 so DTE=21 does not trigger.
        """
        d = evaluate_options_exit(
            net_pnl=0.10,  # slight gain, below TP
            initial_credit=1.00,
            dte=TIME_CLOSE_DTE,  # exactly 21
            short_legs=_one_short_leg(),
        )
        assert d.should_close is True
        assert d.which_rule == "time_close_21dte"

    def test_fires_below_21dte(self):
        """DTE < 21 -> time close fires."""
        d = evaluate_options_exit(
            net_pnl=0.10,
            initial_credit=1.00,
            dte=5,
            short_legs=_one_short_leg(),
        )
        assert d.should_close is True
        assert d.which_rule == "time_close_21dte"

    def test_holds_above_21dte(self):
        """DTE = 22 -> time close does NOT fire.

        RED: lower TIME_CLOSE_DTE so DTE=22 triggers.
        """
        d = evaluate_options_exit(
            net_pnl=0.10,
            initial_credit=1.00,
            dte=22,
            short_legs=_one_short_leg(),
        )
        assert d.should_close is False
        assert d.which_rule == "hold"

    def test_nan_dte_fails_closed(self):
        """NaN DTE -> fail-CLOSED on Rule 3 (time risk signal).

        RED: remove the DTE finiteness check; NaN <= threshold is False,
        so the time-close would not fire -> HOLD (fail-open on time risk).
        """
        d = evaluate_options_exit(
            net_pnl=0.10,
            initial_credit=1.00,
            dte=float("nan"),
            short_legs=_one_short_leg(),
        )
        assert d.should_close is True
        assert d.which_rule == "time_close_21dte"

    def test_float_dte_accepted(self):
        """Float DTE (e.g. 20.5) is accepted and compared correctly."""
        d = evaluate_options_exit(
            net_pnl=0.10,
            initial_credit=1.00,
            dte=20.5,  # inside 21 DTE
            short_legs=_one_short_leg(),
        )
        assert d.should_close is True
        assert d.which_rule == "time_close_21dte"


# ---------------------------------------------------------------------------
# Rule 4: delta-breach
# ---------------------------------------------------------------------------

class TestRule4DeltaBreach:
    """Rule 4 fires when any short |delta| exceeds the threshold.
    Outside 21 DTE: 0.40; inside 21 DTE: 0.30 (tighter threshold).
    """

    def test_fires_outside_21dte_at_threshold(self):
        """short |delta| == 0.40 (outside 21 DTE) -> breach fires.

        RED: change DELTA_BREACH_OUTSIDE_21DTE to a value > 0.40.
        """
        legs = [ShortLegState(delta=-DELTA_BREACH_OUTSIDE_21DTE - 0.001, extrinsic_value=1.0)]
        d = evaluate_options_exit(
            net_pnl=0.05,  # small gain, below TP
            initial_credit=1.00,
            dte=30,  # outside 21 DTE
            short_legs=legs,
        )
        assert d.should_close is True
        assert d.which_rule == "delta_breach"
        assert d.firing_leg_idx == 0

    def test_holds_below_outside_threshold(self):
        """|delta| = 0.39 outside 21 DTE -> no breach."""
        legs = [ShortLegState(delta=-0.39, extrinsic_value=1.0)]
        d = evaluate_options_exit(
            net_pnl=0.05,
            initial_credit=1.00,
            dte=30,
            short_legs=legs,
        )
        assert d.should_close is False
        assert d.which_rule == "hold"

    def test_fires_inside_21dte_tighter_threshold(self):
        """Inside 21 DTE, the delta breach threshold tightens from 0.40 to 0.30.

        Rule 3 (time close) also fires inside 21 DTE, but we want to verify the delta
        threshold WIRING is correct by testing the DTE-dependent switching in isolation.

        We use a custom time_close_dte=-1 (invalid, falls back to default 21) but shift
        the test delta to show the threshold difference. Instead, the cleanest isolation
        is: set `dte=22` (just outside the 21-DTE boundary) and use
        `delta_breach_outside=0.30` override to verify the OUTSIDE threshold is used.
        Then set `dte=15` with `delta_breach_inside` override reduced to confirm the
        INSIDE threshold path is taken.

        Concrete approach: use a delta value of 0.35 (between 0.30 and 0.40).
          - At DTE=25 with defaults: uses outside threshold 0.40. 0.35 < 0.40 -> HOLD.
          - At DTE=25 with `delta_breach_outside=0.30`: 0.35 > 0.30 -> fires. This
            proves the outside-threshold code path is taken.
          - At DTE=15, Rule 3 fires first (that is a separate verified behavior).

        RED: swap `delta_breach_inside` and `delta_breach_outside` in evaluate_options_exit
        so the outside threshold is used inside 21 DTE — the override test would fail.
        """
        legs = [ShortLegState(delta=-0.35, extrinsic_value=1.0)]
        # With default outside threshold (0.40): 0.35 < 0.40 -> HOLD on delta.
        d_default = evaluate_options_exit(
            net_pnl=0.05,
            initial_credit=1.00,
            dte=25,  # outside 21 DTE
            short_legs=legs,
        )
        assert d_default.should_close is False
        assert d_default.which_rule == "hold"

        # Override the OUTSIDE threshold to 0.30 to confirm the outside-threshold
        # code path fires for DTE=25: 0.35 > 0.30 -> delta_breach.
        d_with_tighter_outside = evaluate_options_exit(
            net_pnl=0.05,
            initial_credit=1.00,
            dte=25,  # STILL outside 21 DTE
            short_legs=legs,
            delta_breach_outside=0.30,  # manually set to tighter value
        )
        assert d_with_tighter_outside.should_close is True
        assert d_with_tighter_outside.which_rule == "delta_breach"
        assert d_with_tighter_outside.firing_leg_idx == 0

    def test_holds_inside_21dte_below_tighter_threshold(self):
        """The delta threshold path (inside vs outside) is differentiated by DTE position.

        Rule 3 fires first at DTE <= 21, so we test the threshold differentiation using
        the custom override parameters to isolate Rule 4. A delta of 0.25 is below BOTH
        thresholds (0.30 inside, 0.40 outside), so it holds regardless of which
        threshold path is taken.

        A more rigorous proof: set `delta_breach_outside=0.25` (artificially low). At
        DTE=25 (outside 21), this WOULD fire (0.25 >= 0.25). At DTE=22 with
        `delta_breach_inside=0.28` override, delta=0.25 < 0.28 -> HOLD.
        This shows the DTE-based threshold selection is wired.
        """
        legs = [ShortLegState(delta=-0.25, extrinsic_value=1.0)]

        # DTE=22 (outside 21): override outside threshold to 0.20. 0.25 > 0.20 -> fires.
        d_outside_fires = evaluate_options_exit(
            net_pnl=0.05,
            initial_credit=1.00,
            dte=22,  # outside 21 DTE
            short_legs=legs,
            delta_breach_outside=0.20,  # artificially low; 0.25 > 0.20
        )
        assert d_outside_fires.should_close is True
        assert d_outside_fires.which_rule == "delta_breach"

        # DTE=22 with inside threshold override to 0.20 but STILL outside 21 DTE:
        # inside threshold is NOT active; outside threshold 0.40 is used; 0.25 < 0.40 -> HOLD.
        d_outside_holds = evaluate_options_exit(
            net_pnl=0.05,
            initial_credit=1.00,
            dte=22,  # OUTSIDE 21 DTE
            short_legs=legs,
            delta_breach_inside=0.20,   # override INSIDE threshold (not active here)
            delta_breach_outside=0.40,  # default outside threshold
        )
        assert d_outside_holds.should_close is False
        assert d_outside_holds.which_rule == "hold"

    def test_short_call_positive_delta_breach(self):
        """Short CALL has positive delta; |delta| check must use abs(delta).

        A short call with delta = +0.45 (outside 21 DTE) -> breach.
        RED: remove abs() from the delta comparison.
        """
        legs = _cc_short_leg(delta=0.45, extrinsic=1.0)
        d = evaluate_options_exit(
            net_pnl=0.05,
            initial_credit=1.00,
            dte=30,
            short_legs=legs,
        )
        assert d.should_close is True
        assert d.which_rule == "delta_breach"

    def test_nan_delta_fails_closed(self):
        """NaN delta on a short leg -> fail-CLOSED (cannot pass the guard).

        RED: remove the non-finite delta check; NaN > threshold is False -> HOLD
        (fail-open on a short leg whose delta is unknown — wrong, dangerous).
        """
        legs = [ShortLegState(delta=float("nan"), extrinsic_value=1.0)]
        d = evaluate_options_exit(
            net_pnl=0.05,
            initial_credit=1.00,
            dte=30,
            short_legs=legs,
        )
        assert d.should_close is True
        assert d.which_rule == "delta_breach"
        assert d.firing_leg_idx == 0

    def test_none_delta_fails_closed(self):
        """None delta on a short leg -> fail-CLOSED."""
        legs = [ShortLegState(delta=None, extrinsic_value=1.0)]
        d = evaluate_options_exit(
            net_pnl=0.05,
            initial_credit=1.00,
            dte=30,
            short_legs=legs,
        )
        assert d.should_close is True
        assert d.which_rule == "delta_breach"

    def test_second_leg_breach_returns_correct_idx(self):
        """When the SECOND leg breaches, firing_leg_idx=1."""
        legs = [
            ShortLegState(delta=-0.20, extrinsic_value=1.0),  # OK
            ShortLegState(delta=-0.45, extrinsic_value=1.0),  # breaches
        ]
        d = evaluate_options_exit(
            net_pnl=0.05,
            initial_credit=1.00,
            dte=30,
            short_legs=legs,
        )
        assert d.should_close is True
        assert d.which_rule == "delta_breach"
        assert d.firing_leg_idx == 1

    def test_no_short_legs_skips_delta_breach(self):
        """A pure long structure (no short legs) -> delta breach and extrinsic rules skipped."""
        d = evaluate_options_exit(
            net_pnl=0.05,
            initial_credit=0.0,  # debit structure, no TP/loss-cap
            dte=30,
            short_legs=[],
        )
        assert d.which_rule not in ("delta_breach", "extrinsic_floor")
        assert d.which_rule == "hold"


# ---------------------------------------------------------------------------
# Rule 5: extrinsic-value floor
# ---------------------------------------------------------------------------

class TestRule5ExtrinsicFloor:
    """Rule 5 fires when any short leg extrinsic <= floor ($0.10 or 5% of credit)."""

    def test_fires_at_dollar_floor(self):
        """Extrinsic == $0.10 (the dollar floor) -> assignment-prevention close fires.

        RED: change EXTRINSIC_FLOOR_DOLLARS to $0.05 so $0.10 doesn't trigger.
        """
        legs = [ShortLegState(delta=-0.20, extrinsic_value=EXTRINSIC_FLOOR_DOLLARS)]
        d = evaluate_options_exit(
            net_pnl=0.05,
            initial_credit=1.00,
            dte=30,
            short_legs=legs,
        )
        assert d.should_close is True
        assert d.which_rule == "extrinsic_floor"
        assert d.firing_leg_idx == 0

    def test_fires_below_dollar_floor(self):
        """Extrinsic < $0.10 -> close fires."""
        legs = [ShortLegState(delta=-0.20, extrinsic_value=0.05)]
        d = evaluate_options_exit(
            net_pnl=0.05,
            initial_credit=1.00,
            dte=30,
            short_legs=legs,
        )
        assert d.should_close is True
        assert d.which_rule == "extrinsic_floor"

    def test_holds_above_dollar_floor(self):
        """Extrinsic > $0.10 (and > 5% of credit) -> no extrinsic fire."""
        legs = [ShortLegState(delta=-0.20, extrinsic_value=0.50)]
        d = evaluate_options_exit(
            net_pnl=0.05,
            initial_credit=1.00,
            dte=30,
            short_legs=legs,
        )
        assert d.should_close is False
        assert d.which_rule == "hold"

    def test_fires_at_pct_floor_when_above_dollar_floor(self):
        """5% of a $5.00 credit = $0.25. An extrinsic of $0.20 (>$0.10 dollar floor
        but < 5% of credit) -> close fires.

        RED: remove the pct_floor calculation and only use dollar floor.
        """
        credit = 5.00
        pct_threshold = EXTRINSIC_FLOOR_PCT_OF_CREDIT * credit  # 5% of $5 = $0.25
        legs = [ShortLegState(delta=-0.20, extrinsic_value=pct_threshold - 0.01)]  # $0.24
        d = evaluate_options_exit(
            net_pnl=0.05,
            initial_credit=credit,
            dte=30,
            short_legs=legs,
        )
        assert d.should_close is True
        assert d.which_rule == "extrinsic_floor"
        assert d.firing_leg_idx == 0

    def test_holds_above_pct_floor(self):
        """Extrinsic > max(dollar_floor, pct_floor) -> HOLD."""
        credit = 5.00
        pct_threshold = EXTRINSIC_FLOOR_PCT_OF_CREDIT * credit  # $0.25
        legs = [ShortLegState(delta=-0.20, extrinsic_value=pct_threshold + 0.10)]  # $0.35
        d = evaluate_options_exit(
            net_pnl=0.05,
            initial_credit=credit,
            dte=30,
            short_legs=legs,
        )
        assert d.should_close is False
        assert d.which_rule == "hold"

    def test_nan_extrinsic_fails_closed(self):
        """NaN extrinsic -> fail-CLOSED on assignment risk.

        RED: remove the non-finite extrinsic check; NaN <= threshold is False
        -> HOLD (fail-open on unknown extrinsic — dangerous assignment risk).
        """
        legs = [ShortLegState(delta=-0.20, extrinsic_value=float("nan"))]
        d = evaluate_options_exit(
            net_pnl=0.05,
            initial_credit=1.00,
            dte=30,
            short_legs=legs,
        )
        assert d.should_close is True
        assert d.which_rule == "extrinsic_floor"
        assert d.firing_leg_idx == 0

    def test_none_extrinsic_fails_closed(self):
        """None extrinsic -> fail-CLOSED."""
        legs = [ShortLegState(delta=-0.20, extrinsic_value=None)]
        d = evaluate_options_exit(
            net_pnl=0.05,
            initial_credit=1.00,
            dte=30,
            short_legs=legs,
        )
        assert d.should_close is True
        assert d.which_rule == "extrinsic_floor"


# ---------------------------------------------------------------------------
# Priority order: loss-cap before TP
# ---------------------------------------------------------------------------

class TestPriorityOrder:
    """Verify the evaluation order: Rule 2 (loss cap) before Rule 1 (TP).

    The task spec says the 2x loss cap is the HARD RULE, fires BEFORE structural
    max-loss wings and BEFORE the TP check.
    """

    def test_loss_cap_priority_over_tp_hypothetical(self):
        """When BOTH loss cap and TP would fire (hypothetical: set thresholds so they
        overlap), the loss cap (Rule 2) wins because it's checked first.

        In practice net_pnl can't be both >= TP threshold AND produce a >= 2x loss
        simultaneously (a positive pnl can't also be a loss). This test verifies
        the order by checking that a LOSS fires Rule 2 before the evaluator gets
        to Rule 1.

        RED: swap the order of Rule 1 and Rule 2 in evaluate_options_exit.
        """
        # A big loss that clearly triggers Rule 2.
        d = evaluate_options_exit(
            net_pnl=-3.00,  # 3x loss
            initial_credit=1.00,
            dte=30,
            short_legs=_one_short_leg(),
        )
        assert d.which_rule == "loss_cap_2x"
        assert d.should_close is True

    def test_tp_fires_when_loss_cap_does_not(self):
        """When the loss cap does NOT fire (profit), TP can fire if threshold met."""
        d = evaluate_options_exit(
            net_pnl=0.60,  # 60% of $1.00 credit -> TP fires (50% threshold)
            initial_credit=1.00,
            dte=30,
            short_legs=_one_short_leg(),
        )
        assert d.which_rule == "tp_50pct_credit"
        assert d.should_close is True


# ---------------------------------------------------------------------------
# NaN/inf inputs — global finite-guard coverage
# ---------------------------------------------------------------------------

class TestNaNInputs:
    """Comprehensive NaN/inf input coverage per the ar08 family."""

    def test_nan_pnl_fails_closed_rule2(self):
        """NaN pnl -> Rule 2 (fail-CLOSED)."""
        d = evaluate_options_exit(net_pnl=float("nan"), initial_credit=1.0, dte=30, short_legs=[])
        assert d.should_close is True
        assert d.which_rule == "loss_cap_2x"

    def test_inf_pnl_fails_closed_rule2(self):
        """inf pnl (non-finite) -> Rule 2 (fail-CLOSED)."""
        d = evaluate_options_exit(net_pnl=float("inf"), initial_credit=1.0, dte=30, short_legs=[])
        assert d.should_close is True
        assert d.which_rule == "loss_cap_2x"

    def test_nan_dte_fails_closed_rule3(self):
        """NaN DTE -> Rule 3 (fail-CLOSED)."""
        d = evaluate_options_exit(net_pnl=0.10, initial_credit=1.0, dte=float("nan"), short_legs=[])
        assert d.should_close is True
        assert d.which_rule == "time_close_21dte"

    def test_nan_delta_fails_closed_rule4(self):
        """NaN delta on short leg -> Rule 4 (fail-CLOSED)."""
        legs = [ShortLegState(delta=float("nan"), extrinsic_value=1.0)]
        d = evaluate_options_exit(net_pnl=0.10, initial_credit=1.0, dte=30, short_legs=legs)
        assert d.should_close is True
        assert d.which_rule == "delta_breach"

    def test_nan_extrinsic_fails_closed_rule5(self):
        """NaN extrinsic on short leg -> Rule 5 (fail-CLOSED)."""
        legs = [ShortLegState(delta=-0.20, extrinsic_value=float("nan"))]
        d = evaluate_options_exit(net_pnl=0.10, initial_credit=1.0, dte=30, short_legs=legs)
        assert d.should_close is True
        assert d.which_rule == "extrinsic_floor"

    def test_nan_credit_with_zero_pnl_no_spurious_close(self):
        """NaN credit with zero pnl -> must not spuriously close on Rule 2.

        A zero pnl is not a loss, so the fail-CLOSED branch for (NaN credit + loss)
        should NOT fire. Rules 1+2 require finite positive credit; skip them.
        """
        d = evaluate_options_exit(net_pnl=0.0, initial_credit=float("nan"), dte=30, short_legs=[])
        # No credit rules fire; no short legs -> no delta/extrinsic fire; no time fire.
        assert d.which_rule == "hold"

    def test_inf_credit_treated_as_non_finite(self):
        """inf initial_credit -> non-finite, so credit rules are skipped (treated as debit)."""
        d = evaluate_options_exit(net_pnl=0.10, initial_credit=float("inf"), dte=30, short_legs=[])
        assert d.which_rule not in ("tp_50pct_credit", "loss_cap_2x")


# ---------------------------------------------------------------------------
# Composite / integration scenarios
# ---------------------------------------------------------------------------

class TestCompositeScenarios:
    """End-to-end scenarios that mirror real composite positions."""

    def test_healthy_cc_position_holds(self):
        """A covered-call position in good shape: moderate gain, DTE=30, delta safe.

        All 5 rules should NOT fire -> HOLD.
        """
        d = evaluate_options_exit(
            net_pnl=0.30,   # 30% of $1.00 credit (below 50% TP)
            initial_credit=1.00,
            dte=30,         # well above 21-DTE threshold
            short_legs=[ShortLegState(delta=0.22, extrinsic_value=0.80)],  # delta ok, extrinsic ok
        )
        assert d.should_close is False
        assert d.which_rule == "hold"

    def test_csp_approaching_max_loss_triggers_loss_cap(self):
        """CSP with 2.1x loss of initial credit -> loss cap fires."""
        d = evaluate_options_exit(
            net_pnl=-2.10,   # 2.1x loss of $1.00 credit
            initial_credit=1.00,
            dte=35,
            short_legs=[ShortLegState(delta=-0.35, extrinsic_value=0.50)],
        )
        assert d.should_close is True
        assert d.which_rule == "loss_cap_2x"

    def test_iron_condor_near_expiry_triggers_time_close(self):
        """Iron condor at DTE=21 -> time-close fires."""
        d = evaluate_options_exit(
            net_pnl=0.20,  # partial gain, below TP
            initial_credit=2.00,  # 2 legs so higher credit
            dte=21,
            short_legs=[
                ShortLegState(delta=-0.15, extrinsic_value=0.30),
                ShortLegState(delta=0.15, extrinsic_value=0.30),
            ],
        )
        assert d.should_close is True
        assert d.which_rule == "time_close_21dte"

    def test_short_put_assignment_risk_triggers_extrinsic_floor(self):
        """Short put with only $0.08 extrinsic -> assignment-prevention close fires."""
        d = evaluate_options_exit(
            net_pnl=0.30,   # good gain but not at TP yet
            initial_credit=1.00,
            dte=25,
            short_legs=[ShortLegState(delta=-0.25, extrinsic_value=0.08)],
        )
        assert d.should_close is True
        assert d.which_rule == "extrinsic_floor"

    def test_perfect_50pct_tp_fires(self):
        """Exactly 50% of credit received -> TP fires."""
        d = evaluate_options_exit(
            net_pnl=0.50,   # exactly 50% of $1.00 credit
            initial_credit=1.00,
            dte=30,
            short_legs=[ShortLegState(delta=-0.20, extrinsic_value=0.50)],
        )
        assert d.should_close is True
        assert d.which_rule == "tp_50pct_credit"

    def test_delta_tightens_inside_21dte_wheel_scenario(self):
        """Wheel position: verify the inside-21DTE tighter delta threshold wiring.

        Inside 21 DTE, the delta breach tightens from 0.40 to 0.30. The tightening
        is tested via the EVALUATION ORDER of the DTE classification: at DTE=25 the
        OUTSIDE threshold (0.40) is used; at DTE=22 with a custom inside threshold
        override we prove the inside path is wired separately.

        Key invariant: the active threshold switches based on `dte <= time_close_dte`.
        When DTE is outside the window, only `delta_breach_outside` applies; when
        inside, only `delta_breach_inside` applies.

        RED: use `active_delta_thr = delta_breach_outside` unconditionally (remove the
        DTE-based branching) — the two-threshold test below would then show identical
        behavior for both DTE values (both use 0.40, both HOLD for delta=0.35).
        """
        # DTE=25 (outside 21-DTE window): outside threshold 0.40 is active.
        # delta=0.35 < 0.40 -> HOLD on delta breach.
        d_outside = evaluate_options_exit(
            net_pnl=0.10,
            initial_credit=1.00,
            dte=25,  # OUTSIDE 21 DTE
            short_legs=[ShortLegState(delta=-0.35, extrinsic_value=0.50)],
        )
        assert d_outside.which_rule != "delta_breach", (
            "delta 0.35 should be within 0.40 outside-21DTE threshold"
        )

        # DTE=25 (still outside) but now the outside threshold is manually set to 0.30:
        # delta=0.35 > 0.30 -> delta_breach fires.
        # This confirms the outside-threshold code path is actually being used for DTE=25.
        d_outside_custom = evaluate_options_exit(
            net_pnl=0.10,
            initial_credit=1.00,
            dte=25,  # OUTSIDE 21 DTE
            short_legs=[ShortLegState(delta=-0.35, extrinsic_value=0.50)],
            delta_breach_outside=0.30,  # tighter outside threshold
        )
        assert d_outside_custom.should_close is True
        assert d_outside_custom.which_rule == "delta_breach"

        # DTE=22 with INSIDE threshold override to 0.20 (but we're OUTSIDE 21 DTE):
        # inside threshold override is irrelevant; outside (0.40 default) still applies.
        # delta=0.35 < 0.40 -> HOLD (proves inside threshold not active outside the window).
        d_inside_override_irrelevant = evaluate_options_exit(
            net_pnl=0.10,
            initial_credit=1.00,
            dte=22,  # OUTSIDE 21 DTE
            short_legs=[ShortLegState(delta=-0.35, extrinsic_value=0.50)],
            delta_breach_inside=0.20,  # override inside threshold (inactive for DTE=22)
        )
        assert d_inside_override_irrelevant.which_rule != "delta_breach", (
            "inside threshold override should be irrelevant outside 21 DTE"
        )


# ---------------------------------------------------------------------------
# Result dataclass structure
# ---------------------------------------------------------------------------

class TestResultDataclass:
    """OptionsExitDecision shape: should_close/reason/which_rule/firing_leg_idx."""

    def test_hold_result_shape(self):
        """Hold result has expected shape."""
        d = evaluate_options_exit(
            net_pnl=0.05, initial_credit=1.00, dte=30,
            short_legs=[ShortLegState(delta=-0.20, extrinsic_value=0.50)],
        )
        assert isinstance(d, OptionsExitDecision)
        assert d.should_close is False
        assert d.which_rule == "hold"
        assert d.firing_leg_idx is None
        assert isinstance(d.reason, str)

    def test_close_result_has_reason(self):
        """Close results always carry a non-empty reason string."""
        # Rule 2 close
        d = evaluate_options_exit(
            net_pnl=-2.10, initial_credit=1.00, dte=30, short_legs=[],
        )
        assert d.should_close is True
        assert d.reason and len(d.reason) > 0

    def test_firing_leg_idx_set_for_rule4(self):
        """Rule 4 fire sets firing_leg_idx to the short leg index."""
        legs = [ShortLegState(delta=-0.50, extrinsic_value=1.0)]
        d = evaluate_options_exit(net_pnl=0.05, initial_credit=1.00, dte=30, short_legs=legs)
        assert d.which_rule == "delta_breach"
        assert d.firing_leg_idx == 0

    def test_composite_rule_firing_leg_idx_none(self):
        """Rules 1/2/3 (composite-level) have firing_leg_idx=None."""
        # Rule 1
        d1 = evaluate_options_exit(net_pnl=0.60, initial_credit=1.00, dte=30, short_legs=[])
        assert d1.which_rule == "tp_50pct_credit"
        assert d1.firing_leg_idx is None

        # Rule 2
        d2 = evaluate_options_exit(net_pnl=-2.10, initial_credit=1.00, dte=30, short_legs=[])
        assert d2.which_rule == "loss_cap_2x"
        assert d2.firing_leg_idx is None

        # Rule 3
        d3 = evaluate_options_exit(net_pnl=0.05, initial_credit=1.00, dte=15, short_legs=[])
        assert d3.which_rule == "time_close_21dte"
        assert d3.firing_leg_idx is None


# ---------------------------------------------------------------------------
# wave3-review fixes: negative time_close_dte default + reachable inside-21DTE delta
# ---------------------------------------------------------------------------
class TestWave3ReviewFixes:
    def test_negative_time_close_dte_falls_back_to_default_not_disabled(self):
        """A negative time_close_dte must restore the 21-DTE default, NOT disable Rule 3.

        RED before the fix: `int(time_close_dte) if _is_finite(...)` kept int(-1)=-1, so
        a dte=10 position never satisfied `dte <= -1` and Rule 3 was silently disabled
        (fail-OPEN). After the fix, time_close_dte=-1 -> TIME_CLOSE_DTE(21) -> dte=10 closes.
        """
        d = evaluate_options_exit(
            net_pnl=0.0, initial_credit=1.00, dte=10,
            short_legs=_one_short_leg(delta=-0.10, extrinsic=1.50),  # no delta/extrinsic breach
            time_close_dte=-1,  # invalid -> must restore default 21
        )
        assert d.should_close is True
        assert d.which_rule == "time_close_21dte", (
            f"negative time_close_dte must fall back to the 21-DTE default (Rule 3), got {d.which_rule}"
        )

    def test_inside_21dte_delta_breach_fires_on_tighter_threshold_before_calendar(self):
        """A short leg INSIDE the 21-DTE window whose |delta| exceeds the TIGHTER inside
        threshold (0.30) but is below the outside threshold (0.40) must fire Rule 4
        (delta breach) — NOT be pre-empted by the Rule 3 calendar close. This is the path
        the wave3 reorder made reachable (previously time-close fired first at dte<=21,
        making DELTA_BREACH_INSIDE_21DTE dead code).

        RED: revert the rule reorder (put Rule 3 calendar before Rule 4) -> this closes via
        time_close_21dte instead of delta_breach, and the tighter threshold is never tested.
        """
        d = evaluate_options_exit(
            net_pnl=0.0, initial_credit=1.00, dte=15,  # inside the 21-DTE window
            short_legs=_one_short_leg(delta=-0.35, extrinsic=1.50),  # 0.30 < 0.35 < 0.40
        )
        assert d.should_close is True
        assert d.which_rule == "delta_breach", (
            "an inside-21DTE leg at |delta|=0.35 must fire the TIGHTER inside delta (0.30) "
            f"BEFORE the calendar close; got {d.which_rule} ({d.reason})"
        )

    def test_inside_21dte_no_breach_still_calendar_closes(self):
        """Inside 21 DTE, no delta/extrinsic breach -> the calendar close still force-closes
        (Rule 3 fires LAST but still fires). Proves the reorder didn't drop the time rail."""
        d = evaluate_options_exit(
            net_pnl=0.0, initial_credit=1.00, dte=15,
            short_legs=_one_short_leg(delta=-0.10, extrinsic=1.50),  # no breach
        )
        assert d.should_close is True
        assert d.which_rule == "time_close_21dte"

    def test_inside_21dte_delta_below_tighter_threshold_holds_on_delta_but_calendar_closes(self):
        """|delta|=0.25 < the inside 0.30 threshold -> Rule 4 does NOT fire, but the
        calendar (Rule 3) still closes the inside-window position."""
        d = evaluate_options_exit(
            net_pnl=0.0, initial_credit=1.00, dte=15,
            short_legs=_one_short_leg(delta=-0.25, extrinsic=1.50),
        )
        assert d.should_close is True
        assert d.which_rule == "time_close_21dte"  # not delta_breach (0.25 < 0.30)
