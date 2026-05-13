"""Unit tests for hermes_quant.daemon.heartbeat — bootstrap-grace dead-man-switch.

Anchor: synthesis-v2 §P0-C. Verifies:
- Before first heartbeat: if bootstrap_age > bootstrap_grace, daemon=DEAD.
- After first heartbeat: if last_heartbeat_age > dead_man, daemon=DEAD.
- Inside bootstrap grace with no heartbeat: daemon=ALIVE (waiting).
- Backtest mode synthesizes per-bar heartbeats (no spurious safe-stop).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from hermes_quant.daemon.heartbeat import (
    HeartbeatChecker,
    HeartbeatCheckerConfig,
    HeartbeatEmitter,
    heartbeat_asof,
    is_heartbeat_record,
)


# ---------------------------------------------------------------------------
# HeartbeatChecker — synthesis-v2 §P0-C bootstrap fix
# ---------------------------------------------------------------------------

class TestHeartbeatCheckerBootstrap:
    """Critical path: consumer boots with daemon dead → must enter safe-stop."""

    def test_inside_bootstrap_grace_no_heartbeat_alive(self):
        """During the grace window, no heartbeat is acceptable."""
        start = pd.Timestamp("2026-05-13T00:00:00Z")
        cfg = HeartbeatCheckerConfig(bootstrap_grace_seconds=120.0)
        c = HeartbeatChecker(cfg, start_time=start)

        # 60s after start, no heartbeat — still in grace
        result = c.check(start + pd.Timedelta(seconds=60))
        assert result.daemon_alive is True
        assert result.reason == "bootstrap_grace_active"

    def test_after_bootstrap_grace_no_heartbeat_dead(self):
        """THE BUG synthesis-v2 §P0-C fixes: bootstrap_age > grace and no heartbeat.

        Original logic returned ALIVE here (the `if last_heartbeat is None: return`
        early-exit). New logic flags daemon DEAD.
        """
        start = pd.Timestamp("2026-05-13T00:00:00Z")
        cfg = HeartbeatCheckerConfig(bootstrap_grace_seconds=120.0)
        c = HeartbeatChecker(cfg, start_time=start)

        # 121s after start, no heartbeat — grace expired
        result = c.check(start + pd.Timedelta(seconds=121))
        assert result.daemon_alive is False
        assert result.reason == "no_heartbeat_observed_after_bootstrap"
        assert result.last_heartbeat_age_seconds is None
        assert result.bootstrap_age_seconds == pytest.approx(121.0)

    def test_heartbeat_observed_inside_grace_clears_bootstrap(self):
        """A heartbeat observed during grace transitions to steady-state."""
        start = pd.Timestamp("2026-05-13T00:00:00Z")
        cfg = HeartbeatCheckerConfig(bootstrap_grace_seconds=120.0,
                                      dead_man_switch_seconds=60.0)
        c = HeartbeatChecker(cfg, start_time=start)

        # 30s in, heartbeat arrives
        c.mark_observed(start + pd.Timedelta(seconds=30))

        # Now check at 200s after start — bootstrap grace would have expired,
        # but we have a fresh heartbeat at 30s. The age = 200-30 = 170s, which
        # exceeds dead_man_switch (60s) — so daemon=DEAD by stale-heartbeat path.
        result = c.check(start + pd.Timedelta(seconds=200))
        assert result.daemon_alive is False
        assert result.reason == "heartbeat_stale"

    def test_default_bootstrap_grace_is_120(self):
        cfg = HeartbeatCheckerConfig()
        assert cfg.bootstrap_grace_seconds == 120.0


class TestHeartbeatCheckerSteadyState:
    def test_fresh_heartbeat_alive(self):
        start = pd.Timestamp("2026-05-13T00:00:00Z")
        cfg = HeartbeatCheckerConfig(dead_man_switch_seconds=60.0)
        c = HeartbeatChecker(cfg, start_time=start)
        c.mark_observed(start + pd.Timedelta(seconds=200))
        result = c.check(start + pd.Timedelta(seconds=210))
        assert result.daemon_alive is True
        assert result.reason == "heartbeat_fresh"
        assert result.last_heartbeat_age_seconds == pytest.approx(10.0)

    def test_stale_heartbeat_dead(self):
        start = pd.Timestamp("2026-05-13T00:00:00Z")
        cfg = HeartbeatCheckerConfig(dead_man_switch_seconds=60.0)
        c = HeartbeatChecker(cfg, start_time=start)
        c.mark_observed(start + pd.Timedelta(seconds=200))
        result = c.check(start + pd.Timedelta(seconds=300))
        assert result.daemon_alive is False
        assert result.reason == "heartbeat_stale"
        assert result.last_heartbeat_age_seconds == pytest.approx(100.0)

    def test_mark_observed_takes_max(self):
        """Out-of-order heartbeats: take the newest, not the latest-marked."""
        start = pd.Timestamp("2026-05-13T00:00:00Z")
        c = HeartbeatChecker(start_time=start)
        c.mark_observed(start + pd.Timedelta(seconds=50))
        c.mark_observed(start + pd.Timedelta(seconds=30))  # older — should be ignored
        result = c.check(start + pd.Timedelta(seconds=60))
        assert result.last_heartbeat_age_seconds == pytest.approx(10.0)


class TestHeartbeatCheckerBacktest:
    def test_backtest_mode_no_synthetic_yet_alive(self):
        """At backtest start, no synthetic heartbeat — still alive (just started)."""
        start = pd.Timestamp("2024-01-01T00:00:00Z")
        cfg = HeartbeatCheckerConfig(backtest_synthesize=True)
        c = HeartbeatChecker(cfg, start_time=start)

        # Even after a "long" elapsed time in backtest, no spurious dead-man
        result = c.check(start + pd.Timedelta(days=30))
        assert result.daemon_alive is True
        assert result.reason == "backtest_synthesized"

    def test_backtest_synthetic_per_bar(self):
        """The consumer marks a synthetic heartbeat per bar."""
        start = pd.Timestamp("2024-01-01T00:00:00Z")
        cfg = HeartbeatCheckerConfig(backtest_synthesize=True)
        c = HeartbeatChecker(cfg, start_time=start)
        for h in range(0, 24):
            c.mark_synthetic_heartbeat(start + pd.Timedelta(hours=h))
        result = c.check(start + pd.Timedelta(hours=24))
        assert result.daemon_alive is True
        assert result.reason == "backtest_synthesized"

    def test_backtest_ignores_real_heartbeat(self):
        """Backtest mode doesn't use mark_observed; only mark_synthetic_heartbeat."""
        start = pd.Timestamp("2024-01-01T00:00:00Z")
        cfg = HeartbeatCheckerConfig(backtest_synthesize=True)
        c = HeartbeatChecker(cfg, start_time=start)
        # Even with a real heartbeat marked, we use synthetic only
        c.mark_observed(start + pd.Timedelta(seconds=10))
        result = c.check(start + pd.Timedelta(hours=1))
        # No synthetic marked → still in initial backtest_synthesized state
        assert result.daemon_alive is True
        assert result.reason == "backtest_synthesized"


