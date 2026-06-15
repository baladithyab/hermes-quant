"""ar04 — HaltStateSQLite.add_halt cross-process race + the emergency-stop CLI must not crash.

Archaeology wave-2 (wf_45b3b8cf) RED-reproduced: add_halt does a cross-process check-then-insert on
an autocommit connection (isolation_level=None) serialized ONLY by a process-local threading.RLock —
no BEGIN IMMEDIATE. Two concurrent emergency-stops/halts collide on the UNIQUE PK; the loser raises
sqlite3.IntegrityError (or OperationalError on busy-timeout), NEITHER of which is a ValueError, so
cli/halts.py cmd_emergency_stop's `except ValueError` lets it escape as an unhandled crash BEFORE the
Step-2 bus halt-signal — defeating the documented HALT-FIRST ordering.

FIX: (1) wrap add_halt's SELECT+SELECT+INSERT critical section in BEGIN IMMEDIATE (mirror
state/portfolio_state.py:910), so the 5s busy_timeout serializes the loser behind the winner and it
hits the EXISTING 'active halt already exists' ValueError guard; (2) broaden the CLI emergency-stop
handler to also treat sqlite3.IntegrityError/OperationalError as 'a halt already exists / contention'
and CONTINUE the HALT-FIRST sequence rather than abort.

Offline-deterministic where possible; the cross-process leg uses multiprocessing + a barrier.
"""

from __future__ import annotations

import multiprocessing as mp
import sqlite3
from pathlib import Path

import pytest

from hermes_quant.daemon.halt_state import HaltStateSQLite


@pytest.fixture()
def hs(tmp_path: Path) -> HaltStateSQLite:
    return HaltStateSQLite(
        db_path=tmp_path / "state.db",
        mirror_path=tmp_path / "halt_mirror.json",
    )


def _add_halt_worker(db: str, mirror: str, barrier: mp.Barrier, q: mp.Queue) -> None:
    hs = HaltStateSQLite(db_path=Path(db), mirror_path=Path(mirror))
    barrier.wait()
    try:
        hs.add_halt(None, None, None, reason="concurrent emergency stop")
        q.put("ok")
    except ValueError:
        q.put("value_error")  # the EXPECTED loser outcome (active halt exists)
    except Exception as exc:  # noqa: BLE001
        q.put(f"{type(exc).__name__}")  # a non-ValueError == the ar04 race bug


def test_concurrent_add_halt_loser_raises_valueerror_not_integrityerror(
    tmp_path: Path,
) -> None:
    """Two processes race add_halt at the SAME (*,*,*) scope. The winner inserts; the loser MUST
    surface a ValueError ('active halt exists') — NOT a raw sqlite3.IntegrityError/OperationalError
    (which would escape the CLI's `except ValueError` and crash emergency-stop before the bus signal)."""
    # Pre-create the schema so both workers race the INSERT, not the CREATE TABLE.
    HaltStateSQLite(db_path=tmp_path / "state.db", mirror_path=tmp_path / "m.json")
    db = str(tmp_path / "state.db")
    mirror = str(tmp_path / "m.json")
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(2)
    q: mp.Queue = ctx.Queue()
    procs = [ctx.Process(target=_add_halt_worker, args=(db, mirror, barrier, q)) for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
    results = sorted(q.get() for _ in range(2))
    # Exactly one winner ('ok'); the loser is 'value_error', NEVER a raw sqlite error name.
    assert "ok" in results, f"expected one winner, got {results}"
    loser = [r for r in results if r != "ok"]
    assert loser == ["value_error"], (
        f"the losing concurrent add_halt must raise ValueError (caught by the CLI), "
        f"not a raw sqlite error; got {results}"
    )


def test_add_halt_still_rejects_duplicate_scope_single_process(hs: HaltStateSQLite) -> None:
    """Byte-identical single-process behavior: a second add_halt at an active scope still raises
    the existing ValueError guard (the BEGIN IMMEDIATE wrap must not change this)."""
    hs.add_halt("acct", "equity", "AAPL", reason="first")
    with pytest.raises(ValueError, match="active halt already exists"):
        hs.add_halt("acct", "equity", "AAPL", reason="second")


def test_emergency_stop_cli_continues_on_sqlite_contention(monkeypatch, tmp_path, capsys) -> None:
    """cmd_emergency_stop must NOT crash on a sqlite contention error — it must catch it and
    continue the HALT-FIRST sequence (Step 2 bus signal). Simulate add_halt raising an
    OperationalError (busy-timeout exhaustion) and assert the command returns cleanly + emits the
    bus signal rather than propagating the raw exception."""
    import argparse

    from hermes_quant.cli import halts as halts_cli
    from hermes_quant.daemon import halt_state as halt_module

    monkeypatch.setattr(halt_module, "DEFAULT_STATE_DB", tmp_path / "state.db")
    monkeypatch.setattr(halt_module, "DEFAULT_HALT_JSON_MIRROR", tmp_path / "m.json")

    def _boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(HaltStateSQLite, "add_halt", _boom)
    # Route the bus signal write somewhere harmless.
    monkeypatch.setenv("HERMES_QUANT_HOME", str(tmp_path / "qh"))

    rc = halts_cli.cmd_emergency_stop(argparse.Namespace(account=None))
    out = capsys.readouterr()
    # Must NOT propagate the raw OperationalError; must continue (rc 0) and reach the bus step.
    assert rc == 0, "emergency-stop must continue the HALT-FIRST sequence on a sqlite contention error"
    assert "halt" in (out.out + out.err).lower()
