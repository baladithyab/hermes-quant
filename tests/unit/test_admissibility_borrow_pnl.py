"""Unit tests for hermes_quant.admissibility.borrow_pnl (ADR-0077 D77.3).

/360 basis, Friday x3, longs accrue zero, PIL on ex-div. Deterministic, no I/O.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from hermes_quant.admissibility import (
    accrue_borrow_carry,
    borrow_cost_enabled,
    daily_borrow_fee,
    payment_in_lieu,
)

# 2026-05-27 is a Wednesday; 2026-05-29 is a Friday.
WEDNESDAY = date(2026, 5, 27)
FRIDAY = date(2026, 5, 29)


def test_long_accrues_zero_borrow():
    assert daily_borrow_fee(+100, 100.0, 0.02, WEDNESDAY) == 0.0
    # zero shares also accrues nothing
    assert daily_borrow_fee(0, 100.0, 0.02, WEDNESDAY) == 0.0


def test_daily_borrow_360_basis():
    fee = daily_borrow_fee(-1000, 100.0, 0.02, WEDNESDAY)
    # 1000 * 100 * 0.02 / 360 = 5.5555...
    assert fee == pytest.approx(1000 * 100 * 0.02 / 360)
    assert fee == pytest.approx(5.5555555, abs=1e-4)


def test_friday_accrues_triple():
    wed = daily_borrow_fee(-1000, 100.0, 0.02, WEDNESDAY)
    fri = daily_borrow_fee(-1000, 100.0, 0.02, FRIDAY)
    assert fri == pytest.approx(3 * wed)


def test_zero_cbr_accrues_nothing():
    assert daily_borrow_fee(-1000, 100.0, 0.0, WEDNESDAY) == 0.0
    assert daily_borrow_fee(-1000, 0.0, 0.02, WEDNESDAY) == 0.0


def test_pil_debited_for_short():
    assert payment_in_lieu(-100, 0.50) == pytest.approx(50.0)
    # longs owe nothing
    assert payment_in_lieu(+100, 0.50) == 0.0
    assert payment_in_lieu(-100, 0.0) == 0.0


def test_accrue_sums_over_held_days_and_divs():
    # 5 weekdays Mon..Fri (Fri x3), one ex-div date on the Wednesday.
    close_by_date = {
        date(2026, 5, 25): 100.0,  # Mon
        date(2026, 5, 26): 100.0,  # Tue
        date(2026, 5, 27): 100.0,  # Wed
        date(2026, 5, 28): 100.0,  # Thu
        date(2026, 5, 29): 100.0,  # Fri -> x3
    }
    dividends = {date(2026, 5, 27): 0.25}
    accrual = accrue_borrow_carry("AAPL", -1000, close_by_date, 0.02, dividends)

    per_day = 1000 * 100 * 0.02 / 360
    expected_fee = per_day * (1 + 1 + 1 + 1 + 3)  # Friday triple
    assert accrual.total_borrow_fee == pytest.approx(expected_fee)
    assert accrual.total_pil == pytest.approx(1000 * 0.25)
    assert accrual.days_held == 5
    assert accrual.symbol == "AAPL"


def test_pil_only_when_ex_div_in_held_window():
    """ADR-0077: PIL only accrues for shorts open ACROSS the ex-div date.

    Short held Mon..Wed. An ex-div on Friday (outside the held window) => ZERO PIL;
    an ex-div on Tuesday (inside the held window) => PIL debited.
    """
    held_mon_wed = {
        date(2026, 5, 25): 100.0,  # Mon
        date(2026, 5, 26): 100.0,  # Tue
        date(2026, 5, 27): 100.0,  # Wed
    }
    # Friday ex-div is outside the Mon..Wed held window -> no PIL.
    div_friday = {date(2026, 5, 29): 0.30}  # Fri
    accrual_fri = accrue_borrow_carry("AAPL", -1000, held_mon_wed, 0.02, div_friday)
    assert accrual_fri.total_pil == 0.0
    assert accrual_fri.days_held == 3

    # Tuesday ex-div is inside the held window -> PIL debited.
    div_tuesday = {date(2026, 5, 26): 0.30}  # Tue
    accrual_tue = accrue_borrow_carry("AAPL", -1000, held_mon_wed, 0.02, div_tuesday)
    assert accrual_tue.total_pil == pytest.approx(1000 * 0.30)
    assert accrual_tue.days_held == 3

    # Borrow fee is independent of the dividend window: identical for both runs.
    assert accrual_fri.total_borrow_fee == pytest.approx(accrual_tue.total_borrow_fee)


def test_pil_mixed_divs_only_held_ones_count():
    """A dividend dict spanning both in- and out-of-window ex-div dates accrues PIL
    ONLY for the ex-div date inside the held window."""
    held_mon_wed = {
        date(2026, 5, 25): 100.0,  # Mon
        date(2026, 5, 26): 100.0,  # Tue
        date(2026, 5, 27): 100.0,  # Wed
    }
    dividends = {
        date(2026, 5, 26): 0.30,  # Tue -> held -> counts
        date(2026, 5, 29): 0.50,  # Fri -> not held -> ignored
    }
    accrual = accrue_borrow_carry("AAPL", -1000, held_mon_wed, 0.02, dividends)
    assert accrual.total_pil == pytest.approx(1000 * 0.30)


def test_pil_datetime_keyed_ex_div_in_window_still_debits():
    """A datetime-keyed (not pure-date) ex-div inside the held window MUST still
    debit PIL. `datetime` is a subclass of `date`; exact-key membership against a
    pure-`date` mark series would never match a datetime key, silently dropping a
    genuinely-owed PIL (understating short cost = wrong direction). The held-window
    predicate normalizes both sides to `date`, so the debit is preserved."""
    held_mon_wed = {
        date(2026, 5, 25): 100.0,  # Mon
        date(2026, 5, 26): 100.0,  # Tue
        date(2026, 5, 27): 100.0,  # Wed
    }
    # Ex-div keyed as a datetime (with a wall-clock time) on the Tuesday in-window.
    div_dt = {datetime(2026, 5, 26, 13, 30, 0): 0.30}
    accrual = accrue_borrow_carry("AAPL", -1000, held_mon_wed, 0.02, div_dt)
    assert accrual.total_pil == pytest.approx(1000 * 0.30)
    assert accrual.days_held == 3


def test_pil_ex_div_on_unmarked_day_bracketed_by_held_marks_still_debits():
    """An ex-div on a trading day MISSING from close_by_date but BRACKETED by the
    held marks (a daily-mark gap) MUST still debit PIL. Pre-fix exact-key membership
    dropped it (the date isn't a key), understating cost. The min/max held-window
    predicate debits it because the position was demonstrably open across that date."""
    # Marks recorded Mon and Wed only; Tuesday's mark is missing (data gap), yet
    # the short was clearly open across Tuesday (it is between Mon and Wed marks).
    held_with_gap = {
        date(2026, 5, 25): 100.0,  # Mon
        date(2026, 5, 27): 100.0,  # Wed  (Tuesday mark absent)
    }
    div_tuesday = {date(2026, 5, 26): 0.30}  # ex-div on the unmarked Tuesday
    accrual = accrue_borrow_carry("AAPL", -1000, held_with_gap, 0.02, div_tuesday)
    assert accrual.total_pil == pytest.approx(1000 * 0.30)
    assert accrual.days_held == 2  # only the two recorded marks accrue borrow fee

    # And an ex-div strictly OUTSIDE the [Mon, Wed] window still contributes zero.
    div_after = {date(2026, 5, 29): 0.30}  # Friday, after max(held)
    accrual_after = accrue_borrow_carry("AAPL", -1000, held_with_gap, 0.02, div_after)
    assert accrual_after.total_pil == 0.0


def test_accrue_long_position_zero_carry():
    close_by_date = {date(2026, 5, 27): 100.0}
    accrual = accrue_borrow_carry("AAPL", +1000, close_by_date, 0.02)
    assert accrual.total_borrow_fee == 0.0
    assert accrual.total_pil == 0.0
    assert accrual.days_held == 1


def test_borrow_cost_flag_default_off(monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_BORROW_COST", raising=False)
    assert borrow_cost_enabled() is False
    monkeypatch.setenv("HERMES_QUANT_BORROW_COST", "0")
    assert borrow_cost_enabled() is False
    monkeypatch.setenv("HERMES_QUANT_BORROW_COST", "1")
    assert borrow_cost_enabled() is True
