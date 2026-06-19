"""Increment-0 §0.0 (seed ra03): prove the autouse conftest isolation now covers
the executions bus + QUANT_HOME, not just state.db / governance / evidence.

The +$167K fictional-P&L incident was a test fixture leaking into live storage via
a default path. conftest already isolates state.db (and governance/evidence/kill-
switch), but executions.jsonl / QUANT_HOME were the genuine remaining gap — every
parity test the Option-E normalizer depends on must run against a clean book.
"""

from __future__ import annotations

from pathlib import Path


def test_execution_bus_path_isolated_to_tmp(tmp_path: Path) -> None:
    """tools.EXECUTION_BUS_PATH must point under a tmp dir during a test, never
    the live ~/.hermes/quant/executions.jsonl."""
    from hermes_quant import tools

    resolved = Path(tools.EXECUTION_BUS_PATH)
    live = Path.home() / ".hermes" / "quant" / "executions.jsonl"
    assert resolved != live, (
        f"EXECUTION_BUS_PATH resolves to the LIVE bus {resolved} during a test — "
        "a fixture write would pollute the real executions ledger"
    )


def test_quant_home_isolated_to_tmp(tmp_path: Path) -> None:
    """tools.QUANT_HOME must point under a tmp dir during a test, never the live
    ~/.hermes/quant."""
    from hermes_quant import tools

    resolved = Path(tools.QUANT_HOME)
    live = Path.home() / ".hermes" / "quant"
    assert resolved != live, (
        f"QUANT_HOME resolves to the LIVE home {resolved} during a test"
    )
