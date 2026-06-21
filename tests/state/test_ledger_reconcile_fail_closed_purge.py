"""statedb-nvda-orphan clause (3): the ledger-reconcile tool must be asset_class-aware
AND must FAIL-CLOSED rather than silently purge a broker-confirmed position.

The hazard (real, found firing the daily phases): state.db carried an equity NVDA +
a us_option NVDA leg (broker-confirmed, +$30k lifetime), but executions.jsonl had been
RESET leaving ZERO NVDA records. The reconcile tool's --apply rebuilds positions from
the (incomplete) log via an options-BLIND reconstructor, so it classified the real NVDA
positions as 'phantom' and would have PURGED them. These tests pin:
  (3a) _positions keys by (asset_class, symbol) — an options leg and its equity underlying
       are DISTINCT, never collapsed onto one symbol key.
  (3b) --apply REFUSES (exit 4, no mutation) when it would purge any live position, unless
       --allow-purge is explicitly passed.

RED-PROOF: revert to symbol-only keying -> the (3a) test sees one key not two; drop the
fail-closed guard -> --apply purges and returns 0 (the (3b) test flips).
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "scripts" / "quant-ledger-reconcile.py"


def _load():
    spec = importlib.util.spec_from_file_location("quant_ledger_reconcile_x", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_state_db(db_path: Path, rows: list[tuple]) -> None:
    """Create a positions table (the real PK) and insert rows.

    rows: (account_id, asset_class, symbol, quantity, avg_entry_price).
    """
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            """CREATE TABLE IF NOT EXISTS positions (
                   account_id TEXT NOT NULL, asset_class TEXT NOT NULL, symbol TEXT NOT NULL,
                   quantity REAL NOT NULL, avg_entry_price REAL NOT NULL,
                   last_update_at TEXT NOT NULL,
                   unit_kind TEXT NOT NULL DEFAULT 'nav_fraction',
                   PRIMARY KEY (account_id, asset_class, symbol))"""
        )
        con.executemany(
            "INSERT INTO positions (account_id, asset_class, symbol, quantity, avg_entry_price, "
            "last_update_at, unit_kind) VALUES (?,?,?,?,?, '2026-06-18T08:35:35Z', 'true_unit')",
            rows,
        )
        con.commit()
    finally:
        con.close()


def _count_positions(db_path: Path) -> int:
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute("SELECT COUNT(*) FROM positions WHERE abs(quantity) > 1e-9").fetchone()[0]
    finally:
        con.close()


def test_positions_keyed_by_asset_class_and_symbol(tmp_path):
    """(3a) an equity NVDA and a us_option NVDA leg are DISTINCT positions, not collapsed.

    RED-PROOF: with symbol-only keying both rows map to 'NVDA' and the dict has 1 entry."""
    mod = _load()
    db = tmp_path / "state.db"
    _seed_state_db(
        db,
        [
            ("paper-default", "equity", "NVDA", 600.0, 160.10),
            ("paper-default", "us_option", "NVDA260626C00160000", -6.0, 4.50),
        ],
    )
    live = mod._positions(db, "paper-default")
    assert len(live) == 2, f"equity + option leg must be distinct keys, got {list(live)}"
    assert ("equity", "NVDA") in live
    assert ("us_option", "NVDA260626C00160000") in live
    assert mod._is_options(("us_option", "NVDA260626C00160000")) is True
    assert mod._is_options(("equity", "NVDA")) is False


def test_apply_fails_closed_when_it_would_purge(tmp_path, monkeypatch, capsys):
    """(3b) --apply REFUSES (exit 4) + does NOT mutate state.db when the rebuild would purge
    a live position with no backing execution. The NVDA orphan scenario exactly.

    RED-PROOF: drop the `if phantom and not args.allow_purge` guard -> --apply proceeds,
    rewrites the db (NVDA gone), returns 0."""
    mod = _load()
    state_db = tmp_path / "state.db"
    execs = tmp_path / "executions.jsonl"
    # state.db has the broker-confirmed NVDA positions...
    _seed_state_db(
        state_db,
        [
            ("paper-default", "equity", "NVDA", 600.0, 160.10),
            ("paper-default", "us_option", "NVDA260626C00160000", -6.0, 4.50),
        ],
    )
    # ...but executions.jsonl has ZERO NVDA records (reset log) -> rebuild yields nothing,
    # so both NVDA rows are 'phantom'. Empty (but present) log = no backing executions.
    execs.write_text("", encoding="utf-8")

    monkeypatch.setattr(mod, "DEFAULT_STATE_DB", state_db, raising=False)
    monkeypatch.setattr(mod, "DEFAULT_EXECUTIONS_PATH", execs, raising=False)
    monkeypatch.setattr(mod.sys, "argv", ["quant-ledger-reconcile.py", "--apply"])

    before = _count_positions(state_db)
    rc = mod.main()
    after = _count_positions(state_db)

    assert rc == 4, f"--apply must fail-CLOSED (exit 4) on purge, got {rc}"
    assert after == before == 2, "state.db must be UNCHANGED — no broker-confirmed position purged"
    err = capsys.readouterr().err
    assert "refusing --apply" in err
    assert "PURGE" in err


def test_allow_purge_override_permits_apply(tmp_path, monkeypatch):
    """--allow-purge is the explicit escape hatch for a KNOWN fixture-pollution cleanup:
    with it, --apply proceeds (the original tool purpose is preserved)."""
    mod = _load()
    state_db = tmp_path / "state.db"
    execs = tmp_path / "executions.jsonl"
    _seed_state_db(state_db, [("paper-default", "equity", "FIXTURE", 2200.0, 150.0)])
    execs.write_text("", encoding="utf-8")

    monkeypatch.setattr(mod, "DEFAULT_STATE_DB", state_db, raising=False)
    monkeypatch.setattr(mod, "DEFAULT_EXECUTIONS_PATH", execs, raising=False)
    monkeypatch.setattr(
        mod.sys, "argv", ["quant-ledger-reconcile.py", "--apply", "--allow-purge"]
    )
    rc = mod.main()
    # rc 0 (applied) — the override deliberately permits the purge the operator inspected.
    assert rc == 0, f"--allow-purge must permit the apply, got {rc}"


def test_dry_run_default_never_mutates(tmp_path, monkeypatch):
    """Belt-and-suspenders: the default (no --apply) is read-only on state.db."""
    mod = _load()
    state_db = tmp_path / "state.db"
    execs = tmp_path / "executions.jsonl"
    _seed_state_db(state_db, [("paper-default", "us_option", "NVDA260626C00160000", -6.0, 4.50)])
    execs.write_text("", encoding="utf-8")
    monkeypatch.setattr(mod, "DEFAULT_STATE_DB", state_db, raising=False)
    monkeypatch.setattr(mod, "DEFAULT_EXECUTIONS_PATH", execs, raising=False)
    monkeypatch.setattr(mod.sys, "argv", ["quant-ledger-reconcile.py"])
    rc = mod.main()
    assert rc == 0
    assert _count_positions(state_db) == 1  # untouched
