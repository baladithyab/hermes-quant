"""ar87 — halt_state mirror + watchlist config writers fsync the PARENT DIR.

Atomic-write-durability family-completeness sweep (after ar86 fixed the kill-switch
writer; wave-6 fixed journal/artifacts). Two more money-state writers used only the
file-fd fsync (or, for the halt mirror, NO fsync at all) and never fsync'd the
containing directory after the rename:

  * ``hermes_quant.daemon.halt_state._write_atomic_json`` — the halt mirror is READ
    AS AUTHORITY by a SEPARATE LIVE PROCESS (the freqtrade crypto strategy, to avoid
    SQLite lock contention). A torn/lost write lets that live consumer read STALE
    halt state and trade an asset that is actually halted (fail-OPEN on a halt rail).
    Pre-fix it fsync'd NOTHING (write_text + replace).
  * ``hermes_quant.watchlist._save_config`` — the persisted tradeable universe
    (admit/evict from evolve_watchlist). A lost rename reverts an admit/evict.

POSIX: fsync(file_fd) flushes file DATA; the directory ENTRY the rename creates lives
in the parent dir's own dirty page, so the rename can be reverted by a power-loss
unless the parent dir is fsync'd too. Both tests spy os.fsync and assert a DIRECTORY
fd was fsync'd. Fail before the fix, pass after; non-vacuity via S_ISDIR.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest


def _spy_dir_fsync(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Patch os.fsync to record, per call, whether the fd referred to a directory."""
    saw: list[bool] = []
    real_fsync = os.fsync

    def _spy(fd: int) -> None:
        try:
            saw.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        except OSError:
            saw.append(False)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _spy)
    return saw


def test_halt_mirror_fsyncs_parent_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The halt_state JSON mirror (read by the live freqtrade process) must fsync
    the parent dir so the rename survives a crash."""
    from hermes_quant.daemon import halt_state

    saw = _spy_dir_fsync(monkeypatch)
    mirror = tmp_path / "quant" / "halt_state.json"
    halt_state._write_atomic_json(mirror, [{"asset": "BTC/USDT", "halt_epoch": 1}])

    assert mirror.exists()
    # Round-trips intact (non-vacuity on payload).
    import json

    assert json.loads(mirror.read_text()) == [{"asset": "BTC/USDT", "halt_epoch": 1}]
    assert any(saw), (
        "halt_state mirror fsync'd no directory fd — a crash after the rename can "
        "leave a live freqtrade reader on stale halt state (fail-OPEN on a halt rail)"
    )
    # No stray tmp left behind.
    assert not mirror.with_suffix(mirror.suffix + ".tmp").exists()


def test_watchlist_config_fsyncs_parent_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The persisted watchlist config (tradeable universe) must fsync the parent dir."""
    pytest.importorskip("yaml")
    from hermes_quant import watchlist

    saw = _spy_dir_fsync(monkeypatch)
    cfg_path = tmp_path / "quant" / "watchlist.yaml"
    watchlist._save_config(cfg_path, {"symbols": [{"symbol": "BTC/USDT", "asset_class": "crypto"}]})

    assert cfg_path.exists()
    assert any(saw), (
        "watchlist _save_config fsync'd no directory fd — a crash after the rename "
        "can revert an admit/evict on the tradeable universe"
    )
    assert not cfg_path.with_suffix(cfg_path.suffix + ".tmp").exists()
