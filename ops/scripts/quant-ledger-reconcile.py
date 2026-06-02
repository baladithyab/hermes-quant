#!/usr/bin/env python3
"""quant-ledger-reconcile.py — rebuild state.db positions from executions.jsonl (ADR-0085).

ADR-0085: executions.jsonl is the authoritative, append-only EVENT LOG (decision-truth);
state.db positions is a DERIVED PROJECTION reconstructable from it. A position with no backing
execution is invalid by definition. This tool folds the log into positions via the existing,
idempotent PortfolioState.reconstruct_from() and reports the divergence it heals.

It exists because a test-isolation leak (memory: state-db-test-isolation-leak) polluted the
live paper ledger with fixture positions (NVDA 2200@$150, GME@$200, ...) producing a fictional
+$167K EOD P&L, AND inflated real positions by ~0.2 (one ladder notch) by replaying fixture
fills on top of them. The conftest fix (ADR-0085 PREVENT) stops new pollution; this tool CLEANS
what's already there and is the periodic reconcile the ADR mandates.

Usage:
    python ops/scripts/quant-ledger-reconcile.py            # DRY-RUN: diff only, no writes
    python ops/scripts/quant-ledger-reconcile.py --apply     # back up + rebuild state.db in place
    python ops/scripts/quant-ledger-reconcile.py --account paper-default

Posture: --apply mutates state.db (backed up first). Dry-run is the default. Read-only on
executions.jsonl always (the authoritative log is never modified).
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from hermes_quant.state.portfolio_state import (
    DEFAULT_EXECUTIONS_PATH,
    DEFAULT_STATE_DB,
    PortfolioState,
)


def _positions(db_path: Path, account: str) -> dict[str, tuple[float, float]]:
    if not db_path.exists():
        return {}
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT symbol, quantity, avg_entry_price FROM positions "
            "WHERE account_id = ? AND abs(quantity) > 1e-9",
            (account,),
        ).fetchall()
    finally:
        con.close()
    return {s: (round(q, 6), round(p, 4)) for s, q, p in rows}


def _diff(live: dict, rebuilt: dict) -> tuple[list, list, list]:
    phantom = sorted(set(live) - set(rebuilt))  # in live, no backing execution -> purge
    new = sorted(set(rebuilt) - set(live))  # in log, missing from live -> add
    changed = sorted(s for s in (set(live) & set(rebuilt)) if live[s] != rebuilt[s])
    return phantom, changed, new


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="rebuild state.db in place (backs up first)")
    ap.add_argument("--account", default="paper-default", help="state.db account_id to reconcile")
    args = ap.parse_args()

    live_db = DEFAULT_STATE_DB
    execs = DEFAULT_EXECUTIONS_PATH
    if not execs.exists():
        print(f"❌ executions.jsonl not found at {execs} — cannot reconcile.", file=sys.stderr)
        return 2

    live = _positions(live_db, args.account)

    # Rebuild into a scratch DB first (never touch live during the diff).
    scratch_dir = Path(tempfile.mkdtemp())
    scratch = scratch_dir / "reconcile_state.db"
    res = PortfolioState(state_db_path=scratch).reconstruct_from(execs)
    rebuilt = _positions(scratch, args.account)

    phantom, changed, new = _diff(live, rebuilt)

    print(f"=== ledger reconcile (account={args.account}) — ADR-0085 ===")
    print(f"executions.jsonl: {res.executions_processed} records, {len(res.errors)} replay errors")
    print(f"live positions: {len(live)}  |  execution-backed truth: {len(rebuilt)}")
    print()
    print(f"PHANTOM — in live, NO backing execution (will be PURGED): {len(phantom)}")
    for s in phantom:
        print(f"   - {s}: live={live[s]}")
    print(f"\nINFLATED/CHANGED — live qty/price != execution-backed (will be CORRECTED): {len(changed)}")
    for s in changed:
        print(f"   ~ {s}: live={live[s]} -> truth={rebuilt[s]}")
    print(f"\nMISSING — in log, absent from live (will be ADDED): {len(new)}")
    for s in new:
        print(f"   + {s}: truth={rebuilt[s]}")

    if res.errors:
        print(f"\n⚠️  {len(res.errors)} replay errors (records skipped) — review before --apply:")
        for ln, msg in res.errors[:10]:
            print(f"     line {ln}: {msg}")

    if not args.apply:
        print("\nDRY-RUN (default). Re-run with --apply to back up + rebuild state.db in place.")
        shutil.rmtree(scratch_dir, ignore_errors=True)
        return 0

    if res.errors:
        print("\n❌ refusing --apply with replay errors present (fail-closed). Fix the log first.", file=sys.stderr)
        shutil.rmtree(scratch_dir, ignore_errors=True)
        return 3

    # APPLY: back up the live DB, then rebuild it in place via the same idempotent fold.
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = live_db.with_suffix(f".db.bak-reconcile-{stamp}")
    if live_db.exists():
        shutil.copy2(live_db, backup)
        print(f"\n📦 backed up live state.db -> {backup.name}")
    applied = PortfolioState(state_db_path=live_db).reconstruct_from(execs)
    after = _positions(live_db, args.account)
    print(f"✅ rebuilt in place: {applied.executions_processed} execs, {len(after)} positions "
          f"(was {len(live)}). Phantom +$ removed; inflated positions corrected.")
    shutil.rmtree(scratch_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
