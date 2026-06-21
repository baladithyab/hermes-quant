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
            "WHERE account_id = ? AND abs(quantity) > 1e-12",
            (account,),
        ).fetchall()
    finally:
        con.close()
    return {(ac, s): (round(q, 6), round(p, 4)) for ac, s, q, p in rows}


def _cash(db_path: Path) -> dict[str, tuple[float, float]]:
    """Cash row per account: account_id -> (balance_usd, equity_total).

    statedb-nvda-orphan / wave-3: the fail-closed guard protected only the POSITIONS
    table's quantity. But reconstruct_from also does an account-UNSCOPED ``DELETE FROM
    cash`` then re-bootstraps every account from ``_default_initial_cash()`` + replayed
    deltas. The incremental apply_execution path mutates live cash FORWARD, so live cash
    legitimately diverges from a from-scratch replay — an --apply can therefore DESTROY
    real broker-confirmed cash (the NVDA-orphan shape on the cash axis: cash booked, log
    reset re-bootstraps to initial_cash) even when the position diff is clean. This read
    lets the guard compare live-vs-rebuilt cash and refuse a balance DECREASE.
    """
    if not db_path.exists():
        return {}
    con = sqlite3.connect(str(db_path))
    try:
        try:
            rows = con.execute("SELECT account_id, balance_usd, equity_total FROM cash").fetchall()
        except sqlite3.OperationalError:
            return {}  # no cash table (a bare/empty db) — nothing to compare
    finally:
        con.close()
    return {a: (round(b, 4), round(e, 4)) for a, b, e in rows}


def _cash_destruction(live_cash: dict, rebuilt_cash: dict) -> list[str]:
    """Accounts whose rebuilt balance_usd OR equity_total would DECREASE (or vanish).

    A decrease destroys broker-confirmed money-state. An increase (the log shows more
    than live) is additive/safe. An absent rebuilt row = the account vanishes = a full
    cash purge (treated as a decrease from its live balance to 0).
    """
    out = []
    for acct, (live_bal, live_eq) in live_cash.items():
        reb = rebuilt_cash.get(acct)
        if reb is None:
            out.append(acct)  # account's cash row would be purged entirely
            continue
        reb_bal, reb_eq = reb
        if reb_bal < live_bal - 1e-6 or reb_eq < live_eq - 1e-6:
            out.append(acct)
    return sorted(out)


def _all_positions(db_path: Path) -> dict[tuple[str, str, str], tuple[float, float]]:
    """ALL live positions across EVERY account, keyed by (account, asset_class, symbol).

    statedb-nvda-orphan clause (3b) / wave-2 Q1b: reconstruct_from() does an
    account-UNSCOPED ``DELETE FROM positions`` then re-inserts the full-log fold for
    ALL accounts (portfolio_state.py). So an --apply that reconciles ONE --account
    still DELETES every other account's positions that lack a log backing. The
    account-scoped guard was blind to that. This whole-table read lets the
    fail-closed check see EVERY account's destructive change, not just the named one.
    """
    if not db_path.exists():
        return {}
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT account_id, asset_class, symbol, quantity, avg_entry_price FROM positions "
            "WHERE abs(quantity) > 1e-12"
        ).fetchall()
    finally:
        con.close()
    return {(a, ac, s): (round(q, 6), round(p, 4)) for a, ac, s, q, p in rows}


def _diff(live: dict, rebuilt: dict) -> tuple[list, list, list]:
    phantom = sorted(set(live) - set(rebuilt))  # in live, no backing execution -> purge
    new = sorted(set(rebuilt) - set(live))  # in log, missing from live -> add
    changed = sorted(s for s in (set(live) & set(rebuilt)) if live[s] != rebuilt[s])
    return phantom, changed, new


def _destructive_changes(live: dict, rebuilt: dict) -> list:
    """Keys whose rebuilt value DESTROYS live money-state: a |quantity| REDUCTION
    (wave-2 Q1a) OR an avg_entry_price (cost-basis) REWRITE (wave-3).

    wave-2 Q1a: the guard originally checked only ``phantom`` (a position fully ABSENT
    from the rebuild). But a reset/incomplete log more often REDUCES a position than
    zeroes it (NVDA 600sh -> log backs 100sh) — that lands in ``changed``, which --apply
    "corrects" 600 -> 100, destroying 500 broker-confirmed shares with exit 0. A |qty|
    reduction is a partial purge. An INCREASE is additive and safe.

    wave-3: a position with IDENTICAL |qty| but a different rebuilt avg_entry_price also
    rewrites money-state — cost basis drives unrealized P&L + the equity_total/kill-switch
    notional, so a silent rewrite (e.g. a reset re-bootstrapping basis) mis-states the
    live book. Any cost-basis move beyond a cent-level tolerance is destructive.
    """
    out = []
    for k in set(live) & set(rebuilt):
        live_qty, live_px = abs(live[k][0]), live[k][1]
        rebuilt_qty, rebuilt_px = abs(rebuilt[k][0]), rebuilt[k][1]
        if rebuilt_qty < live_qty - 1e-9:
            out.append(k)
        elif abs(rebuilt_px - live_px) > 1e-4:
            out.append(k)  # cost-basis rewrite (P&L / kill-switch basis mis-stated)
    return sorted(out)


