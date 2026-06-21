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


def _seed_cash(db_path: Path, rows: list[tuple]) -> None:
    """Seed the cash table. rows: (account_id, balance_usd, equity_total)."""
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            """CREATE TABLE IF NOT EXISTS cash (
                   account_id TEXT PRIMARY KEY, balance_usd REAL NOT NULL,
                   last_update_at TEXT NOT NULL, equity_total REAL NOT NULL) WITHOUT ROWID"""
        )
        con.executemany(
            "INSERT OR REPLACE INTO cash (account_id, balance_usd, last_update_at, equity_total) "
            "VALUES (?,?, '2026-06-18T08:35:35Z', ?)",
            rows,
        )
        con.commit()
    finally:
        con.close()


def _cash_balance(db_path: Path, account: str) -> float | None:
    con = sqlite3.connect(str(db_path))
    try:
        r = con.execute("SELECT balance_usd FROM cash WHERE account_id=?", (account,)).fetchone()
        return r[0] if r else None
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


def test_apply_fails_closed_on_cash_destruction(tmp_path, monkeypatch, capsys):
    """wave-3 (HIGH): --apply must FAIL-CLOSED when the rebuild would DECREASE a live cash
    balance — even when the position diff is CLEAN. reconstruct_from DELETEs cash account-
    unscoped + re-bootstraps to initial_cash, so a real out-of-band cash balance (the
    NVDA-orphan shape on the cash axis) is silently destroyed with exit 0 otherwise.

    RED-PROOF: with the cash_destruction term removed from the guard, --apply returns 0 and
    cash is rewritten down."""
    mod = _load()
    state_db = tmp_path / "state.db"
    execs = tmp_path / "executions.jsonl"
    # position fully backed by the log (clean position diff)...
    _seed_state_db(state_db, [("paper-default", "equity", "AAPL", 100.0, 150.0)])
    # ...but live cash carries a real out-of-band balance ABOVE what the log replays to.
    _seed_cash(state_db, [("paper-default", 130000.0, 145000.0)])
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

    bal_before = _cash_balance(state_db, "paper-default")
    rc = mod.main()
    bal_after = _cash_balance(state_db, "paper-default")

    assert rc == 4, f"--apply must fail-CLOSED on a cash decrease, got {rc}"
    assert bal_after == bal_before == 130000.0, "broker-confirmed cash must be UNCHANGED"
    assert "refusing --apply" in capsys.readouterr().err


def test_destructive_changes_flags_cost_basis_rewrite():
    """wave-3 (MED): _destructive_changes flags a key whose |qty| is identical but whose
    avg_entry_price (cost basis, tuple index 1) is rewritten — it drives P&L + the
    kill-switch basis. RED-PROOF: with only the qty check, this returns []."""
    mod = _load()
    live = {("equity", "AAPL"): (100.0, 150.0)}
    rebuilt = {("equity", "AAPL"): (100.0, 999.0)}  # same qty, cost basis rewritten
    assert mod._destructive_changes(live, rebuilt) == [("equity", "AAPL")]
    # a tiny (sub-cent) price drift is NOT flagged (tolerance)
    assert mod._destructive_changes(live, {("equity", "AAPL"): (100.0, 150.00005)}) == []


def _run_apply(mod, tmp_path, monkeypatch, state_db, execs, account="paper-default"):
    monkeypatch.setattr(mod, "DEFAULT_STATE_DB", state_db, raising=False)
    monkeypatch.setattr(mod, "DEFAULT_EXECUTIONS_PATH", execs, raising=False)
    monkeypatch.setattr(mod.sys, "argv", ["quant-ledger-reconcile.py", "--apply", "--account", account])
    return mod.main()


