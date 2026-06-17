"""tests/ops/test_quant_portfolio_daily_unit_aware_pnl.py — unit-aware MTM P&L regression.

The EOD report `ops/scripts/quant-portfolio-daily.py` (live cron #14, `5 13 * * 1-5`,
delivered to Discord) reads `state.db.positions` and computes mark-to-market P&L.

The standard PaperReactor equity path stores `Position.quantity` as a SIGNED
NAV-FRACTION (e.g. 0.20 = a 20%-of-NAV long), because
`_apply_execution_unsafe` sets `pos_delta = fill_size_pct` on the legacy equity path
(portfolio_state.py:523). The canonical mark-to-market formula for these rows is the
one `PortfolioState.get_marked_equity` documents (portfolio_state.py:805):

    unrealized_i = quantity * nav_ref * (mark / avg_entry - 1.0)

with `nav_ref = cash.equity_total`. The daily script instead computed the SHARE
formula `(mark - avg) * qty`, which treats a 0.20 NAV-fraction as 0.20 *shares* —
off by ~`avg * nav_ref / qty` and dimensionally meaningless. Discord then reported
garbage unrealized_pnl / market_value / total_unrealized_pnl for the only path that
currently writes state.db.

These tests pin:
  • NAV-fraction rows are valued with the canonical get_marked_equity form, not (mark-avg)*qty.
  • nav_ref comes from cash.equity_total.
  • a missing nav_ref (no cash row) fails HONESTLY (pnl fields None), never emits the share garbage.

The script is a standalone file (not a package module), loaded via spec_from_file_location.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "ops" / "scripts" / "quant-portfolio-daily.py"
)


def _load_script(tmp_path: Path):
    """Load the standalone brief and repoint its module-level paths into tmp_path."""
    spec = importlib.util.spec_from_file_location("ops_portfolio_daily", _SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Repoint the module-level state.db / snapshot paths into the sandbox.
    mod.STATE_DB_PATH = tmp_path / "state.db"
    mod.EXECUTIONS_PATH = tmp_path / "executions.jsonl"
    mod.SNAPSHOT_DIR = tmp_path / "snaps"
    return mod


def _seed_state_db(
    db_path: Path,
    *,
    account: str = "paper-default",
    rows: list[tuple] | None = None,
    equity_total: float | None = 100_000.0,
) -> None:
    """Seed a minimal state.db with positions (+ optional cash).

    `rows` is a list of (asset_class, symbol, quantity, avg_entry_price) tuples, or
    (asset_class, symbol, quantity, avg_entry_price, unit_kind) 5-tuples (ar118). When
    unit_kind is omitted it defaults production-faithfully: 'true_unit' for us_option
    (ADR-0088), else 'nav_fraction' (the column default). The schema now carries the
    unit_kind column the real state.db has (portfolio_state.py _SCHEMA).
    """
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE positions (
            account_id      TEXT NOT NULL,
            asset_class     TEXT NOT NULL,
            symbol          TEXT NOT NULL,
            quantity        REAL NOT NULL,
            avg_entry_price REAL NOT NULL,
            last_update_at  TEXT NOT NULL,
            unit_kind       TEXT NOT NULL DEFAULT 'nav_fraction',
            PRIMARY KEY (account_id, asset_class, symbol)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE cash (
            account_id     TEXT PRIMARY KEY,
            balance_usd    REAL NOT NULL,
            last_update_at TEXT NOT NULL,
            equity_total   REAL NOT NULL
        )
        """
    )
    for row in rows or []:
        asset_class, symbol, qty, avg = row[0], row[1], row[2], row[3]
        unit_kind = (
            row[4]
            if len(row) > 4
            else ("true_unit" if asset_class == "us_option" else "nav_fraction")
        )
        conn.execute(
            "INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
            (account, asset_class, symbol, qty, avg, "2026-06-14T20:00:00+00:00", unit_kind),
        )
    if equity_total is not None:
        conn.execute(
            "INSERT INTO cash VALUES (?,?,?,?)",
            (account, equity_total, "2026-06-14T20:00:00+00:00", equity_total),
        )
    conn.commit()
    conn.close()


def test_nav_fraction_unrealized_uses_get_marked_equity_form(tmp_path):
    """qty=0.20 (NAV-fraction long) @ avg=100, nav_ref=100k, mark=110.

    Canonical: 0.20 * 100_000 * (110/100 - 1) = 0.20 * 100_000 * 0.10 = +$2,000.
    WRONG share formula (mark-avg)*qty = (110-100)*0.20 = +$2.00 (garbage).
    """
    mod = _load_script(tmp_path)
    _seed_state_db(
        mod.STATE_DB_PATH,
        rows=[("equity", "NVDA", 0.20, 100.0)],
        equity_total=100_000.0,
    )
    positions = mod.load_positions(account="paper-default")
    assert len(positions) == 1
    nav_ref = mod.load_nav_ref(account="paper-default")
    assert nav_ref == 100_000.0  # comes from cash.equity_total
    enriched = mod.compute_position_pnl(positions, {"NVDA": 110.0}, {}, nav_ref)
    p = enriched[0]

    expected = 0.20 * nav_ref * (110.0 / 100.0 - 1.0)  # +2000.0
    assert p["unrealized_pnl"] == pytest.approx(expected)
    # And explicitly NOT the share-formula garbage.
    assert p["unrealized_pnl"] != pytest.approx((110.0 - 100.0) * 0.20)
    # market_value is the signed notional, not mark*qty.
    assert p["market_value"] == pytest.approx(0.20 * nav_ref)


