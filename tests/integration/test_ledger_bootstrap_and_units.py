"""tests/integration/test_ledger_bootstrap_and_units.py

ADR-0086 Phase-2 (DEFERRED share-migration) regression safety net.

These three cross-tool integration tests are the ones the 2026-06-02 premortem
(docs/research/2026-06-02-premortem.md) says are ABSENT — and whose absence is
precisely why the deferred share-migration is predicted to ship buggy. They are
written NOW, against CURRENT (NAV-fraction) behavior, so they exist as the net
the migration must keep green (and will UPDATE the expected magnitudes for) when
state.db.positions.quantity flips from a signed NAV-fraction to signed SHARES and
cash becomes real dollars.

CURRENT UNITS REALITY (the invariant these tests lock — see ADR-0086 §Context):
  * positions.quantity is a SIGNED NAV-FRACTION (e.g. -0.2 == 20% short), NOT shares.
  * avg_entry_price is the entry mark.
  * cash.balance_usd is mutated by the dimensionally-approximate
    delta_cash = -fill_size_pct * fill_price (a fraction × price proxy).
  * cash.equity_total = balance_usd + Σ |quantity| * avg_entry_price
    (cost-basis coherent: a 0.20 buy at $100 on flat $100k lands equity_total == $100k).

NOTHING in production is changed by this file. All three tests PASS against the
current code. Each test's docstring states (a) which premortem failure mode it
guards and (b) the post-migration assertion the migration author must flip.

Map test → premortem failure mode:
  test_bootstrap_sequence_20pct_buy_on_flat_100k        → #1 NAV_at_fill circularity
  test_quantity_unit_consumers_agree_on_fraction_semantics → #2 share-migration blast radius
  test_reconcile_is_idempotent_flat_to_flat             → #4 replay/reconcile divergence
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from hermes_quant.state.portfolio_state import PortfolioState

# ---------------------------------------------------------------------------
# Shared fixtures / helpers (style mirrors tests/unit/test_portfolio_state_accounting.py)
# ---------------------------------------------------------------------------

_ACCOUNT = "paper-default"


def _exec_rec(**kw: Any) -> dict[str, Any]:
    """Build an executions.jsonl / apply_execution record dict.

    Matches the field shape paper._record_to_dict produces and that
    PortfolioState._replay_record / _apply_execution_unsafe consume.
    """
    base: dict[str, Any] = dict(
        proposal_id="test_prop",
        asof_execution="2026-06-02T12:00:00Z",
        account_id=_ACCOUNT,
        asset_class="equity",
        fill_price=100.0,
        fill_size_pct=0.0,
    )
    base.update(kw)
    return base


def _write_executions(path: Path, records: list[dict[str, Any]]) -> None:
    """Write records as canonical one-object-per-line JSONL (the authoritative log)."""
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def _logical_state(db_path: Path) -> tuple[list[tuple], list[tuple]]:
    """Return the MEANINGFUL ('bit-identical') contents of state.db.

    We compare logical row tuples rather than raw file bytes: SQLite in WAL mode
    embeds page/journal/timestamp noise that is never bit-stable across two writes
    even for identical data, so a byte-hash comparison would be a flaky proxy for
    the property the premortem actually cares about (#4): that reconcile produces
    the SAME positions + cash projection every time. Logical-row equality is that
    property exactly.
    """
    con = sqlite3.connect(str(db_path))
    try:
        con.row_factory = sqlite3.Row
        positions = [
            (r["account_id"], r["asset_class"], r["symbol"], r["quantity"], r["avg_entry_price"])
            for r in con.execute(
                "SELECT account_id, asset_class, symbol, quantity, avg_entry_price "
                "FROM positions ORDER BY account_id, asset_class, symbol"
            )
        ]
        cash = [
            (r["account_id"], r["balance_usd"], r["equity_total"])
            for r in con.execute(
                "SELECT account_id, balance_usd, equity_total FROM cash ORDER BY account_id"
            )
        ]
    finally:
        con.close()
    return positions, cash


def _load_reconcile_module():
    """Import ops/scripts/quant-ledger-reconcile.py via spec_from_file_location.

    ops/scripts/* filenames carry hyphens (not importable as normal modules) so we
    use the spec_from_file_location loader idiom (see
    tests/ops/test_quant_research_loop_cron.py). This script has NO execv/venv guard
    (verified at write time), so — unlike the research-loop cron — no sys.executable
    neutralization is required; it imports cleanly under the test interpreter.
    """
    repo = Path(__file__).resolve().parents[2]
    path = repo / "ops" / "scripts" / "quant-ledger-reconcile.py"
    spec = importlib.util.spec_from_file_location("quant_ledger_reconcile", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===========================================================================
# TEST 1 — premortem failure mode #1: NAV_at_fill bootstrapping circularity
# ===========================================================================


def test_bootstrap_sequence_20pct_buy_on_flat_100k(tmp_path, monkeypatch) -> None:
    """GUARDS premortem #1 (NAV_at_fill circularity corrupts first fills).

    The premortem (§1.4) states explicitly that NO existing test combines:
      (1) a FRESH state.db created by the production reconcile/reconstruct fold,
      (2) the FIRST fill on that DB, and
      (3) a coherence assertion on the resulting position + cash + equity.
    The autonomous tests build PortfolioState in isolation; the accounting unit
    tests call apply_execution directly. This is the missing cross-tool bootstrap
    test — the one that would have caught the first post-migration fill writing
    ~2 shares / $20 instead of 200 shares / $80k.

    It exercises the REAL production bootstrap path: a flat $100k book, a single
    +0.20 (20% of NAV) buy at $100.00 recorded in executions.jsonl, folded into a
    brand-new state.db via PortfolioState.reconstruct_from (the exact fold that
    quant-ledger-reconcile and the post-data-loss rebuild use).

    CURRENT (NAV-fraction) coherence asserted here:
      * position quantity == 0.20            (the NAV-fraction itself)
      * avg_entry_price   == 100.0
      * cash.equity_total == 100_000.0       (cost-basis coherent on a fresh book)
      * balance_usd       == 99_980.0        (= 100_000 - 0.20*100; the documented
                                              fraction×price cash proxy)

    POST-MIGRATION (Phase 2) the migration author MUST flip these to:
      * quantity      == 200.0  shares       (= 0.20 * NAV_at_fill / fill_price)
      * balance_usd   == 80_000.0 dollars    (= 100_000 - 200*100)
      * equity_total  == 100_000.0 (cash + Σ qty*mark, mark==entry on a fresh book)
    If the migration reintroduces the NAV_at_fill circularity, the new quantity
    assertion (200 shares) fails LOUDLY here on a fresh-DB-via-reconcile path —
    which is exactly the seam the premortem says shipped uncaught.
    """
    # Production-shaped fresh book: HERMES_QUANT_PAPER_INITIAL_CASH backs the bootstrap.
    monkeypatch.setenv("HERMES_QUANT_PAPER_INITIAL_CASH", "100000")

    executions = tmp_path / "executions.jsonl"
    _write_executions(
        executions,
        [
            _exec_rec(
                proposal_id="bootstrap_first_fill",
                asof_execution="2026-06-02T14:30:00Z",
                asset="AAPL",
                fill_size_pct=0.20,  # +20% of NAV long
                fill_price=100.0,
            )
        ],
    )

    # Fresh DB built by the SAME idempotent fold quant-ledger-reconcile uses.
    state_db = tmp_path / "state.db"
    ps = PortfolioState(state_db_path=state_db)
    result = ps.reconstruct_from(executions)

    assert result.executions_processed == 1
    assert result.errors == []

    positions = ps.get_positions(_ACCOUNT)
    cash = ps.get_cash(_ACCOUNT)

    # --- position coherence (CURRENT fraction model) ---
    assert ("equity", "AAPL") in positions, "first fill must materialize an AAPL position"
    aapl = positions[("equity", "AAPL")]
    assert aapl.quantity == pytest.approx(0.20), (
        f"CURRENT model: quantity is the NAV-fraction 0.20; got {aapl.quantity}. "
        "(Post-migration this becomes 200.0 shares.)"
    )
    assert aapl.avg_entry_price == pytest.approx(100.0)

    # --- cash / equity coherence ---
    assert cash is not None, "bootstrap must create a cash row for the account"
    # equity_total is cost-basis coherent: a 20%-of-100k buy on a flat book leaves
    # equity at $100k. This is the magnitude invariant the premortem ties the whole
    # failure to ("20%-of-NAV buy on a flat $100k book yields ~200 shares" → $100k equity).
    assert cash.equity_total == pytest.approx(100_000.0), (
        f"fresh-book equity must stay $100k cost-basis; got {cash.equity_total}. "
        "A corrupted NAV_at_fill (premortem #1) shows up as equity O(100) here."
    )
    # balance_usd is the CURRENT dimensionally-approximate proxy: 100_000 - 0.20*100.
    assert cash.balance_usd == pytest.approx(99_980.0), (
        f"CURRENT model: cash moves by fill_size_pct*fill_price=$20; got {cash.balance_usd}. "
        "(Post-migration this becomes $80,000 = real dollars spent on 200 shares.)"
    )

    # The cross-consumer magnitude bridge: today's 0.20 fraction at this NAV/price
    # corresponds to 200 shares. This is the number the migration will STORE directly,
    # and it pins the "~200 shares" expectation the premortem anchors on.
    from hermes_quant.admissibility import target_pct_to_shares

    implied_shares = target_pct_to_shares(aapl.quantity, cash.equity_total, aapl.avg_entry_price)
    assert implied_shares == 200, (
        f"0.20 fraction @ NAV={cash.equity_total} price={aapl.avg_entry_price} must imply "
        f"200 shares (the premortem's intended magnitude); got {implied_shares}"
    )


# ===========================================================================
# TEST 2 — premortem failure mode #2: share-migration blast radius
# ===========================================================================


def test_quantity_unit_consumers_agree_on_fraction_semantics(tmp_path) -> None:
    """GUARDS premortem #2 (consumers silently misinterpret quantity's unit).

    The premortem lists ≥4 consumers that read positions.quantity AS a NAV-fraction
    today and will silently produce garbage once it becomes shares unless each is
    audited: risk/portfolio_normalize.py (the seam clip), the admissibility
    restatement (target_pct_to_shares), cli/status.py, and retro/daily scripts.

    This is the single test that FAILS THE MOMENT any covered consumer's unit
    assumption diverges from the ledger's. It builds a known 2-position book
    (+0.20 long, -0.10 short) and asserts EVERY cheaply-callable consumer reads
    those numbers as NAV-fractions, consistently.

    Consumers covered here (cheap, pure, importable):
      (a) risk.portfolio_normalize.PortfolioState — the ADR-0087 seam-clip snapshot.
          gross/net/cash are pure sums over `positions` treated as fractions.
      (b) admissibility.target_pct_to_shares — converts a NAV-fraction → shares
          using account NAV; proves the ledger value is consumed AS a fraction.
      (c) PortfolioState.get_marked_equity — the ADR-0086 Phase-1 MTM read path,
          whose formula (unrealized = quantity * nav_ref * (mark/entry - 1)) is
          ONLY correct if quantity is a fraction.

    Consumers NOT covered here (documented audit gap for the migration author):
      * cli/status.py — renders `qty={p.quantity:+.4f}` for human display only; no
        pure numeric seam to assert without driving the full CLI/Console. The
        premortem (§2.2.2) flags it as a display-units hazard, not a math seam.
      * ops/scripts/quant-admissibility-restate.py restate_book() — wraps consumer
        (b) but additionally needs an ETB oracle + asof snapshot fixture; its core
        unit conversion IS covered via target_pct_to_shares directly.
      * ops/scripts/quant-portfolio-daily.py / quant-strategy-retro-weekly.py —
        read symbol/quantity/avg_entry_price for notional+borrow estimates; they
        require provider marks + heavier scaffolding. Their unit ASSUMPTION is the
        same fraction semantics asserted here, but the scripts themselves are an
        un-covered audit surface the Phase-2 migration must address (premortem #2).

    POST-MIGRATION: when quantity becomes shares, consumer (a) must derive
    position_pct = qty*mark/equity (ADR-0086 §Consequences) and consumer (b) must
    stop double-multiplying by NAV. This test will be REWRITTEN to assert the
    shares-era contract; today it locks the fraction-era contract so a half-migrated
    consumer (one updated, one not) is impossible to land silently.
    """
    # Known book: +20% long AAPL @100, -10% short MSFT @50.
    state_db = tmp_path / "state.db"
    ps = PortfolioState(state_db_path=state_db)
    ps.apply_execution(
        _exec_rec(proposal_id="long_aapl", asset="AAPL", fill_size_pct=0.20, fill_price=100.0)
    )
    ps.apply_execution(
        _exec_rec(
            proposal_id="short_msft",
            asof_execution="2026-06-02T12:05:00Z",
            asset="MSFT",
            fill_size_pct=-0.10,
            fill_price=50.0,
        )
    )

    positions = ps.get_positions(_ACCOUNT)
    cash = ps.get_cash(_ACCOUNT)
    assert cash is not None

    # Ledger ground truth (fractions).
    qty_by_symbol = {sym: pos.quantity for (_ac, sym), pos in positions.items()}
    assert qty_by_symbol == pytest.approx({"AAPL": 0.20, "MSFT": -0.10})

    # --- consumer (a): risk.portfolio_normalize.PortfolioState seam snapshot ---
    from hermes_quant.risk.portfolio_normalize import PortfolioState as RiskPortfolioState

    risk_snapshot = RiskPortfolioState(positions=dict(qty_by_symbol))
    # If risk/ reads quantity as a fraction (it does, today), gross == Σ|fraction|.
    assert risk_snapshot.gross_exposure_pct == pytest.approx(0.30), (
        "risk seam MUST read quantity as NAV-fraction: gross == |0.20|+|0.10| == 0.30. "
        "If this reads 30 (shares-as-fraction) the migration broke the cap seam (#2 §2.3)."
    )
    assert risk_snapshot.net_exposure_pct == pytest.approx(0.10)  # 0.20 + (-0.10)
    assert risk_snapshot.cash_pct == pytest.approx(0.70)  # 1 - 0.30

    # --- consumer (b): admissibility.target_pct_to_shares reads value AS a fraction ---
    from hermes_quant.admissibility import target_pct_to_shares

    nav = cash.equity_total  # the NAV basis the restatement script uses
    # A fraction-consumer turns 0.20 into floor(0.20 * nav / 100); a shares-consumer
    # fed 0.20 would instead emit ~0 shares. Asserting the fraction interpretation:
    aapl_shares = target_pct_to_shares(qty_by_symbol["AAPL"], nav, 100.0)
    msft_shares = target_pct_to_shares(qty_by_symbol["MSFT"], nav, 50.0)
    assert aapl_shares == int((0.20 * nav) / 100.0), (
        f"target_pct_to_shares must treat {qty_by_symbol['AAPL']} as a NAV-fraction; "
        f"got {aapl_shares} shares at NAV={nav}"
    )
    assert aapl_shares > 0 and msft_shares < 0, "sign must survive the fraction→share conversion"

    # --- consumer (c): get_marked_equity formula presumes fraction quantity ---
    # unrealized_AAPL = +0.20 * nav_ref * (110/100 - 1) = +0.20*nav*0.10
    # unrealized_MSFT = -0.10 * nav_ref * (45/50  - 1) = -0.10*nav*(-0.10) = +0.01*nav
    marked = ps.get_marked_equity(_ACCOUNT, {"AAPL": 110.0, "MSFT": 45.0})
    nav_ref = marked.cost_basis_equity
    expected_unrealized = (0.20 * nav_ref * 0.10) + (-0.10 * nav_ref * (45.0 / 50.0 - 1.0))
    assert marked.total_unrealized == pytest.approx(expected_unrealized), (
        "get_marked_equity's signed-fraction MTM formula must match a fraction reading "
        f"of quantity; expected {expected_unrealized}, got {marked.total_unrealized}"
    )
    assert marked.equity_basis == "mark"
    assert marked.n_positions == 2 and marked.n_marked == 2


# ===========================================================================
# TEST 3 — premortem failure mode #4: replay / reconcile divergence
# ===========================================================================


def test_reconcile_is_idempotent_flat_to_flat(tmp_path, monkeypatch) -> None:
    """GUARDS premortem #4 (reconcile/replay becomes non-idempotent vs history).

    The premortem (§4) predicts the share-migration fold will produce a state.db
    that DIVERGES on re-run / vs archived snapshots. The defense is an idempotency
    lock: folding the SAME authoritative executions.jsonl must yield the SAME
    state.db projection every time (flat-$100k → known book → identical known book).

    This test drives the END-TO-END quant-ledger-reconcile path (loaded via the
    spec_from_file_location idiom because the filename has hyphens), pointing its
    module-level DEFAULT_STATE_DB / DEFAULT_EXECUTIONS_PATH at tmp_path so real
    ~/.hermes/quant data is never touched. It runs `--apply` twice and asserts the
    logical state.db (positions + cash rows) is identical across runs.

    It additionally asserts the underlying PortfolioState.reconstruct_from fold is
    idempotent on its own (the seam the reconcile tool wraps), so a regression is
    localized whether it lands in the script or the fold.

    POST-MIGRATION: the fold changes units (fraction→shares, dollars), so the
    EXPECTED book values below get updated — but the IDEMPOTENCE property asserted
    here must hold unchanged. If the migration's NAV_at_fill conversion makes the
    fold path-dependent (re-run drifts), this test fails, catching premortem #4
    before it reaches a live ledger.
    """
    monkeypatch.setenv("HERMES_QUANT_PAPER_INITIAL_CASH", "100000")

    executions = tmp_path / "executions.jsonl"
    records = [
        _exec_rec(
            proposal_id="r1",
            asof_execution="2026-06-02T12:00:00Z",
            asset="AAPL",
            fill_size_pct=0.20,
            fill_price=100.0,
        ),
        _exec_rec(
            proposal_id="r2",
            asof_execution="2026-06-02T12:30:00Z",
            asset="MSFT",
            fill_size_pct=-0.10,
            fill_price=50.0,
        ),
    ]
    _write_executions(executions, records)

    # --- (i) underlying fold idempotency: reconstruct_from twice into one DB ---
    fold_db = tmp_path / "fold_state.db"
    ps = PortfolioState(state_db_path=fold_db)
    ps.reconstruct_from(executions)
    fold_after_first = _logical_state(fold_db)
    ps.reconstruct_from(executions)
    fold_after_second = _logical_state(fold_db)
    assert fold_after_first == fold_after_second, (
        "PortfolioState.reconstruct_from must be idempotent (premortem #4): "
        f"first={fold_after_first} second={fold_after_second}"
    )
    # And a fresh independent DB must reach the same projection.
    fresh_db = tmp_path / "fresh_state.db"
    PortfolioState(state_db_path=fresh_db).reconstruct_from(executions)
    assert _logical_state(fresh_db) == fold_after_first, (
        "two independent reconstructs of the same log must agree (no path dependence)"
    )

    # --- (ii) end-to-end quant-ledger-reconcile --apply twice → identical state.db ---
    reconcile = _load_reconcile_module()
    live_db = tmp_path / "live_state.db"
    # Redirect the script's module-level paths at tmp_path so real data is untouched.
    monkeypatch.setattr(reconcile, "DEFAULT_STATE_DB", live_db, raising=True)
    monkeypatch.setattr(reconcile, "DEFAULT_EXECUTIONS_PATH", executions, raising=True)
    monkeypatch.setattr("sys.argv", ["quant-ledger-reconcile.py", "--apply"])

    rc1 = reconcile.main()
    assert rc1 == 0, "first reconcile --apply must succeed"
    live_after_first = _logical_state(live_db)

    rc2 = reconcile.main()
    assert rc2 == 0, "second reconcile --apply must succeed"
    live_after_second = _logical_state(live_db)

    assert live_after_first == live_after_second, (
        "quant-ledger-reconcile must be idempotent end-to-end (premortem #4): "
        f"first={live_after_first} second={live_after_second}"
    )

    # --- (iii) the actual flat-→known-book values (CURRENT fraction model) ---
    # These are the snapshot the premortem says future folds drift away from; pin them.
    positions, cash_rows = live_after_first
    pos_by_symbol = {sym: (qty, px) for (_acct, _cls, sym, qty, px) in positions}
    assert pos_by_symbol["AAPL"] == pytest.approx((0.20, 100.0))
    assert pos_by_symbol["MSFT"] == pytest.approx((-0.10, 50.0))
    # cash row: balance_usd = 100_000 - 0.20*100 + 0.10*50 = 99_985 ; equity_total =
    # 99_985 + |0.20|*100 + |0.10|*50 = 99_985 + 20 + 5 = 100_010 (current proxy).
    (_acct, balance_usd, equity_total) = cash_rows[0]
    assert balance_usd == pytest.approx(99_985.0)
    assert equity_total == pytest.approx(100_010.0)
