#!/usr/bin/env python3
"""ops/scripts/quant-reset-paper-book.py — Safe, idempotent paper-default book reset.

Wipes the paper-default paper book to a clean flat $100k state, suitable for
starting a fresh test run.  DEFAULT is DRY-RUN — it will print exactly what it
WOULD do and exit 0 without mutating anything.  Pass ``--apply`` (and
optionally ``--yes``) to actually mutate.

What it resets
--------------
1. state.db  (scoped ONLY to account_id='paper-default'):
   - DELETE FROM positions WHERE account_id='paper-default'
   - UPDATE cash SET balance_usd=100000, equity_total=100000 for paper-default
     (INSERT if the row is absent)
   - DELETE FROM processed_fills (account-agnostic; the fills are gone because
     executions.jsonl is archived)
   - DELETE FROM executions_replayed (watermark; rebuild will regenerate it
     from an empty bus)
   All mutations happen inside a single BEGIN IMMEDIATE / COMMIT so a crash
   mid-reset leaves state.db either fully reset or fully unreset (no half-state).

2. executions.jsonl  — the paper-default fill bus:
   Archived to <bus>.bak-RESET-<UTC-stamp> then truncated to zero bytes.

3. proposals.db  — if it exists, backed up but NOT modified (it is a separate
   store; wiping it would lose outstanding cron state that the operator may
   want to keep).

Account scope
-------------
ONLY paper-default is touched.  freqtrade (account_id='freqtrade') and
alpaca-paper (account_id='alpaca-paper') rows in state.db are LEFT UNTOUCHED.
The executions.jsonl bus is FULLY archived (it carries all producers), but it
is reconstructed as empty so a fresh rebuild from the empty bus naturally
produces a clean paper-default book while freqtrade/alpaca-paper will
rehydrate on their next tick from their own state sources.

Usage
-----
Dry-run (default — safe to run at any time):
    python3 ops/scripts/quant-reset-paper-book.py

Dry-run with explicit paths (for testing):
    python3 ops/scripts/quant-reset-paper-book.py \\
        --state-db /tmp/test/state.db \\
        --bus      /tmp/test/executions.jsonl

Actual reset (requires --apply):
    python3 ops/scripts/quant-reset-paper-book.py --apply

Actual reset with non-interactive confirm (CI / operator script):
    python3 ops/scripts/quant-reset-paper-book.py --apply --yes

Safety invariants
-----------------
- Dry-run exits 0 WITHOUT touching any file (verified by test).
- Backups are written BEFORE any mutation (verified by test).
- freqtrade / alpaca-paper rows survive (verified by test).
- Running --apply twice on an already-flat book is a clean no-op (re-backs-up,
  resets to the same flat state — idempotent).
- BEGIN IMMEDIATE wraps ALL state.db mutations (ar04/ar116 autocommit-race
  family — a SQLite store that is also written by live crons MUST use explicit
  transaction boundaries).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PAPER_DEFAULT_ACCOUNT = "paper-default"
FLAT_CASH_USD = 100_000.0

_DEFAULT_QUANT_HOME = Path.home() / ".hermes" / "quant"


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _resolve_paths(
    *,
    home: Path | None,
    state_db: Path | None,
    bus: Path | None,
) -> tuple[Path, Path, Path]:
    """Return (state_db_path, bus_path, proposals_db_path).

    Priority: explicit --state-db / --bus / --home, then QUANT_HOME env,
    then the hard-coded default (~/.hermes/quant).

    proposals.db is resolved from the SAME directory as state.db so that
    explicit --state-db /tmp/test/state.db automatically locates
    /tmp/test/proposals.db — making the script testable against a tmp dir
    without needing a separate --proposals-db flag.
    """
    quant_home = home or Path(os.environ.get("QUANT_HOME", str(_DEFAULT_QUANT_HOME)))
    resolved_state_db = state_db or (quant_home / "state.db")
    resolved_bus = bus or (quant_home / "executions.jsonl")
    # Co-locate proposals.db with state.db (same directory).
    resolved_proposals = resolved_state_db.parent / "proposals.db"
    return resolved_state_db, resolved_bus, resolved_proposals


# ---------------------------------------------------------------------------
# Inspection: what would we touch?
# ---------------------------------------------------------------------------


def _inspect_state_db(state_db: Path) -> dict:
    """Return counts of paper-default rows we would wipe, plus whether rows exist."""
    if not state_db.exists():
        return {
            "exists": False,
            "paper_positions": 0,
            "all_positions": 0,
            "processed_fills": 0,
            "has_cash_row": False,
            "executions_replayed": 0,
        }
    conn = sqlite3.connect(str(state_db), timeout=5.0, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        info: dict = {"exists": True}

        def _count(query: str, params: tuple = ()) -> int:
            row = conn.execute(query, params).fetchone()
            return int(row[0]) if row else 0

        info["paper_positions"] = _count(
            "SELECT COUNT(*) FROM positions WHERE account_id=?",
            (PAPER_DEFAULT_ACCOUNT,),
        )
        info["all_positions"] = _count("SELECT COUNT(*) FROM positions")
        info["processed_fills"] = _count("SELECT COUNT(*) FROM processed_fills")
        info["executions_replayed"] = _count("SELECT COUNT(*) FROM executions_replayed")
        cash_row = conn.execute(
            "SELECT balance_usd, equity_total FROM cash WHERE account_id=?",
            (PAPER_DEFAULT_ACCOUNT,),
        ).fetchone()
        info["has_cash_row"] = cash_row is not None
        if cash_row:
            info["current_cash"] = float(cash_row[0])
            info["current_equity"] = float(cash_row[1])

        # Count non-paper-default accounts (preserved)
        non_paper_positions = _count(
            "SELECT COUNT(*) FROM positions WHERE account_id != ?",
            (PAPER_DEFAULT_ACCOUNT,),
        )
        info["non_paper_default_positions"] = non_paper_positions

        return info
    finally:
        conn.close()


def _inspect_bus(bus: Path) -> dict:
    """Return basic stats about the bus file."""
    if not bus.exists():
        return {"exists": False, "size_bytes": 0, "line_count": 0}
    size = bus.stat().st_size
    line_count = sum(1 for _ in open(bus, "rb") if _.strip())
    return {"exists": True, "size_bytes": size, "line_count": line_count}


# ---------------------------------------------------------------------------
# Backup helpers
# ---------------------------------------------------------------------------


def _utc_stamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _backup(src: Path, stamp: str) -> Path | None:
    """Copy src -> src.bak-RESET-<stamp>.  Returns the backup path, or None if src absent."""
    if not src.exists():
        return None
    dst = src.with_name(f"{src.name}.bak-RESET-{stamp}")
    shutil.copy2(src, dst)
    return dst


# ---------------------------------------------------------------------------
# Apply: the actual mutations
# ---------------------------------------------------------------------------


def _apply_state_db(state_db: Path) -> None:
    """Reset paper-default in state.db using a single atomic BEGIN IMMEDIATE tx.

    - DELETE positions WHERE account_id='paper-default'
    - UPSERT cash row: balance_usd=100000, equity_total=100000
    - DELETE FROM processed_fills
    - DELETE FROM executions_replayed
    - Non-paper-default rows are untouched.

    ar04/ar116 pattern: autocommit SQLite stores that live crons also write
    MUST wrap their mutations in BEGIN IMMEDIATE to avoid the split-tx race.
    """
    if not state_db.exists():
        # Fresh db — just ensure it has the schema by creating it.
        # The PortfolioState class handles schema creation; we just need an
        # empty, ready-to-use db.  For the reset case, having no rows IS flat.
        return

    conn = sqlite3.connect(str(state_db), timeout=10.0, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")

        conn.execute("BEGIN IMMEDIATE")
        try:
            # 1. Wipe paper-default positions ONLY
            conn.execute(
                "DELETE FROM positions WHERE account_id=?",
                (PAPER_DEFAULT_ACCOUNT,),
            )
            # 2. Set cash to flat 100k for paper-default
            now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                """
                INSERT OR REPLACE INTO cash
                    (account_id, balance_usd, last_update_at, equity_total)
                VALUES (?, ?, ?, ?)
                """,
                (PAPER_DEFAULT_ACCOUNT, FLAT_CASH_USD, now_iso, FLAT_CASH_USD),
            )
            # 3. Clear the processed_fills idempotency guard — the bus is being
            #    archived, so old fill keys are gone.  Fresh fills after the reset
            #    will re-populate this table.
            conn.execute("DELETE FROM processed_fills")
            # 4. Clear the executions_replayed watermark — a fresh rebuild from
            #    an empty bus must start from scratch.
            conn.execute("DELETE FROM executions_replayed")

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _apply_bus(bus: Path) -> None:
    """Archive executions.jsonl then truncate it to zero bytes.

    Truncating (not deleting) preserves the inode so any process that already
    has the file open does not error on the next append.
    """
    # Archive copy done by the caller BEFORE this is called.
    with open(bus, "wb"):
        pass  # truncate


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:  # noqa: C901
    parser = argparse.ArgumentParser(
        description="Reset the paper-default paper book to a clean flat $100k state.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples
--------
# Dry-run (safe — prints what it would do):
  python3 ops/scripts/quant-reset-paper-book.py

# Actual reset (prompts for confirmation):
  python3 ops/scripts/quant-reset-paper-book.py --apply

# Non-interactive (CI / operator script):
  python3 ops/scripts/quant-reset-paper-book.py --apply --yes

# Test against a tmp dir:
  python3 ops/scripts/quant-reset-paper-book.py \\
      --state-db /tmp/test/state.db \\
      --bus      /tmp/test/executions.jsonl \\
      --apply --yes
""",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually perform the reset.  Default is dry-run (no mutations).",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        default=False,
        help="Skip the interactive confirmation prompt (use with --apply).",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        metavar="DIR",
        help="Override QUANT_HOME (default: ~/.hermes/quant or $QUANT_HOME).",
    )
    parser.add_argument(
        "--state-db",
        type=Path,
        default=None,
        dest="state_db",
        metavar="PATH",
        help="Override path to state.db.",
    )
    parser.add_argument(
        "--bus",
        type=Path,
        default=None,
        metavar="PATH",
        help="Override path to executions.jsonl.",
    )

    args = parser.parse_args(argv)
    state_db, bus, proposals_db = _resolve_paths(
        home=args.home,
        state_db=args.state_db,
        bus=args.bus,
    )

    # ------------------------------------------------------------------
    # Inspection pass (always)
    # ------------------------------------------------------------------
    db_info = _inspect_state_db(state_db)
    bus_info = _inspect_bus(bus)
    proposals_exists = proposals_db.exists()

    stamp = _utc_stamp()
    db_bak = state_db.with_name(f"{state_db.name}.bak-RESET-{stamp}")
    bus_bak = bus.with_name(f"{bus.name}.bak-RESET-{stamp}")
    proposals_bak = proposals_db.with_name(f"{proposals_db.name}.bak-RESET-{stamp}")

    # ------------------------------------------------------------------
    # Dry-run summary
    # ------------------------------------------------------------------
    print("=" * 70)
    print("quant-reset-paper-book — paper-default book reset")
    print("=" * 70)
    print(f"Mode         : {'DRY-RUN (pass --apply to mutate)' if not args.apply else 'APPLY'}")
    print()
    print(f"state.db     : {state_db}")
    if db_info["exists"]:
        print(f"  paper-default positions to DELETE : {db_info['paper_positions']}")
        if db_info.get("non_paper_default_positions", 0):
            print(
                f"  other accounts' positions (KEPT) : "
                f"{db_info['non_paper_default_positions']}"
            )
        if db_info["has_cash_row"]:
            print(
                f"  current cash / equity_total      : "
                f"${db_info.get('current_cash', 0):.2f} / "
                f"${db_info.get('current_equity', 0):.2f}"
            )
        print(f"  processed_fills to CLEAR         : {db_info['processed_fills']}")
        print(f"  executions_replayed to CLEAR     : {db_info['executions_replayed']}")
        print(f"  -> backup would be               : {db_bak.name}")
    else:
        print("  (does not exist — will be a no-op)")
    print()
    print(f"executions.jsonl : {bus}")
    if bus_info["exists"]:
        print(
            f"  size: {bus_info['size_bytes']:,} bytes  |  "
            f"lines: {bus_info['line_count']:,}"
        )
        print(f"  -> archive: {bus_bak.name}")
        print("  -> truncate to 0 bytes (preserve inode)")
    else:
        print("  (does not exist — will create empty file)")
    print()
    print(f"proposals.db : {proposals_db}")
    if proposals_exists:
        print(f"  -> backup only (NOT wiped): {proposals_bak.name}")
    else:
        print("  (does not exist — skip)")
    print()

    if not args.apply:
        print("DRY-RUN complete.  Re-run with --apply to perform the reset.")
        return 0

    # ------------------------------------------------------------------
    # Interactive confirm (unless --yes)
    # ------------------------------------------------------------------
    if not args.yes:
        print("=" * 70)
        print("WARNING: This will wipe the paper-default book.  Are you sure?")
        print("  Type 'yes' to confirm, anything else to abort: ", end="", flush=True)
        try:
            answer = input().strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 1
        if answer.lower() != "yes":
            print("Aborted.")
            return 1
        print()

    # ------------------------------------------------------------------
    # BACKUP first (before any mutation)
    # ------------------------------------------------------------------
    print("--- Backing up ---")
    backed_up: list[str] = []

    if db_info["exists"]:
        shutil.copy2(state_db, db_bak)
        print(f"  state.db       -> {db_bak}")
        backed_up.append(str(db_bak))
    else:
        print("  state.db       : does not exist, no backup needed")

    if bus_info["exists"]:
        shutil.copy2(bus, bus_bak)
        print(f"  executions.jsonl -> {bus_bak}")
        backed_up.append(str(bus_bak))
    else:
        print("  executions.jsonl : does not exist, no backup needed")

    if proposals_exists:
        shutil.copy2(proposals_db, proposals_bak)
        print(f"  proposals.db   -> {proposals_bak}")
        backed_up.append(str(proposals_bak))
    else:
        print("  proposals.db   : does not exist, skipping")

    # ------------------------------------------------------------------
    # APPLY mutations
    # ------------------------------------------------------------------
    print()
    print("--- Applying ---")

    # 1. state.db reset (atomic BEGIN IMMEDIATE)
    _apply_state_db(state_db)
    if db_info["exists"]:
        print(
            f"  state.db: deleted {db_info['paper_positions']} paper-default positions, "
            f"set cash=$100,000, cleared processed_fills, cleared watermark"
        )
    else:
        print("  state.db: did not exist — no changes")

    # 2. Bus archive + truncate
    if bus_info["exists"]:
        _apply_bus(bus)
        print(
            f"  executions.jsonl: archived "
            f"({bus_info['size_bytes']:,} bytes, {bus_info['line_count']:,} lines) "
            f"and truncated to 0 bytes"
        )
    else:
        # Create an empty file so the bus path exists for new appends.
        bus.parent.mkdir(parents=True, exist_ok=True)
        bus.touch()
        print("  executions.jsonl: did not exist — created empty file")

    # ------------------------------------------------------------------
    # Verification pass
    # ------------------------------------------------------------------
    print()
    print("--- Verifying ---")
    post_db = _inspect_state_db(state_db)
    post_bus = _inspect_bus(bus)

    errors: list[str] = []

    if post_db["exists"]:
        if post_db["paper_positions"] != 0:
            errors.append(
                f"FAIL: {post_db['paper_positions']} paper-default positions remain in state.db"
            )
        else:
            print("  state.db paper-default positions : 0 (clean)")

        if post_db.get("non_paper_default_positions", 0) != db_info.get(
            "non_paper_default_positions", 0
        ):
            errors.append(
                "FAIL: non-paper-default position count changed — scope breach!"
            )
        else:
            print(
                f"  non-paper-default rows preserved : "
                f"{post_db.get('non_paper_default_positions', 0)}"
            )

        if post_db.get("current_cash") != FLAT_CASH_USD:
            errors.append(
                f"FAIL: cash is ${post_db.get('current_cash')} not ${FLAT_CASH_USD}"
            )
        else:
            print(f"  cash row                         : ${FLAT_CASH_USD:,.0f} (flat)")

        if post_db["processed_fills"] != 0:
            errors.append(
                f"FAIL: {post_db['processed_fills']} processed_fills remain"
            )
        else:
            print("  processed_fills                  : 0 (cleared)")

    if post_bus["size_bytes"] != 0:
        errors.append(f"FAIL: bus is {post_bus['size_bytes']} bytes, expected 0")
    else:
        print("  executions.jsonl size            : 0 bytes (truncated)")

    print()
    if errors:
        print("ERRORS:")
        for e in errors:
            print(f"  {e}")
        return 2

    print("=" * 70)
    print("RESET COMPLETE.  paper-default book is flat at $100,000.")
    print()
    print("Backups:")
    for b in backed_up:
        print(f"  {b}")
    print()
    print("Next steps:")
    print(
        "  1. Confirm the book is flat:  python3 ops/scripts/quant-flatten-paper-default.py"
    )
    print(
        "  2. The bus is empty; the first cron tick will rebuild state.db from scratch."
    )
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
