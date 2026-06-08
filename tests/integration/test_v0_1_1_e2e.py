"""Integration tests for the v0.1.1 vertical slice.

Tests that exercise the whole pipeline end-to-end with synthetic data:
  - Daemon-style tick loop emits signals to the bus
  - Heartbeat emitter writes to the bus
  - Halt registry blocks emissions when active
  - Multi-process flock concurrency holds up under daemon + freqtrade-style
    concurrent writers

These tests are still unit-y (no real network, no real broker), but
exercise the full module wiring beyond what the per-module unit tests cover.
"""

from __future__ import annotations

import json
import multiprocessing
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from hermes_quant.aggregators.bma import BMAAggregator
from hermes_quant.daemon.halt_state import HaltStateSQLite
from hermes_quant.daemon.heartbeat import HeartbeatEmitter
from hermes_quant.daemon.signal_bus import (
    read_jsonl_tail,
)
from hermes_quant.daemon.tick_loop import (
    AssetTask,
    TickLoopState,
    run_one_tick,
)
from hermes_quant.protocol import (
    AnalystView,
    Portfolio,
)
from hermes_quant.risk.gate import DefaultRiskGate, RiskConfig


def _make_bars(n: int = 100, base: float = 60_000.0):
    """Synthetic crypto-like bars."""
    rng = np.random.default_rng(42)
    ts = pd.date_range("2026-05-13T00:00:00", periods=n, freq="1h")
    closes = base * np.cumprod(1 + 0.001 + rng.normal(0, 0.005, n))
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": closes,
            "high": closes * 1.005,
            "low": closes * 0.995,
            "close": closes,
            "volume": [1000.0] * n,
        }
    )


def _mock_provider(bars):
    p = MagicMock()
    p.name = "mock"
    p.fetch_bars.return_value = bars
    return p


def _mock_analyst(direction: int, name: str, conf: float = 0.7):
    a = MagicMock()
    a.name = name
    a.analyze.return_value = AnalystView(
        analyst=name,
        direction=direction,
        magnitude=0.02,
        confidence=conf,
        confidence_raw=conf + 0.2,
        horizon="4h",
    )
    return a


def _empty_portfolio_for(equity: float = 100_000.0):
    def _f(account_id, asset_class):
        return Portfolio(
            account_id=account_id,
            asset_class=asset_class,
            asof=pd.Timestamp.utcnow(),
            positions={},
            cash=equity,
            equity_total=equity,
            realized_pnl_total=0.0,
            realized_fees_total=0.0,
            peak_equity=equity,
            daily_open_equity=equity,
        )

    return _f


# ---------------------------------------------------------------------------
# End-to-end: tick → bus emission
# ---------------------------------------------------------------------------


class TestE2ETickEmits:
    def test_full_tick_emits_signal_to_bus(self, tmp_path):
        bus = tmp_path / "signals.jsonl"
        halt_db = tmp_path / "halts.db"
        halt_mirror = tmp_path / "halt.json"

        bars = _make_bars(100)
        provider = _mock_provider(bars)
        analysts = [_mock_analyst(1, "a"), _mock_analyst(1, "b")]
        agg = BMAAggregator(require_ensemble=False)
        gate = DefaultRiskGate(
            RiskConfig(
                cost_multiple=0.5,
                min_trade_size=0.0,
                cooldown_after_loss_minutes=0,
                max_position_pct=0.20,
            )
        )
        halt_state = HaltStateSQLite(halt_db, halt_mirror)

        state = TickLoopState()
        n = run_one_tick(
            tasks=[AssetTask("BTC/USDT", "crypto", "1h", exchange="binance", horizon="4h")],
            data_providers=[provider],
            analysts=analysts,
            aggregator=agg,
            risk_gate=gate,
            halt_state=halt_state,
            portfolio_for=_empty_portfolio_for(),
            state=state,
            bus_path=bus,
        )
        assert n >= 1

        # Verify the bus has a well-formed record
        records = read_jsonl_tail(bus, n=10)
        signals = [r for r in records if r.get("type") != "heartbeat"]
        assert len(signals) >= 1
        sig = signals[-1]
        # ADR-0008 schema, bumped to v2 by ADR-0068 (split bar_ts replay anchor
        # from asof_decision wall-clock; `asof` retains its v1 meaning == bar_ts).
        assert sig["schema_version"] == 2
        assert sig["asset"] == "BTC/USDT"
        assert "id" in sig
        assert "asof" in sig
        # ADR-0068 explicit aliases must be present on a v2 record.
        assert "bar_ts" in sig, "ADR-0068 bar_ts missing from v2 bus record"
        assert "asof_decision" in sig, "ADR-0068 asof_decision missing from v2 bus record"
        assert sig["direction"] in (-1, 1)
        assert "target_position_pct" in sig
        assert "components" in sig
        # Phase-8 P0-A.1: decision_price MUST be on the bus record so the
        # freqtrade strategy + settlement loop can compute realized_return
        # correctly. A `decision_price=fill_price` artifact would make the
        # entire calibration loop train on noise.
        assert "decision_price" in sig, (
            "decision_price missing from bus record — Phase-8 P0-A.1 regression"
        )
        assert sig["decision_price"] > 0
        # Decision price comes from MarketContext.last_close (the most
        # recent bar close at signal.asof), so it should equal the test
        # bars' last close
        assert abs(sig["decision_price"] - float(bars["close"].iloc[-1])) < 1e-6

    def test_halt_blocks_emission(self, tmp_path):
        bus = tmp_path / "signals.jsonl"
        halt_state = HaltStateSQLite(tmp_path / "halts.db", tmp_path / "halt.json")

        # Pre-halt the asset
        halt_state.add_halt("default", "crypto", "BTC/USDT", reason="test")

        bars = _make_bars(100)
        provider = _mock_provider(bars)
        analysts = [_mock_analyst(1, "a")]
        agg = BMAAggregator(require_ensemble=False)
        gate = DefaultRiskGate(RiskConfig(cost_multiple=0.5, min_trade_size=0.0))
        state = TickLoopState()

        n = run_one_tick(
            tasks=[AssetTask("BTC/USDT", "crypto", "1h")],
            data_providers=[provider],
            analysts=analysts,
            aggregator=agg,
            risk_gate=gate,
            halt_state=halt_state,
            portfolio_for=_empty_portfolio_for(),
            state=state,
            bus_path=bus,
        )
        assert n == 0


# ---------------------------------------------------------------------------
# Heartbeat emitter writes to bus
# ---------------------------------------------------------------------------


class TestHeartbeatToBus:
    @pytest.mark.timeout(10)
    def test_heartbeat_writes_distinguishable_records(self, tmp_path):
        bus = tmp_path / "signals.jsonl"
        emitter = HeartbeatEmitter(
            get_state=lambda: {
                "last_tick_at": pd.Timestamp.utcnow(),
                "active_assets": ["BTC/USDT"],
            },
            interval_seconds=0.05,
            bus_path=bus,
        )
        emitter.start()
        time.sleep(0.2)
        emitter.stop(timeout=2.0)

        records = read_jsonl_tail(bus, n=50)
        heartbeats = [r for r in records if r.get("type") == "heartbeat"]
        assert len(heartbeats) >= 2

        # Heartbeats are JSON-distinguishable from regular signals
        for hb in heartbeats:
            assert "daemon_pid" in hb
            assert "uptime_seconds" in hb


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
