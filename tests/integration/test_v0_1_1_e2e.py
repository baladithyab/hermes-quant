"""Integration tests for the v0.1.1 vertical slice.

Tests that exercise the whole pipeline end-to-end with synthetic data:
  - Halt registry persists across simulated daemon restart
  - Multi-process flock concurrency holds up under daemon + freqtrade-style
    concurrent writers

These tests are still unit-y (no real network, no real broker), but
exercise the full module wiring beyond what the per-module unit tests cover.

NOTE (vestigial-daemon-spine deletion): the original tick-loop + heartbeat
end-to-end emission tests lived here. The documented daemon → signals.jsonl →
freqtrade spine they exercised is vestigial — the live spine is cron scripts
that call advisor.recommend + reactors directly — so `daemon/tick_loop.py` and
`daemon/heartbeat.py` were removed and those two test classes with them. The
flock-concurrency + halt-persistence + freqtrade-strategy-import coverage below
exercises the KEPT signal_bus / halt_state utilities and stays.
"""

from __future__ import annotations

import json
import multiprocessing
import sys
from pathlib import Path

import pandas as pd

from hermes_quant.daemon.halt_state import HaltStateSQLite

# ---------------------------------------------------------------------------
# Multi-process flock under daemon + freqtrade concurrent writers
# ---------------------------------------------------------------------------


def _bus_writer(args: tuple) -> int:
    bus_path_str, n_records, role = args
    bus_path = Path(bus_path_str)
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from hermes_quant.daemon.signal_bus import (  # noqa
        emit_execution_record,
        emit_signal_record,
    )

    if role == "daemon-signal":
        for i in range(n_records):
            emit_signal_record(
                {
                    "schema_version": 1,
                    "id": f"sig-{role}-{i:04d}",
                    "asof": pd.Timestamp.utcnow().isoformat(),
                    "asset": "BTC/USDT",
                    "exchange": "binance",
                    "timeframe": "1h",
                    "direction": 1,
                    "magnitude": 0.01,
                    "confidence": 0.6,
                    "horizon": "4h",
                    "target_position_pct": 0.05,
                    "reason": "test",
                    "halt": False,
                    "role": role,
                },
                path=bus_path,
            )
    else:  # freqtrade-exec
        for i in range(n_records):
            emit_execution_record(
                {
                    "schema_version": 1,
                    "exec_id": f"exec-{role}-{i:04d}",
                    "asof": pd.Timestamp.utcnow().isoformat(),
                    "asset": "BTC/USDT",
                    "side": "buy",
                    "qty": 0.001,
                    "fill_price": 60_000.0,
                    "decision_price": 60_000.0,
                    "fees": 0.05,
                    "account_id": "freqtrade",
                    "asset_class": "crypto",
                    "role": role,
                },
                path=bus_path,
            )
    return n_records


class TestDaemonFreqtradeConcurrentWrites:
    """Synthesis-v2 §P0-B canonical test: daemon + freqtrade hammer the SAME
    flock-protected bus concurrently. No corruption — exact line counts."""

    def test_concurrent_signal_and_exec_writes_no_corruption(self, tmp_path):
        signal_bus = tmp_path / "signals.jsonl"
        exec_bus = tmp_path / "executions.jsonl"

        with multiprocessing.Pool(4) as pool:
            results = pool.map(
                _bus_writer,
                [
                    (str(signal_bus), 30, "daemon-signal"),
                    (str(signal_bus), 20, "daemon-signal"),
                    (str(exec_bus), 25, "freqtrade-exec"),
                    (str(exec_bus), 30, "freqtrade-exec"),
                ],
            )

        assert sum(results) == 105

        # signal bus has exactly 50 well-formed lines
        sig_lines = signal_bus.read_text().splitlines()
        assert len(sig_lines) == 50
        for line in sig_lines:
            json.loads(line)  # raises if malformed

        # exec bus has exactly 55 well-formed lines
        exec_lines = exec_bus.read_text().splitlines()
        assert len(exec_lines) == 55
        for line in exec_lines:
            json.loads(line)


# ---------------------------------------------------------------------------
# Halt persistence across daemon restart simulation
# ---------------------------------------------------------------------------


class TestHaltPersistsAcrossSimulatedRestart:
    def test_halt_survives_reopen_via_sqlite(self, tmp_path):
        """Per synthesis-v2 §P0-D: halt registry must survive daemon restart."""
        db = tmp_path / "state.db"
        mirror = tmp_path / "halt.json"

        # First "daemon process" creates a halt
        hs1 = HaltStateSQLite(db, mirror)
        hs1.add_halt("alpaca-paper", "crypto", "BTC/USDT", reason="first")

        # "Restart": instantiate a fresh HaltStateSQLite
        hs2 = HaltStateSQLite(db, mirror)
        assert hs2.is_halted("alpaca-paper", "crypto", "BTC/USDT")
        active = hs2.active_halts()
        assert len(active) == 1
        assert active[0].reason == "first"


# ---------------------------------------------------------------------------
# Freqtrade strategy can be imported (smoke; no IStrategy method execution)
# ---------------------------------------------------------------------------


class TestFreqtradeStrategyImports:
    def test_strategy_module_importable(self):
        """The strategy file must be import-clean even without freqtrade installed."""
        # Direct file import (don't go through hermes_quant.consumers since that
        # may have its own __init__.py imports)
        strategy_path = Path(__file__).parent.parent.parent / (
            "hermes_quant/consumers/freqtrade/quant_consumer_strategy.py"
        )
        assert strategy_path.exists(), f"strategy missing at {strategy_path}"
        # Just verify it's valid Python by compiling
        with open(strategy_path) as f:
            compile(f.read(), str(strategy_path), "exec")

    def test_strategy_class_definition(self):
        """Verify HermesQuantConsumer is a defined class with expected methods."""
        from hermes_quant.consumers.freqtrade.quant_consumer_strategy import (
            HermesQuantConsumer,
        )

        # Per ADR-0008
        assert hasattr(HermesQuantConsumer, "populate_indicators")
        assert hasattr(HermesQuantConsumer, "populate_entry_trend")
        assert hasattr(HermesQuantConsumer, "populate_exit_trend")
        assert hasattr(HermesQuantConsumer, "custom_stake_amount")
        # Synthesis-v2 §P0-C heartbeat fields
        assert HermesQuantConsumer.bootstrap_grace_seconds == 120.0
        assert HermesQuantConsumer.dead_man_switch_seconds == 60.0
