"""Durability guarantees for hermes_quant.artifacts.atomic_write_json.

The module docstring of agents/llm_budget.py documents the shared
``atomic_write_json`` primitive as crash-safe ("a crash mid-write cannot leave a
half-written ledger"). That guarantee is only true if the tmp file is
flush+fsync'd BEFORE the rename and the parent directory is fsync'd AFTER the
rename — otherwise a power-loss/kernel-panic in the page-cache-flush window can
lose BOTH the new tmp data AND the rename metadata, reverting the file to its
prior (valid but stale, lower-cumulative-spend) contents. That silently
re-opens already-spent LLM budget (fail-OPEN, real money).

Every other money-state writer in the tree (governance/audit_log.py,
journal/writer.py, daemon/signal_bus.py, watchlist.py, autonomous.py,
playbook/watchlist_evolution.py) flush+fsync before rename. atomic_write_json
must too.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_quant import artifacts
from hermes_quant.agents import llm_budget


def _fsync_spy(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    return calls


def test_atomic_write_json_fsyncs_file_and_parent_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single atomic_write_json must fsync the file fd AND the parent dir fd.

    Two distinct fsync calls (file + directory) are required for the rename
    itself to survive a crash. With no fsync at all this assertion fails.
    """
    calls = _fsync_spy(monkeypatch)
    target = tmp_path / "nested" / "spend.json"

    artifacts.atomic_write_json(target, {"hello": "world"})

    # Round-trip survives.
    import json

    assert json.loads(target.read_text(encoding="utf-8")) == {"hello": "world"}
    # File fsync + parent-directory fsync = at least two distinct fsync calls.
    assert len(calls) >= 2, (
        "atomic_write_json must fsync the file fd and the parent-dir fd "
        f"before/after the rename; saw {len(calls)} fsync call(s)"
    )


def test_llm_budget_record_persists_durably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLMBudgetGuard.record() persists the cumulative-spend ledger through
    atomic_write_json; the documented crash-safety requires an fsync on the
    file fd before the rename (otherwise a crash reverts the spend ledger and
    re-opens already-spent budget)."""
    calls = _fsync_spy(monkeypatch)
    spend_path = tmp_path / "llm_budget" / "spend.json"
    guard = llm_budget.LLMBudgetGuard(
        ceilings=llm_budget.BudgetCeilings(per_tick_usd=1.0),
        path=spend_path,
    )

    guard.record(
        model_id="openai/gpt-4o",
        prompt_tokens=100,
        completion_tokens=50,
        decision_id="d1",
        tick_id="t1",
    )

    # The ledger must be on disk and durably flushed.
    assert spend_path.exists()
    snap = guard.snapshot(decision_id="d1", tick_id="t1")
    assert snap["tick_usd"] > 0.0
    assert len(calls) >= 2, (
        "the LLM budget spend ledger must be fsync'd (file + parent dir) on "
        f"record() to be crash-safe; saw {len(calls)} fsync call(s)"
    )
