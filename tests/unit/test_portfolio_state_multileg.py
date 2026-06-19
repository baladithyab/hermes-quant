"""Unit tests for PortfolioState multi-leg extensions (ADR-0029 §2.4).

Deterministic, no network. Verifies the contract/share quantity unit + the per-leg
idempotency key, and that the legacy equity NAV-fraction path is bit-identical.
"""

from __future__ import annotations

import json

from hermes_quant.state.portfolio_state import PortfolioState


def _rec(**kw):
    base = dict(
        proposal_id="prop_x",
        asof_execution="2026-05-30T18:00:00Z",
        asset="NVDA260626C00160000",
        asset_class="us_option",
        fill_price=4.5,
        fill_size_pct=-0.05,
        account_id="paper-default",
    )
    base.update(kw)
    return base


def test_option_child_quantity_is_contracts(tmp_path) -> None:
    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    ps.apply_execution(_rec(reactor_metadata={"quantity": -1, "role": "leg"}))
    positions = ps.get_positions("paper-default")
    pos = positions[("us_option", "NVDA260626C00160000")]
    assert pos.quantity == -1  # CONTRACTS, not NAV fraction (-0.05)


def test_equity_legacy_path_unchanged(tmp_path) -> None:
    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    # No reactor_metadata.quantity => the legacy NAV-fraction proxy.
    ps.apply_execution(
        _rec(asset="AAPL", asset_class="equity", fill_size_pct=0.10, fill_price=150.0)
    )
    positions = ps.get_positions("paper-default")
    pos = positions[("equity", "AAPL")]
    assert abs(pos.quantity - 0.10) < 1e-12  # NAV-fraction units preserved


def test_two_children_same_family_both_applied(tmp_path) -> None:
    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    opt = _rec(
        asset="NVDA260626C00160000",
        asset_class="us_option",
        reactor_metadata={"quantity": -1},
    )
    eq = _rec(
        asset="NVDA",
        asset_class="equity",
        fill_size_pct=0.05,
        fill_price=160.0,
        reactor_metadata={"quantity": 100},
    )
    # Both share (proposal_id, asof_execution) but differ in asset/asset_class.
    ps.apply_execution(opt)
    ps.apply_execution(eq)
    positions = ps.get_positions("paper-default")
    assert positions[("us_option", "NVDA260626C00160000")].quantity == -1
    assert positions[("equity", "NVDA")].quantity == 100


def test_reapply_same_child_is_noop(tmp_path) -> None:
    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    rec = _rec(reactor_metadata={"quantity": -1})
    ps.apply_execution(rec)
    ps.apply_execution(rec)  # idempotency: per-leg key holds
    positions = ps.get_positions("paper-default")
    assert positions[("us_option", "NVDA260626C00160000")].quantity == -1


def test_legacy_equity_dedup_still_per_proposal(tmp_path) -> None:
    """Two legacy equity records sharing (proposal_id, asof_execution) with the ""
    sentinel collide on the legacy key (one fill per proposal) — bit-identical."""
    ps = PortfolioState(state_db_path=tmp_path / "state.db")
    rec = _rec(asset="AAPL", asset_class="equity", fill_size_pct=0.10, fill_price=150.0)
    ps.apply_execution(rec)
    ps.apply_execution(rec)  # same legacy key => no double-apply
    positions = ps.get_positions("paper-default")
    assert abs(positions[("equity", "AAPL")].quantity - 0.10) < 1e-12


