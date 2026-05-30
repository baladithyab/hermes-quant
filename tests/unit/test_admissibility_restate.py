"""Unit tests for ops/scripts/quant-admissibility-restate.py (ADR-0077 rollout phase 2).

Builds a synthetic state.db of fake shorts + one long, runs restate_book against a static snapshot,
and verifies: non-ETB shorts REJECT(NOT_ETB), ETB short ACCEPTED, longs ignored, read-only on the
positions table, and the §4.3 JSON shape. Deterministic, no network.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hermes_quant.admissibility import ETBSnapshotEntry, StaticETBAllowlistOracle

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "ops"
    / "scripts"
    / "quant-admissibility-restate.py"
)


def _load_script():
    spec = importlib.util.spec_from_file_location("quant_admissibility_restate", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def restate_mod():
    return _load_script()


def _make_state_db(tmp_path: Path) -> Path:
    """Create a state.db with the positions schema (per AGENTS §1.3) + 4 shorts + 1 long."""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE positions (
            account_id       TEXT NOT NULL,
            asset_class      TEXT NOT NULL,
            symbol           TEXT NOT NULL,
            quantity         REAL NOT NULL,
            avg_entry_price  REAL NOT NULL,
            last_update_at   TEXT NOT NULL,
            PRIMARY KEY (account_id, asset_class, symbol)
        ) WITHOUT ROWID;
        """
    )
    ts = "2026-05-25T14:00:00Z"
    rows = [
        ("paper-default", "equity", "AAPL", -50.0, 200.0, ts),  # ETB short
        ("paper-default", "equity", "SMALLCAP", -100.0, 12.0, ts),  # non-ETB short
        ("paper-default", "equity", "MEMECO", -30.0, 8.0, ts),  # absent from snapshot
        ("paper-default", "equity", "NOSHORT", -25.0, 50.0, ts),  # shortable=False
        ("paper-default", "equity", "MSFT", +40.0, 300.0, ts),  # long (ignored)
    ]
    conn.executemany(
        "INSERT INTO positions VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db


def _snapshot() -> dict[str, ETBSnapshotEntry]:
    asof = "2026-05-25"
    return {
        "AAPL": ETBSnapshotEntry("AAPL", asof, True, True, True, 0.0030),
        "SMALLCAP": ETBSnapshotEntry("SMALLCAP", asof, False, True, True, 0.0030),
        "NOSHORT": ETBSnapshotEntry("NOSHORT", asof, True, False, True, 0.0030),
        # MEMECO deliberately absent -> fail-closed REJECT(NOT_ETB)
    }


def test_restate_rejects_non_etb_shorts(restate_mod, tmp_path):
    db = _make_state_db(tmp_path)
    snapshot = _snapshot()
    oracle = StaticETBAllowlistOracle(snapshot)
    # Pin `now` so the carry estimate is deterministic.
    now = datetime(2026, 5, 30, 0, 0, 0, tzinfo=UTC)
    result = restate_mod.restate_book(
        db, "paper-default", snapshot, oracle, asof_snapshot="2026-05-25", now=now
    )
    by_symbol = {r["symbol"]: r for r in result["rows"]}

    assert by_symbol["AAPL"]["state"] == "ACCEPTED"
    assert by_symbol["AAPL"]["reason"] is None

    assert by_symbol["SMALLCAP"]["state"] == "REJECTED"
    assert by_symbol["SMALLCAP"]["reason"] == "NOT_ETB"

    # Absent-from-snapshot name is fail-closed REJECT(NOT_ETB).
    assert by_symbol["MEMECO"]["state"] == "REJECTED"
    assert by_symbol["MEMECO"]["reason"] == "NOT_ETB"

    # shortable=False -> NOT_SHORTABLE.
    assert by_symbol["NOSHORT"]["state"] == "REJECTED"
    assert by_symbol["NOSHORT"]["reason"] == "NOT_SHORTABLE"


def test_restate_long_positions_ignored(restate_mod, tmp_path):
    db = _make_state_db(tmp_path)
    snapshot = _snapshot()
    oracle = StaticETBAllowlistOracle(snapshot)
    result = restate_mod.restate_book(db, "paper-default", snapshot, oracle)
    assert result["n_shorts"] == 4  # the long is excluded
    assert "MSFT" not in {r["symbol"] for r in result["rows"]}


def test_restate_does_not_mutate_state_db(restate_mod, tmp_path):
    db = _make_state_db(tmp_path)
    snapshot = _snapshot()
    oracle = StaticETBAllowlistOracle(snapshot)

    def _dump():
        conn = sqlite3.connect(db)
        try:
            return sorted(conn.execute("SELECT * FROM positions").fetchall())
        finally:
            conn.close()

    before = _dump()
    restate_mod.restate_book(db, "paper-default", snapshot, oracle)
    after = _dump()
    assert before == after  # read-only guarantee


def test_restate_json_shape(restate_mod, tmp_path):
    db = _make_state_db(tmp_path)
    snapshot = _snapshot()
    oracle = StaticETBAllowlistOracle(snapshot)
    result = restate_mod.restate_book(
        db, "paper-default", snapshot, oracle, asof_snapshot="2026-05-25"
    )
    for key in (
        "asof_snapshot",
        "account_id",
        "n_shorts",
        "n_rejected",
        "n_rejected_not_etb",
        "n_accepted",
        "total_est_borrow_carry_usd",
        "rows",
    ):
        assert key in result, f"missing key {key}"
    assert result["n_rejected_not_etb"] <= result["n_rejected"] <= result["n_shorts"]
    assert result["n_accepted"] + result["n_rejected"] <= result["n_shorts"]
    # Exactly one ETB short accepted, three rejected (2 NOT_ETB + 1 NOT_SHORTABLE).
    assert result["n_accepted"] == 1
    assert result["n_rejected"] == 3
    assert result["n_rejected_not_etb"] == 2


def test_restate_main_json_runs(restate_mod, tmp_path, capsys):
    db = _make_state_db(tmp_path)
    snap_path = tmp_path / "snap.json"
    snap_path.write_text(
        '{"asof": "2026-05-25", "etb": {'
        '"AAPL": {"easy_to_borrow": true, "shortable": true, "marginable": true, "annual_cbr": 0.003}'
        "}}"
    )
    rc = restate_mod.main(
        [
            "--book",
            str(db),
            "--account-id",
            "paper-default",
            "--asof-snapshot",
            str(snap_path),
            "--oracle",
            "static",
            "--json",
        ]
    )
    assert rc == 0
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["account_id"] == "paper-default"
    assert payload["n_shorts"] == 4
