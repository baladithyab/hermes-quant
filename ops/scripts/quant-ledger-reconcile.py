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


def _positions(db_path: Path, account: str) -> dict[tuple[str, str], tuple[float, float]]:
    """Live positions keyed by (asset_class, symbol) — the positions PRIMARY KEY.

    statedb-nvda-orphan clause (3a): keying by ``symbol`` ALONE collapsed an equity
    NVDA and a us_option NVDA260626C00160000 into one key, and made the diff blind to
    asset_class. The positions table PK is (account_id, asset_class, symbol), so an
    options leg and its underlying equity are DISTINCT positions; key on the real
    composite so each is visible (and so an options leg is not silently merged into,
    or mistaken for, the equity row).
    """
    if not db_path.exists():
        return {}
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT asset_class, symbol, quantity, avg_entry_price FROM positions "
            "WHERE account_id = ? AND abs(quantity) > 1e-9",
            (account,),
        ).fetchall()
    finally:
        con.close()
    return {(ac, s): (round(q, 6), round(p, 4)) for ac, s, q, p in rows}


def _diff(live: dict, rebuilt: dict) -> tuple[list, list, list]:
    phantom = sorted(set(live) - set(rebuilt))  # in live, no backing execution -> purge
    new = sorted(set(rebuilt) - set(live))  # in log, missing from live -> add
    changed = sorted(s for s in (set(live) & set(rebuilt)) if live[s] != rebuilt[s])
    return phantom, changed, new


def _is_options(key: tuple[str, str]) -> bool:
    """True if a (asset_class, symbol) key is an options/multi-leg position.

    reconstruct_from() is options-blind (it folds only equity NAV-fractions), so EVERY
    options leg in state.db has no backing in the rebuilt projection and would look
    'phantom'. Purging those = deleting real, broker-confirmed legs to match an
    incomplete log. This predicate lets --apply fail-CLOSED on them specifically.
    """
    return key[0] in ("us_option", "option", "multi_leg")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="rebuild state.db in place (backs up first)")
    ap.add_argument("--account", default="paper-default", help="state.db account_id to reconcile")
    ap.add_argument(
        "--allow-purge",
        action="store_true",
        help="REQUIRED to apply when the rebuild would PURGE any live position. Without it, "
        "--apply fails-CLOSED on any phantom (statedb-nvda-orphan): the reconstructor is "
        "options-blind + the log may have been reset, so a 'phantom' can be a real broker-"
        "confirmed position. Only pass this for a KNOWN fixture-pollution cleanup you have "
        "inspected in dry-run.",
    )
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
    def _k(key: tuple[str, str]) -> str:
        return f"{key[1]} [{key[0]}]"

    phantom_options = [s for s in phantom if _is_options(s)]
    print(f"PHANTOM — in live, NO backing execution (would be PURGED): {len(phantom)}"
          f"  (of which options/multi-leg: {len(phantom_options)})")
    for s in phantom:
        flag = "  ⚠️ OPTIONS — reconstructor is options-blind; likely REAL, not phantom" if _is_options(s) else ""
        print(f"   - {_k(s)}: live={live[s]}{flag}")
    print(f"\nINFLATED/CHANGED — live qty/price != execution-backed (will be CORRECTED): {len(changed)}")
    for s in changed:
        print(f"   ~ {_k(s)}: live={live[s]} -> truth={rebuilt[s]}")
    print(f"\nMISSING — in log, absent from live (will be ADDED): {len(new)}")
    for s in new:
        print(f"   + {_k(s)}: truth={rebuilt[s]}")

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

    # statedb-nvda-orphan clause (3b): FAIL-CLOSED on purge. A rebuild that would DELETE
    # a live position is the dangerous direction — the reconstructor is options-blind and
    # the log may have been RESET after a real fill (the NVDA orphan: 600sh +$30k lifetime,
    # broker-confirmed, ZERO backing rows after a Jun-17 executions.jsonl reset). Silently
    # purging to match an incomplete log deletes real money-state. So --apply REFUSES to
    # purge anything unless --allow-purge is explicitly passed after a dry-run inspection.
    if phantom and not args.allow_purge:
        opt_note = (
            f" {len(phantom_options)} of these are OPTIONS/MULTI-LEG legs the reconstructor "
            "cannot rebuild — almost certainly REAL, not phantom." if phantom_options else ""
        )
        print(
            f"\n❌ refusing --apply: it would PURGE {len(phantom)} live position(s) with no "
            f"backing execution.{opt_note}\n"
            "   A 'phantom' can be a real broker-confirmed position whose log was reset "
            "(statedb-nvda-orphan), or an options leg the equity-only reconstructor is blind "
            "to. Deleting it to match an incomplete log destroys real state.\n"
            "   If you have inspected the dry-run and these ARE fixture pollution, re-run with "
            "--allow-purge. Otherwise resolve the log/state divergence first (NEVER purge a "
            "broker-confirmed position).",
            file=sys.stderr,
        )
        shutil.rmtree(scratch_dir, ignore_errors=True)
        return 4

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
