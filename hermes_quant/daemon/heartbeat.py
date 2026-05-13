"""hermes_quant.daemon.heartbeat — Heartbeat emitter + dead-man-switch checker.

Per synthesis-v2 §P0-C: bootstrap hole fix. The original strategy logic
`if self._last_heartbeat is None: return` meant the strategy never entered
dead-man-safe-stop if it booted while the daemon was dead. New entries
proceeded from stale signals on the bus.

Fix: introduce a `bootstrap_grace_seconds` window (default 120s) during which
no heartbeat is required, but if NO heartbeat has been observed when the grace
expires, treat the daemon as dead.

The daemon emits a heartbeat record to signals.jsonl on every tick (and at
least every `tick_interval_seconds`). The freqtrade strategy reads them and
maintains `_last_heartbeat` + `_strategy_start_time`.

For backtests, the test harness synthesizes a per-bar heartbeat so the
dead-man-switch doesn't trigger spuriously in offline replay.

Heartbeat record schema (in signals.jsonl):
    {
        "schema_version": 1,
        "type": "heartbeat",
        "asof": "2026-05-13T00:00:00.000000Z",
        "daemon_pid": 12345,
        "uptime_seconds": 3600.0,
        "last_tick_seconds_ago": 0.5,
        "active_assets": ["BTC/USDT", "ETH/USDT"]
    }
"""
from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from hermes_quant.daemon.signal_bus import SIGNAL_BUS_PATH, emit_signal_record

# ---------------------------------------------------------------------------
# Heartbeat emitter (daemon side)
# ---------------------------------------------------------------------------