def test_legacy_2col_pk_db_migrates_and_both_cc_legs_land(tmp_path) -> None:
    """REGRESSION (Wave-D review P0): on a pre-wave state.db with the OLD 2-column
    processed_fills PK, a covered call's two legs share (proposal_id, asof_execution)
    and the 2nd leg was SILENTLY DROPPED (INSERT OR IGNORE dedup on the legacy key)
    while both legs still hit executions.jsonl — a money-path bus/state divergence.
    The migration must REBUILD the table to the 4-column PK so BOTH legs land. The
    eval gate + other tests use fresh DBs (4-col PK from _SCHEMA), so only this
    legacy-shaped seed exercises the bug.
    """
    import sqlite3

    db = tmp_path / "state.db"
    # Seed a LEGACY-shaped DB: processed_fills with the OLD 2-column PRIMARY KEY,
    # plus one pre-existing row (so the rebuild's row-preservation is exercised).
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE processed_fills (
            proposal_id    TEXT NOT NULL,
            asof_execution TEXT NOT NULL,
            applied_at     TEXT NOT NULL,
            PRIMARY KEY (proposal_id, asof_execution)
        );
        INSERT INTO processed_fills VALUES ('prop_old', '2026-01-01T00:00:00Z', '2026-01-01T00:00:01Z');
        """
    )
    conn.commit()
    conn.close()

    # Constructing PortfolioState runs _init_schema -> _migrate_processed_fills.
    ps = PortfolioState(state_db_path=db)

    # The PK must now be the 5-column form (the rebuild happened: cs44 asset/asset_class
    # + cs51 leg_index). A 2-col legacy seed rebuilds straight to 5-col.
    with sqlite3.connect(db) as c:
        info = list(c.execute("PRAGMA table_info(processed_fills)"))
        pk = [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]
        assert pk == ["proposal_id", "asof_execution", "asset", "asset_class", "leg_index"]
        # The legacy row was preserved across the rebuild.
        n_old = c.execute(
            "SELECT COUNT(*) FROM processed_fills WHERE proposal_id='prop_old'"
        ).fetchone()[0]
        assert n_old == 1

    # Fire BOTH legs of a covered call (shared proposal_id+asof, distinct asset).
    opt = _rec(asset="NVDA260626C00160000", asset_class="us_option",
               reactor_metadata={"quantity": -1})
    eq = _rec(asset="NVDA", asset_class="equity", fill_size_pct=0.05,
              fill_price=160.0, reactor_metadata={"quantity": 100})
    ps.apply_execution(opt)
    ps.apply_execution(eq)

    positions = ps.get_positions("paper-default")
    # BOTH legs must be present — the equity leg is no longer silently dropped.
    assert positions[("us_option", "NVDA260626C00160000")].quantity == -1
    assert positions[("equity", "NVDA")].quantity == 100


def test_same_occ_roll_incremental_equals_rebuild(tmp_path, monkeypatch) -> None:
    """cs51 REGRESSION: ONE multi-leg family ROLLs the SAME OCC contract — leg_index 0
    sell-to-close (-1) + leg_index 1 buy-to-open (+2), net +1 LONG. Both option children
    share (proposal_id, asof_execution, asset=OCC, asset_class=us_option), so the cs44
    4-column dedup key COLLIDES: the incremental fold's 2nd-leg INSERT OR IGNORE returns
    rowcount==0 and the leg is SILENTLY DROPPED (qty stuck at -1, a phantom SHORT) while
    reconstruct_from (no dedup table) folds BOTH legs to +1. That is a bus/state money
    divergence on equity_total (the gate-sized NAV). The leg_index disambiguator in the
    dedup key must make incremental == rebuild for both quantity AND equity_total.

    Pinned HERMES_QUANT_DELTA_NORMALIZER=0 so the regime stamp/guard never fires (this is
    the default-OFF legacy-equivalent fold; the normalizer path is a separate concern).
    """
    monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "0")
    occ = "AAPL260116C00200000"
    asof = "2026-06-13T15:30:00+00:00"
    leg0 = _rec(
        proposal_id="ROLL-1",
        asof_execution=asof,
        asset=occ,
        asset_class="us_option",
        target_position_pct=-0.01,
        fill_price=1.50,
        fill_size_pct=-0.01,
        reactor_metadata={"multi_leg_id": "ROLL-1", "leg_index": 0, "quantity": -1.0,
                          "role": "leg", "paper": True},
    )
    leg1 = _rec(
        proposal_id="ROLL-1",
        asof_execution=asof,
        asset=occ,
        asset_class="us_option",
        target_position_pct=0.02,
        fill_price=1.70,
        fill_size_pct=0.02,
        reactor_metadata={"multi_leg_id": "ROLL-1", "leg_index": 1, "quantity": 2.0,
                          "role": "leg", "paper": True},
    )

    # ── INCREMENTAL fold ────────────────────────────────────────────────
    ps_inc = PortfolioState(state_db_path=tmp_path / "inc.db")
    ps_inc.apply_execution(dict(leg0))
    ps_inc.apply_execution(dict(leg1))
    inc_pos = ps_inc.get_positions("paper-default")
    inc_qty = inc_pos[("us_option", occ)].quantity
    inc_equity = ps_inc.get_cash("paper-default").equity_total

    # ── REBUILD fold (canonical: reconstruct_from has no dedup table) ───
    jsonl = tmp_path / "executions.jsonl"
    jsonl.write_text(json.dumps(leg0) + "\n" + json.dumps(leg1) + "\n")
    ps_reb = PortfolioState(state_db_path=tmp_path / "reb.db")
    ps_reb.reconstruct_from(jsonl)
    reb_pos = ps_reb.get_positions("paper-default")
    reb_qty = reb_pos[("us_option", occ)].quantity
    reb_equity = ps_reb.get_cash("paper-default").equity_total

    # Both legs land: net +1 LONG, and incremental matches the canonical rebuild.
    assert reb_qty == 1.0
    assert inc_qty == 1.0
    assert inc_qty == reb_qty
    assert abs(inc_equity - reb_equity) < 1e-9
