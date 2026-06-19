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


def test_true_unit_us_option_uses_share_formula_times_100() -> None:
    """A us_option row is TRUE-UNIT (real signed contracts), valued via the
    contract-multiplier share form — the EXACT branch the ar60 daily sibling pins
    (tests/ops/test_quant_portfolio_daily_unit_aware_pnl.py::test_true_unit_option_path_unchanged).

    A 2-contract long bought at 1.50 marking 2.50:
      unrealized = (2.50 - 1.50) * 2 * 100 = +$200  (NOT the NAV-fraction form).

    Non-vacuity: the assertion would be wrong if the option fell through the
    NAV-fraction branch — qty * nav_ref * (mark/avg - 1) with any plausible nav_ref
    is nowhere near 200 — and the row must NOT be dropped on nav_ref=None (a
    true-unit row needs no NAV reference). This pins the us_option *100 form so the
    weekly retro can never silently apply the NAV-fraction form to a real option.
    """
    mod = _load_weekly_retro_module()

    # 4-tuple state.db read shape: (symbol, quantity_contracts, avg, asset_class).
    positions = [("NVDA260117C00120000", 2.0, 1.50, "us_option")]
    marks = {"NVDA260117C00120000": 2.50}

    # nav_ref=None on purpose: a true-unit option must NOT be dropped for lack of a
    # NAV reference (only NAV-fraction rows need one).
    out = mod.compute_unrealized_pnl(positions, marks, nav_ref=None)

    assert "NVDA260117C00120000" in out, (
        "a true-unit us_option row must be valued (it needs no nav_ref), not dropped"
    )
    reported = out["NVDA260117C00120000"]["unrealized_pnl"]
    assert reported == pytest.approx((2.50 - 1.50) * 2.0 * 100.0)  # +$200
    # Guard the units did NOT silently use the bare share formula without the ×100.
    assert reported != pytest.approx(2.0)


def test_true_unit_us_option_short_profits_when_mark_falls() -> None:
    """A SHORT us_option (negative contracts) profits when the mark falls — pins
    both the ×100 scale and the sign on the true-unit branch.

    -3 contracts written at 4.00, mark falls to 2.50:
      unrealized = (2.50 - 4.00) * (-3) * 100 = +$450 (a short gains as the mark drops).
    """
    mod = _load_weekly_retro_module()

    positions = [("AAPL260117P00150000", -3.0, 4.00, "us_option")]
    marks = {"AAPL260117P00150000": 2.50}

    out = mod.compute_unrealized_pnl(positions, marks, nav_ref=None)
    reported = out["AAPL260117P00150000"]["unrealized_pnl"]
    assert reported == pytest.approx((2.50 - 4.00) * -3.0 * 100.0)  # +$450
    assert reported > 0.0, "a short option whose mark fell must show a profit"