# ---------------------------------------------------------------------------
# is_heartbeat_record / heartbeat_asof helpers
# ---------------------------------------------------------------------------

class TestRecordHelpers:
    def test_is_heartbeat_record_positive(self):
        assert is_heartbeat_record({"type": "heartbeat", "schema_version": 1})

    def test_is_heartbeat_record_negative(self):
        assert not is_heartbeat_record({"type": "signal", "schema_version": 1})
        assert not is_heartbeat_record({"type": "heartbeat", "schema_version": 2})
        assert not is_heartbeat_record({})

    def test_heartbeat_asof_parsable(self):
        ts = heartbeat_asof({"asof": "2026-05-13T00:00:00.000000Z"})
        assert ts is not None
        assert ts.year == 2026

    def test_heartbeat_asof_missing(self):
        assert heartbeat_asof({}) is None

    def test_heartbeat_asof_malformed(self):
        assert heartbeat_asof({"asof": "garbage"}) is None


# ---------------------------------------------------------------------------
# HeartbeatEmitter — daemon side
# ---------------------------------------------------------------------------

class TestHeartbeatEmitter:
    def test_emit_writes_to_bus(self, tmp_path: Path):
        bus = tmp_path / "bus.jsonl"
        state = {"last_tick_at": pd.Timestamp("2026-05-13T00:00:00Z"),
                 "active_assets": ["BTC/USDT"]}
        emitter = HeartbeatEmitter(get_state=lambda: state, bus_path=bus)
        emitter.emit_now()
        assert bus.exists()
        import json
        rec = json.loads(bus.read_text().strip())
        assert rec["type"] == "heartbeat"
        assert rec["schema_version"] == 1
        assert "daemon_pid" in rec
        assert "uptime_seconds" in rec
        assert rec["active_assets"] == ["BTC/USDT"]

    def test_emit_handles_missing_last_tick(self, tmp_path: Path):
        """Daemon hasn't ticked yet — last_tick_at=None."""
        bus = tmp_path / "bus.jsonl"
        emitter = HeartbeatEmitter(get_state=lambda: {}, bus_path=bus)
        emitter.emit_now()
        import json
        rec = json.loads(bus.read_text().strip())
        assert rec["last_tick_seconds_ago"] is None
        assert rec["active_assets"] == []

    @pytest.mark.timeout(10)
    def test_start_stop_lifecycle(self, tmp_path: Path):
        """Thread starts, emits, and stops cleanly."""
        bus = tmp_path / "bus.jsonl"
        state = {"last_tick_at": pd.Timestamp("2026-05-13T00:00:00Z")}
        emitter = HeartbeatEmitter(
            get_state=lambda: state, interval_seconds=0.1, bus_path=bus
        )
        emitter.start()
        import time
        time.sleep(0.4)  # ~3-4 emits
        emitter.stop(timeout=2.0)
        # Bus should have at least 2 heartbeat records
        lines = bus.read_text().splitlines()
        assert len(lines) >= 2, f"got {len(lines)} heartbeats"