def _full_positions(db_path: Path) -> dict[tuple[str, str, str], tuple[float, float, str]]:
    """EVERY live position, ALL accounts, FULL money-bearing row:
    (account, asset_class, symbol) -> (quantity, avg_entry_price, unit_kind).

    Includes ``unit_kind`` — a wave-3-converge HIGH: unit_kind decides whether ``quantity``
    means a NAV-fraction or a true unit count (position_gross_fraction reads it), so a
    rebuild that holds qty+avg_price constant but flips unit_kind silently changes the
    row's MONEY MEANING and the live gross-exposure cap. The whitelist guard compares the
    full tuple so that reinterpretation cannot slip through as 'unchanged'.
    """
    if not db_path.exists():
        return {}
    con = sqlite3.connect(str(db_path))
    try:
        try:
            rows = con.execute(
                "SELECT account_id, asset_class, symbol, quantity, avg_entry_price, unit_kind "
                "FROM positions WHERE abs(quantity) > 1e-12"
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
    finally:
        con.close()
    return {(a, ac, s): (round(q, 6), round(p, 4), uk) for a, ac, s, q, p, uk in rows}


def _user_version(db_path: Path) -> int | None:
    """The PRAGMA user_version regime stamp (ft1 delta-normalizer), or None if absent."""
    if not db_path.exists():
        return None
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()


def _nonadditive_divergences(live_db: Path, scratch_db: Path) -> list[str]:
    """THE authoritative fail-closed invariant (wave-3-converge): return human-readable
    descriptions of every way the rebuild is NOT a pure ADDITION to live money-state.

    WHY a whitelist, not a blacklist: each prior wave patched one destructive DIRECTION
    (qty-down, cost-basis, cash-down, cross-account) and the next adversarial pass found
    its mirror (qty-UP fabricating shares, equity-UP loosening the kill-switch, unit_kind
    reinterpretation, user_version regime flip). Enumerating destructive axes never
    converges. So invert: --apply (without --allow-purge) is permitted ONLY when the
    rebuild ADDS rows and leaves every existing live money-bearing value byte-identical.
    ANY removal, reduction, increase, reinterpretation, cash change, or regime-stamp flip
    is non-additive and fails-closed. This subsumes every known axis AND every unknown one.

    Covers: positions (qty / avg_entry_price / unit_kind, all accounts), cash
    (balance_usd / equity_total, all accounts), and the user_version regime stamp.
    (executions_replayed watermark + processed_fills are O(delta) bookkeeping the next
    incremental fold self-heals via its MAX-merge — not broker-confirmed value, so they
    are intentionally not gated here; see the seed for the rationale.)
    """
    div: list[str] = []
    live_p, reb_p = _full_positions(live_db), _full_positions(scratch_db)
    for key, lv in live_p.items():
        rv = reb_p.get(key)
        a, ac, s = key
        if rv is None:
            div.append(f"position PURGED: {a}/{s} [{ac}] live={lv}")
        elif rv != lv:
            div.append(f"position CHANGED: {a}/{s} [{ac}] live={lv} -> rebuilt={rv}")
    live_c, reb_c = _cash(live_db), _cash(scratch_db)
    for acct, lv in live_c.items():
        rv = reb_c.get(acct)
        if rv is None:
            div.append(f"cash PURGED: {acct} live(bal,eq)={lv}")
        elif rv != lv:
            div.append(f"cash CHANGED: {acct} live(bal,eq)={lv} -> rebuilt={rv}")
    lver, rver = _user_version(live_db), _user_version(scratch_db)
    if lver is not None and rver is not None and lver != rver:
        div.append(f"user_version (normalizer regime) FLIP: live={lver} -> rebuilt={rver}")
    return div


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
    live_all = _all_positions(live_db)  # Q1b: every account (the apply DELETEs all)

    # Rebuild into a scratch DB first (never touch live during the diff).
    scratch_dir = Path(tempfile.mkdtemp())
    scratch = scratch_dir / "reconcile_state.db"
    res = PortfolioState(state_db_path=scratch).reconstruct_from(execs)
    rebuilt = _positions(scratch, args.account)
    rebuilt_all = _all_positions(scratch)  # Q1b: rebuilt truth for every account

    phantom, changed, new = _diff(live, rebuilt)
    reductions = _destructive_changes(live, rebuilt)  # Q1a: |qty| shrunk in named acct
    # Q1b: every account's destructive change (purge OR reduction) the apply would do.
    all_phantom = sorted(set(live_all) - set(rebuilt_all))
    all_reductions = _destructive_changes(live_all, rebuilt_all)
    # Cross-account destructive rows OUTSIDE the named account (the blind spot).
    other_acct_destructive = sorted(
        k for k in (set(all_phantom) | set(all_reductions)) if k[0] != args.account
    )
    # wave-3: the CASH axis — reconstruct_from DELETEs cash account-unscoped + re-bootstraps
    # to initial_cash, so an --apply can destroy broker-confirmed cash even on a clean
    # position diff. Compare live-vs-rebuilt cash for ALL accounts.
    live_cash = _cash(live_db)
    rebuilt_cash = _cash(scratch)
    cash_destruction = _cash_destruction(live_cash, rebuilt_cash)
    # wave-3-converge: THE authoritative fail-closed invariant — any NON-ADDITIVE
    # divergence (the per-axis lists above are an informative subset; this is the
    # whitelist that also catches qty-UP / equity-UP / unit_kind / regime-flip).
    nonadditive = _nonadditive_divergences(live_db, scratch)

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
        red = "  ⚠️ REDUCES |qty| — partial purge (destroys real shares if the log was reset)" if s in reductions else ""
        print(f"   ~ {_k(s)}: live={live[s]} -> truth={rebuilt[s]}{red}")
    print(f"\nMISSING — in log, absent from live (will be ADDED): {len(new)}")
    for s in new:
        print(f"   + {_k(s)}: truth={rebuilt[s]}")
    if other_acct_destructive:
        print(f"\n⚠️  CROSS-ACCOUNT — {len(other_acct_destructive)} destructive change(s) OUTSIDE "
              f"--account={args.account} (the apply DELETEs ALL accounts):")
        for k in other_acct_destructive:
            print(f"   ! {k[0]}/{k[2]} [{k[1]}]: live={live_all[k]} -> "
                  f"{'PURGED' if k not in rebuilt_all else rebuilt_all[k]}")
    if cash_destruction:
        print(f"\n⚠️  CASH — {len(cash_destruction)} account(s) whose balance/equity would "
              f"DECREASE (the apply re-bootstraps cash from initial_cash):")
        for acct in cash_destruction:
            reb = rebuilt_cash.get(acct, "PURGED")
            print(f"   $ {acct}: live(bal,eq)={live_cash[acct]} -> {reb}")
    if nonadditive:
        print(f"\n⛔ NON-ADDITIVE — {len(nonadditive)} change(s) that are NOT a pure addition "
              f"to live money-state (the authoritative --apply blocker):")
        for d in nonadditive[:50]:
            print(f"   ⛔ {d}")

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

    # statedb-nvda-orphan clause (3b) + wave-2 Q1a/Q1b: FAIL-CLOSED on ANY destructive
    # change, ACROSS ALL ACCOUNTS. A rebuild that DELETES or REDUCES a live position is the
    # dangerous direction — the reconstructor is options-blind and the log may have been
    # RESET after a real fill (NVDA orphan: 600sh +$30k, broker-confirmed, zero backing rows).
    # FIVE destructive shapes, each money-destroying with exit 0 if unguarded:
    #   * phantom        — position fully ABSENT from the rebuild (the original guard)
    #   * reductions     — |qty| SHRUNK or avg_entry_price REWRITTEN (Q1a + wave-3 cost-basis;
    #                      lands in `changed`, which --apply "corrects" -> real shares / P&L gone)
    #   * cross-account  — the apply's DELETE FROM positions is account-UNSCOPED (Q1b), so a
    #                      broker-confirmed row in ANY OTHER account is purged while reconciling THIS one.
    #   * cash           — reconstruct_from DELETEs cash account-unscoped + re-bootstraps to
    #                      initial_cash (wave-3); a clean position diff can still DESTROY real cash.
    # --apply REFUSES all unless --allow-purge is explicitly passed after a dry-run.
    # AUTHORITATIVE FAIL-CLOSED INVARIANT (wave-3-converge): --apply is permitted without
    # --allow-purge ONLY when the rebuild is a PURE ADDITION to live money-state. Any
    # non-additive divergence (purge / reduction / increase / cost-basis or unit_kind
    # reinterpretation / cash change / regime-stamp flip — across ALL accounts) refuses.
    # This whitelist subsumes the per-axis blacklist above AND axes not yet enumerated:
    # enumerating destructive directions never converged (each patch surfaced its mirror).
    if nonadditive and not args.allow_purge:
        opt_note = (
            f"\n   {len(phantom_options)} of the purged rows are OPTIONS/MULTI-LEG legs the "
            "reconstructor cannot rebuild — almost certainly REAL, not phantom." if phantom_options else ""
        )
        print(
            f"\n❌ refusing --apply: the rebuild is NOT a pure addition to live money-state — "
            f"{len(nonadditive)} non-additive change(s) (positions/cash/regime across all "
            f"accounts) that would OVERWRITE broker-confirmed state with exit 0.{opt_note}\n"
            "   A change/purge/reduction/increase can be a real broker-confirmed position, cash "
            "balance, or unit-kind whose log was reset (statedb-nvda-orphan), or an options leg "
            "the equity-only reconstructor is blind to. The reconcile only SAFELY heals by ADDING "
            "missing rows; any alteration of existing money-state must be inspected.\n"
            "   If you have inspected the dry-run and these ARE fixture pollution / an intended "
            "correction, re-run with --allow-purge. Otherwise resolve the log/state divergence "
            "first (NEVER overwrite broker-confirmed money-state).",
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
