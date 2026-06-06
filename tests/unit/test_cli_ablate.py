"""tests/unit/test_cli_ablate.py — `hermes quant ablate` CLI verb (D2).

Runs a flag A/B walk-forward and prints a compact card (OFF/ON metrics, deltas,
DSR, PROMOTE/HOLD verdict). The heavy REAL-data path is gated behind
HERMES_QUANT_RUN_BACKTEST=1 (same release-gate convention as the fundamentals
ablation). Without the flag it prints the gate message and exits 0 — so CI never
stalls on bar-cache / yfinance history.

A `--synthetic` self-test path runs fully offline (deterministic GBM bars) so the
card-rendering + verdict plumbing is exercised per-PR without network.
"""

from __future__ import annotations

import argparse
import json

from hermes_quant.cli.ablate import cmd_ablate


def _ns(**kw) -> argparse.Namespace:
    base = dict(
        flag="HERMES_QUANT_STACKING",
        from_date="2024-01-02",
        to_date="2024-06-01",
        universe="SYN",
        on="1",
        off="0",
        synthetic=False,
        json=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# Release-gate behavior
# ---------------------------------------------------------------------------


def test_without_run_backtest_flag_prints_gate_message_exit_0(capsys, monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_RUN_BACKTEST", raising=False)
    rc = cmd_ablate(_ns())
    assert rc == 0
    out = capsys.readouterr().out
    assert "HERMES_QUANT_RUN_BACKTEST=1" in out
    # Mentions the real-data needs so the operator knows why it didn't run.
    assert "bar cache" in out.lower() or "history" in out.lower()


def test_without_run_backtest_flag_json_mode_exit_0(capsys, monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_RUN_BACKTEST", raising=False)
    rc = cmd_ablate(_ns(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is False
    assert payload["gate"] == "HERMES_QUANT_RUN_BACKTEST"


# ---------------------------------------------------------------------------
# Synthetic self-test path (offline, deterministic) — bypasses the data gate
# ---------------------------------------------------------------------------


def test_synthetic_runs_offline_and_prints_card(capsys, monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_RUN_BACKTEST", raising=False)
    rc = cmd_ablate(_ns(synthetic=True))
    assert rc == 0
    out = capsys.readouterr().out
    # Card surfaces the flag, both legs, and a verdict.
    assert "HERMES_QUANT_STACKING" in out
    assert "OFF" in out and "ON" in out
    assert "VERDICT" in out.upper()
    assert "PROMOTE" in out or "HOLD" in out


def test_synthetic_json_is_machine_readable(capsys, monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_RUN_BACKTEST", raising=False)
    rc = cmd_ablate(_ns(synthetic=True, json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ran"] is True
    assert payload["flag"] == "HERMES_QUANT_STACKING"
    assert payload["verdict"] in {"PROMOTE", "HOLD"}
    assert "deltas" in payload
    assert "d_sharpe" in payload["deltas"]


def test_synthetic_off_vs_off_is_null(capsys, monkeypatch):
    """With on==off the two legs are identical -> zero delta -> HOLD. Proves the
    card's determinism end-to-end through the CLI."""
    monkeypatch.delenv("HERMES_QUANT_RUN_BACKTEST", raising=False)
    rc = cmd_ablate(_ns(synthetic=True, json=True, on="0", off="0"))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["deltas"]["d_sharpe"] == 0.0
    assert payload["verdict"] == "HOLD"


# ---------------------------------------------------------------------------
# Wiring: registered in setup_argparse + routed by dispatch
# ---------------------------------------------------------------------------


def test_cli_registered_in_setup_argparse():
    from hermes_quant import cli

    parser = argparse.ArgumentParser()
    cli.setup_argparse(parser)
    args = parser.parse_args(
        ["ablate", "HERMES_QUANT_STACKING", "--from", "2024-01-02", "--to", "2024-06-01"]
    )
    assert args.quant_cmd == "ablate"
    assert args.flag == "HERMES_QUANT_STACKING"
    assert args.from_date == "2024-01-02"
    assert args.to_date == "2024-06-01"


def test_dispatch_routes_ablate(capsys, monkeypatch):
    from hermes_quant import cli

    monkeypatch.delenv("HERMES_QUANT_RUN_BACKTEST", raising=False)
    args = argparse.Namespace(
        quant_cmd="ablate",
        flag="HERMES_QUANT_STACKING",
        from_date="2024-01-02",
        to_date="2024-06-01",
        universe="SYN",
        on="1",
        off="0",
        synthetic=True,
        json=True,
    )
    rc = cli.dispatch(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["flag"] == "HERMES_QUANT_STACKING"
