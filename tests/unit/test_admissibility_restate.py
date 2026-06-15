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


_NAV = 100_000.0  # account equity_total backing the NAV-fraction -> share conversion


def _make_state_db(tmp_path: Path) -> Path:
    """Create a state.db with positions + cash (per AGENTS §1.3) + 4 shorts + 1 long.

    position.quantity is a NAV FRACTION (cumulative fill_size_pct), NOT shares
    (portfolio_state §D7). With NAV=100k each fraction converts to whole shares:
      AAPL -0.20 @ 200  -> floor(0.20*100k/200)  = 100 shares
      SMALLCAP -0.10 @ 12 -> floor(0.10*100k/12) = 833 shares
      MEMECO -0.05 @ 8   -> floor(0.05*100k/8)   = 625 shares
      NOSHORT -0.05 @ 50 -> floor(0.05*100k/50)  = 100 shares
    """
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
    conn.execute(
        """
        CREATE TABLE cash (
            account_id     TEXT PRIMARY KEY,
            balance_usd    REAL NOT NULL,
            last_update_at TEXT NOT NULL,
            equity_total   REAL NOT NULL
        ) WITHOUT ROWID;
        """
    )
    ts = "2026-05-25T14:00:00Z"
    rows = [
        ("paper-default", "equity", "AAPL", -0.20, 200.0, ts),  # ETB short
        ("paper-default", "equity", "SMALLCAP", -0.10, 12.0, ts),  # non-ETB short
        ("paper-default", "equity", "MEMECO", -0.05, 8.0, ts),  # absent from snapshot
        ("paper-default", "equity", "NOSHORT", -0.05, 50.0, ts),  # shortable=False
        ("paper-default", "equity", "MSFT", +0.20, 300.0, ts),  # long (ignored)
    ]
    conn.executemany(
        "INSERT INTO positions VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.execute(
        "INSERT INTO cash VALUES (?,?,?,?)",
        ("paper-default", _NAV, ts, _NAV),
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
            # Explicit data columns (not SELECT *): restate is a read-only data
            # guarantee, and PortfolioState's idempotent schema migration may
            # additively introduce columns (e.g. unit_kind, ar13/ar14 units fix).
            # Comparing the position DATA proves restate mutated no row VALUES
            # without coupling the assertion to the table's column count.
            return sorted(
                conn.execute(
                    "SELECT account_id, asset_class, symbol, quantity, "
                    "avg_entry_price, last_update_at FROM positions"
                ).fetchall()
            )
        finally:
            conn.close()

    before = _dump()
    restate_mod.restate_book(db, "paper-default", snapshot, oracle)
    after = _dump()
    assert before == after  # read-only guarantee (position data unchanged)


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


def test_restate_converts_nav_fraction_to_whole_shares(restate_mod, tmp_path):
    """The unit bug: positions are NAV fractions, not shares. Passing the fraction as
    qty made every short fail the whole-share check -> blanket FRACTIONAL_SHORT. After
    the fix, qty_shares is a non-fractional integer = floor(|fraction|*NAV/price), and the
    ETB short is ACCEPTED (NOT FRACTIONAL_SHORT)."""
    db = _make_state_db(tmp_path)
    snapshot = _snapshot()
    oracle = StaticETBAllowlistOracle(snapshot)
    result = restate_mod.restate_book(
        db, "paper-default", snapshot, oracle, asof_snapshot="2026-05-25"
    )
    by_symbol = {r["symbol"]: r for r in result["rows"]}

    # Conversion is exact whole shares (floor of magnitude, sign preserved).
    assert by_symbol["AAPL"]["qty_shares"] == -100  # floor(0.20*100k/200)
    assert by_symbol["SMALLCAP"]["qty_shares"] == -833  # floor(0.10*100k/12)
    assert by_symbol["MEMECO"]["qty_shares"] == -625  # floor(0.05*100k/8)
    assert by_symbol["NOSHORT"]["qty_shares"] == -100  # floor(0.05*100k/50)
    for row in result["rows"]:
        assert isinstance(row["qty_shares"], int)

    # The ETB whole-share short is ACCEPTED — NOT silenced as FRACTIONAL_SHORT.
    assert by_symbol["AAPL"]["state"] == "ACCEPTED"
    assert by_symbol["AAPL"]["reason"] != "FRACTIONAL_SHORT"
    assert by_symbol["AAPL"]["reason"] is None
    # No short reports FRACTIONAL_SHORT anymore (the bug's signature).
    assert all(r["reason"] != "FRACTIONAL_SHORT" for r in result["rows"])


def test_restate_fail_closed_when_nav_unknown(restate_mod, tmp_path):
    """No cash row => NAV unknown => fail-closed: zero shares + MISSING_ACCOUNT_CONTEXT,
    NEVER an assumed-admissible ETB short. The ETB AAPL short must NOT be ACCEPTED."""
    db = _make_state_db(tmp_path)
    # Drop the cash row so equity_total is unknown.
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM cash WHERE account_id=?", ("paper-default",))
    conn.commit()
    conn.close()

    snapshot = _snapshot()
    oracle = StaticETBAllowlistOracle(snapshot)
    result = restate_mod.restate_book(
        db, "paper-default", snapshot, oracle, asof_snapshot="2026-05-25"
    )
    by_symbol = {r["symbol"]: r for r in result["rows"]}

    # Zero NAV -> zero shares -> the ETB name is NOT admitted (fail-closed).
    assert by_symbol["AAPL"]["qty_shares"] == 0
    assert by_symbol["AAPL"]["state"] == "REJECTED"
    assert result["n_accepted"] == 0  # nothing assumed admissible


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
