"""Unit-honesty guard for ops/scripts/quant-strategy-retro-weekly.py.

The LIVE Sunday weekly retro cron (job e52c189b6582, `0 13 * * 0`, delivered to
Discord) computes a "Total unrealized" mark-to-market headline from
``compute_unrealized_pnl``. That function read ``Position.quantity`` straight from
state.db and applied the SHARE formula ``(mark - avg) * qty``.

But on the legacy equity path ``Position.quantity`` is a SIGNED FRACTION OF NAV
(ADR-0086 / ADR-0088 ar13/ar14), NOT shares — exactly the unit-confusion class
the daily sibling (quant-portfolio-daily.py) was fixed for in ar60. A 20%-of-NAV
long in a $100 stock that rallied to $110 has a TRUE unrealized of
``0.20 * NAV * (110/100 - 1) = 0.20 * 100_000 * 0.10 = $2,000`` — but the share
formula reports ``(110 - 100) * 0.20 = $2.00``, a ~1000x understatement of an
operator-facing P&L number. The canonical form lives in
``PortfolioState.get_marked_equity`` (portfolio_state.py): for non-option rows
``unrealized = quantity * nav_ref * (mark / avg - 1.0)``.

These tests pin the weekly retro's per-symbol unrealized to that canonical form
so the Discord headline can never silently mis-scale the book again.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_weekly_retro_module():
    """Import ops/scripts/quant-strategy-retro-weekly.py (hyphenated filename)."""
    repo = Path(__file__).resolve().parents[2]
    path = repo / "ops" / "scripts" / "quant-strategy-retro-weekly.py"
    spec = importlib.util.spec_from_file_location("quant_strategy_retro_weekly", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _canonical_nav_fraction_unrealized(qty: float, avg: float, mark: float, nav: float) -> float:
    """Reference form lifted from PortfolioState.get_marked_equity (non-option)."""
    return qty * nav * (mark / avg - 1.0)


def test_nav_fraction_long_unrealized_matches_canonical_form() -> None:
    """A 20%-of-NAV long that rallies 10% must report ~$2,000, not ~$2.00.

    RED before the fix: the share formula (mark-avg)*qty returns 2.0, so the
    canonical-vs-reported assertion fails by ~1000x.
    """
    mod = _load_weekly_retro_module()

    nav = 100_000.0
    qty = 0.20  # signed NAV-fraction: 20% long
    avg = 100.0
    mark = 110.0

    # positions: list[(symbol, quantity, avg_entry_price)] — the real state.db read shape.
    positions = [("AAPL", qty, avg)]
    marks = {"AAPL": mark}

    out = mod.compute_unrealized_pnl(positions, marks, nav_ref=nav)
    reported = out["AAPL"]["unrealized_pnl"]

    expected = _canonical_nav_fraction_unrealized(qty, avg, mark, nav)
    assert expected == pytest.approx(2000.0)

    # Non-vacuity: the share-formula lie (2.0) is NOT close to the honest number.
    assert reported != pytest.approx(2.0), (
        "compute_unrealized_pnl is still using the (mark-avg)*qty SHARE formula on "
        "a NAV-fraction position — reports a fictional ~1000x-understated headline."
    )
    assert reported == pytest.approx(expected, rel=1e-9)


def test_nav_fraction_short_unrealized_profits_when_mark_falls() -> None:
    """A -10%-of-NAV short that drops 5% must show a POSITIVE unrealized profit.

    Guards both the unit scale AND the sign (a short profits when mark < avg).
    """
    mod = _load_weekly_retro_module()

    nav = 250_000.0
    qty = -0.10  # 10% short
    avg = 50.0
    mark = 47.5  # fell 5%

    positions = [("TSLA", qty, avg)]
    marks = {"TSLA": mark}

    out = mod.compute_unrealized_pnl(positions, marks, nav_ref=nav)
    reported = out["TSLA"]["unrealized_pnl"]

    expected = _canonical_nav_fraction_unrealized(qty, avg, mark, nav)
    # -0.10 * 250_000 * (47.5/50 - 1) = -0.10 * 250_000 * -0.05 = +1250.
    assert expected == pytest.approx(1250.0)
    assert reported > 0.0, "a short that fell in price must show a profit"
    assert reported == pytest.approx(expected, rel=1e-9)


def test_total_unrealized_headline_is_honest_for_a_realistic_book() -> None:
    """The summed 'Total unrealized' headline must be the canonical sum.

    This is the number that lands in the Discord weekly report; pin it.
    """
    mod = _load_weekly_retro_module()

    nav = 100_000.0
    positions = [
        ("AAPL", 0.20, 100.0),   # +10% move below → +2000
        ("MSFT", 0.15, 200.0),   # flat → 0
        ("TSLA", -0.10, 50.0),   # short, fell → +1250
    ]
    marks = {"AAPL": 110.0, "MSFT": 200.0, "TSLA": 47.5}

    out = mod.compute_unrealized_pnl(positions, marks, nav_ref=nav)
    total = sum(v["unrealized_pnl"] for v in out.values())

    expected_total = sum(
        _canonical_nav_fraction_unrealized(q, a, marks[s], nav)
        for s, q, a in positions
    )
    # AAPL: 0.20*100k*0.10 = +2000 ; MSFT: flat → 0 ; TSLA: -0.10*100k*-0.05 = +500.
    assert expected_total == pytest.approx(2000.0 + 0.0 + 500.0)
    assert total == pytest.approx(expected_total, rel=1e-9)


def test_nav_fraction_row_dropped_when_nav_ref_unavailable() -> None:
    """No NAV reference ⇒ a NAV-fraction row is un-markable (silence over a lie).

    Mirrors the daily sibling: rather than emit a fictional share-formula dollar
    figure, the row is omitted from the headline.
    """
    mod = _load_weekly_retro_module()

    positions = [("AAPL", 0.20, 100.0)]
    marks = {"AAPL": 110.0}

    out = mod.compute_unrealized_pnl(positions, marks, nav_ref=None)
    # Dropped (not in the dict) OR present with None pnl — either is honest; what
    # must NOT happen is a finite share-formula dollar figure.
    if "AAPL" in out:
        assert out["AAPL"]["unrealized_pnl"] is None
