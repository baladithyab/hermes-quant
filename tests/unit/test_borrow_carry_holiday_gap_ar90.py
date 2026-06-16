"""ar90 — borrow carry must charge the FULL calendar gap a marked date carries
(weekend AND holidays), not only the hardcoded Friday×3 weekend.

`daily_borrow_fee` charges `weekend_mult = 3 if on.weekday()==4 else 1`, which covers
a normal 2-day weekend after a Friday but NOTHING for a market HOLIDAY that extends a
gap. A short held across Fri + Mon-holiday + Tue accrues Fri×3 (Fri+Sat+Sun) + Tue×1,
DROPPING the Monday holiday — the short is borrowed over the holiday but pays zero for
it: a borrow-COST UNDERSTATEMENT (shorts look cheaper than they are), the wrong-money
direction the module's own PIL guard (lines 86-93) exists to prevent. Mid-week
holidays (July 4th, Thanksgiving) and any market-data gap have the same flaw.

FIX (ar90): preserve the conservative Friday×3 base (over-charging a Friday-close
weekend is fail-safe for a COST), and in `accrue_borrow_carry` add the SHORTFALL for
any gap to the next marked date beyond what the earlier date's base multiplier already
covered: `shortfall_days = max(0, (next_mark - this_mark).days - base_mult)`, accrued
at this_mark's close. This MEASURES the borrowed days from the held series for the gap
the base didn't cover, leaving every existing weekend/normal-week total byte-identical
and adding only the missing holiday days.
"""

from __future__ import annotations

from datetime import date

from hermes_quant.admissibility.borrow_pnl import accrue_borrow_carry


def _per_day(short_shares: float, close: float, cbr: float) -> float:
    return abs(short_shares) * close * cbr / 360.0


def test_ar90_holiday_extends_weekend_carry():
    """Fri + Mon-holiday(unmarked) + Tue: Fri base covers 3 (Fri+Sat+Sun); the Fri→Tue
    gap is 4, so +1 shortfall (the Monday holiday). Tue (last) = 1. Total 5 day-fees."""
    cbr = 0.36
    close_by_date = {
        date(2026, 5, 22): 100.0,  # Friday before US Memorial Day (Mon 2026-05-25)
        date(2026, 5, 26): 100.0,  # Tuesday after (Mon 25th = holiday, unmarked)
    }
    acc = accrue_borrow_carry("XYZ", -100.0, close_by_date, annual_cbr=cbr)
    per_day = _per_day(-100.0, 100.0, cbr)
    assert acc.total_borrow_fee == per_day * 5, (
        f"holiday borrow understated: got {acc.total_borrow_fee/per_day} day-fees, "
        "expected 5 (Fri base 3 + Mon-holiday shortfall 1 + Tue 1)"
    )


def test_ar90_midweek_long_weekend_carry():
    """Thu + (Fri-holiday) + Mon: Thu base covers 1; the Thu→Mon gap is 4, so +3
    shortfall (Fri-holiday + Sat + Sun). Mon (last) = 1. Total 5 day-fees."""
    cbr = 0.36
    close_by_date = {
        date(2026, 7, 2): 100.0,   # Thursday
        date(2026, 7, 6): 100.0,   # Monday (Fri 2026-07-03 observed July-4th holiday)
    }
    acc = accrue_borrow_carry("XYZ", -100.0, close_by_date, annual_cbr=cbr)
    per_day = _per_day(-100.0, 100.0, cbr)
    assert acc.total_borrow_fee == per_day * 5, (
        f"long-weekend borrow understated: got {acc.total_borrow_fee/per_day} day-fees, expected 5"
    )


def test_ar90_midweek_single_holiday_carry():
    """Tue + (Wed-holiday) + Thu: Tue base 1; Tue→Thu gap 2 -> +1 shortfall (Wed).
    Thu (last) = 1. Total 3 day-fees (vs the buggy 2)."""
    cbr = 0.36
    close_by_date = {
        date(2026, 6, 2): 100.0,  # Tue
        date(2026, 6, 4): 100.0,  # Thu (Wed 2026-06-03 a hypothetical mid-week close)
    }
    acc = accrue_borrow_carry("XYZ", -100.0, close_by_date, annual_cbr=cbr)
    per_day = _per_day(-100.0, 100.0, cbr)
    assert acc.total_borrow_fee == per_day * 3, (
        f"mid-week holiday understated: got {acc.total_borrow_fee/per_day} day-fees, expected 3"
    )


def test_ar90_normal_mon_fri_byte_identical():
    """Non-vacuity / byte-identity: a normal Mon..Fri week (ending Friday) is UNCHANGED
    — Mon..Thu carry 1 each (gap 1, base 1, no shortfall), Fri (last) carries its ×3
    base. Total 7. The conservative Friday×3 contract is preserved exactly."""
    cbr = 0.36
    close_by_date = {
        date(2026, 6, 1): 100.0,  # Mon
        date(2026, 6, 2): 100.0,  # Tue
        date(2026, 6, 3): 100.0,  # Wed
        date(2026, 6, 4): 100.0,  # Thu
        date(2026, 6, 5): 100.0,  # Fri (last mark -> ×3 base, the documented weekend carry)
    }
    acc = accrue_borrow_carry("XYZ", -100.0, close_by_date, annual_cbr=cbr)
    per_day = _per_day(-100.0, 100.0, cbr)
    assert acc.total_borrow_fee == per_day * 7, (
        f"normal Mon..Fri must stay 7 day-fees (1+1+1+1+3), got {acc.total_borrow_fee/per_day}"
    )


def test_ar90_friday_into_next_monday_byte_identical():
    """Fri→Mon (normal weekend, both marked): Fri base 3 covers exactly the Fri→Mon gap
    of 3, no shortfall; Mon (last) = 1. Total 4 — UNCHANGED from the pre-fix behavior."""
    cbr = 0.36
    close_by_date = {
        date(2026, 6, 5): 100.0,  # Fri
        date(2026, 6, 8): 100.0,  # Mon
    }
    acc = accrue_borrow_carry("XYZ", -100.0, close_by_date, annual_cbr=cbr)
    per_day = _per_day(-100.0, 100.0, cbr)
    assert acc.total_borrow_fee == per_day * 4, (
        f"Fri→Mon must stay 4 day-fees (Fri base 3 + Mon 1, no shortfall), "
        f"got {acc.total_borrow_fee/per_day}"
    )
