"""Tests for cli/halts.py — halt + resume + emergency-stop with synthesis-v2 ordering."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from hermes_quant.cli.halts import (
    _parse_scope_arg,
    cmd_emergency_stop,
    cmd_halt,
    cmd_resume,
)
from hermes_quant.daemon.halt_state import HaltStateSQLite


@pytest.fixture()
def isolated_quant_home(tmp_path: Path, monkeypatch):
    """Patch HOME so HaltStateSQLite + signal_bus paths land in tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # The defaults are computed at import time using Path.home(); patch via
    # module attributes
    fake_state_db = tmp_path / ".hermes" / "quant" / "state.db"
    fake_mirror = tmp_path / ".hermes" / "quant" / "halt_state.json"
    fake_signal_bus = tmp_path / ".hermes" / "quant" / "signals.jsonl"

    # Patch every spot that hardcodes the default
    from hermes_quant.daemon import halt_state as halt_module
    from hermes_quant.daemon import signal_bus as bus_module
    monkeypatch.setattr(halt_module, "DEFAULT_STATE_DB", fake_state_db)
    monkeypatch.setattr(halt_module, "DEFAULT_HALT_JSON_MIRROR", fake_mirror)
    monkeypatch.setattr(bus_module, "SIGNAL_BUS_PATH", fake_signal_bus)
    return tmp_path


class TestParseScopeArg:
    def test_wildcard_to_none(self):
        assert _parse_scope_arg("*") is None
        assert _parse_scope_arg("") is None
        assert _parse_scope_arg(None) is None

    def test_specific_passthrough(self):
        assert _parse_scope_arg("alpaca-paper") == "alpaca-paper"


class TestCmdHalt:
    def test_basic_halt_succeeds(self, isolated_quant_home, capsys):
        ns = argparse.Namespace(
            account="alpaca-paper", asset_class="crypto",
            asset="BTC/USDT", reason="manual halt for review",
        )
        rc = cmd_halt(ns)
        assert rc == 0
        out = capsys.readouterr().out
        assert "halted:" in out
        assert "alpaca-paper" in out

    def test_empty_reason_returns_1(self, isolated_quant_home, capsys):
        ns = argparse.Namespace(
            account="alpaca-paper", asset_class="crypto",
            asset="BTC/USDT", reason="",
        )
        rc = cmd_halt(ns)
        assert rc == 1
        err = capsys.readouterr().err
        assert "halt failed" in err

    def test_wildcard_account(self, isolated_quant_home, capsys):
        ns = argparse.Namespace(
            account="*", asset_class="*", asset="*",
            reason="emergency",
        )
        rc = cmd_halt(ns)
        assert rc == 0


class TestCmdResume:
    def test_resume_active_halt(self, isolated_quant_home, capsys):
        # First halt, then resume
        cmd_halt(argparse.Namespace(
            account="alpaca-paper", asset_class="crypto",
            asset="BTC/USDT", reason="halt",
        ))
        capsys.readouterr()  # clear

        rc = cmd_resume(argparse.Namespace(
            account="alpaca-paper", asset_class="crypto",
            asset="BTC/USDT", reason="reviewed and clear",
        ))
        assert rc == 0
        out = capsys.readouterr().out
        assert "resumed" in out

    def test_resume_nonexistent_returns_1(self, isolated_quant_home, capsys):
        rc = cmd_resume(argparse.Namespace(
            account="alpaca-paper", asset_class="crypto",
            asset="BTC/USDT", reason="r",
        ))
        assert rc == 1
        err = capsys.readouterr().err
        assert "no active halt" in err

    def test_empty_reason_rejected(self, isolated_quant_home, capsys):
        cmd_halt(argparse.Namespace(
            account="alpaca-paper", asset_class="crypto",
            asset="BTC/USDT", reason="halt",
        ))
        capsys.readouterr()

        rc = cmd_resume(argparse.Namespace(
            account="alpaca-paper", asset_class="crypto",
            asset="BTC/USDT", reason="",
        ))
        assert rc == 1


class TestCmdEmergencyStop:
    def test_emergency_stop_creates_durable_halt_first(self, isolated_quant_home, capsys):
        """Per synthesis-v2 §P0-D ordering: halt FIRST, then bus signal, then broker."""
        rc = cmd_emergency_stop(argparse.Namespace(account="alpaca-paper"))
        assert rc == 0
        out = capsys.readouterr().out
        # Verify the order in the output
        halt_idx = out.find("durable halt installed")
        signal_idx = out.find("halt signal emitted to bus")
        broker_idx = out.find("Broker cancel")
        assert halt_idx >= 0
        assert signal_idx > halt_idx, "signal must come AFTER halt per synthesis-v2 §P0-D"
        assert broker_idx > signal_idx, "broker must come AFTER signal"

    def test_emergency_stop_persists_halt(self, isolated_quant_home):
        """The durable halt must be queryable AFTER emergency-stop returns."""
        cmd_emergency_stop(argparse.Namespace(account="alpaca-paper"))
        # Use the patched defaults to verify
        from hermes_quant.daemon import halt_state as _halt_module
        hs = HaltStateSQLite(
            db_path=_halt_module.DEFAULT_STATE_DB,
            mirror_path=_halt_module.DEFAULT_HALT_JSON_MIRROR,
        )
        active = hs.active_halts()
        assert len(active) == 1
        rec = active[0]
        assert rec.account_id == "alpaca-paper"
        assert rec.asset_class == "*"
        assert rec.reason == "operator_emergency_stop"

    def test_emergency_stop_emits_halt_signal_to_bus(self, isolated_quant_home):
        cmd_emergency_stop(argparse.Namespace(account="alpaca-paper"))
        # Verify the bus has a halt-type record
        from hermes_quant.daemon import signal_bus as bus_module
        bus_path = bus_module.SIGNAL_BUS_PATH
        assert bus_path.exists()
        import json
        records = [json.loads(line) for line in bus_path.read_text().splitlines() if line.strip()]
        halt_records = [r for r in records if r.get("type") == "halt"]
        assert len(halt_records) >= 1

    def test_emergency_stop_idempotent_when_already_halted(
        self, isolated_quant_home, capsys
    ):
        """If a halt already exists at the scope, emergency-stop continues
        with bus signal + broker step; doesn't crash."""
        cmd_emergency_stop(argparse.Namespace(account="alpaca-paper"))
        capsys.readouterr()
        rc = cmd_emergency_stop(argparse.Namespace(account="alpaca-paper"))
        # Second call: halt already exists, but bus signal + broker print still run
        assert rc == 0
        out = capsys.readouterr().out
        assert "halt signal emitted" in out
