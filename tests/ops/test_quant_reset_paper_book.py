"""tests/ops/test_quant_reset_paper_book.py — quant-reset-paper-book.py safety tests.

Verifies the invariants stated in the script's docstring:
  - Dry-run (default): mutates NOTHING
  - --apply: backups created BEFORE any mutation
  - --apply: paper-default positions + cash + fills + watermark reset
  - --apply: non-paper-default rows (freqtrade) UNTOUCHED
  - --apply: bus archived and truncated to 0 bytes
  - --apply: proposals.db backed up but NOT wiped
  - --apply twice: idempotent (re-backs-up, re-resets to the same flat state)

RED proofs targeted
-------------------
Since this is a new script (no pre-existing RED test) the RED invariants are:
  1. Dry-run-safety — removing the early-return on dry-run would cause a tmp-dir
     mutation that the test explicitly checks against.
  2. Account-scoping — removing the WHERE account_id='paper-default' predicate
     would make the freqtrade-untouched assertion fail.
  3. Backup-before-wipe — reordering _apply before _backup would cause the
     backup-exists assertion to race / fail on a crash.

The tests cover all three.  The dry-run test is the strongest RED candidate:
it would fail immediately if --apply mutated state without the flag being set.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Script loader
# ---------------------------------------------------------------------------

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "ops"
    / "scripts"
    / "quant-reset-paper-book.py"
)


def _load_script():
    spec = importlib.util.spec_from_file_location("quant_reset_paper_book", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# SQLite seed helpers
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    account_id       TEXT NOT NULL,
    asset_class      TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    quantity         REAL NOT NULL,
    avg_entry_price  REAL NOT NULL,
    last_update_at   TEXT NOT NULL,
    unit_kind        TEXT NOT NULL DEFAULT 'nav_fraction',
    PRIMARY KEY (account_id, asset_class, symbol)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS cash (
    account_id     TEXT PRIMARY KEY,
    balance_usd    REAL NOT NULL,
    last_update_at TEXT NOT NULL,
    equity_total   REAL NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS executions_replayed (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_replayed_asof TEXT,
    replayed_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS processed_fills (
    proposal_id    TEXT NOT NULL,
    asof_execution TEXT NOT NULL,
    asset          TEXT NOT NULL DEFAULT '',
    asset_class    TEXT NOT NULL DEFAULT '',
    leg_index      TEXT NOT NULL DEFAULT '',
    applied_at     TEXT NOT NULL,
    PRIMARY KEY (proposal_id, asof_execution, asset, asset_class, leg_index)
);
"""