class HeartbeatEmitter:
    """Daemon-side heartbeat emitter.

    Runs in its own thread; emits a heartbeat record to signals.jsonl every
    `interval_seconds` seconds. Deduplication: only emit if the daemon's
    `last_tick_at` has been updated OR the interval has passed since last
    emit (whichever is longer).

    Per synthesis-v2 §P0-C: this is the source side of the dead-man-switch
    contract. The strategy is the consumer side.

    Args:
        get_state: callable returning a dict with current daemon state
            (last_tick_at, active_assets, etc.). Called every interval_seconds.
        interval_seconds: emission interval (default 10).
        bus_path: signal bus path.
        clock: function returning current UTC datetime (injectable for tests).
    """

    def __init__(
        self,
        get_state: Callable[[], dict],
        *,
        interval_seconds: float = 10.0,
        bus_path: Path = SIGNAL_BUS_PATH,
        clock: Callable[[], pd.Timestamp] = pd.Timestamp.utcnow,
    ):
        self._get_state = get_state
        self._interval = interval_seconds
        self._bus_path = bus_path
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._daemon_pid = os.getpid()
        self._daemon_start = self._clock()

    def emit_now(self) -> None:
        """Emit a heartbeat record immediately (synchronous)."""
        now = self._clock()
        state = self._get_state() or {}
        last_tick_at = state.get("last_tick_at")
        last_tick_ago = (
            (now - last_tick_at).total_seconds()
            if last_tick_at is not None
            else None
        )
        record = {
            "schema_version": 1,
            "type": "heartbeat",
            "asof": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "daemon_pid": self._daemon_pid,
            "uptime_seconds": (now - self._daemon_start).total_seconds(),
            "last_tick_seconds_ago": last_tick_ago,
            "active_assets": list(state.get("active_assets", [])),
        }
        emit_signal_record(record, path=self._bus_path)

    def start(self) -> None:
        """Spawn the emitter thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="hermes-quant-heartbeat", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the thread."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        # Emit immediately so consumers don't wait `interval` before first beat
        try:
            self.emit_now()
        except Exception:  # noqa: BLE001
            # Heartbeat emission failures should not crash the daemon
            pass
        while not self._stop.wait(self._interval):
            try:
                self.emit_now()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Heartbeat checker (consumer side, e.g. freqtrade strategy)
# ---------------------------------------------------------------------------

@dataclass
class HeartbeatCheckerConfig:
    """Per synthesis-v2 §P0-C: separate bootstrap grace from steady-state stale window."""
    bootstrap_grace_seconds: float = 120.0
    """Max seconds after consumer start with no heartbeat before declaring daemon dead.
    Default 120s = 2× typical daemon boot + first-tick fetch."""

    dead_man_switch_seconds: float = 60.0
    """Max seconds without a heartbeat (after first heartbeat seen) before
    declaring daemon dead. Default 60s = 6× heartbeat interval (10s)."""

    backtest_synthesize: bool = False
    """If True, the consumer is in backtest mode and should call
    `mark_synthetic_heartbeat` per bar instead of expecting real heartbeats."""


@dataclass
class HeartbeatCheckResult:
    """Result of a single dead-man-switch check."""
    daemon_alive: bool
    reason: str
    """One of: 'heartbeat_fresh', 'no_heartbeat_observed_after_bootstrap',
    'heartbeat_stale', 'bootstrap_grace_active', 'backtest_synthesized'."""
    last_heartbeat_age_seconds: float | None
    bootstrap_age_seconds: float


class HeartbeatChecker:
    """Consumer-side heartbeat checker.

    The freqtrade strategy (and any other consumer) maintains a
    HeartbeatChecker. On each tick:
        1. Update with the latest heartbeat seen (if any) via `mark_observed`.
        2. Call `check(now)` to get a HeartbeatCheckResult.
        3. If `daemon_alive=False`, enter safe-stop.

    Per synthesis-v2 §P0-C bootstrap fix:
        - Before any heartbeat is observed: if `(now - start) > bootstrap_grace`,
          declare daemon DEAD (reason='no_heartbeat_observed_after_bootstrap').
        - After a heartbeat is observed: if `(now - last_heartbeat) > stale`,
          declare daemon DEAD (reason='heartbeat_stale').

    Backtest mode: if `backtest_synthesize=True`, the consumer must call
    `mark_synthetic_heartbeat()` once per bar; this prevents the dead-man-switch
    from firing spuriously during offline replay.
    """

    def __init__(
        self,
        config: HeartbeatCheckerConfig | None = None,
        *,
        start_time: pd.Timestamp | None = None,
        monotonic_clock_ns: "Callable[[], int] | None" = None,
    ):
        self.config = config or HeartbeatCheckerConfig()
        self._start_time = start_time if start_time is not None else pd.Timestamp.utcnow()
        self._last_heartbeat: pd.Timestamp | None = None
        self._last_synthetic_heartbeat: pd.Timestamp | None = None

        # Wave C.4 (Phase-8 P1-ε): monotonic-clock liveness tracking.
        # Wall-clock can go backward (NTP sync, manual `date`, leap second
        # smearing) which makes the dead-man-switch unreliable. We track
        # monotonic_ns AT EACH mark_observed/mark_synthetic_heartbeat call;
        # the dead-man-switch decision uses monotonic age while the JSONL
        # records keep wall-clock asof (operators read those).
        import time as _time
        self._monotonic_clock_ns = monotonic_clock_ns or _time.monotonic_ns
        self._start_monotonic_ns = self._monotonic_clock_ns()
        self._last_heartbeat_monotonic_ns: int | None = None
        self._last_synthetic_monotonic_ns: int | None = None

    def mark_observed(self, asof: pd.Timestamp) -> None:
        """Update the last-heartbeat timestamp from a real heartbeat record.

        Wave C.4: we ALSO snapshot monotonic_ns at the moment of mark, so
        the dead-man-switch can decide via wall-clock-immune duration.
        """
        if self._last_heartbeat is None or asof > self._last_heartbeat:
            self._last_heartbeat = asof
            self._last_heartbeat_monotonic_ns = self._monotonic_clock_ns()

    def mark_synthetic_heartbeat(self, asof: pd.Timestamp) -> None:
        """In backtest mode, the consumer synthesizes per-bar heartbeats.

        Per synthesis-v2 §P0-C: backtests get a synthesized heartbeat per bar
        so the dead-man-switch doesn't trigger spuriously in offline replay.

        Wave C.4: monotonic snapshot too, for the same reason as
        mark_observed.
        """
        self._last_synthetic_heartbeat = asof
        self._last_synthetic_monotonic_ns = self._monotonic_clock_ns()

    def check(self, now: pd.Timestamp) -> HeartbeatCheckResult:
        # Wave C.4 (Phase-8 P1-ε): bootstrap_age and last-heartbeat age
        # both compute from monotonic_ns when the corresponding monotonic
        # snapshot exists. Wall-clock `now` is preserved for the
        # bootstrap/last fallback (legacy behavior + readability of result).
        now_mono_ns = self._monotonic_clock_ns()
        bootstrap_age_mono = (now_mono_ns - self._start_monotonic_ns) / 1e9
        bootstrap_age_wall = (now - self._start_time).total_seconds()
        # Use the LARGER of the two — this is conservative (a shorter
        # answer would let a wall-clock jump-backward extend the grace
        # window indefinitely; we want the dead-man-switch to fire as
        # soon as ANY clock confirms staleness).
        bootstrap_age = max(bootstrap_age_mono, bootstrap_age_wall)

        # Backtest mode: rely on synthetic heartbeats only
        if self.config.backtest_synthesize:
            if self._last_synthetic_heartbeat is None:
                return HeartbeatCheckResult(
                    daemon_alive=True,  # backtest just started; no signal yet
                    reason="backtest_synthesized",
                    last_heartbeat_age_seconds=None,
                    bootstrap_age_seconds=bootstrap_age,
                )
            age_wall = (now - self._last_synthetic_heartbeat).total_seconds()
            age_mono = (
                (now_mono_ns - self._last_synthetic_monotonic_ns) / 1e9
                if self._last_synthetic_monotonic_ns is not None
                else age_wall
            )
            age = max(age_wall, age_mono)
            return HeartbeatCheckResult(
                daemon_alive=True,
                reason="backtest_synthesized",
                last_heartbeat_age_seconds=age,
                bootstrap_age_seconds=bootstrap_age,
            )

        # Production mode
        if self._last_heartbeat is None:
            # Bootstrap path — synthesis-v2 §P0-C fix
            if bootstrap_age > self.config.bootstrap_grace_seconds:
                return HeartbeatCheckResult(
                    daemon_alive=False,
                    reason="no_heartbeat_observed_after_bootstrap",
                    last_heartbeat_age_seconds=None,
                    bootstrap_age_seconds=bootstrap_age,
                )
            # Still inside grace window
            return HeartbeatCheckResult(
                daemon_alive=True,
                reason="bootstrap_grace_active",
                last_heartbeat_age_seconds=None,
                bootstrap_age_seconds=bootstrap_age,
            )

        # Steady-state path — wall + monotonic, take max for safety.
        age_wall = (now - self._last_heartbeat).total_seconds()
        age_mono = (
            (now_mono_ns - self._last_heartbeat_monotonic_ns) / 1e9
            if self._last_heartbeat_monotonic_ns is not None
            else age_wall
        )
        age = max(age_wall, age_mono)
        if age > self.config.dead_man_switch_seconds:
            return HeartbeatCheckResult(
                daemon_alive=False,
                reason="heartbeat_stale",
                last_heartbeat_age_seconds=age,
                bootstrap_age_seconds=bootstrap_age,
            )

        return HeartbeatCheckResult(
            daemon_alive=True,
            reason="heartbeat_fresh",
            last_heartbeat_age_seconds=age,
            bootstrap_age_seconds=bootstrap_age,
        )


def is_heartbeat_record(record: dict) -> bool:
    """Identify heartbeat records on the signal bus."""
    return record.get("type") == "heartbeat" and record.get("schema_version") == 1


def heartbeat_asof(record: dict) -> pd.Timestamp | None:
    """Extract the `asof` timestamp from a heartbeat record."""
    asof = record.get("asof")
    if asof is None:
        return None
    try:
        return pd.Timestamp(asof)
    except (ValueError, TypeError):
        return None
