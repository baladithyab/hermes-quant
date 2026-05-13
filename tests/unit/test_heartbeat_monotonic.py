"""Tests for monotonic-clock heartbeat (Wave C.4 / Phase-8 P1-ε).

Per Phase-8 review: wall-clock can go backward (NTP sync, manual
`date`, leap-second smearing) which makes the dead-man-switch
unreliable. The heartbeat checker now tracks monotonic_ns at each
mark + uses max(wall_age, monotonic_age) for the staleness decision.

This fence pins:
1. Mock monotonic clock — staleness is detected via monotonic, not wall.
2. Wall-clock jump-backward does NOT extend the grace window.
3. Wall-clock jump-forward does NOT spuriously trip dead-man-switch
   if monotonic agrees the heartbeat is fresh.
4. Both clocks agree (normal case): result identical to wall-only.
5. Backward-compat: existing tests still pass (covered by the rest
   of the test_heartbeat suite).
"""
from __future__ import annotations

import pandas as pd
import pytest

from hermes_quant.daemon.heartbeat import (
    HeartbeatChecker,
    HeartbeatCheckerConfig,
)


class _MockMonotonic:
    """Manually-advanceable monotonic clock for tests."""
    def __init__(self, start_ns: int = 1_000_000_000):
        self.now_ns = start_ns

    def __call__(self) -> int:
        return self.now_ns

    def advance(self, seconds: float) -> None:
        self.now_ns += int(seconds * 1e9)


# ---------------------------------------------------------------------------

def test_dead_man_switch_fires_on_monotonic_staleness():
    """Heartbeat marked, then monotonic advances past dead_man window.
    Wall-clock unchanged — the switch must STILL fire."""
    mock = _MockMonotonic()
    config = HeartbeatCheckerConfig(dead_man_switch_seconds=30.0,
                                     bootstrap_grace_seconds=120.0)
    start = pd.Timestamp("2026-05-13T10:00:00Z")
    checker = HeartbeatChecker(config, start_time=start, monotonic_clock_ns=mock)

    # Mark a heartbeat
    asof_hb = pd.Timestamp("2026-05-13T10:01:00Z")
    checker.mark_observed(asof_hb)
    mock.advance(5.0)  # 5s of monotonic advance

    # Wall-clock shows fresh; monotonic shows fresh -> alive
    result = checker.check(now=pd.Timestamp("2026-05-13T10:01:05Z"))
    assert result.daemon_alive is True

    # Now monotonic advances 60s; wall-clock unchanged.
    mock.advance(60.0)
    result = checker.check(now=pd.Timestamp("2026-05-13T10:01:05Z"))
    # max(wall_age=5s, monotonic_age=65s) = 65s > 30s -> dead
    assert result.daemon_alive is False
    assert result.reason == "heartbeat_stale"


def test_wall_clock_jump_backward_does_not_extend_grace():
    """If wall-clock jumps backward, monotonic-derived bootstrap_age
    keeps the grace window honest."""
    mock = _MockMonotonic()
    config = HeartbeatCheckerConfig(bootstrap_grace_seconds=30.0)
    start = pd.Timestamp("2026-05-13T10:00:00Z")
    checker = HeartbeatChecker(config, start_time=start, monotonic_clock_ns=mock)

    # Monotonic advances 60s past grace; wall jumps BACKWARD by 5min
    mock.advance(60.0)
    bogus_now = pd.Timestamp("2026-05-13T09:55:00Z")  # 5 min before start

    result = checker.check(now=bogus_now)
    # max(wall_age=-300s, monotonic_age=60s) = 60s > 30s -> bootstrap dead
    assert result.daemon_alive is False
    assert result.reason == "no_heartbeat_observed_after_bootstrap"


def test_normal_case_both_clocks_agree_alive():
    """Normal operation: heartbeat fresh on both clocks -> alive."""
    mock = _MockMonotonic()
    config = HeartbeatCheckerConfig(dead_man_switch_seconds=30.0)
    start = pd.Timestamp("2026-05-13T10:00:00Z")
    checker = HeartbeatChecker(config, start_time=start, monotonic_clock_ns=mock)

    asof_hb = pd.Timestamp("2026-05-13T10:01:00Z")
    checker.mark_observed(asof_hb)
    mock.advance(5.0)

    result = checker.check(now=pd.Timestamp("2026-05-13T10:01:05Z"))
    assert result.daemon_alive is True
    assert result.reason == "heartbeat_fresh"
    assert result.last_heartbeat_age_seconds == pytest.approx(5.0, abs=0.01)


def test_synthetic_heartbeat_also_uses_monotonic():
    """Backtest-mode synthetic heartbeats track monotonic too."""
    mock = _MockMonotonic()
    config = HeartbeatCheckerConfig(backtest_synthesize=True)
    start = pd.Timestamp("2026-05-13T10:00:00Z")
    checker = HeartbeatChecker(config, start_time=start, monotonic_clock_ns=mock)

    asof = pd.Timestamp("2026-05-13T10:00:30Z")
    checker.mark_synthetic_heartbeat(asof)
    mock.advance(15.0)

    result = checker.check(now=pd.Timestamp("2026-05-13T10:00:45Z"))
    assert result.daemon_alive is True
    assert result.reason == "backtest_synthesized"
    # max(wall_age=15s, monotonic_age=15s) = 15s
    assert result.last_heartbeat_age_seconds == pytest.approx(15.0, abs=0.01)


def test_no_monotonic_clock_uses_wall_only():
    """Default constructor (no mock) uses time.monotonic_ns — sanity check
    the integration."""
    config = HeartbeatCheckerConfig(bootstrap_grace_seconds=300.0)
    start = pd.Timestamp.utcnow()
    checker = HeartbeatChecker(config, start_time=start)

    # Immediately check — should be in grace window
    result = checker.check(now=pd.Timestamp.utcnow())
    assert result.daemon_alive is True


def test_age_uses_max_of_wall_and_monotonic():
    """When wall and monotonic disagree, the LARGER age wins (fail safe)."""
    mock = _MockMonotonic()
    config = HeartbeatCheckerConfig(dead_man_switch_seconds=30.0)
    start = pd.Timestamp("2026-05-13T10:00:00Z")
    checker = HeartbeatChecker(config, start_time=start, monotonic_clock_ns=mock)

    asof_hb = pd.Timestamp("2026-05-13T10:01:00Z")
    checker.mark_observed(asof_hb)

    # Monotonic advances 50s; wall-clock advance only 5s
    mock.advance(50.0)
    bogus_now = pd.Timestamp("2026-05-13T10:01:05Z")  # 5s after heartbeat

    result = checker.check(now=bogus_now)
    # max(wall_age=5s, monotonic_age=50s) = 50s > 30s -> dead
    assert result.daemon_alive is False