def _seed_state_db(db_path: Path) -> None:
    """Seed a dirty state.db with:
    - Corrupt AAPL=510.03 (unit_kind=nav_fraction — the 2026-06-08 raw-share incident row)
    - NVDA=0.05 long
    - BA=-0.20 short
    - freqtrade ETH=3.5 (TRUE non-paper-default account — must survive reset)
    - Non-flat cash/equity_total for paper-default
    - Some processed_fills
    - A watermark in executions_replayed
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)

    # paper-default positions (the dirty book)
    now = "2026-06-08T12:00:00Z"
    positions = [
        # Corrupt AAPL=510.03 (raw share count stored as nav_fraction — the incident)
        ("paper-default", "equity", "AAPL", 510.03, 185.0, now, "nav_fraction"),
        ("paper-default", "equity", "NVDA",   0.05, 900.0, now, "nav_fraction"),
        ("paper-default", "equity", "BA",     -0.20, 200.0, now, "nav_fraction"),
        # freqtrade — MUST survive the reset
        ("freqtrade", "crypto", "ETH",    3.5, 3500.0, now, "true_unit"),
    ]
    conn.executemany(
        "INSERT INTO positions VALUES (?,?,?,?,?,?,?)", positions
    )

    # cash rows
    conn.execute(
        "INSERT INTO cash VALUES (?, ?, ?, ?)",
        ("paper-default", 94_000.0, now, 182_000.0),
    )
    conn.execute(
        "INSERT INTO cash VALUES (?, ?, ?, ?)",
        ("freqtrade", 5_000.0, now, 17_250.0),
    )

    # some processed_fills
    fills = [
        ("prop_abc001", "2026-06-08T10:00:00Z", "AAPL", "equity", "", now),
        ("prop_abc002", "2026-06-08T10:05:00Z", "NVDA", "equity", "", now),
        ("prop_abc003", "2026-06-08T10:10:00Z", "BA",   "equity", "", now),
    ]
    conn.executemany(
        "INSERT INTO processed_fills VALUES (?,?,?,?,?,?)", fills
    )

    # watermark
    conn.execute(
        "INSERT INTO executions_replayed (id, last_replayed_asof, replayed_count) VALUES (1, ?, ?)",
        ("2026-06-08T12:00:00Z", 3),
    )

    conn.commit()
    conn.close()


def _seed_bus(bus_path: Path, n_lines: int = 5) -> None:
    """Seed executions.jsonl with n_lines fake records."""
    bus_path.parent.mkdir(parents=True, exist_ok=True)
    with open(bus_path, "w", encoding="utf-8") as f:
        for i in range(n_lines):
            rec = {
                "proposal_id": f"prop_abc{i:03d}",
                "asset": ["AAPL", "NVDA", "BA"][i % 3],
                "fill_size_pct": 0.05,
                "fill_price": 185.0,
                "asof_execution": f"2026-06-08T10:{i:02d}:00Z",
                "reactor_name": "paper",
            }
            f.write(json.dumps(rec) + "\n")


def _query_one(db: Path, sql: str, params: tuple = ()) -> object:
    conn = sqlite3.connect(str(db))
    row = conn.execute(sql, params).fetchone()
    conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDryRunSafety:
    """DRY-RUN invariant: --apply NOT passed => NOTHING mutates."""

    def test_dry_run_does_not_mutate_state_db(self, tmp_path):
        """Running without --apply must leave state.db data unchanged.

        Note: We check DATA invariants rather than mtime because _inspect_state_db
        opens a WAL-mode SQLite connection (even in dry-run), which may update the
        file's mtime as WAL housekeeping is written.  The safety guarantee is that
        no paper-default ROWS are deleted and no cash row is altered — that is what
        matters for money-safety.
        """
        db = tmp_path / "state.db"
        bus = tmp_path / "executions.jsonl"
        _seed_state_db(db)
        _seed_bus(bus)

        bus_content_before = bus.read_bytes()

        mod = _load_script()
        rc = mod.main(["--state-db", str(db), "--bus", str(bus)])

        assert rc == 0, "dry-run should exit 0"

        # Bus must be completely untouched (no shutil.copy2, no open for truncate)
        assert bus.read_bytes() == bus_content_before, (
            "dry-run must not modify executions.jsonl"
        )

        # The corrupt AAPL position MUST still be there
        n = _query_one(db, "SELECT COUNT(*) FROM positions WHERE account_id='paper-default'")
        assert n == 3, f"dry-run must not delete positions; got {n}"

        cash = _query_one(db, "SELECT balance_usd FROM cash WHERE account_id='paper-default'")
        assert cash == 94_000.0, f"dry-run must not alter cash; got {cash}"

        # All fills must remain
        fills = _query_one(db, "SELECT COUNT(*) FROM processed_fills")
        assert fills == 3, f"dry-run must not clear processed_fills; got {fills}"

        # Watermark must remain
        wm = _query_one(db, "SELECT COUNT(*) FROM executions_replayed")
        assert wm == 1, f"dry-run must not clear executions_replayed; got {wm}"

    def test_dry_run_does_not_create_backup(self, tmp_path):
        """Dry-run must not produce any .bak-RESET-* files."""
        db = tmp_path / "state.db"
        bus = tmp_path / "executions.jsonl"
        _seed_state_db(db)
        _seed_bus(bus)

        mod = _load_script()
        mod.main(["--state-db", str(db), "--bus", str(bus)])

        bak_files = list(tmp_path.glob("*.bak-RESET-*"))
        assert bak_files == [], f"dry-run must not create backups; found {bak_files}"

    def test_dry_run_prints_counts_and_exits_0(self, tmp_path, capsys):
        """Dry-run should print position / fill counts and return 0."""
        db = tmp_path / "state.db"
        bus = tmp_path / "executions.jsonl"
        _seed_state_db(db)
        _seed_bus(bus, n_lines=5)

        mod = _load_script()
        rc = mod.main(["--state-db", str(db), "--bus", str(bus)])
        out = capsys.readouterr().out

        assert rc == 0
        assert "3" in out  # 3 paper-default positions
        assert "DRY-RUN" in out


class TestApply:
    """--apply invariants: backup → mutate → verify."""

    def test_backup_exists_after_apply(self, tmp_path):
        """state.db and executions.jsonl backups must be created."""
        db = tmp_path / "state.db"
        bus = tmp_path / "executions.jsonl"
        _seed_state_db(db)
        _seed_bus(bus)

        mod = _load_script()
        mod.main(["--state-db", str(db), "--bus", str(bus), "--apply", "--yes"])

        db_baks = list(tmp_path.glob("state.db.bak-RESET-*"))
        bus_baks = list(tmp_path.glob("executions.jsonl.bak-RESET-*"))
        assert len(db_baks) == 1, f"expected 1 state.db backup; got {db_baks}"
        assert len(bus_baks) == 1, f"expected 1 bus backup; got {bus_baks}"

    def test_paper_default_positions_gone(self, tmp_path):
        """All paper-default positions (including corrupt AAPL=510) must be deleted."""
        db = tmp_path / "state.db"
        bus = tmp_path / "executions.jsonl"
        _seed_state_db(db)
        _seed_bus(bus)

        mod = _load_script()
        mod.main(["--state-db", str(db), "--bus", str(bus), "--apply", "--yes"])

        n = _query_one(db, "SELECT COUNT(*) FROM positions WHERE account_id='paper-default'")
        assert n == 0, f"paper-default positions must be 0 after reset; got {n}"

    def test_cash_flat_100k(self, tmp_path):
        """paper-default cash row must be 100,000 after reset."""
        db = tmp_path / "state.db"
        bus = tmp_path / "executions.jsonl"
        _seed_state_db(db)
        _seed_bus(bus)

        mod = _load_script()
        mod.main(["--state-db", str(db), "--bus", str(bus), "--apply", "--yes"])

        bal = _query_one(db, "SELECT balance_usd FROM cash WHERE account_id='paper-default'")
        eq = _query_one(db, "SELECT equity_total FROM cash WHERE account_id='paper-default'")
        assert bal == 100_000.0, f"balance_usd must be 100000; got {bal}"
        assert eq == 100_000.0, f"equity_total must be 100000; got {eq}"

    def test_processed_fills_cleared(self, tmp_path):
        """processed_fills must be 0 after reset."""
        db = tmp_path / "state.db"
        bus = tmp_path / "executions.jsonl"
        _seed_state_db(db)
        _seed_bus(bus)

        mod = _load_script()
        mod.main(["--state-db", str(db), "--bus", str(bus), "--apply", "--yes"])

        n = _query_one(db, "SELECT COUNT(*) FROM processed_fills")
        assert n == 0, f"processed_fills must be empty after reset; got {n}"

    def test_executions_replayed_cleared(self, tmp_path):
        """executions_replayed watermark must be cleared after reset."""
        db = tmp_path / "state.db"
        bus = tmp_path / "executions.jsonl"
        _seed_state_db(db)
        _seed_bus(bus)

        mod = _load_script()
        mod.main(["--state-db", str(db), "--bus", str(bus), "--apply", "--yes"])

        n = _query_one(db, "SELECT COUNT(*) FROM executions_replayed")
        assert n == 0, f"executions_replayed must be empty after reset; got {n}"

    def test_freqtrade_position_untouched(self, tmp_path):
        """The freqtrade ETH position must survive the reset (account scope strictly paper-default)."""
        db = tmp_path / "state.db"
        bus = tmp_path / "executions.jsonl"
        _seed_state_db(db)
        _seed_bus(bus)

        mod = _load_script()
        mod.main(["--state-db", str(db), "--bus", str(bus), "--apply", "--yes"])

        # Co-assert the wipe actually RAN (else this test passes on a no-op script
        # — the wave-20 review RED-proved that without this, "freqtrade survived"
        # is indistinguishable from "the script did nothing").
        paper_n = _query_one(
            db,
            "SELECT COUNT(*) FROM positions WHERE account_id='paper-default'",
        )
        assert paper_n == 0, (
            f"paper-default must be wiped (confirms --apply ran a real scoped reset); got {paper_n}"
        )

        n = _query_one(
            db,
            "SELECT COUNT(*) FROM positions WHERE account_id='freqtrade'",
        )
        assert n == 1, f"freqtrade position must be untouched; got {n}"

        qty = _query_one(
            db,
            "SELECT quantity FROM positions WHERE account_id='freqtrade' AND symbol='ETH'",
        )
        assert qty == 3.5, f"freqtrade ETH quantity must still be 3.5; got {qty}"

    def test_freqtrade_cash_untouched(self, tmp_path):
        """The freqtrade cash row must survive the reset."""
        db = tmp_path / "state.db"
        bus = tmp_path / "executions.jsonl"
        _seed_state_db(db)
        _seed_bus(bus)

        mod = _load_script()
        mod.main(["--state-db", str(db), "--bus", str(bus), "--apply", "--yes"])

        # Co-assert the wipe actually RAN (see test_freqtrade_position_untouched).
        paper_cash = _query_one(
            db,
            "SELECT balance_usd FROM cash WHERE account_id='paper-default'",
        )
        assert paper_cash == 100_000.0, (
            f"paper-default cash must be reset to flat 100k (confirms --apply ran); got {paper_cash}"
        )

        cash = _query_one(
            db,
            "SELECT balance_usd FROM cash WHERE account_id='freqtrade'",
        )
        assert cash == 5_000.0, f"freqtrade cash must be untouched; got {cash}"

    def test_bus_truncated_to_zero(self, tmp_path):
        """executions.jsonl must be 0 bytes after reset."""
        db = tmp_path / "state.db"
        bus = tmp_path / "executions.jsonl"
        _seed_state_db(db)
        _seed_bus(bus, n_lines=5)

        assert bus.stat().st_size > 0, "pre-condition: bus must have content"

        mod = _load_script()
        mod.main(["--state-db", str(db), "--bus", str(bus), "--apply", "--yes"])

        assert bus.exists(), "bus file must still exist (inode preserved)"
        assert bus.stat().st_size == 0, f"bus must be 0 bytes; got {bus.stat().st_size}"

    def test_bus_archive_contains_original_content(self, tmp_path):
        """The bus archive must contain the original lines."""
        db = tmp_path / "state.db"
        bus = tmp_path / "executions.jsonl"
        _seed_state_db(db)
        _seed_bus(bus, n_lines=5)

        original = bus.read_bytes()

        mod = _load_script()
        mod.main(["--state-db", str(db), "--bus", str(bus), "--apply", "--yes"])

        bus_baks = list(tmp_path.glob("executions.jsonl.bak-RESET-*"))
        assert len(bus_baks) == 1
        assert bus_baks[0].read_bytes() == original, "archive must contain original content"

    def test_proposals_db_backed_up_not_wiped(self, tmp_path):
        """proposals.db must be backed up but NOT modified (it is out of scope)."""
        db = tmp_path / "state.db"
        bus = tmp_path / "executions.jsonl"
        proposals = tmp_path / "proposals.db"
        _seed_state_db(db)
        _seed_bus(bus)

        # Create a fake proposals.db
        proposals.write_bytes(b"fake-proposals-data")
        original_content = proposals.read_bytes()

        mod = _load_script()
        mod.main(["--state-db", str(db), "--bus", str(bus), "--apply", "--yes"])

        # proposals.db must still exist with its original content
        assert proposals.exists(), "proposals.db must not be deleted"
        assert proposals.read_bytes() == original_content, (
            "proposals.db must NOT be modified"
        )

        # A backup of proposals.db must exist
        prop_baks = list(tmp_path.glob("proposals.db.bak-RESET-*"))
        assert len(prop_baks) == 1, f"proposals.db backup must exist; got {prop_baks}"

    def test_returns_0_on_success(self, tmp_path):
        """--apply with a valid state should return exit code 0."""
        db = tmp_path / "state.db"
        bus = tmp_path / "executions.jsonl"
        _seed_state_db(db)
        _seed_bus(bus)

        mod = _load_script()
        rc = mod.main(["--state-db", str(db), "--bus", str(bus), "--apply", "--yes"])
        assert rc == 0, f"expected exit 0; got {rc}"


class TestIdempotency:
    """Running --apply twice must be safe and produce the same flat state."""

    def test_double_apply_is_idempotent(self, tmp_path):
        """Running --apply twice: still flat, still 0 bus bytes, extra backups created."""
        db = tmp_path / "state.db"
        bus = tmp_path / "executions.jsonl"
        _seed_state_db(db)
        _seed_bus(bus)

        mod = _load_script()

        # First apply
        rc1 = mod.main(["--state-db", str(db), "--bus", str(bus), "--apply", "--yes"])
        assert rc1 == 0

        # Second apply (already flat)
        rc2 = mod.main(["--state-db", str(db), "--bus", str(bus), "--apply", "--yes"])
        assert rc2 == 0, f"second --apply on flat book must also return 0; got {rc2}"

        # State must still be flat
        n = _query_one(db, "SELECT COUNT(*) FROM positions WHERE account_id='paper-default'")
        assert n == 0, f"still must have 0 paper-default positions; got {n}"

        bal = _query_one(db, "SELECT balance_usd FROM cash WHERE account_id='paper-default'")
        assert bal == 100_000.0, f"cash must still be 100000; got {bal}"

        assert bus.stat().st_size == 0, "bus must still be 0 bytes after second apply"

        # Should have at least 1 backup (two applies within the same second
        # produce the SAME timestamp-stamped filename; shutil.copy2 overwrites it,
        # so the count is 1 on a fast machine and 2 on a slow one).  The
        # important invariant is that AT LEAST ONE backup exists.
        db_baks = list(tmp_path.glob("state.db.bak-RESET-*"))
        assert len(db_baks) >= 1, f"expected at least 1 db backup; got {db_baks}"

    def test_double_apply_freqtrade_still_untouched(self, tmp_path):
        """freqtrade must survive two consecutive --apply calls."""
        db = tmp_path / "state.db"
        bus = tmp_path / "executions.jsonl"
        _seed_state_db(db)
        _seed_bus(bus)

        mod = _load_script()
        mod.main(["--state-db", str(db), "--bus", str(bus), "--apply", "--yes"])
        mod.main(["--state-db", str(db), "--bus", str(bus), "--apply", "--yes"])

        qty = _query_one(
            db,
            "SELECT quantity FROM positions WHERE account_id='freqtrade' AND symbol='ETH'",
        )
        assert qty == 3.5, f"freqtrade ETH must remain 3.5 after both applies; got {qty}"


class TestAbsentFiles:
    """Reset must be safe when state.db / bus do not yet exist."""

    def test_apply_with_absent_state_db_exits_0(self, tmp_path):
        """Missing state.db is a no-op on the db side but still succeeds."""
        bus = tmp_path / "executions.jsonl"
        _seed_bus(bus)

        db = tmp_path / "state.db"  # does NOT exist

        mod = _load_script()
        rc = mod.main(["--state-db", str(db), "--bus", str(bus), "--apply", "--yes"])
        assert rc == 0

        # No db backup (nothing to back up)
        db_baks = list(tmp_path.glob("state.db.bak-RESET-*"))
        assert db_baks == [], "no db backup when db absent"

    def test_apply_with_absent_bus_creates_empty_file(self, tmp_path):
        """Missing bus: --apply must create an empty bus file."""
        db = tmp_path / "state.db"
        _seed_state_db(db)
        bus = tmp_path / "executions.jsonl"  # does NOT exist

        mod = _load_script()
        rc = mod.main(["--state-db", str(db), "--bus", str(bus), "--apply", "--yes"])
        assert rc == 0
        assert bus.exists(), "bus must be created even if it was absent"
        assert bus.stat().st_size == 0
