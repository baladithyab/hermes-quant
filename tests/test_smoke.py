"""Smoke test — verify register(ctx) runs cleanly with a mock context.

Per references/plugin-authoring.md "Smoke-testing your plugin BEFORE pushing".
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


def test_register_runs_clean():
    """Critical: register(ctx) MUST run without error, register expected things."""
    import hermes_quant
    ctx = MagicMock()
    ctx.register_tool = MagicMock()
    ctx.register_command = MagicMock()
    ctx.register_cli_command = MagicMock()
    ctx.register_hook = MagicMock()
    ctx.register_skill = MagicMock()

    hermes_quant.register(ctx)

    # Tools (per ADR-0007)
    tool_names = [c.kwargs.get("name") for c in ctx.register_tool.call_args_list]
    assert "quant_status" in tool_names
    assert "quant_show_signals" in tool_names
    assert "quant_show_views" in tool_names
    assert "quant_doctor" in tool_names

    # Slash command
    assert ctx.register_command.call_count >= 1
    cmd_names = [c.args[0] for c in ctx.register_command.call_args_list]
    assert "quant" in cmd_names

    # CLI subcommand registration
    assert ctx.register_cli_command.call_count >= 1

    # Discord slash deferred-install hook
    hook_names = [c.args[0] for c in ctx.register_hook.call_args_list]
    assert "pre_gateway_dispatch" in hook_names


def test_protocol_contracts_importable():
    """All protocol classes must be importable from the top-level."""
    from hermes_quant import (
        Action,
        AggregatedSignal,
        AnalystView,
        MarketContext,
    )
    # Just verify they're real classes
    assert hasattr(MarketContext, "__dataclass_fields__")
    assert hasattr(AnalystView, "__dataclass_fields__")
    assert hasattr(AggregatedSignal, "__dataclass_fields__")
    assert hasattr(Action, "__dataclass_fields__")


def test_plugin_yaml_valid():
    """plugin.yaml must parse and have required fields."""
    import yaml
    p = Path(__file__).parent.parent / "plugin.yaml"
    assert p.exists(), f"plugin.yaml missing at {p}"
    data = yaml.safe_load(p.read_text())
    assert data["name"] == "hermes-quant"
    assert data["kind"] == "standalone"
    assert "manifest_version" in data
    # Must use optional_env, NOT requires_env (per references/plugin-authoring.md)
    assert "requires_env" not in data, ("requires_env BLOCKS install. "
                                          "Use optional_env per plugin-authoring guide.")
    assert "optional_env" in data


def test_no_eager_heavy_imports():
    """register() must NOT eagerly import torch/sklearn/etc.
    Heavy deps must be lazy-loaded inside analyst/aggregator code.
    """
    import sys
    # Reset modules that might be cached from earlier tests
    for mod in list(sys.modules):
        if mod.startswith("hermes_quant") or mod in ("torch", "sklearn"):
            sys.modules.pop(mod, None)

    from unittest.mock import MagicMock

    import hermes_quant
    ctx = MagicMock()
    hermes_quant.register(ctx)

    # These MUST not be loaded just by register()
    assert "torch" not in sys.modules, "torch eagerly loaded; lazy-load in analyst code"
    # Note: sklearn may be pulled in by other things; we mainly care about torch


def test_canonical_cli_surface():
    """All canonical CLI subcommands per ADR-0009 §P1-11 must be parseable."""
    import argparse

    from hermes_quant.cli import setup_argparse

    parser = argparse.ArgumentParser()
    setup_argparse(parser)

    expected_subcommands = {
        "setup", "start", "stop", "restart", "uninstall", "status",
        "resume", "halt", "emergency-stop",
        "signals", "show-views", "doctor", "logs",
        "backtest", "backtest-replay",
        "freqtrade-setup", "freqtrade-backtest",
        "config",
    }
    # Parse a sample to verify the parser has all subcommands
    for sub in expected_subcommands:
        try:
            # Minimal valid args per subcommand
            if sub == "resume":
                parser.parse_args([sub, "alpaca-paper", "--reason", "test"])
            elif sub == "halt":
                parser.parse_args([sub, "alpaca-paper", "--reason", "test"])
            elif sub == "show-views":
                parser.parse_args([sub, "--asset", "BTC/USDT"])
            elif sub == "backtest":
                parser.parse_args([sub, "--symbol", "BTC/USDT",
                                   "--asset-class", "crypto",
                                   "--bars-file", "/dev/null"])
            elif sub == "backtest-replay":
                parser.parse_args([sub, "test-run-id"])
            elif sub == "freqtrade-backtest":
                parser.parse_args([sub, "test-signals.jsonl"])
            elif sub == "config":
                parser.parse_args([sub, "show"])
            else:
                parser.parse_args([sub])
        except SystemExit as e:
            # argparse exits on error — that's a parse failure
            raise AssertionError(f"subcommand {sub!r} failed to parse: SystemExit({e.code})")


def test_tools_are_safe_when_daemon_absent():
    """All read-only tools must produce graceful output when daemon hasn't started."""
    import json

    from hermes_quant.tools import quant_doctor, quant_show_signals, quant_status

    # These shouldn't raise even if the bus doesn't exist
    s = json.loads(quant_status({}))
    assert s["success"] is True
    assert "v0.1.0_state" in s

    sigs = json.loads(quant_show_signals({"n": 5}))
    assert sigs["success"] is True

    doc = json.loads(quant_doctor({}))
    assert doc["success"] is True
    assert "checks" in doc