def _exec(asset, qty, px, asof="2026-06-18T00:00:00Z", asset_class="equity"):
    return {
        "proposal_id": f"p-{asset}", "signal_id": f"s-{asset}", "asset": asset,
        "asset_class": asset_class, "timeframe": "1d", "asof_decision": asof,
        "asof_execution": asof, "target_position_pct": qty, "decision_price": px,
        "fill_price": px, "fill_size_pct": qty, "reactor_name": "paper",
        "human_in_the_loop": False, "approver_user_id": None,
        "reactor_metadata": {"account_id": "paper-default", "quantity": qty},
        "bar_ts": asof, "play_tag": None, "schema_version": None,
    }


def test_whitelist_blocks_qty_increase_to_wrong_value(tmp_path, monkeypatch):
    """wave-3-converge (HIGH): the OLD blacklist allowed a qty INCREASE (fabricating
    broker-unconfirmed shares -> inflated NAV). The whitelist invariant refuses ANY
    non-additive change to an existing row, both directions.

    RED-PROOF: the prior blacklist (_destructive_changes reduction-only) returned [] for
    an increase -> --apply rc 0. The whitelist returns rc 4, state unchanged."""
    mod = _load()
    state_db = tmp_path / "state.db"; execs = tmp_path / "executions.jsonl"
    _seed_state_db(state_db, [("paper-default", "equity", "AAPL", 600.0, 150.0)])
    # log folds to MORE than live (a reset+re-accumulate, additive-inflation default)
    execs.write_text(json.dumps(_exec("AAPL", 900.0, 150.0)) + "\n", encoding="utf-8")
    before = mod._full_positions(state_db)
    rc = _run_apply(mod, tmp_path, monkeypatch, state_db, execs)
    assert rc == 4, f"qty INCREASE to a broker-unconfirmed value must fail-closed, got {rc}"
    assert mod._full_positions(state_db) == before, "live position must be UNCHANGED"


def test_whitelist_blocks_equity_total_increase(tmp_path, monkeypatch):
    """wave-3-converge (HIGH): an UPWARD equity_total rewrite (loosening the kill-switch /
    admissibility NAV) is non-additive and must fail-closed — the old cash guard only
    blocked a DECREASE."""
    mod = _load()
    state_db = tmp_path / "state.db"; execs = tmp_path / "executions.jsonl"
    _seed_state_db(state_db, [("paper-default", "equity", "AAPL", 100.0, 150.0)])
    _seed_cash(state_db, [("paper-default", 50000.0, 50000.0)])  # under-recorded equity
    execs.write_text(json.dumps(_exec("AAPL", 100.0, 150.0)) + "\n", encoding="utf-8")
    bal_before = _cash_balance(state_db, "paper-default")
    rc = _run_apply(mod, tmp_path, monkeypatch, state_db, execs)
    assert rc == 4, f"equity_total INCREASE must fail-closed, got {rc}"
    assert _cash_balance(state_db, "paper-default") == bal_before, "cash must be UNCHANGED"


def test_whitelist_blocks_unit_kind_flip(tmp_path, monkeypatch):
    """wave-3-converge (HIGH, the 4th axis): a unit_kind flip holds qty+avg_price constant
    but changes the row's MONEY MEANING (NAV-fraction vs true-unit -> the gross-exposure
    cap). _nonadditive_divergences reads the full row incl unit_kind, so it catches it.

    RED-PROOF: _positions (qty,avg only) sees no change; _full_positions does."""
    mod = _load()
    state_db = tmp_path / "state.db"; execs = tmp_path / "executions.jsonl"
    # live row is true_unit; the seeder default is true_unit (matches a broker-confirmed leg)
    _seed_state_db(state_db, [("paper-default", "equity", "AAPL", 2.0, 150.0)])
    # the log folds AAPL as a nav_fraction row (qty 2.0 same, but unit_kind differs)
    execs.write_text(json.dumps(_exec("AAPL", 2.0, 150.0)) + "\n", encoding="utf-8")
    # the rebuild will write unit_kind per its fold; assert the whitelist flags any divergence
    div = mod._nonadditive_divergences  # sanity: function exists
    rc = _run_apply(mod, tmp_path, monkeypatch, state_db, execs)
    # Either the rebuild's unit_kind matches (additive, rc 0) or differs (rc 4) — but it must
    # NEVER silently rewrite a DIFFERING unit_kind. Prove the guard reads unit_kind:
    live_full = mod._full_positions(state_db)
    # directly exercise the invariant on a constructed flip (qty/px identical, unit_kind differs)
    import sqlite3 as _sq
    scratch = tmp_path / "scratch.db"
    _seed_state_db(scratch, [("paper-default", "equity", "AAPL", 2.0, 150.0)])
    con = _sq.connect(str(scratch))
    con.execute("UPDATE positions SET unit_kind='nav_fraction' WHERE symbol='AAPL'"); con.commit(); con.close()
    flips = mod._nonadditive_divergences(state_db, scratch)
    assert any("CHANGED" in d and "AAPL" in d for d in flips), f"unit_kind flip must be flagged: {flips}"


