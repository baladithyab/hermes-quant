"""Unit tests for quant-playbook-quarterly metric computation.

These tests exercise the pure metric logic (compute_metrics) and the
report renderer with a fixed, deterministic portfolio. NO network IO,
NO file IO, NO yfinance — everything is constructed in-memory.

The script under test lives at ~/.hermes/scripts/quant-playbook-quarterly.py
(outside the repo so the cron can find it directly). We import it via
importlib.util to keep this test self-contained.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

QUARTERLY_SCRIPT = Path.home() / ".hermes" / "scripts" / "quant-playbook-quarterly.py"


@pytest.fixture(scope="module")
def quarterly_module():
    """Import the script as a module without executing main()."""
    if not QUARTERLY_SCRIPT.exists():
        pytest.skip(f"script not installed at {QUARTERLY_SCRIPT}")
    # Prevent the script's venv-exec shim from triggering when we import
    # it from inside an already-active venv: setting argv[0] ensures the
    # exec path matches our current interpreter.
    spec = importlib.util.spec_from_file_location(
        "quarterly_under_test", QUARTERLY_SCRIPT
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["quarterly_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_positions(qm, specs):
    return [
        qm.Position(
            symbol=s["symbol"],
            qty=s["qty"],
            cost_basis=s["cost_basis"],
            last_price=s["last_price"],
            sector=s["sector"],
            beta=s["beta"],
        )
        for s in specs
    ]


# --------------------------------------------------------------------------- #
# Position dataclass
# --------------------------------------------------------------------------- #


def test_position_market_value_and_pnl(quarterly_module):
    qm = quarterly_module
    p = qm.Position(symbol="X", qty=10, cost_basis=900.0, last_price=100.0)
    assert p.market_value == 1000.0
    assert p.unrealized_pnl == 100.0
    assert p.unrealized_pnl_pct == pytest.approx(100.0 / 900.0)


def test_position_zero_cost_basis_is_safe(quarterly_module):
    qm = quarterly_module
    p = qm.Position(symbol="X", qty=10, cost_basis=0.0, last_price=100.0)
    # Should not divide-by-zero
    assert p.unrealized_pnl_pct == 0.0


# --------------------------------------------------------------------------- #
# Empty / degenerate
# --------------------------------------------------------------------------- #


def test_empty_portfolio(quarterly_module):
    m = quarterly_module.compute_metrics(0.0, [])
    assert m.nav == 0.0
    assert m.flags == []
    assert m.rebalance_proposals == []


def test_cash_only_portfolio(quarterly_module):
    qm = quarterly_module
    m = qm.compute_metrics(50_000.0, [])
    assert m.nav == 50_000.0
    assert m.cash == 50_000.0
    assert m.gross_dollar_exposure == 0.0
    # All-cash should NOT fire any flags (no exposure to flag)
    assert m.flags == []


# --------------------------------------------------------------------------- #
# Clean portfolio — no flags fired
# --------------------------------------------------------------------------- #


def test_clean_portfolio_no_flags(quarterly_module):
    qm = quarterly_module
    # 5 sectors, each ~20%, beta ≈ 1.0, modest net exposure
    specs = [
        {"symbol": "A", "qty": 100, "cost_basis": 9000, "last_price": 100, "sector": "Tech", "beta": 1.1},
        {"symbol": "B", "qty": 100, "cost_basis": 9000, "last_price": 100, "sector": "Health", "beta": 0.9},
        {"symbol": "C", "qty": 100, "cost_basis": 9000, "last_price": 100, "sector": "Energy", "beta": 1.0},
        {"symbol": "D", "qty": 100, "cost_basis": 9000, "last_price": 100, "sector": "Financial", "beta": 1.0},
        {"symbol": "E", "qty": 100, "cost_basis": 9000, "last_price": 100, "sector": "Consumer", "beta": 1.0},
    ]
    positions = _make_positions(qm, specs)
    # Cash large enough to keep net dollar exposure ≤ 60% NAV
    # Total mv = 50_000; cash = 60_000 → NAV = 110_000, net = 50_000 / 110_000 ≈ 45.5%
    m = qm.compute_metrics(60_000.0, positions)
    assert m.nav == 110_000.0
    assert m.gross_dollar_exposure == 50_000.0
    assert m.weighted_beta == pytest.approx(1.0, abs=0.05)
    assert m.flags == []
    assert m.rebalance_proposals == []


# --------------------------------------------------------------------------- #
# Sector concentration flag
# --------------------------------------------------------------------------- #


def test_sector_concentration_fires(quarterly_module):
    qm = quarterly_module
    # 60% NAV in Tech → flag
    specs = [
        {"symbol": "T1", "qty": 100, "cost_basis": 30000, "last_price": 300, "sector": "Tech", "beta": 1.0},
        {"symbol": "T2", "qty": 100, "cost_basis": 30000, "last_price": 300, "sector": "Tech", "beta": 1.0},
        {"symbol": "F1", "qty": 100, "cost_basis": 20000, "last_price": 200, "sector": "Financial", "beta": 1.0},
    ]
    positions = _make_positions(qm, specs)
    # NAV: cash 20k + mv 80k = 100k; Tech = 60k = 60% — fires
    m = qm.compute_metrics(20_000.0, positions)
    sector_flags = [f for f in m.flags if "sector concentration: Tech" in f]
    assert len(sector_flags) == 1
    # And a rebalance proposal must be queued
    sector_props = [p for p in m.rebalance_proposals if p["kind"] == "scale_down_sector"]
    assert len(sector_props) == 1
    assert sector_props[0]["sector"] == "Tech"


# --------------------------------------------------------------------------- #
# Beta high / low flags
# --------------------------------------------------------------------------- #


def test_high_beta_fires(quarterly_module):
    qm = quarterly_module
    specs = [
        {"symbol": "HB1", "qty": 100, "cost_basis": 10000, "last_price": 100, "sector": "Tech", "beta": 2.0},
        {"symbol": "HB2", "qty": 100, "cost_basis": 10000, "last_price": 100, "sector": "Health", "beta": 1.8},
    ]
    positions = _make_positions(qm, specs)
    m = qm.compute_metrics(10_000.0, positions)
    assert any("beta high" in f for f in m.flags)
    assert any(p["kind"] == "reduce_beta" for p in m.rebalance_proposals)


def test_low_beta_fires(quarterly_module):
    qm = quarterly_module
    specs = [
        {"symbol": "LB1", "qty": 100, "cost_basis": 10000, "last_price": 100, "sector": "Utilities", "beta": 0.30},
        {"symbol": "LB2", "qty": 100, "cost_basis": 10000, "last_price": 100, "sector": "Consumer", "beta": 0.40},
    ]
    positions = _make_positions(qm, specs)
    m = qm.compute_metrics(10_000.0, positions)
    assert any("beta low" in f for f in m.flags)
    assert any(p["kind"] == "increase_beta" for p in m.rebalance_proposals)


# --------------------------------------------------------------------------- #
# Net dollar exposure flag
# --------------------------------------------------------------------------- #


def test_net_dollar_exposure_fires(quarterly_module):
    qm = quarterly_module
    # Tiny cash, big long position → net exposure ≈ 90% NAV → fires (> 60%)
    specs = [
        {"symbol": "L1", "qty": 100, "cost_basis": 50000, "last_price": 500, "sector": "Tech", "beta": 1.0},
        {"symbol": "L2", "qty": 100, "cost_basis": 40000, "last_price": 400, "sector": "Health", "beta": 1.0},
    ]
    positions = _make_positions(qm, specs)
    m = qm.compute_metrics(10_000.0, positions)
    # net exposure 90k / 100k NAV = 90%, > 60% → fires
    assert any("net dollar exposure" in f for f in m.flags)


# --------------------------------------------------------------------------- #
# Top-1 concentration flag
# --------------------------------------------------------------------------- #


def test_top_position_concentration_fires(quarterly_module):
    qm = quarterly_module
    # One position 25% of NAV
    specs = [
        {"symbol": "BIG", "qty": 100, "cost_basis": 25000, "last_price": 250, "sector": "Tech", "beta": 1.0},
        {"symbol": "S1",  "qty": 100, "cost_basis": 5000,  "last_price": 50,  "sector": "Health", "beta": 1.0},
        {"symbol": "S2",  "qty": 100, "cost_basis": 5000,  "last_price": 50,  "sector": "Energy", "beta": 1.0},
        {"symbol": "S3",  "qty": 100, "cost_basis": 5000,  "last_price": 50,  "sector": "Financial", "beta": 1.0},
    ]
    positions = _make_positions(qm, specs)
    m = qm.compute_metrics(60_000.0, positions)
    # NAV = 60k + 40k = 100k; BIG = 25k = 25% → fires
    assert m.top_position_symbol == "BIG"
    assert m.top_position_weight == pytest.approx(0.25)
    assert any("top-1 concentration: BIG" in f for f in m.flags)
    assert any(p["kind"] == "trim_top_position" for p in m.rebalance_proposals)


# --------------------------------------------------------------------------- #
# Sector-breakdown sums to gross
# --------------------------------------------------------------------------- #


def test_sector_breakdown_sums_to_net_mv(quarterly_module):
    qm = quarterly_module
    specs = [
        {"symbol": "A", "qty": 10, "cost_basis": 1000, "last_price": 100, "sector": "Tech", "beta": 1.0},
        {"symbol": "B", "qty": 10, "cost_basis": 1000, "last_price": 100, "sector": "Health", "beta": 1.0},
        {"symbol": "C", "qty": 10, "cost_basis": 1000, "last_price": 100, "sector": "Tech", "beta": 1.0},
    ]
    positions = _make_positions(qm, specs)
    m = qm.compute_metrics(0.0, positions)
    assert m.sector_breakdown["Tech"] == 2000.0
    assert m.sector_breakdown["Health"] == 1000.0
    assert sum(m.sector_breakdown.values()) == pytest.approx(m.net_dollar_exposure)


# --------------------------------------------------------------------------- #
# Renderer smoke
# --------------------------------------------------------------------------- #


def test_render_report_clean_portfolio(quarterly_module):
    qm = quarterly_module
    # Spread across enough names that no single position breaches 15%.
    specs = [
        {"symbol": chr(65 + i), "qty": 100, "cost_basis": 9000,
         "last_price": 100, "sector": f"Sector{i % 5}", "beta": 1.0}
        for i in range(10)
    ]
    positions = _make_positions(qm, specs)
    # NAV: cash 100k + mv 100k = 200k; each position 5% (clean)
    m = qm.compute_metrics(100_000.0, positions)
    md = qm.render_report("2026Q3", 100_000.0, positions, m)
    assert "# Quarterly Portfolio Review — 2026Q3" in md
    assert "## Summary" in md
    assert "## Top 10 Positions" in md
    assert "## Sector Breakdown" in md
    assert "## Factor-Exposure Flags" in md
    assert "✅ No flags" in md  # clean portfolio
    assert "## Recommended Actions" in md


def test_render_report_with_flags(quarterly_module):
    qm = quarterly_module
    # Force concentration flag
    specs = [
        {"symbol": "T1", "qty": 100, "cost_basis": 50000, "last_price": 500, "sector": "Tech", "beta": 1.5},
    ]
    positions = _make_positions(qm, specs)
    m = qm.compute_metrics(10_000.0, positions)
    md = qm.render_report("2026Q3", 10_000.0, positions, m)
    assert "⚠️" in md
    assert "manual confirmation required" in md


def test_render_report_halt_state(quarterly_module):
    qm = quarterly_module
    md = qm.render_report(
        "2026Q3", 0.0, [], qm.PortfolioMetrics(),
        halts=[{"reason": "circuit_breaker", "scope": "global"}],
    )
    assert "HALT STATE ACTIVE" in md
    assert "circuit_breaker" in md


def test_render_report_empty(quarterly_module):
    qm = quarterly_module
    md = qm.render_report("2026Q3", 0.0, [], qm.PortfolioMetrics())
    assert "Portfolio is empty" in md


# --------------------------------------------------------------------------- #
# Quarter label
# --------------------------------------------------------------------------- #


def test_quarter_label_format(quarterly_module):
    qm = quarterly_module
    label = qm.quarter_label()
    # YYYY + Q + 1-4
    assert len(label) == 6
    assert label[4] == "Q"
    assert label[5] in "1234"
    assert label[:4].isdigit()


def test_quarter_label_january(quarterly_module):
    from datetime import datetime
    qm = quarterly_module
    # In January the label should be Q1
    dt = datetime(2027, 1, 15, tzinfo=qm.UTC).astimezone(qm.ET)
    assert qm.quarter_label(dt) == "2027Q1"


def test_quarter_label_april(quarterly_module):
    from datetime import datetime
    qm = quarterly_module
    dt = datetime(2027, 4, 5, tzinfo=qm.UTC).astimezone(qm.ET)
    assert qm.quarter_label(dt) == "2027Q2"


def test_quarter_label_december(quarterly_module):
    from datetime import datetime
    qm = quarterly_module
    dt = datetime(2027, 12, 31, tzinfo=qm.UTC).astimezone(qm.ET)
    assert qm.quarter_label(dt) == "2027Q4"


# --------------------------------------------------------------------------- #
# First-Monday-of-quarter guard (defends against cron POSIX OR semantics)
# --------------------------------------------------------------------------- #


def test_is_first_monday_of_quarter_yes(quarterly_module):
    from datetime import datetime
    qm = quarterly_module
    # 2026-04-06 was a Monday in April → first Monday of Q2 2026
    dt = datetime(2026, 4, 6, 12, 0, tzinfo=qm.UTC).astimezone(qm.ET)
    assert qm.is_first_monday_of_quarter(dt) is True


def test_is_first_monday_of_quarter_wrong_month(quarterly_module):
    from datetime import datetime
    qm = quarterly_module
    # 2026-05-04 was a Monday but May isn't a quarter month
    dt = datetime(2026, 5, 4, 12, 0, tzinfo=qm.UTC).astimezone(qm.ET)
    assert qm.is_first_monday_of_quarter(dt) is False


def test_is_first_monday_of_quarter_not_monday(quarterly_module):
    from datetime import datetime
    qm = quarterly_module
    # 2026-07-01 was a Wednesday — would falsely trigger under POSIX OR cron
    dt = datetime(2026, 7, 1, 12, 0, tzinfo=qm.UTC).astimezone(qm.ET)
    assert qm.is_first_monday_of_quarter(dt) is False


def test_is_first_monday_of_quarter_second_monday(quarterly_module):
    from datetime import datetime
    qm = quarterly_module
    # 2026-04-13 is the second Monday of April — must NOT fire
    dt = datetime(2026, 4, 13, 12, 0, tzinfo=qm.UTC).astimezone(qm.ET)
    assert qm.is_first_monday_of_quarter(dt) is False


def test_is_first_monday_of_quarter_jan(quarterly_module):
    from datetime import datetime
    qm = quarterly_module
    # 2027-01-04 is the first Monday of January 2027
    dt = datetime(2027, 1, 4, 12, 0, tzinfo=qm.UTC).astimezone(qm.ET)
    assert qm.is_first_monday_of_quarter(dt) is True
