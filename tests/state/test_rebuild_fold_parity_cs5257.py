"""cs52 + cs57 — rebuild-fold parity with the incremental fold (ADR-0004 money-state).

state.db is a CACHE; executions.jsonl is canonical; reconstruct_from is the rebuild path
the ledger-reconcile --apply trusts as truth. Two defects make the REBUILD fold
(reconstruct_from -> _replay_record) DIVERGE from the incremental fold
(_apply_execution_unsafe):

  cs52: a persisted alpaca-paper fill carries its account_id ONLY inside
        reactor_metadata (react/paper.py:_record_to_dict emits NO top-level account_id;
        the reactors inject it at runtime BEFORE apply_execution). The incremental fold
        therefore resolves the right account, but reconstruct_from -> _replay_record reads
        `acct = rec.get("account_id","paper-default")` with NO reactor_metadata fallback,
        so on a full rebuild every alpaca-paper fill is re-pooled into paper-default —
        corrupting the per-account NAV partition. (cs24 added EXACTLY this fallback to the
        daemon loader; the canonical rebuild fold never got it.)

  cs57: the incremental fold dedups a true byte-duplicate (the C2 append-before-apply
        crash-retry record) via INSERT OR IGNORE on processed_fills. reconstruct_from folds
        EVERY raw record with NO dedup, so a duplicated line double-counts. The fix dedups
        byte-identical records in reconstruct_from's accumulator on the SAME cs51 5-col key
        the incremental fold uses (incl. leg_index, so the cs51 same-OCC legs are NOT
        re-collapsed).

Deterministic, no network. HERMES_QUANT_DELTA_NORMALIZER pinned to 0 in each test (the
default-OFF legacy-equivalent fold; the normalizer path is a separate concern).
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_quant.state.portfolio_state import PortfolioState

_ASOF = "2026-06-13T15:00:00.000000Z"


def _write(path: Path, recs: list[dict]) -> None:
    with open(path, "w") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


def _alpaca_fill() -> dict:
    """An alpaca-paper fill as PERSISTED to executions.jsonl: account_id lives ONLY in
    reactor_metadata (react/paper.py:_record_to_dict writes the log WITHOUT a top-level
    account_id). Signed-shares quantity in reactor_metadata => true-unit path."""
    return {
        "proposal_id": "alp_prop_1",
        "asset": "NVDA",
        "asset_class": "equity",
        "asof_execution": _ASOF,
        "fill_price": 100.0,
        "fill_size_pct": 0.05,
        "reactor_name": "alpaca-paper",
        "reactor_metadata": {"account_id": "alpaca-paper", "quantity": 10.0},
    }


def _equity_fill() -> dict:
    """A plain paper-default equity fill (legacy NAV-fraction path), no leg_quantity,
    top-level account_id present."""
    return {
        "proposal_id": "eq_prop_1",
        "asset": "SPY",
        "asset_class": "equity",
        "asof_execution": _ASOF,
        "fill_price": 100000.0,
        "fill_size_pct": 0.05,
        "reactor_name": "paper",
        "account_id": "paper-default",
    }


def test_cs52_alpaca_paper_fill_rebuilds_into_correct_account(tmp_path, monkeypatch) -> None:
    """cs52 RED->GREEN: a rebuild of an alpaca-paper fill (account_id only in
    reactor_metadata) must land in the alpaca-paper partition, NOT paper-default.

    RED today: reconstruct_from -> _replay_record reads only the top-level account_id and
    pools the fill into paper-default. GREEN after the cs24-style fallback is mirrored into
    the rebuild fold.
    """
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "0")
    execs = tmp_path / "executions.jsonl"
    _write(execs, [_alpaca_fill()])

    ps = PortfolioState(state_db_path=tmp_path / "cs52.db")
    ps.reconstruct_from(execs)

    alpaca = ps.get_positions("alpaca-paper")
    paper_default = ps.get_positions("paper-default")

    # The fill lands in its own partition with the true-unit (signed-shares) quantity.
    assert ("equity", "NVDA") in alpaca
    assert alpaca[("equity", "NVDA")].quantity == 10.0
    # And it is NOT pooled into paper-default.
    assert ("equity", "NVDA") not in paper_default


def test_cs57_byte_duplicate_fill_rebuild_equals_incremental(tmp_path, monkeypatch) -> None:
    """cs57 RED->GREEN: a true byte-duplicate fill (the crash-retry record) must fold to
    the SAME book on the rebuild path as on the incremental path.

    RED today: incremental dedups (qty 0.05, cash 95000) but reconstruct_from double-counts
    (qty 0.10, cash 90000). GREEN after reconstruct_from dedups byte-identical records on
    the cs51 5-col key in its accumulator.
    """
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "0")

    # ── INCREMENTAL fold: apply the exact same fill twice (crash-retry) ──
    inc = PortfolioState(state_db_path=tmp_path / "inc.db")
    inc.apply_execution(_equity_fill())
    inc.apply_execution(_equity_fill())  # exact byte-duplicate retry => deduped
    inc_pos = inc.get_positions("paper-default")
    inc_cash = inc.get_cash("paper-default")
    inc_qty = inc_pos[("equity", "SPY")].quantity

    # ── REBUILD fold: a log with the duplicated line ──
    dup = tmp_path / "dup.jsonl"
    _write(dup, [_equity_fill(), _equity_fill()])
    reb = PortfolioState(state_db_path=tmp_path / "reb.db")
    reb.reconstruct_from(dup)
    reb_pos = reb.get_positions("paper-default")
    reb_cash = reb.get_cash("paper-default")
    reb_qty = reb_pos[("equity", "SPY")].quantity

    # The two folds AGREE (no divergence).
    assert inc_qty == reb_qty
    assert abs(inc_cash.balance_usd - reb_cash.balance_usd) < 1e-9
    assert abs(inc_cash.equity_total - reb_cash.equity_total) < 1e-9
    # Pin the deduped values: one fill folded, not two.
    assert reb_qty == 0.05
    assert abs(reb_cash.balance_usd - 95000.0) < 1e-9


def test_cs57_dedup_does_not_recollapse_same_occ_legs(tmp_path, monkeypatch) -> None:
    """cs51 REGRESSION (must STAY green): the cs57 rebuild-dedup keys on the cs51 5-col key
    INCLUDING leg_index, so two distinct legs of ONE same-OCC family (same proposal_id /
    asof / OCC / us_option, leg_index 0 and 1) are NOT collapsed.

    leg_index 0 sell-to-close (-1) + leg_index 1 buy-to-open (+2) => net +1 LONG on both
    folds. If cs57 dropped on a key WITHOUT leg_index it would re-collapse these to the
    cs51 phantom-short bug — this asserts it does not.
    """
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "0")
    occ = "AAPL260116C00200000"
    asof = "2026-06-13T15:30:00+00:00"
    leg0 = {
        "proposal_id": "ROLL-1",
        "asof_execution": asof,
        "asset": occ,
        "asset_class": "us_option",
        "target_position_pct": -0.01,
        "fill_price": 1.50,
        "fill_size_pct": -0.01,
        "account_id": "paper-default",
        "reactor_metadata": {"multi_leg_id": "ROLL-1", "leg_index": 0, "quantity": -1.0,
                             "role": "leg", "paper": True},
    }
    leg1 = {
        "proposal_id": "ROLL-1",
        "asof_execution": asof,
        "asset": occ,
        "asset_class": "us_option",
        "target_position_pct": 0.02,
        "fill_price": 1.70,
        "fill_size_pct": 0.02,
        "account_id": "paper-default",
        "reactor_metadata": {"multi_leg_id": "ROLL-1", "leg_index": 1, "quantity": 2.0,
                             "role": "leg", "paper": True},
    }

    # ── INCREMENTAL fold ──
    inc = PortfolioState(state_db_path=tmp_path / "inc.db")
    inc.apply_execution(dict(leg0))
    inc.apply_execution(dict(leg1))
    inc_qty = inc.get_positions("paper-default")[("us_option", occ)].quantity
    inc_equity = inc.get_cash("paper-default").equity_total

    # ── REBUILD fold ──
    jsonl = tmp_path / "executions.jsonl"
    _write(jsonl, [leg0, leg1])
    reb = PortfolioState(state_db_path=tmp_path / "reb.db")
    reb.reconstruct_from(jsonl)
    reb_qty = reb.get_positions("paper-default")[("us_option", occ)].quantity
    reb_equity = reb.get_cash("paper-default").equity_total

    # Both legs land: net +1 LONG, and the two folds agree (cs51 not regressed).
    assert reb_qty == 1.0
    assert inc_qty == 1.0
    assert inc_qty == reb_qty
    assert abs(inc_equity - reb_equity) < 1e-9


def test_clean_paper_default_fold_byte_identical(tmp_path, monkeypatch) -> None:
    """Byte-identity baseline: a single clean paper-default equity fill (top-level
    account_id present, no duplicate) folds to the SAME book before and after both fixes.

    A truthy top-level account_id resolves identically under the new fallback; a log with
    no duplicates is unchanged by the dedup. Pins the legacy values.
    """
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "0")
    clean = tmp_path / "clean.jsonl"
    _write(clean, [_equity_fill()])

    ps = PortfolioState(state_db_path=tmp_path / "clean.db")
    ps.reconstruct_from(clean)
    cash = ps.get_cash("paper-default")
    qty = ps.get_positions("paper-default")[("equity", "SPY")].quantity

    assert qty == 0.05
    assert abs(cash.balance_usd - 95000.0) < 1e-9
    assert abs(cash.equity_total - 100000.0) < 1e-9
