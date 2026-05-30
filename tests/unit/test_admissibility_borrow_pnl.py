"""Unit tests for hermes_quant.admissibility.borrow_pnl (ADR-0077 D77.3).

/360 basis, Friday x3, longs accrue zero, PIL on ex-div. Deterministic, no I/O.
"""
from __future__ import annotations

from datetime import date

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
