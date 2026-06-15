"""ar06 — DrawdownBaselineStore.reconcile must not lose a cross-process peak update.

Archaeology wave-2 (wf_45b3b8cf) found reconcile did a SELECT(peak) + ON-CONFLICT upsert on an
autocommit connection serialized only by a process-local RLock — no BEGIN IMMEDIATE. Two concurrent
same-account reconciles in different PROCESSES could both read the old peak; the low-equity loser
would then commit max(old, low) < the high writer's committed peak, LOWERING the durable HWM below the
true peak. A lowered peak makes the ADR-0004 drawdown breaker measure a smaller drawdown and trip
later/never — a fail-OPEN, violating the monotonic-HWM invariant (baseline_store.py:185-186).

The fix wrapped reconcile's read-then-upsert in BEGIN IMMEDIATE. This test regression-LOCKS it: it is
the cross-process race test the archaeology-verify (wo8wu8kou) flagged as missing (ar04/ar05 ship one;
ar06 did not). N concurrent processes reconcile the SAME account with DIFFERENT equities; the durable
peak MUST converge to the global MAX regardless of scheduling. Without BEGIN IMMEDIATE a lost update
can leave it below the max.
"""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

import pandas as pd
import pytest

from hermes_quant.risk.baseline_store import DrawdownBaselineStore

_ASOF = "2026-05-13T12:00:00Z"


def _reconcile_worker(db: str, mirror: str, equity: float, barrier: mp.Barrier, q: mp.Queue) -> None:
    store = DrawdownBaselineStore(db_path=Path(db), mirror_path=Path(mirror))
    barrier.wait()  # maximize the overlap window
    try:
        b = store.reconcile("acct", "crypto", equity, pd.Timestamp(_ASOF), "UTC")
        q.put(("ok", b.peak_equity))
    except Exception as exc:  # noqa: BLE001
        q.put(("err", f"{type(exc).__name__}: {exc}"))


def test_concurrent_reconcile_converges_to_global_max_peak(tmp_path: Path) -> None:
    """N processes reconcile the same account with different equities; the durable peak MUST equal
    the global max (no cross-process lost update lowers the HWM below the true peak)."""
    db = tmp_path / "state.db"
    mirror = tmp_path / "drawdown_baselines.json"
    # Seed a baseline so every worker does the read-then-write (not the no-row seed path).
    seed = DrawdownBaselineStore(db_path=db, mirror_path=mirror)
    seed.reconcile("acct", "crypto", 100_000.0, pd.Timestamp(_ASOF), "UTC")

    equities = [150_000.0, 120_000.0, 140_000.0, 110_000.0, 130_000.0]
    global_max = max(equities + [100_000.0])

    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(len(equities))
    q: mp.Queue = ctx.Queue()
    procs = [
        ctx.Process(target=_reconcile_worker, args=(str(db), str(mirror), eq, barrier, q))
        for eq in equities
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)

    results = [q.get() for _ in range(len(equities))]
    errs = [r for r in results if r[0] != "ok"]
    assert not errs, f"reconcile workers errored (expected serialized success): {errs}"

    # The durable peak, read fresh, MUST be the global max — no lost update lowered it.
    final = DrawdownBaselineStore(db_path=db, mirror_path=mirror).reconcile(
        "acct", "crypto", 1.0, pd.Timestamp(_ASOF), "UTC"
    )
    assert final.peak_equity == pytest.approx(global_max), (
        f"durable peak {final.peak_equity} != global max {global_max} — a cross-process "
        f"reconcile lost-update lowered the HWM (ar06 BEGIN IMMEDIATE regression)"
    )


def test_reconcile_monotonic_single_process_byte_identical(tmp_path: Path) -> None:
    """Single-process monotonic HWM is byte-identical after the BEGIN IMMEDIATE wrap."""
    store = DrawdownBaselineStore(db_path=tmp_path / "s.db", mirror_path=tmp_path / "m.json")
    store.reconcile("acct", "crypto", 100_000.0, pd.Timestamp(_ASOF), "UTC")
    b = store.reconcile("acct", "crypto", 130_000.0, pd.Timestamp(_ASOF), "UTC")
    assert b.peak_equity == 130_000.0
    b2 = store.reconcile("acct", "crypto", 90_000.0, pd.Timestamp(_ASOF), "UTC")
    assert b2.peak_equity == 130_000.0  # never decreases
