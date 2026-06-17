"""ar24-family: persist_loss_cooldown_sidecar concurrent lost-update fix.

DEFECT (ar24-family, P2 — latent until POST_LOSS_COOLDOWN=1 is enabled):
    persist_loss_cooldown_sidecar did an unguarded read-merge-write.  Two
    concurrent processes (cron tick + agent tool) both read the same sidecar,
    each built a merged dict containing only its own disjoint key, then the
    second os.replace silently overwrote the first — dropping one process's
    cooldown entry.

FIX: wrap the entire read-merge-write in an exclusive cross-process flock on
    a `.lock` sidecar file (matching the _flocked pattern in watchlist.py).

RED->GREEN protocol:
    test_concurrent_persist_no_lost_update_with_lock — the canonical proof.
    Two multiprocessing.Process workers write disjoint asset keys simultaneously
    (coordinated via a Barrier so they overlap).  After both join, BOTH keys
    must be present in the sidecar.

    To reproduce the RED state (pre-fix), replace _flocked_sidecar with a
    no-op context manager in the function body.  The test then fails
    non-deterministically (one key is lost when the second writer's read races
    ahead of the first writer's os.replace).

NOTE: this test uses multiprocessing (not threading) because fcntl.flock is
    process-scoped — locks from different threads in the same process are the
    same lock holder and would not exhibit the race.
"""

from __future__ import annotations

import multiprocessing
import time
from pathlib import Path

import pandas as pd
import pytest

from hermes_quant.daemon.settlement_loop import (
    load_loss_cooldown_sidecar,
    persist_loss_cooldown_sidecar,
)


# ---------------------------------------------------------------------------
# Worker helpers (must be module-level for multiprocessing picklability)
# ---------------------------------------------------------------------------


def _worker_persist(
    sidecar_path: str,
    asset: str,
    barrier_path: str,
    ready_path: str,
    n_processes: int,
) -> None:
    """Worker: touch ready file, spin-wait for all workers, then persist."""
    # Signal readiness by creating a marker file.
    Path(ready_path).touch()

    # Busy-wait until all workers are ready (poor-man's barrier via files).
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if sum(1 for p in Path(barrier_path).glob("*.ready") if p.is_file()) >= n_processes:
            break
        time.sleep(0.001)

    # All workers are now ready — persist concurrently.
    ts = pd.Timestamp("2026-06-17T10:00:00Z")
    persist_loss_cooldown_sidecar(
        {("paper-default", "equity", asset): ts},
        Path(sidecar_path),
    )


def test_concurrent_persist_no_lost_update_with_lock(tmp_path: Path) -> None:
    """RED->GREEN: two concurrent processes writing disjoint assets both survive.

    Pre-fix: the second os.replace silently overwrote the first writer's
    result, dropping one key (non-deterministic but frequent).
    Post-fix: the exclusive flock serialises the two read-merge-write cycles
    so both keys land in the final sidecar.

    To reproduce the pre-fix RED state: temporarily replace the body of
    _flocked_sidecar with a no-op ``@contextmanager`` and re-run.
    """
    sidecar = tmp_path / "loss_cooldown_state.json"
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir()

    n_workers = 2
    assets = ["TSLA", "MSFT"]

    # Spawn workers.
    processes = []
    for i, asset in enumerate(assets):
        ready_marker = str(barrier_dir / f"{i}.ready")
        p = multiprocessing.Process(
            target=_worker_persist,
            args=(str(sidecar), asset, str(barrier_dir), ready_marker, n_workers),
            daemon=True,
        )
        p.start()
        processes.append(p)

    # Wait for all workers to finish (5s timeout per worker).
    for p in processes:
        p.join(timeout=10)
        assert p.exitcode == 0, f"worker {p.pid} exited with code {p.exitcode}"

    # Verify: BOTH keys must be present — no lost update.
    loaded = load_loss_cooldown_sidecar(sidecar)
    missing = [a for a in assets if ("paper-default", "equity", a) not in loaded]
    assert not missing, (
        f"concurrent lost-update: key(s) {missing!r} were dropped by a concurrent "
        f"persist_loss_cooldown_sidecar call. "
        f"Sidecar has: {list(loaded.keys())!r}. "
        f"The flock-based serialisation must prevent this."
    )


def test_sequential_persist_two_assets(tmp_path: Path) -> None:
    """Regression: sequential calls (no race) still write both keys correctly."""
    sidecar = tmp_path / "loss_cooldown_state.json"
    ts = pd.Timestamp("2026-06-17T10:00:00Z")
    persist_loss_cooldown_sidecar({("paper-default", "equity", "TSLA"): ts}, sidecar)
    persist_loss_cooldown_sidecar({("paper-default", "equity", "MSFT"): ts}, sidecar)
    loaded = load_loss_cooldown_sidecar(sidecar)
    assert ("paper-default", "equity", "TSLA") in loaded
    assert ("paper-default", "equity", "MSFT") in loaded
