"""tests/eval/test_options_evidence.py — dbcd: AG-OPT-EV-1 made executable.

ADR-0029 (multi-leg paper reactor) requires an EVIDENCE WINDOW before options
origination/live: a 30-calendar-day paper window with N_options >= 30 SETTLED
multi-leg outcomes, with win-rate / premium-capture / assignment / gate-reject
all MEASURED. Until this lane that gate lived only as process prose (handoff doc
+ ADR). This module proves the executable check ``compute_options_evidence``:

  - it counts ONLY options settled outcomes (asset_class in {multi_leg, us_option}),
    excluding equity round-trips entirely;
  - it filters PRE-GATE-0 (asof_exit < t0) outcomes (poisoned data, zero weight);
  - it finite-guards realized_return (a NaN must not become a free win);
  - it computes win_rate, premium_capture_pct, assignment_count, gate_reject_rate;
  - N_options < 30 => RED (not-yet-evidenced), even if every other metric is strong;
  - N_options >= 30 over >= 30 calendar days => GREEN (the documented threshold);
  - the gate is READ-ONLY / additive — it returns a structured result and changes
    no live gate decision.

DEFAULT-OFF posture: ``compute_options_evidence`` is a pure metric harness, like
``compute_gate_metrics``; nothing on the live path consumes its verdict.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from hermes_quant.eval.clean_window import (
    OptionsEvidence,
    OptionsEvidenceTrip,
    compute_options_evidence,
)

_T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _opt_trip(
    delta_days: float,
    realized_return: float,
    *,
    asset_class: str = "us_option",
    premium_capture_frac: float | None = None,
    was_assignment: bool = False,
    t0: datetime = _T0,
) -> OptionsEvidenceTrip:
    return OptionsEvidenceTrip(
        asof_exit=t0 + timedelta(days=delta_days),
        realized_return=realized_return,
        asset_class=asset_class,
        premium_capture_frac=premium_capture_frac,
        was_assignment=was_assignment,
    )


# ---------------------------------------------------------------------------
# N_options counting: ONLY options outcomes, by asset_class
# ---------------------------------------------------------------------------
class TestOptionsCounting:
    def test_counts_us_option_and_multi_leg_excludes_equity(self):
        """Only asset_class in {us_option, multi_leg} count toward N_options."""
        trips = (
            [_opt_trip(i + 1, 0.05, asset_class="us_option") for i in range(10)]
            + [_opt_trip(i + 1, 0.05, asset_class="multi_leg") for i in range(7)]
            + [_opt_trip(i + 1, 0.05, asset_class="equity") for i in range(20)]
            + [_opt_trip(i + 1, 0.05, asset_class="crypto") for i in range(5)]
        )
        ev = compute_options_evidence(trips, t0=_T0)
        assert ev.n_options == 17, (
            f"only the 10 us_option + 7 multi_leg outcomes count; got {ev.n_options}"
        )

    def test_pre_t0_options_excluded(self):
        """An options outcome that settled BEFORE t0 is poisoned, zero weight."""
        trips = [
            _opt_trip(-5, 0.05),  # PRE-GATE-0 — excluded
            _opt_trip(1, 0.05),
            _opt_trip(2, 0.05),
        ]
        ev = compute_options_evidence(trips, t0=_T0)
        assert ev.n_options == 2, f"pre-t0 trip must be discarded; got {ev.n_options}"

    def test_nonfinite_return_excluded(self):
        """A NaN realized_return is missing data, not a free win — excluded."""
        trips = [
            _opt_trip(1, float("nan")),
            _opt_trip(2, 0.05),
            _opt_trip(3, float("inf")),
        ]
        ev = compute_options_evidence(trips, t0=_T0)
        assert ev.n_options == 1, f"non-finite returns excluded; got {ev.n_options}"
        assert ev.warnings, "excluding a non-finite return must be observable"


# ---------------------------------------------------------------------------
# Metric correctness
# ---------------------------------------------------------------------------
class TestMetricCorrectness:
    def test_win_rate(self):
        """win_rate = options wins / n_options."""
        trips = (
            [_opt_trip(i + 1, 0.05) for i in range(6)]  # 6 wins
            + [_opt_trip(10 + i, -0.02) for i in range(4)]  # 4 losses
        )
        ev = compute_options_evidence(trips, t0=_T0)
        assert ev.n_options == 10
        assert ev.win_rate == pytest.approx(0.6)

    def test_premium_capture_pct_mean_of_present(self):
        """premium_capture_pct = mean of the present per-trip premium-capture fracs * 100."""
        trips = [
            _opt_trip(1, 0.05, premium_capture_frac=0.50),
            _opt_trip(2, 0.03, premium_capture_frac=0.30),
            _opt_trip(3, 0.01, premium_capture_frac=None),  # not measured -> ignored
        ]
        ev = compute_options_evidence(trips, t0=_T0)
        # mean of the two PRESENT fracs (0.50, 0.30) = 0.40 -> 40.0%
        assert ev.premium_capture_pct == pytest.approx(40.0)

    def test_premium_capture_nan_when_none_present(self):
        """No premium-capture annotations => premium_capture_pct is NaN (not measured)."""
        trips = [_opt_trip(i + 1, 0.05) for i in range(5)]
        ev = compute_options_evidence(trips, t0=_T0)
        assert math.isnan(ev.premium_capture_pct)

    def test_assignment_count(self):
        """assignment_count counts options outcomes flagged was_assignment=True."""
        trips = [
            _opt_trip(1, -0.10, was_assignment=True),
            _opt_trip(2, 0.05, was_assignment=False),
            _opt_trip(3, -0.04, was_assignment=True),
        ]
        ev = compute_options_evidence(trips, t0=_T0)
        assert ev.assignment_count == 2

    def test_gate_reject_rate(self):
        """gate_reject_rate = rejects / (rejects + admitted evals)."""
        trips = [_opt_trip(i + 1, 0.05) for i in range(5)]
        ev = compute_options_evidence(
            trips, t0=_T0, gate_eval_count=10, gate_reject_count=3
        )
        assert ev.gate_reject_rate == pytest.approx(0.3)

    def test_gate_reject_rate_nan_without_counts(self):
        """No gate eval counts supplied => gate_reject_rate is NaN (not measured)."""
        trips = [_opt_trip(i + 1, 0.05) for i in range(5)]
        ev = compute_options_evidence(trips, t0=_T0)
        assert math.isnan(ev.gate_reject_rate)

    def test_calendar_days(self):
        """calendar_days = first-to-last options exit span."""
        trips = [_opt_trip(1, 0.05), _opt_trip(31, 0.05)]
        ev = compute_options_evidence(trips, t0=_T0)
        assert ev.calendar_days == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# GREEN / RED verdict on N_options >= 30 + the documented 30-day threshold
# ---------------------------------------------------------------------------
class TestVerdict:
    def test_n_below_30_is_red_even_when_strong(self):
        """N_options < 30 is NOT-YET-EVIDENCED (RED), regardless of win-rate."""
        trips = [_opt_trip(i + 1, 0.05) for i in range(29)]  # all wins, but only 29
        ev = compute_options_evidence(trips, t0=_T0)
        assert ev.n_options == 29
        assert ev.n_threshold_met is False
        assert ev.is_green is False, "N<30 must report RED (not-yet-evidenced)"

    def test_empty_is_red(self):
        """No options outcomes => RED, fail-CLOSED."""
        ev = compute_options_evidence([], t0=_T0)
        assert ev.n_options == 0
        assert ev.is_green is False

    def test_absent_t0_is_red(self):
        """t0 None (GATE-0 not run) => RED, all metrics fail-CLOSED."""
        trips = [_opt_trip(i + 1, 0.05) for i in range(40)]
        ev = compute_options_evidence(trips, t0=None)
        assert ev.is_green is False
        assert ev.n_options == 0
        assert ev.warnings

    def test_30_options_over_30_days_is_green(self):
        """N_options >= 30 spanning >= 30 calendar days => GREEN."""
        # 30 outcomes, one per day from day 1..30 => calendar span 29 days < 30 would be RED.
        # Spread to day 1..31 so the span clears 30 calendar days too.
        trips = [_opt_trip(i + 1, 0.05) for i in range(30)]  # days 1..30 => span 29d
        trips.append(_opt_trip(31, 0.05))  # extend span to 30 days, N=31
        ev = compute_options_evidence(trips, t0=_T0)
        assert ev.n_options == 31
        assert ev.n_threshold_met is True
        assert ev.calendar_days >= 30.0
        assert ev.is_green is True, f"30+ options over 30+ days must be GREEN; ev={ev}"

    def test_30_options_but_window_too_short_is_red(self):
        """N_options >= 30 but < 30 calendar days => still RED (the window contract)."""
        # 30 outcomes all within a single 5-day burst.
        trips = [
            _opt_trip(1 + (i % 5) + i * 0.0, 0.05) for i in range(30)
        ]
        # Force them into a 5-day span.
        trips = [_opt_trip(1 + (i % 5), 0.05) for i in range(30)]
        ev = compute_options_evidence(trips, t0=_T0)
        assert ev.n_options == 30
        assert ev.calendar_days < 30.0
        assert ev.is_green is False, "30 outcomes in <30 days is not the documented window"

    def test_result_is_options_evidence_dataclass(self):
        """The result is the structured OptionsEvidence dataclass."""
        ev = compute_options_evidence([_opt_trip(1, 0.05)], t0=_T0)
        assert isinstance(ev, OptionsEvidence)


# ---------------------------------------------------------------------------
# Duck-typed reuse: a SettledRoundTrip-shaped object works directly.
# ---------------------------------------------------------------------------
class TestSettledRoundTripReuse:
    def test_accepts_settled_round_trip_objects(self):
        """compute_options_evidence reads asof_exit/realized_return/asset_class off
        any object (e.g. a daemon SettledRoundTrip) so the run-card can pass the
        join_exit_fills output directly — no adapter layer."""
        from hermes_quant.daemon.settlement_loop import SettledRoundTrip
        import pandas as pd

        srt = SettledRoundTrip(
            asset="SPY",
            account_id="paper-default",
            asset_class="us_option",
            side="sell",
            qty=0.05,
            entry_price=2.0,
            exit_price=1.0,
            asof_entry=pd.Timestamp("2026-01-02T15:00:00Z"),
            asof_exit=pd.Timestamp("2026-01-03T15:00:00Z"),
            entry_exec_id="e",
            exit_exec_id="x",
            entry_signal_id="s",
            exit_signal_id="s",
            fees=0.0,
            realized_return=0.10,
        )
        ev = compute_options_evidence([srt], t0=_T0)
        assert ev.n_options == 1
        assert ev.win_rate == pytest.approx(1.0)
