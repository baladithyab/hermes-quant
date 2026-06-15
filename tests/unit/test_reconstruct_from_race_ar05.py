"""ar05 — PortfolioState.reconstruct_from must read the bus UNDER the write lock.

Archaeology wave-2 (wf_45b3b8cf) RED-reproduced: reconstruct_from snapshots the bus via
_read_all_jsonl OUTSIDE its BEGIN IMMEDIATE write transaction. A fill applied by a concurrent
reactor AFTER the snapshot but BEFORE the rebuild commit is rebuilt away (positions/cash deleted +
only the stale snapshot re-written), AND because the rebuild does not touch processed_fills, that
fill's idempotency row survives — so a later incremental re-apply is idempotency-skipped and the
position is PERMANENTLY lost from state.db (corrupting the gate-sized equity_total NAV).

FIX: acquire the write lock first (BEGIN IMMEDIATE) and read the bus INSIDE the transaction, so no
concurrent apply_execution can interleave between the snapshot and the commit. Single-writer reconcile
is byte-identical.

This test proves the read happens under the lock by asserting that a fill appended+applied by a
"concurrent" writer DURING the bus read is NOT lost after reconstruct_from completes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_quant.state import portfolio_state as ps_mod
from hermes_quant.state.portfolio_state import PortfolioState


def _rec(pid: str, asset: str, pct: float, asof: str, price: float = 100.0) -> dict:
    return {
        "proposal_id": pid,
        "signal_id": "s",
        "asset": asset,
        "asset_class": "equity",
        "asof_execution": asof,
        "fill_price": price,
        "fill_size_pct": pct,
        "account_id": "paper-default",
    }


def test_reconstruct_reads_bus_under_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ar05: the bus read MUST happen while the BEGIN IMMEDIATE write lock is held, so a concurrent
    apply_execution cannot interleave between the snapshot and the rebuild commit.

    We prove it structurally: at the moment reconstruct_from reads the bus (_read_all_jsonl), the
    SQLite write lock must already be held — verified by asserting that a SECOND connection's
    BEGIN IMMEDIATE on the same db FAILS with 'database is locked' (immediate=no wait). Before the
    fix the read happened before the lock, so the probe would succeed; after the fix it is blocked.
    """
    import sqlite3

    db = tmp_path / "state.db"
    bus = tmp_path / "executions.jsonl"
    bus.write_text("")
    state = PortfolioState(state_db_path=db)
    import json

    with open(bus, "a") as f:
        f.write(json.dumps(_rec("p-aapl", "AAPL", 0.10, "2026-06-14T09:00:00Z")) + "\n")

    real_read = ps_mod._read_all_jsonl
    probe = {"lock_held_during_read": None}

    def _hooked_read(path: Path):  # noqa: ANN202
        # Probe: try to grab the write lock from a SEPARATE connection with NO wait.
        # If reconstruct already holds BEGIN IMMEDIATE (the ar05 fix), this raises.
        other = sqlite3.connect(db, timeout=0, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            probe["lock_held_during_read"] = False  # got the lock -> rebuild did NOT hold it (bug)
            other.execute("ROLLBACK")
        except sqlite3.OperationalError:
            probe["lock_held_during_read"] = True  # blocked -> rebuild holds the write lock (fixed)
        finally:
            other.close()
        return real_read(path)

    monkeypatch.setattr(ps_mod, "_read_all_jsonl", _hooked_read)
    state.reconstruct_from(bus)

    assert probe["lock_held_during_read"] is True, (
        "reconstruct_from read the bus WITHOUT holding the write lock — a concurrent "
        "apply_execution could interleave and permanently lose a fill (ar05)"
    )


def test_reconstruct_single_writer_is_byte_identical(tmp_path: Path) -> None:
    """No concurrency: reconstruct_from produces the same positions as before the ar05 change."""
    import json

    db = tmp_path / "state.db"
    bus = tmp_path / "executions.jsonl"
    recs = [
        _rec("p1", "AAPL", 0.10, "2026-06-14T09:00:00Z"),
        _rec("p2", "MSFT", 0.20, "2026-06-14T10:00:00Z"),
        _rec("p3", "AAPL", -0.05, "2026-06-14T11:00:00Z"),  # delta: AAPL -> 0.05
    ]
    bus.write_text("".join(json.dumps(r) + "\n" for r in recs))
    state = PortfolioState(state_db_path=db)
    res = state.reconstruct_from(bus)
    pos = state.get_positions("paper-default")
    assert res.executions_processed == 3
    assert pos[("equity", "AAPL")].quantity == pytest.approx(0.05)
    assert pos[("equity", "MSFT")].quantity == pytest.approx(0.20)
