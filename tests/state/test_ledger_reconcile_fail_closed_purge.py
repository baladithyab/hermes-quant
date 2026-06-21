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
    assert "purge" in err.lower()


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


def test_apply_fails_closed_on_partial_reduction(tmp_path, monkeypatch, capsys):
    """wave-2 Q1a: --apply must FAIL-CLOSED (exit 4, no mutation) when the rebuild would
    REDUCE a live position's |qty| (a partial purge), not just when it fully zeroes it.

    A reset/incomplete log more often shrinks a position than deletes it (NVDA 600sh ->
    log backs 100sh); that lands in `changed`, which an unguarded --apply 'corrects'
    600 -> 100, destroying 500 broker-confirmed shares with exit 0.

    RED-PROOF: with the destructive-reduction guard removed, --apply proceeds (rc 0) and
    rewrites the qty down."""
    mod = _load()
    state_db = tmp_path / "state.db"
    execs = tmp_path / "executions.jsonl"
    # live: AAPL 600sh; the log backs only 100sh (a real partial-reset shape)
    _seed_state_db(state_db, [("paper-default", "equity", "AAPL", 600.0, 150.0)])
    rec = {
        "proposal_id": "p1", "signal_id": "s1", "asset": "AAPL", "asset_class": "equity",
        "timeframe": "1d", "asof_decision": "2026-06-18T00:00:00Z",
        "asof_execution": "2026-06-18T00:00:00Z", "target_position_pct": 100.0,
        "decision_price": 150.0, "fill_price": 150.0, "fill_size_pct": 100.0,
        "reactor_name": "paper", "human_in_the_loop": False, "approver_user_id": None,
        "reactor_metadata": {"account_id": "paper-default", "quantity": 100.0},
        "bar_ts": "2026-06-18T00:00:00Z", "play_tag": None, "schema_version": None,
    }
    execs.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    monkeypatch.setattr(mod, "DEFAULT_STATE_DB", state_db, raising=False)
    monkeypatch.setattr(mod, "DEFAULT_EXECUTIONS_PATH", execs, raising=False)
    monkeypatch.setattr(mod.sys, "argv", ["quant-ledger-reconcile.py", "--apply"])

    live_before = mod._positions(state_db, "paper-default")
    rc = mod.main()
    live_after = mod._positions(state_db, "paper-default")

    assert rc == 4, f"--apply must fail-CLOSED on a qty reduction, got {rc}"
    assert live_after == live_before, "state.db must be UNCHANGED — no real shares reduced away"
    assert "refusing --apply" in capsys.readouterr().err


def test_apply_fails_closed_on_cross_account_purge(tmp_path, monkeypatch, capsys):
    """wave-2 Q1b: reconcile's apply does an account-UNSCOPED DELETE FROM positions, so
    reconciling account A would DELETE a broker-confirmed position in account B. The guard
    must see ALL accounts and fail-CLOSED.

    RED-PROOF: with the cross-account check removed, --account=paper-default reports
    'PHANTOM: 0' for its own account and returns 0, silently purging paper-alt's row."""
    mod = _load()
    state_db = tmp_path / "state.db"
    execs = tmp_path / "executions.jsonl"
    # account A (paper-default): a position the log WILL back; account B (paper-alt): a
    # broker-confirmed position with NO log backing.
    _seed_state_db(
        state_db,
        [
            ("paper-default", "equity", "AAPL", 100.0, 150.0),
            ("paper-alt", "equity", "TSLA", 300.0, 200.0),
        ],
    )
    rec = {
        "proposal_id": "p1", "signal_id": "s1", "asset": "AAPL", "asset_class": "equity",
        "timeframe": "1d", "asof_decision": "2026-06-18T00:00:00Z",
        "asof_execution": "2026-06-18T00:00:00Z", "target_position_pct": 100.0,
        "decision_price": 150.0, "fill_price": 150.0, "fill_size_pct": 100.0,
        "reactor_name": "paper", "human_in_the_loop": False, "approver_user_id": None,
        "reactor_metadata": {"account_id": "paper-default", "quantity": 100.0},
        "bar_ts": "2026-06-18T00:00:00Z", "play_tag": None, "schema_version": None,
    }
    execs.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    monkeypatch.setattr(mod, "DEFAULT_STATE_DB", state_db, raising=False)
    monkeypatch.setattr(mod, "DEFAULT_EXECUTIONS_PATH", execs, raising=False)
    # reconcile ONLY paper-default — paper-alt's TSLA must NOT be silently purged.
    monkeypatch.setattr(
        mod.sys, "argv", ["quant-ledger-reconcile.py", "--apply", "--account", "paper-default"]
    )

    tsla_before = mod._all_positions(state_db).get(("paper-alt", "equity", "TSLA"))
    assert tsla_before is not None
    rc = mod.main()
    tsla_after = mod._all_positions(state_db).get(("paper-alt", "equity", "TSLA"))

    assert rc == 4, f"--apply must fail-CLOSED on a cross-account purge, got {rc}"
    assert tsla_after == tsla_before, "paper-alt TSLA must survive — cross-account purge blocked"
    err = capsys.readouterr().err
    assert "refusing --apply" in err


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