def test_nav_fraction_short_profits_when_mark_below_entry(tmp_path):
    """qty=-0.20 (20% short) @ avg=100, mark=90 → short PROFITS.

    Canonical: -0.20 * 100_000 * (90/100 - 1) = -0.20 * 100_000 * -0.10 = +$2,000.
    """
    mod = _load_script(tmp_path)
    _seed_state_db(
        mod.STATE_DB_PATH,
        rows=[("equity", "TSLA", -0.20, 100.0)],
        equity_total=100_000.0,
    )
    positions = mod.load_positions(account="paper-default")
    nav_ref = mod.load_nav_ref(account="paper-default")
    enriched = mod.compute_position_pnl(positions, {"TSLA": 90.0}, {}, nav_ref)
    p = enriched[0]
    expected = -0.20 * 100_000.0 * (90.0 / 100.0 - 1.0)  # +2000.0
    assert p["unrealized_pnl"] == pytest.approx(expected)
    assert p["unrealized_pnl"] > 0  # short profited


def test_nav_fraction_missing_nav_ref_fails_honestly(tmp_path):
    """No cash row → no NAV reference → must NOT emit the share-formula garbage.

    The row's pnl fields are set to None (drop, don't lie)."""
    mod = _load_script(tmp_path)
    _seed_state_db(
        mod.STATE_DB_PATH,
        rows=[("equity", "AMD", 0.20, 100.0)],
        equity_total=None,  # no cash row
    )
    positions = mod.load_positions(account="paper-default")
    nav_ref = mod.load_nav_ref(account="paper-default")
    assert nav_ref is None  # no cash row → no NAV reference
    enriched = mod.compute_position_pnl(positions, {"AMD": 110.0}, {}, nav_ref)
    p = enriched[0]
    assert p["unrealized_pnl"] is None
    assert p["market_value"] is None


def test_true_unit_option_path_unchanged(tmp_path):
    """A true-unit us_option leg (real contracts) keeps the share formula × 100.

    qty=2 contracts @ avg=1.50 premium, mark=2.50:
      unreal = (2.50 - 1.50) * 2 * 100 = +$200 ; market_value = 2.50 * 2 * 100 = $500.
    This path must stay byte-identical in intent to the contract-multiplier accounting.
    """
    mod = _load_script(tmp_path)
    _seed_state_db(
        mod.STATE_DB_PATH,
        rows=[("us_option", "NVDA260117C00120000", 2.0, 1.50)],
        equity_total=100_000.0,
    )
    positions = mod.load_positions(account="paper-default")
    enriched = mod.compute_position_pnl(positions, {"NVDA260117C00120000": 2.50}, {})
    p = enriched[0]
    assert p["unrealized_pnl"] == pytest.approx((2.50 - 1.50) * 2.0 * 100.0)
    assert p["market_value"] == pytest.approx(2.50 * 2.0 * 100.0)


def test_true_unit_equity_det_equity_uses_share_formula_not_nav_fraction(tmp_path):
    """ar118 REGRESSION: a deterministic-equity EQUITY position is unit_kind='true_unit'
    (real signed SHARES, ADR-0086), NOT a NAV-fraction. The report must value it with the
    SHARE formula (mult=1, no ×100 contract multiplier), NOT the NAV-fraction form that
    the old `true_unit = asset_class=='us_option'` heuristic forced it into.

    50 shares of AAPL @ avg=$100, mark=$110, nav_ref=$100k:
      CORRECT (share):       market_value = 110*50      = $5,500
                             unrealized   = (110-100)*50 = +$500
      WRONG (old, NAV-frac): market_value = 50*100_000             = $5,000,000  (1000×)
                             unrealized   = 50*100_000*(110/100-1)  = +$500,000   (1000×)
    DETERMINISTIC_EQUITY=1 is LIVE, so this row class is real on the production book.
    """
    mod = _load_script(tmp_path)
    _seed_state_db(
        mod.STATE_DB_PATH,
        # 5-tuple: explicit unit_kind='true_unit' on an EQUITY row (det-equity shares).
        rows=[("equity", "AAPL", 50.0, 100.0, "true_unit")],
        equity_total=100_000.0,
    )
    positions = mod.load_positions(account="paper-default")
    assert len(positions) == 1
    assert positions[0]["unit_kind"] == "true_unit"
    nav_ref = mod.load_nav_ref(account="paper-default")
    enriched = mod.compute_position_pnl(positions, {"AAPL": 110.0}, {}, nav_ref)
    p = enriched[0]

    # Share formula, mult=1 (equity true-unit, NOT the option ×100).
    assert p["market_value"] == pytest.approx(110.0 * 50.0), (
        f"ar118: true-unit equity market_value must be mark*shares=$5,500, "
        f"not the NAV-fraction $5,000,000; got {p['market_value']}"
    )
    assert p["unrealized_pnl"] == pytest.approx((110.0 - 100.0) * 50.0), (
        f"ar118: true-unit equity unrealized must be (mark-avg)*shares=+$500, "
        f"not the NAV-fraction +$500,000; got {p['unrealized_pnl']}"
    )
    # Explicitly NOT the phantom NAV-fraction figures the bug produced.
    assert p["market_value"] != pytest.approx(50.0 * nav_ref)
    assert p["unrealized_pnl"] != pytest.approx(50.0 * nav_ref * (110.0 / 100.0 - 1.0))