def test_corrupt_nav_fraction_excluded_from_weekly_pnl() -> None:
    """A nav_fraction row with |qty| > 1.0 is IMPOSSIBLE (max 100% of NAV) and is a
    corrupt raw-share count (the 2026-06-08 AAPL=510 incident).

    In the weekly retro's compute_unrealized_pnl, such a row MUST be excluded (not
    present in the return dict, or present with unrealized_pnl=None) and MUST emit
    a warning to stderr. Legit nav_fraction (qty=0.20) and true_unit (50 shares) must
    be unaffected — byte-identical to the normal unit-aware path.

    CORRUPT row (AAPL, nav_fraction, qty=510):
      - At mark=$300, nav=$100k: fake unrealized ≈ 510*100_000*(300/299-1) ≈ +$170k.
      - After the fix: EXCLUDED (must not appear in the result dict with a finite value).

    LEGIT nav_fraction (NVDA, qty=0.20):
      - unrealized = 0.20 * 100_000 * (110/100 - 1) = +$2,000 (unaffected).

    TRUE-UNIT equity (BA, qty=50 shares, unit_kind='true_unit'):
      - unrealized = (220 - 200) * 50 = +$1,000 (never filtered by nav_fraction guard).
    """
    mod = _load_weekly_retro_module()

    import io
    import sys as _sys

    nav = 100_000.0
    positions = [
        # corrupt: nav_fraction but qty=510 (impossible raw share count)
        ("AAPL", 510.0, 299.0, "equity", "nav_fraction"),
        # legit nav_fraction
        ("NVDA", 0.20, 100.0, "equity", "nav_fraction"),
        # true-unit equity (det-equity shares) — must NOT be touched by nav_fraction guard
        ("BA", 50.0, 200.0, "equity", "true_unit"),
    ]
    marks = {"AAPL": 300.0, "NVDA": 110.0, "BA": 220.0}

    captured_stderr = io.StringIO()
    old_stderr = _sys.stderr
    _sys.stderr = captured_stderr
    try:
        out = mod.compute_unrealized_pnl(positions, marks, nav_ref=nav)
    finally:
        _sys.stderr = old_stderr
    stderr_output = captured_stderr.getvalue()

    # Warning MUST be emitted to stderr for the corrupt row.
    assert "AAPL" in stderr_output or "corrupt" in stderr_output.lower() or "warn" in stderr_output.lower(), (
        f"Expected a stderr warning for the corrupt AAPL nav_fraction row, got: {stderr_output!r}"
    )

    # Corrupt row must be absent OR present with unrealized_pnl=None — never a finite phantom value.
    if "AAPL" in out:
        assert out["AAPL"]["unrealized_pnl"] is None, (
            f"corrupt AAPL must NOT have a finite unrealized_pnl; got {out['AAPL']['unrealized_pnl']}"
        )
    else:
        pass  # excluded entirely — this is the preferred behaviour

    # Legit nav_fraction: unrealized = qty * nav * (mark/avg - 1)
    assert "NVDA" in out, "legit nav_fraction NVDA must still be valued"
    assert out["NVDA"]["unrealized_pnl"] == pytest.approx(0.20 * 100_000.0 * (110.0 / 100.0 - 1.0))

    # True-unit equity: share formula, mult=1
    assert "BA" in out, "true_unit BA must still be valued"
    assert out["BA"]["unrealized_pnl"] == pytest.approx((220.0 - 200.0) * 50.0)


def test_true_unit_equity_det_equity_uses_share_formula_not_nav_fraction() -> None:
    """ar118 REGRESSION (weekly retro): a deterministic-equity EQUITY position is
    unit_kind='true_unit' (real signed SHARES, ADR-0086), NOT a NAV-fraction. The
    weekly headline must value it with the SHARE formula (mult=1, no ×100), NOT the
    NAV-fraction form the old `true_unit = asset_class=='us_option'` heuristic forced.

    50 shares of AAPL @ avg=$100, mark=$110, nav_ref=$100k:
      CORRECT (share):       unrealized = (110-100)*50            = +$500
      WRONG (old, NAV-frac): unrealized = 50*100_000*(110/100-1)  = +$500,000  (1000×)
    DETERMINISTIC_EQUITY=1 is LIVE, so this row class is real on the production book.

    The 5-tuple state.db read shape is (symbol, qty, avg, asset_class, unit_kind).
    """
    mod = _load_weekly_retro_module()

    nav = 100_000.0
    # us_option ×100 multiplier must NOT apply to a true-unit EQUITY row.
    positions = [("AAPL", 50.0, 100.0, "equity", "true_unit")]
    marks = {"AAPL": 110.0}

    # nav_ref present, but a true-unit row must NOT use it (share formula, not NAV-frac).
    out = mod.compute_unrealized_pnl(positions, marks, nav_ref=nav)
    assert "AAPL" in out, "a true-unit equity row must be valued, not dropped"
    reported = out["AAPL"]["unrealized_pnl"]

    assert reported == pytest.approx((110.0 - 100.0) * 50.0), (
        f"ar118: true-unit equity unrealized must be (mark-avg)*shares=+$500, not the "
        f"NAV-fraction +$500,000 nor the option ×100 form; got {reported}"
    )
    # Explicitly NOT the phantom NAV-fraction figure the bug produced.
    assert reported != pytest.approx(50.0 * nav * (110.0 / 100.0 - 1.0))
    # And NOT the ×100 option form either (mult must be 1 for equity).
    assert reported != pytest.approx((110.0 - 100.0) * 50.0 * 100.0)