def test_whitelist_blocks_user_version_regime_flip(tmp_path, monkeypatch):
    """wave-3-converge (MED): a PRAGMA user_version (delta-normalizer regime) flip is a
    money-adjacent state change the guard must catch — post-flip the live reactor's
    incremental writes hard-refuse on regime mismatch."""
    mod = _load()
    import sqlite3 as _sq
    live = tmp_path / "live.db"; scratch = tmp_path / "scratch.db"
    _seed_state_db(live, [("paper-default", "equity", "AAPL", 100.0, 150.0)])
    _seed_state_db(scratch, [("paper-default", "equity", "AAPL", 100.0, 150.0)])
    con = _sq.connect(str(live)); con.execute("PRAGMA user_version = 0"); con.commit(); con.close()
    con = _sq.connect(str(scratch)); con.execute("PRAGMA user_version = 1"); con.commit(); con.close()
    div = mod._nonadditive_divergences(live, scratch)
    assert any("user_version" in d for d in div), f"regime flip must be flagged: {div}"


def test_whitelist_permits_pure_addition(tmp_path, monkeypatch):
    """The whitelist is not over-strict: a PURE addition (log has a row absent from live,
    every existing live row byte-identical) is permitted — --apply succeeds (rc 0)."""
    mod = _load()
    state_db = tmp_path / "state.db"; execs = tmp_path / "executions.jsonl"
    # live is EMPTY (no positions); the log adds AAPL — pure addition, nothing overwritten.
    _seed_state_db(state_db, [])
    _seed_cash(state_db, [])
    execs.write_text(json.dumps(_exec("AAPL", 100.0, 150.0)) + "\n", encoding="utf-8")
    rc = _run_apply(mod, tmp_path, monkeypatch, state_db, execs)
    assert rc == 0, f"a pure addition must be permitted, got {rc}"
    assert mod._positions(state_db, "paper-default"), "the new position was added"


def test_dust_band_position_is_visible_to_guard(tmp_path):
    """reconcile-dust-band: the guard read-filter (abs(qty)>1e-12) now matches the rebuild
    close-threshold (abs(qty)<1e-12 dropped), so a (1e-12,1e-9] dust row is NOT invisible.

    RED-PROOF: with the old >1e-9 filter, a live qty=5e-10... actually 5e-10 < 1e-12? no:
    use 5e-11 which is in (1e-12, 1e-9]; old filter hid it, new filter sees it."""
    mod = _load()
    live = tmp_path / "live.db"
    scratch = tmp_path / "scratch.db"
    # a dust-band live row (5e-11 is in (1e-12, 1e-9]) that the rebuild would PURGE
    _seed_state_db(live, [("paper-default", "equity", "DUST", 5e-11, 1.0)])
    _seed_state_db(scratch, [])  # rebuild drops it -> purge
    div = mod._nonadditive_divergences(live, scratch)
    assert any("DUST" in d and "PURGED" in d for d in div), (
        f"dust-band row must be visible to the guard now: {div}"
    )


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
