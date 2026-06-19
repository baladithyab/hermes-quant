"""ar86 — trip_kill_switch must fsync the PARENT DIR after os.replace so the trip
survives a crash (atomic-write-durability family sibling wave-6 missed).

The wave-6 parallel find->fix wave fixed the journal (538b2f6) and artifacts
(8e69840) atomic-write durability gaps — write tmp -> fsync FILE -> rename ->
fsync PARENT DIR. But `hermes_quant.autonomous.trip_kill_switch` (the ADR-0016
§D9 kill-switch writer — the single highest-stakes money-state file) fsync'd only
the file fd, NOT the containing directory after `os.replace`.

POSIX: fsync(file_fd) flushes the file's DATA; the directory ENTRY that points the
filename at the new inode (created by the rename) lives in the parent directory's
own dirty page. A power-loss after os.replace returns but before the directory
metadata is flushed can REVERT the rename — the kill-switch file reads as its
PRE-trip state on reboot. For a safety rail that is a FAIL-OPEN: the system trips
the kill-switch at -12% drawdown, the box loses power, and on restart the rail reads
NOT-tripped and trading resumes. So trip_kill_switch must fsync the parent dir too.

This test pins the durability contract by spying on os.fsync and asserting the
CONTAINING DIRECTORY's fd is fsync'd (S_ISDIR) after the write, in addition to the
file fd. It fails before the fix (only the file fd is fsync'd) and passes after.
Non-vacuity: the assertion distinguishes a directory-fd fsync from a file-fd fsync
via os.fstat / stat.S_ISDIR, so a no-op fix cannot satisfy it.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from hermes_quant import autonomous


@pytest.fixture
def ks_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "quant" / "kill_switch.json"
    monkeypatch.setattr(autonomous, "KILL_SWITCH_PATH", p)
    return p


def _fsynced_a_directory(fsync_targets: list[int]) -> bool:
    """True iff any fsync'd fd referred to a DIRECTORY at the time of the call.

    We record the fds passed to os.fsync; a directory fd is opened O_RDONLY on the
    parent dir. We cannot fstat after the fds are closed, so the spy fstat-checks
    INSIDE the wrapped os.fsync (below) and records the S_ISDIR verdict directly.
    """
    return any(fsync_targets)


def test_trip_kill_switch_fsyncs_parent_dir(ks_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE RAIL: trip_kill_switch must fsync the containing directory after the
    rename so the trip survives a crash (not just the file fd)."""
    saw_dir_fsync: list[bool] = []
    real_fsync = os.fsync

    def _spy_fsync(fd: int) -> None:
        try:
            st = os.fstat(fd)
            saw_dir_fsync.append(stat.S_ISDIR(st.st_mode))
        except OSError:
            saw_dir_fsync.append(False)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _spy_fsync)

    autonomous.trip_kill_switch(
        cumulative_pnl_pct=-0.12, threshold_pct=0.10, reason="test-drawdown"
    )

    # The trip must have landed durably.
    assert ks_path.exists()
    # And a DIRECTORY fd must have been fsync'd (the parent-dir flush that makes the
    # rename itself crash-durable) — not only the file fd.
    assert any(saw_dir_fsync), (
        "trip_kill_switch fsync'd no directory fd — the os.replace rename is not "
        "crash-durable; a power-loss after the trip can revert the kill-switch to "
        "its pre-trip state (fail-OPEN on the ADR-0016 §D9 safety rail)"
    )


def test_trip_kill_switch_still_writes_correct_payload(ks_path: Path) -> None:
    """Non-vacuity / byte-identity on the happy path: the durability fix must not
    change WHAT is written. The tripped payload is intact and readable."""
    import json

    autonomous.trip_kill_switch(
        cumulative_pnl_pct=-0.12, threshold_pct=0.10, reason="test-drawdown"
    )
    data = json.loads(ks_path.read_text())
    assert data["tripped"] is True
    assert data["cumulative_pnl_pct"] == pytest.approx(-0.12)
    assert data["threshold_pct"] == pytest.approx(0.10)
    assert data["reason"] == "test-drawdown"
    # No stray .tmp left behind after the atomic rename.
    assert not ks_path.with_suffix(ks_path.suffix + ".tmp").exists()
