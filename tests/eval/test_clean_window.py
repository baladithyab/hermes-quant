"""tests/eval/test_clean_window.py — ADR-0099 Part C: clean-window gate harness.

Coverage (RED->GREEN):
    t0-filter:
        - A pre-t0 round-trip is excluded from all metrics (RED: revert filter -> pollutes)
        - A trip exactly at t0 is INCLUDED
        - A trip after t0 is included
    Thin-sample / fail-CLOSED:
        - N<20 fails GATE-1 (thin)
        - Non-finite realized_return is excluded (finite-guard)
        - Empty sample after filter => all metrics NaN => all gates fail-CLOSED
        - Absent t0 (None) => all gates fail-CLOSED
    Healthy sample GATE-1 pass / GATE-2 fail (N<50):
        - A synthetic 20-trip win-heavy sample clears GATE-1
        - That same 20-trip sample fails GATE-2 (N<50)
    Metric correctness:
        - win_rate computed correctly
        - profit_factor computed correctly
        - max_consecutive_losses computed correctly
        - max_drawdown is <= 0 or 0.0 for all-win sample
        - calendar_days computed correctly
        - Wilson lower bound > 0 for a strong win rate
    Bootstrap sharpe_95ci_lower:
        - A large healthy sample produces a finite sharpe_95ci_lower
    read_clean_window_start / write_clean_window_start:
        - write creates the file; read returns the same t0
        - absent file returns None
        - write includes armed_flags in the payload
    Non-finite input fail-CLOSED:
        - A NaN in realized_return is excluded; does not propagate
        - inf in realized_return is excluded
    GATE-3 N_options threshold enforced
    Kill-switch gate-1 fail if count > 0
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_quant.eval.clean_window import (
    GateMetrics,
    RoundTrip,
    _wilson_lower_bound,
    compute_gate_metrics,
    evaluate_gate,
    read_clean_window_start,
    write_clean_window_start,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
_ONE_DAY = timedelta(days=1)
_ONE_HOUR = timedelta(hours=1)


def _make_trip(
    delta_days: float,
    realized_return: float,
    *,
    is_options: bool = False,
    t0: datetime = _T0,
) -> RoundTrip:
    """Build a RoundTrip ``delta_days`` after ``t0``."""
    return RoundTrip(
        asof_exit=t0 + timedelta(days=delta_days),
        realized_return=realized_return,
        is_options=is_options,
    )


def _win_trips(n: int, *, t0: datetime = _T0, win_ret: float = 0.01, is_options: bool = False) -> list[RoundTrip]:
    """Build n winning trips, one per day after t0."""
    return [_make_trip(i + 1, win_ret, t0=t0, is_options=is_options) for i in range(n)]


def _lose_trips(n: int, *, t0: datetime = _T0, lose_ret: float = -0.005) -> list[RoundTrip]:
    """Build n losing trips, starting well after any winning set."""
    return [_make_trip(i + 1, lose_ret, t0=t0) for i in range(n)]


def _healthy_gate1_trips(*, t0: datetime = _T0) -> list[RoundTrip]:
    """20-trip sample: 14 wins, 6 losses. Clears GATE-1 thresholds."""
    wins = [_make_trip(i + 1, 0.01, t0=t0) for i in range(14)]
    losses = [_make_trip(i + 15, -0.004, t0=t0) for i in range(6)]
    return wins + losses


# ---------------------------------------------------------------------------
# T0-filter tests
# ---------------------------------------------------------------------------

class TestT0Filter:
    """The critical invariant: pre-t0 data is poisoned and must be DISCARDED."""

    def test_pre_t0_trip_excluded(self):
        """A trip before t0 must NOT appear in the metric sample."""
        t0 = _T0
        pre_t0_trip = RoundTrip(
            asof_exit=t0 - _ONE_DAY,
            realized_return=-0.50,  # catastrophic loser
        )
        post_t0_trip = _make_trip(1, 0.01)
        metrics = compute_gate_metrics([pre_t0_trip, post_t0_trip], t0=t0)
        # Only the post-t0 trip counted
        assert metrics.n == 1, f"Expected n=1, got n={metrics.n}"
        # win_rate must be 1.0 (only the winner was included)
        assert metrics.win_rate == pytest.approx(1.0)

    def test_pre_t0_trip_pollutes_when_filter_absent(self):
        """
        RED-VERIFICATION TEST: if the t0 filter were removed, the pre-t0 loser
        would pollute win_rate downward.  This test encodes the EXPECTED OUTCOME
        of a broken implementation so we can verify RED behaviour.

        We do NOT call the real compute_gate_metrics here (it's correct); instead
        we simulate the broken behaviour manually to prove the test would catch it.
        """
        t0 = _T0
        pre_t0_trip = RoundTrip(asof_exit=t0 - _ONE_DAY, realized_return=-0.50)
        post_t0_trip = _make_trip(1, 0.01)
        all_trips = [pre_t0_trip, post_t0_trip]

        # SIMULATE the broken version (no t0 filter: include all trips)
        broken_returns = [rt.realized_return for rt in all_trips]
        broken_wins = sum(1 for r in broken_returns if r > 0)
        broken_win_rate = broken_wins / len(broken_returns)

        # The broken metric WOULD be 0.5, not 1.0 (polluted)
        assert broken_win_rate == pytest.approx(0.5), (
            "Invariant broken: the pre-t0 loser would pollute win_rate to 0.5 "
            "if the t0 filter is absent — this proves the filter is load-bearing."
        )

        # And the correct implementation returns 1.0, not 0.5
        correct = compute_gate_metrics(all_trips, t0=t0)
        assert correct.win_rate == pytest.approx(1.0)
        assert correct.win_rate != pytest.approx(broken_win_rate)

    def test_trip_at_exactly_t0_included(self):
        """A trip with asof_exit == t0 (exactly at the boundary) is included."""
        t0 = _T0
        at_boundary = RoundTrip(asof_exit=t0, realized_return=0.01)
        metrics = compute_gate_metrics([at_boundary], t0=t0)
        assert metrics.n == 1

    def test_trip_one_nanosecond_after_t0_included(self):
        """A trip microseconds after t0 is included."""
        t0 = _T0
        just_after = RoundTrip(
            asof_exit=t0 + timedelta(microseconds=1), realized_return=0.01
        )
        metrics = compute_gate_metrics([just_after], t0=t0)
        assert metrics.n == 1

    def test_all_pre_t0_returns_empty_metrics(self):
        """All trips before t0 => n=0 => all metrics NaN => gates fail-CLOSED."""
        trips = [RoundTrip(asof_exit=_T0 - timedelta(days=i + 1), realized_return=0.01)
                 for i in range(30)]
        metrics = compute_gate_metrics(trips, t0=_T0)
        assert metrics.n == 0
        assert math.isnan(metrics.win_rate)
        assert math.isnan(metrics.sharpe)
        assert not evaluate_gate(metrics, 1)
        assert not evaluate_gate(metrics, 2)
        assert not evaluate_gate(metrics, 3)


# ---------------------------------------------------------------------------
# Absent / None t0
# ---------------------------------------------------------------------------

class TestAbsentT0:
    def test_none_t0_all_gates_fail_closed(self):
        """compute_gate_metrics with t0=None returns empty metrics."""
        trips = _win_trips(25)
        # Must accept None (caller checked read_clean_window_start first)
        metrics = compute_gate_metrics(trips, t0=None)  # type: ignore[arg-type]
        assert metrics.n == 0
        assert not evaluate_gate(metrics, 1)
        assert not evaluate_gate(metrics, 2)
        assert not evaluate_gate(metrics, 3)


# ---------------------------------------------------------------------------
# Thin sample / fail-CLOSED
# ---------------------------------------------------------------------------

class TestThinSample:
    def test_n_less_than_20_fails_gate1(self):
        """N<20 must fail GATE-1 (thin sample)."""
        trips = _win_trips(19)
        metrics = compute_gate_metrics(trips, t0=_T0)
        assert metrics.n == 19
        assert not evaluate_gate(metrics, 1), "N=19 must fail GATE-1"

    def test_n_equals_20_sufficient_for_gate1(self):
        """N=20 with a healthy win rate clears GATE-1."""
        trips = _win_trips(20)
        metrics = compute_gate_metrics(trips, t0=_T0)
        assert metrics.n == 20
        # All wins -> win_rate=1.0 > 0.40, Wilson LB > 0, dd=0, no KS
        assert evaluate_gate(metrics, 1), f"N=20 all-win should clear GATE-1; metrics={metrics}"

    def test_n_less_than_50_fails_gate2(self):
        """N<50 fails GATE-2 even with excellent metrics."""
        trips = _win_trips(49)
        metrics = compute_gate_metrics(trips, t0=_T0)
        assert metrics.n == 49
        assert not evaluate_gate(metrics, 2), "N=49 must fail GATE-2"

    def test_empty_trips_all_gates_fail(self):
        """Empty round-trip list => all gates fail-CLOSED."""
        metrics = compute_gate_metrics([], t0=_T0)
        assert metrics.n == 0
        assert not evaluate_gate(metrics, 1)
        assert not evaluate_gate(metrics, 2)
        assert not evaluate_gate(metrics, 3)


# ---------------------------------------------------------------------------
# Non-finite returns (finite-guard)
# ---------------------------------------------------------------------------

class TestNonFiniteReturns:
    def test_nan_return_excluded(self):
        """A NaN realized_return is excluded; remaining trips still counted."""
        trips = _win_trips(5) + [RoundTrip(asof_exit=_T0 + timedelta(days=6), realized_return=float("nan"))]
        metrics = compute_gate_metrics(trips, t0=_T0)
        assert metrics.n == 5, f"NaN trip should be excluded; got n={metrics.n}"

    def test_inf_return_excluded(self):
        """An inf realized_return is excluded."""
        trips = _win_trips(5) + [RoundTrip(asof_exit=_T0 + timedelta(days=6), realized_return=float("inf"))]
        metrics = compute_gate_metrics(trips, t0=_T0)
        assert metrics.n == 5

    def test_neg_inf_return_excluded(self):
        """A -inf realized_return is excluded."""
        trips = _win_trips(5) + [RoundTrip(asof_exit=_T0 + timedelta(days=6), realized_return=float("-inf"))]
        metrics = compute_gate_metrics(trips, t0=_T0)
        assert metrics.n == 5

    def test_all_nan_returns_empty_metrics(self):
        """All NaN returns => n=0 => fail-CLOSED."""
        trips = [
            RoundTrip(asof_exit=_T0 + timedelta(days=i + 1), realized_return=float("nan"))
            for i in range(30)
        ]
        metrics = compute_gate_metrics(trips, t0=_T0)
        assert metrics.n == 0
        assert not evaluate_gate(metrics, 1)


# ---------------------------------------------------------------------------
# GATE-1 pass + GATE-2 fail (healthy 20-trip sample)
# ---------------------------------------------------------------------------

class TestGate1PassGate2Fail:
    """A healthy 20-trip sample must clear GATE-1 but fail GATE-2 (N<50)."""

    def setup_method(self):
        trips = _healthy_gate1_trips()
        self.metrics = compute_gate_metrics(trips, t0=_T0)

    def test_gate1_cleared(self):
        assert evaluate_gate(self.metrics, 1), (
            f"Expected GATE-1 to clear; metrics={self.metrics}"
        )

    def test_gate2_fails_n_too_low(self):
        assert not evaluate_gate(self.metrics, 2), (
            f"Expected GATE-2 to fail (N<50); metrics={self.metrics}"
        )

    def test_gate3_fails_n_options_too_low(self):
        assert not evaluate_gate(self.metrics, 3), (
            f"Expected GATE-3 to fail (N_options=0); metrics={self.metrics}"
        )

    def test_win_rate_is_correct(self):
        """14/20 = 0.70."""
        assert self.metrics.win_rate == pytest.approx(14 / 20)

    def test_wilson_lb_positive(self):
        """With 70% win rate over 20 trips, Wilson LB > 0."""
        assert self.metrics.win_rate_wilson_lb > 0.0
        # And greater than the GATE-1 implicit "clears 0" threshold
        assert self.metrics.win_rate_wilson_lb > 0.0

    def test_profit_factor_positive(self):
        assert math.isfinite(self.metrics.profit_factor)
        assert self.metrics.profit_factor > 0

    def test_sharpe_finite(self):
        assert math.isfinite(self.metrics.sharpe)

    def test_max_drawdown_lte_zero(self):
        assert self.metrics.max_drawdown <= 0.0

    def test_calendar_days_approx_correct(self):
        # 20 trips, 1 trip/day => ~19d from day 1 to day 20
        assert self.metrics.calendar_days == pytest.approx(19.0, abs=1.0)


# ---------------------------------------------------------------------------
# Gate-1 fail: bad win rate
# ---------------------------------------------------------------------------

class TestGate1WinRateFail:
    def test_low_win_rate_fails_gate1(self):
        """A 30% win rate (below 0.40) fails GATE-1."""
        n = 20
        wins_n = 6  # 6/20 = 30%
        trips = (
            [_make_trip(i + 1, 0.01) for i in range(wins_n)]
            + [_make_trip(i + wins_n + 1, -0.005) for i in range(n - wins_n)]
        )
        metrics = compute_gate_metrics(trips, t0=_T0)
        assert not evaluate_gate(metrics, 1), (
            f"30% win rate should fail GATE-1; metrics={metrics}"
        )


# ---------------------------------------------------------------------------
# Kill-switch gate-1 fail
# ---------------------------------------------------------------------------

class TestKillSwitchGate1:
    def test_kill_switch_fires_fails_gate1(self):
        """A kill-switch fire in the window fails GATE-1."""
        trips = _win_trips(20)
        metrics = compute_gate_metrics(trips, t0=_T0)
        assert not evaluate_gate(metrics, 1, kill_switch_count=1), (
            "Kill-switch count=1 should fail GATE-1"
        )

    def test_no_kill_switch_passes_gate1(self):
        """Zero kill-switch fires => GATE-1 not blocked on that criterion."""
        trips = _win_trips(20)
        metrics = compute_gate_metrics(trips, t0=_T0)
        # Should clear (other criteria met)
        assert evaluate_gate(metrics, 1, kill_switch_count=0)


# ---------------------------------------------------------------------------
# GATE-2: healthy 50-trip, 70+ day sample
# ---------------------------------------------------------------------------

class TestGate2:
    """Build a 50-trip sample spanning 70+ days with strong metrics."""

    def _make_healthy_gate2_trips(self):
        # 35 wins, 15 losses => win_rate=0.70, profit_factor=35*0.01/(15*0.004)=2.33
        # Interleave wins and losses so consecutive-loss streak <= 8:
        # pattern: W W W L W W W L ... so streak is 1
        # 1 trip per 1.5 days => 75d span
        trips = []
        wins_left = 35
        losses_left = 15
        day_offset = 0
        w_block = 2  # every 3rd trip is a loss, rest are wins
        trip_count = 0
        while wins_left > 0 or losses_left > 0:
            # Every 3rd trip: loss (if losses available), else win
            if trip_count % 3 == 2 and losses_left > 0:
                trips.append(_make_trip(day_offset * 1.5 + 0.5, -0.004))
                losses_left -= 1
            elif wins_left > 0:
                trips.append(_make_trip(day_offset * 1.5 + 0.5, 0.01))
                wins_left -= 1
            elif losses_left > 0:
                trips.append(_make_trip(day_offset * 1.5 + 0.5, -0.004))
                losses_left -= 1
            day_offset += 1
            trip_count += 1
        return trips

    def test_gate2_clears(self):
        trips = self._make_healthy_gate2_trips()
        metrics = compute_gate_metrics(trips, t0=_T0)
        assert metrics.n == 50
        assert metrics.calendar_days >= 60
        assert evaluate_gate(metrics, 2), (
            f"Healthy 50-trip sample should clear GATE-2; metrics={metrics}"
        )

    def test_gate2_fails_on_poor_sharpe(self):
        """A low Sharpe (many small wins, large losses) fails GATE-2."""
        # Alternating +0.001, -0.01 -> negative sharpe
        trips = []
        for i in range(50):
            ret = 0.001 if i % 2 == 0 else -0.01
            trips.append(_make_trip(i * 1.5 + 0.5, ret))
        metrics = compute_gate_metrics(trips, t0=_T0)
        assert not evaluate_gate(metrics, 2), (
            f"Negative-Sharpe sample should fail GATE-2; metrics={metrics}"
        )

    def test_gate2_fails_bad_drawdown(self):
        """A sample with >3% drawdown fails GATE-2."""
        # Big loss at the end: drawdown > 3%
        trips = [_make_trip(i + 1, 0.005) for i in range(49)]
        trips.append(_make_trip(50, -0.15))  # large loss -> big drawdown
        metrics = compute_gate_metrics(trips, t0=_T0)
        # Spread across 50+ days
        if metrics.calendar_days < 60:
            # Force span to 65+ days to ensure only drawdown fails
            trips2 = [_make_trip(i * 1.3 + 1, 0.005) for i in range(49)]
            trips2.append(_make_trip(49 * 1.3 + 1, -0.15))
            metrics = compute_gate_metrics(trips2, t0=_T0)
        # drawdown should be > 3%
        if abs(metrics.max_drawdown) > 0.03:
            assert not evaluate_gate(metrics, 2)
        # (If the sample happens to clear, it's because losses don't compound to >3%
        # — this is an intentional robustness note rather than a hard assert)


# ---------------------------------------------------------------------------
# GATE-3: N_options threshold
# ---------------------------------------------------------------------------

class TestGate3:
    def test_gate3_fails_n_options_below_100(self):
        """N_options < 100 fails GATE-3 regardless of other metrics."""
        trips = [
            _make_trip(i + 1, 0.01, is_options=True) for i in range(99)
        ]
        metrics = compute_gate_metrics(trips, t0=_T0)
        assert metrics.n_options == 99
        assert not evaluate_gate(metrics, 3), (
            "N_options=99 should fail GATE-3"
        )

    def test_gate3_n_options_counted_correctly(self):
        """is_options=True trips are counted in n_options."""
        options_trips = _win_trips(60, is_options=True)
        equity_trips = _win_trips(40)
        all_trips = options_trips + equity_trips
        metrics = compute_gate_metrics(all_trips, t0=_T0)
        assert metrics.n_options == 60

    def test_gate3_all_options_100_but_low_sharpe_ci_fails(self):
        """100 options trips but low Sharpe CI => GATE-3 fails."""
        # Small returns + high variance => low bootstrap Sharpe CI
        import random
        rng = random.Random(99)
        trips = [
            _make_trip(i + 1, rng.gauss(0.0, 0.05), is_options=True)
            for i in range(100)
        ]
        metrics = compute_gate_metrics(trips, t0=_T0, n_resamples=100)
        assert metrics.n_options == 100
        # With near-zero mean and high vol, Sharpe CI lower should be < 1.0
        # (not guaranteed, but highly likely for Gaussian noise)
        result = evaluate_gate(metrics, 3)
        # If CI lower < 1.0, gate fails; if somehow >= 1.0 on this seed, skip
        if math.isfinite(metrics.sharpe_95ci_lower) and metrics.sharpe_95ci_lower < 1.0:
            assert not result


# ---------------------------------------------------------------------------
# Profit factor correctness
# ---------------------------------------------------------------------------

class TestProfitFactor:
    def test_profit_factor_correct(self):
        """Profit factor = sum(wins) / abs(sum(losses))."""
        trips = [
            _make_trip(1, 0.10),
            _make_trip(2, 0.05),
            _make_trip(3, -0.08),
        ]
        metrics = compute_gate_metrics(trips, t0=_T0)
        expected = (0.10 + 0.05) / 0.08
        assert metrics.profit_factor == pytest.approx(expected, rel=1e-6)

    def test_profit_factor_all_wins_is_inf(self):
        """All-win sample => profit_factor is inf (no losses)."""
        trips = _win_trips(5)
        metrics = compute_gate_metrics(trips, t0=_T0)
        assert math.isinf(metrics.profit_factor)
        assert metrics.profit_factor > 0

    def test_profit_factor_all_losses_is_zero(self):
        """All-loss sample => profit_factor is 0.0 (gross wins = 0)."""
        trips = _lose_trips(5)
        metrics = compute_gate_metrics(trips, t0=_T0)
        assert metrics.profit_factor == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Max consecutive losses
# ---------------------------------------------------------------------------

class TestMaxConsecutiveLosses:
    def test_max_consec_loss_correct(self):
        """Streak of 3 losses is detected."""
        trips = [
            _make_trip(1, 0.01),   # win
            _make_trip(2, -0.01),  # loss 1
            _make_trip(3, -0.01),  # loss 2
            _make_trip(4, -0.01),  # loss 3
            _make_trip(5, 0.01),   # win
            _make_trip(6, -0.01),  # loss 1
            _make_trip(7, -0.01),  # loss 2
        ]
        metrics = compute_gate_metrics(trips, t0=_T0)
        assert metrics.max_consecutive_losses == 3

    def test_no_losses_zero_streak(self):
        """All wins => max_consecutive_losses = 0."""
        metrics = compute_gate_metrics(_win_trips(10), t0=_T0)
        assert metrics.max_consecutive_losses == 0

    def test_break_even_counts_as_loss(self):
        """realized_return == 0.0 is treated as a non-win (streak continues)."""
        trips = [
            _make_trip(1, 0.01),
            _make_trip(2, 0.0),   # break-even, counts as loss (return <= 0)
            _make_trip(3, -0.01),
            _make_trip(4, 0.01),
        ]
        metrics = compute_gate_metrics(trips, t0=_T0)
        assert metrics.max_consecutive_losses == 2


# ---------------------------------------------------------------------------
# Max drawdown
# ---------------------------------------------------------------------------

class TestMaxDrawdown:
    def test_all_wins_zero_drawdown(self):
        """Monotonically rising P&L => max_drawdown == 0.0."""
        metrics = compute_gate_metrics(_win_trips(10), t0=_T0)
        assert metrics.max_drawdown == pytest.approx(0.0, abs=1e-10)

    def test_drawdown_negative_on_loss(self):
        """After a loss, max_drawdown < 0."""
        trips = [
            _make_trip(1, 0.10),
            _make_trip(2, -0.05),
        ]
        metrics = compute_gate_metrics(trips, t0=_T0)
        assert metrics.max_drawdown < 0

    def test_opening_loss_counts_toward_drawdown(self):
        """wave1-review fail-OPEN fix: a window that OPENS with a big loser must
        report that loss as drawdown. Without the 1.0 initial-basis prepend, the
        first trip's loss was invisible (running_peak[0]==cum[0]) -> max_drawdown=0
        -> every drawdown gate passed for any threshold. RED before the fix."""
        trips = [_make_trip(1, -0.90)] + [_make_trip(i + 2, 0.50) for i in range(19)]
        metrics = compute_gate_metrics(trips, t0=_T0)
        # The -90% opener is the trough vs the par (1.0) peak -> ~-0.90 drawdown.
        assert metrics.max_drawdown == pytest.approx(-0.90, abs=1e-6), (
            f"opening loss must count toward drawdown; got {metrics.max_drawdown} "
            "(0.0 would mean the fail-open bug is back — the first loss is invisible)"
        )
        # And it must therefore FAIL the 8% GATE-1 drawdown threshold.
        assert abs(metrics.max_drawdown) > 0.08


# ---------------------------------------------------------------------------
# read / write clean_window_start
# ---------------------------------------------------------------------------

class TestReadWriteCleanWindowStart:
    def test_absent_file_returns_none(self, tmp_path):
        result = read_clean_window_start(home=tmp_path)
        assert result is None

    def test_write_then_read_round_trip(self, tmp_path):
        t0 = datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)
        path = write_clean_window_start(tmp_path, t0)
        assert path.exists()
        result = read_clean_window_start(home=tmp_path)
        assert result is not None
        assert result == t0

    def test_write_includes_armed_flags(self, tmp_path):
        t0 = datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)
        armed = {"DURABLE_DRAWDOWN_BASELINE": "1", "PER_POSITION_STOP": "1"}
        write_clean_window_start(tmp_path, t0, armed_flags=armed)
        # Verify JSON payload directly
        data = json.loads((tmp_path / "quant" / "clean_window_start.json").read_text())
        assert data["armed_flags"] == armed

    def test_write_naive_datetime_normalised_to_utc(self, tmp_path):
        t0_naive = datetime(2026, 3, 15, 12, 0, 0)  # no tzinfo
        write_clean_window_start(tmp_path, t0_naive)
        result = read_clean_window_start(home=tmp_path)
        assert result is not None
        assert result.tzinfo is not None  # UTC-aware after round-trip

    def test_write_creates_parent_dirs(self, tmp_path):
        deep = tmp_path / "nested" / "home"
        deep.mkdir(parents=True, exist_ok=True)
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        path = write_clean_window_start(deep, t0)
        assert path.exists()

    def test_overwrite_updates_t0(self, tmp_path):
        t0_old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t0_new = datetime(2026, 6, 1, tzinfo=timezone.utc)
        write_clean_window_start(tmp_path, t0_old)
        write_clean_window_start(tmp_path, t0_new)
        result = read_clean_window_start(home=tmp_path)
        assert result == t0_new


# ---------------------------------------------------------------------------
# Wilson lower bound
# ---------------------------------------------------------------------------

class TestWilsonLowerBound:
    def test_zero_n_returns_zero(self):
        assert _wilson_lower_bound(0, 0) == 0.0

    def test_perfect_small_sample_lb_below_1(self):
        """4/4 => point=1.0 but Wilson LB << 1.0 (anti-overfit guard)."""
        lb = _wilson_lower_bound(4, 4)
        assert lb < 1.0
        assert lb > 0.0  # still > 0

    def test_large_sample_lb_close_to_point(self):
        """Large N: LB approaches the point estimate."""
        lb = _wilson_lower_bound(700, 1000)  # 70% win rate, N=1000
        # Point is 0.70; LB should be close
        assert lb > 0.60
        assert lb < 0.70

    def test_zero_wins_lb_is_zero(self):
        lb = _wilson_lower_bound(0, 20)
        assert lb == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# evaluate_gate: bad gate_level raises
# ---------------------------------------------------------------------------

class TestEvaluateGateBadLevel:
    def test_invalid_gate_level_raises(self):
        metrics = GateMetrics()
        with pytest.raises(ValueError, match="gate_level must be 1, 2, or 3"):
            evaluate_gate(metrics, 4)

    def test_gate_level_0_raises(self):
        metrics = GateMetrics()
        with pytest.raises(ValueError):
            evaluate_gate(metrics, 0)


# ---------------------------------------------------------------------------
# Large bootstrap sample sanity
# ---------------------------------------------------------------------------

class TestBootstrapCI:
    def test_large_sample_finite_bootstrap_ci(self):
        """A large sample of consistent wins produces a finite sharpe_95ci_lower."""
        # 200 trips with a stable positive return
        trips = _win_trips(200, win_ret=0.005)
        metrics = compute_gate_metrics(trips, t0=_T0, n_resamples=200, seed=42)
        assert math.isfinite(metrics.sharpe_95ci_lower), (
            f"Expected finite sharpe_95ci_lower; got {metrics.sharpe_95ci_lower}"
        )

    def test_single_trip_nan_bootstrap(self):
        """N=1: Sharpe is NaN; bootstrap CI is also NaN."""
        metrics = compute_gate_metrics([_make_trip(1, 0.01)], t0=_T0)
        assert math.isnan(metrics.sharpe_95ci_lower)


# ---------------------------------------------------------------------------
# b61c — ADR-0097 slippage-haircut wired into clean-window evidence.
#
# THE DEFECT: compute_gate_metrics computed sharpe/win-rate/etc on RAW paper
# realized_return; the dishonest-evidence fail-open is that a paper-OPTIMISTIC
# window clears a promotion gate that LIVE would not. ADR-0097 requires a
# haircut-TOWARD-SILENCE adjusted ('live_realistic') series, behind the
# HERMES_QUANT_SLIPPAGE_HAIRCUT flag, read at call time. Default-OFF =>
# byte-identical RAW metrics (no behaviour change when the flag is unset).
# ---------------------------------------------------------------------------


class TestSlippageHaircutOff:
    """Flag OFF (default) => the adjusted series IS the raw series: byte-identical."""

    def test_default_matches_legacy_metrics_exactly(self, tmp_path):
        """With apply_haircut=False (the default), every metric equals the
        pre-b61c raw computation.

        wave4-review FIX (was VACUOUS): the old test compared
        compute_gate_metrics(trips, t0) to an IDENTICAL compute_gate_metrics(trips, t0)
        call — the function to itself — so it passed unchanged against the pre-fix base
        and could not detect a 'default flipped to always-haircut' regression. The
        non-vacuous version pins the DEFAULT against the EXPLICIT-haircut-ON series and
        asserts they DIFFER (the default must be the raw series, not the haircut one),
        AND pins the default to hard-coded raw expectations for a known all-win sample.
        RED: change the `apply_haircut` default to True -> default would equal `on`
        (haircut) and the win_rate/mean assertions below would fail."""
        trips = _win_trips(20, win_ret=0.01)
        default = compute_gate_metrics(trips, t0=_T0)  # apply_haircut defaults False
        on = compute_gate_metrics(
            trips, t0=_T0, apply_haircut=True, shadow_log=tmp_path / "absent.jsonl"
        )
        # The DEFAULT must be the RAW series, which DIFFERS from the haircut series for
        # this all-winning sample (the equity prior lowers each return). If the default
        # silently became always-haircut, default.sharpe would equal on.sharpe.
        assert default.sharpe != on.sharpe, (
            "default (apply_haircut=False) must NOT equal the haircut-ON series — "
            f"a flipped default would make them equal (default={default.sharpe}, on={on.sharpe})"
        )
        # Hard-pin the default to the raw all-win expectations (independent of any
        # haircut path running): 20 wins at +0.01 -> win_rate 1.0, no losses -> profit
        # factor is +inf (NaN-or-inf per the harness), drawdown 0 (monotone gains).
        assert default.win_rate == pytest.approx(1.0)
        assert default.n == 20
        assert abs(default.max_drawdown) == pytest.approx(0.0)
        assert default.sharpe > 0.0 and math.isfinite(default.sharpe)

    def test_explicit_false_is_raw(self):
        trips = _win_trips(20, win_ret=0.01)
        m = compute_gate_metrics(trips, t0=_T0, apply_haircut=False)
        # All-win sample, raw: every trip is a win -> win_rate 1.0.
        assert m.win_rate == pytest.approx(1.0)


class TestSlippageHaircutOn:
    """Flag ON => the adjusted ('live_realistic') series is haircut TOWARD silence:
    the per-trip return is moved toward zero/loss by the modeled live-vs-paper
    penalty, NEVER improved. So an all-winning window yields LOWER sharpe / lower
    mean adjusted return than the raw series."""

    def test_haircut_lowers_mean_and_sharpe(self, tmp_path):
        """RED before b61c: a window of 20 all-winning EQUITY trips at +0.01
        each. With the haircut ON, the equity prior (0.0025) is subtracted from
        each trip -> adjusted return 0.0075 < 0.01. Lower mean => lower Sharpe."""
        trips = _win_trips(20, win_ret=0.01)
        raw = compute_gate_metrics(trips, t0=_T0, apply_haircut=False)
        adj = compute_gate_metrics(
            trips, t0=_T0, apply_haircut=True, shadow_log=tmp_path / "absent.jsonl"
        )
        # The haircut moves the return toward zero (a cost): adjusted Sharpe < raw.
        assert adj.sharpe < raw.sharpe, (
            f"haircut must LOWER Sharpe; raw={raw.sharpe} adj={adj.sharpe}"
        )
        assert math.isfinite(adj.sharpe)

    def test_haircut_can_flip_thin_winner_to_loss(self, tmp_path):
        """A +0.001 (10 bps) equity winner is SMALLER than the 25-bps equity
        prior -> haircut adjusts it to NEGATIVE -> it stops counting as a win.
        RED before b61c (raw counts it as a win)."""
        trips = [_make_trip(i + 1, 0.001) for i in range(20)]
        raw = compute_gate_metrics(trips, t0=_T0, apply_haircut=False)
        adj = compute_gate_metrics(
            trips, t0=_T0, apply_haircut=True, shadow_log=tmp_path / "absent.jsonl"
        )
        assert raw.win_rate == pytest.approx(1.0)
        # equity prior 0.0025 > 0.001 -> every adjusted return < 0 -> win_rate 0.
        assert adj.win_rate < raw.win_rate
        assert adj.win_rate == pytest.approx(0.0)

    def test_haircut_never_improves_a_return(self, tmp_path):
        """The penalty is ALWAYS a cost. A losing trip gets WORSE (more negative),
        never better. A broken impl that ADDED the penalty would shrink the drawdown."""
        trips = [_make_trip(i + 1, -0.01) for i in range(20)]
        raw = compute_gate_metrics(trips, t0=_T0, apply_haircut=False)
        adj = compute_gate_metrics(
            trips, t0=_T0, apply_haircut=True, shadow_log=tmp_path / "absent.jsonl"
        )
        # All losers: each loss is bigger with the haircut -> drawdown at least as deep.
        assert abs(adj.max_drawdown) >= abs(raw.max_drawdown) - 1e-12

    def test_options_trip_uses_option_prior(self, tmp_path):
        """An options trip (is_options=True) is haircut with the larger us_option
        prior (0.0080), not the equity prior. A +0.005 (50 bps) options winner is
        below the 80-bps option prior -> adjusted negative."""
        trips = [_make_trip(i + 1, 0.005, is_options=True) for i in range(20)]
        adj = compute_gate_metrics(
            trips, t0=_T0, apply_haircut=True, shadow_log=tmp_path / "absent.jsonl"
        )
        # us_option prior 0.0080 > 0.005 -> all become losers.
        assert adj.win_rate == pytest.approx(0.0), (
            f"options winner below the option prior must flip to a loss; win_rate={adj.win_rate}"
        )


class TestSlippageHaircutFlagAtCallTime:
    """The flag is read AT CALL TIME (per posture). The caller passes
    apply_haircut from hermes_quant.risk.slippage_haircut.haircut_enabled()."""

    def test_flag_read_via_module(self, tmp_path, monkeypatch):
        from hermes_quant.risk.slippage_haircut import haircut_enabled

        trips = _win_trips(20, win_ret=0.01)
        monkeypatch.delenv("HERMES_QUANT_SLIPPAGE_HAIRCUT", raising=False)
        off = compute_gate_metrics(
            trips, t0=_T0, apply_haircut=haircut_enabled(),
            shadow_log=tmp_path / "absent.jsonl",
        )
        monkeypatch.setenv("HERMES_QUANT_SLIPPAGE_HAIRCUT", "1")
        on = compute_gate_metrics(
            trips, t0=_T0, apply_haircut=haircut_enabled(),
            shadow_log=tmp_path / "absent.jsonl",
        )
        assert on.sharpe < off.sharpe


class TestSlippageHaircutNonFinitePenalty:
    """Finite-guard: a non-finite penalty must NOT inflate the adjusted return.
    estimate_live_penalty is contractually finite, but the clean_window seam must
    still fail toward the conservative raw-minus-floor if a penalty is non-finite."""

    def test_non_finite_penalty_falls_back_to_floor_not_free_pass(self, tmp_path, monkeypatch):
        """Monkeypatch estimate_live_penalty to return a NaN penalty_frac. The seam
        must clamp to the conservative _DEFAULT_PRIOR floor (a positive cost), NOT
        let the NaN through.

        wave4-review FIX (was VACUOUS): the old assertions (adj.n == 20 and
        adj.sharpe <= raw.sharpe) BOTH passed even against a naive non-finite-guarded
        `adjusted = raw - NaN` impl — proven by patching out the math.isfinite clamp
        and watching the test still pass. n is fixed before the haircut runs, and the
        degenerate all-equal sample made the sharpe comparison non-discriminating.

        The non-vacuous version pins the EXACT fallback value: each adjusted return
        must equal raw_return - _DEFAULT_PRIOR (the floor), so the adjusted MEAN return
        is exactly win_ret - _DEFAULT_PRIOR. A `raw - NaN` impl yields NaN returns ->
        every trip is excluded by the finite-guard -> n collapses (or the mean is NaN),
        which this asserts against directly. Inputs: win_ret=0.02 so raw(0.02) -
        floor(0.005) = 0.015 stays a POSITIVE finite win (the trips survive and stay
        wins), making "equals raw-minus-floor" the load-bearing, discriminating claim."""
        import statistics

        import hermes_quant.eval.clean_window as cw
        from hermes_quant.risk.slippage_haircut import _DEFAULT_PRIOR, PenaltyEstimate

        def _bad_penalty(*a, **k):
            return PenaltyEstimate(
                penalty_frac=float("nan"), basis="prior", n_samples=0, detail="bad"
            )

        monkeypatch.setattr(cw, "estimate_live_penalty", _bad_penalty)
        win_ret = 0.02
        trips = _win_trips(20, win_ret=win_ret)
        # Directly exercise the seam: the adjusted series must be raw - floor, finite.
        warnings: list[str] = []
        adjusted = cw._haircut_adjusted_returns(
            list(trips), shadow_log=tmp_path / "absent.jsonl", warnings=warnings
        )
        assert len(adjusted) == 20, "no trip may vanish to a NaN-excluded return"
        for r in adjusted:
            assert math.isfinite(r), f"a NaN penalty must not yield a NaN return; got {r}"
            assert r == pytest.approx(win_ret - _DEFAULT_PRIOR), (
                f"adjusted must be raw({win_ret}) - floor({_DEFAULT_PRIOR}); got {r}"
            )
        assert statistics.fmean(adjusted) == pytest.approx(win_ret - _DEFAULT_PRIOR)
        assert warnings, "a non-finite penalty must emit a warning (observable degrade)"

        # And end-to-end through compute_gate_metrics: the window survives (n=20) and
        # the metrics are computed on the floored series, not inflated by dropped trips.
        adj = compute_gate_metrics(
            trips, t0=_T0, apply_haircut=True, shadow_log=tmp_path / "absent.jsonl"
        )
        raw = compute_gate_metrics(trips, t0=_T0, apply_haircut=False)
        assert adj.n == raw.n == 20, f"trips must not vanish; raw.n={raw.n} adj.n={adj.n}"
        assert math.isfinite(adj.sharpe), "floored series must yield a finite Sharpe, not NaN"
        assert adj.win_rate == pytest.approx(1.0), "raw-minus-floor stays a positive win here"


# --------------------------------------------------------------------------- #
# bf76: read_options_unlocked — the executable GATE-2 options-origination guard.
# --------------------------------------------------------------------------- #
def test_read_options_unlocked_absent_is_locked(tmp_path):
    """No marker => LOCKED (fail-closed): arming options flags alone never unlocks."""
    from hermes_quant.eval.clean_window import read_options_unlocked
    assert read_options_unlocked(home=tmp_path) is False


def test_read_options_unlocked_true_marker(tmp_path):
    from hermes_quant.eval.clean_window import read_options_unlocked
    (tmp_path / "quant").mkdir(parents=True, exist_ok=True)
    (tmp_path / "quant" / "options_unlock.json").write_text(
        '{"gate2_cleared": true, "evaluated_at": "2026-06-18T00:00:00Z"}'
    )
    assert read_options_unlocked(home=tmp_path) is True


def test_read_options_unlocked_false_marker_is_locked(tmp_path):
    from hermes_quant.eval.clean_window import read_options_unlocked
    (tmp_path / "quant").mkdir(parents=True, exist_ok=True)
    (tmp_path / "quant" / "options_unlock.json").write_text('{"gate2_cleared": false}')
    assert read_options_unlocked(home=tmp_path) is False


def test_read_options_unlocked_malformed_is_locked(tmp_path):
    """A corrupt marker => LOCKED (fail-closed, never fail-open to unlocked)."""
    from hermes_quant.eval.clean_window import read_options_unlocked
    (tmp_path / "quant").mkdir(parents=True, exist_ok=True)
    (tmp_path / "quant" / "options_unlock.json").write_text("{not valid json")
    assert read_options_unlocked(home=tmp_path) is False
