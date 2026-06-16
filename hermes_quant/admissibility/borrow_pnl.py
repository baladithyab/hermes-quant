"""hermes_quant.admissibility.borrow_pnl — borrow-aware carry for shorts (ADR-0077 D77.3).

Daily borrow fee on short notional + dividend-on-short payment-in-lieu (PIL), so short P&L is
no longer fictitiously free (Alpaca paper does NOT charge borrow fees: "Borrow Fees: Coming Soon").

Gated by HERMES_QUANT_BORROW_COST (default OFF). Pure functions; no I/O. /360 stock-loan basis;
Friday accrues x3 (weekend). UTC dates only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime

DAY_COUNT_BASIS: int = 360  # stock-loan money-market convention (research §3)


def _as_date(value: date) -> date:
    """Coerce a date-or-datetime to a pure UTC calendar date.

    `datetime` is a SUBCLASS of `date`, so a `datetime` key in `close_by_date` /
    `dividends` would (a) never match a pure-`date` key via `in`, and (b) raise
    `TypeError` when compared against pure `date` bounds. Normalizing both sides
    to `.date()` makes the held-window predicate type-robust so a datetime-keyed
    ex-div can NEVER silently drop a genuinely-owed PIL (understating cost is the
    wrong-direction error this guard exists to prevent)."""
    if isinstance(value, datetime):
        return value.date()
    return value


def borrow_cost_enabled() -> bool:
    return os.environ.get("HERMES_QUANT_BORROW_COST", "0") == "1"


def daily_borrow_fee(short_shares: float, close_price: float, annual_cbr: float, on: date) -> float:
    """abs(short_shares) * close_price * annual_cbr / 360, x3 on Friday (carries the weekend).

    short_shares is the SIGNED quantity (negative for shorts); longs / non-negative => 0.0.
    Returns a positive cost (a debit). `on` is the UTC calendar date the fee accrues for.
    """
    if short_shares >= 0 or close_price <= 0 or annual_cbr <= 0:
        return 0.0
    weekend_mult = 3 if on.weekday() == 4 else 1  # Friday=4 -> Fri+Sat+Sun
    return abs(short_shares) * close_price * annual_cbr / DAY_COUNT_BASIS * weekend_mult


def payment_in_lieu(short_shares: float, cash_dividend_per_share: float) -> float:
    """abs(short_shares) * dividend/share, debited on pay date if short across ex-div.
    Longs => 0.0. Returns a positive liability (a debit)."""
    if short_shares >= 0 or cash_dividend_per_share <= 0:
        return 0.0
    return abs(short_shares) * cash_dividend_per_share


@dataclass(frozen=True)
class BorrowAccrual:
    symbol: str
    total_borrow_fee: float
    total_pil: float
    days_held: int


def accrue_borrow_carry(
    symbol: str,
    short_shares: float,
    close_by_date: dict[date, float],  # UTC date -> close price (marks daily)
    annual_cbr: float,
    dividends: dict[date, float] | None = None,  # ex-div date -> cash dividend/share
) -> BorrowAccrual:
    """Sum daily_borrow_fee over each held UTC date in close_by_date, plus PIL on any ex-div
    date the short was actually open ACROSS (ADR-0077: PIL only for shorts held across ex-div).
    The total carry is a positive number to SUBTRACT from short P&L."""
    total_fee = 0.0
    days_held = 0
    held_dates: list[date] = []
    # ar90: each marked date's base fee already carries a weekend when it is a Friday
    # (daily_borrow_fee ×3). But a marked date is borrowed for EVERY calendar day until
    # the NEXT marked date — a market HOLIDAY (or any data gap) that extends the gap
    # beyond what the base multiplier covers is otherwise UNPAID (e.g. Fri + Mon-holiday
    # + Tue charges Fri×3 + Tue×1 and drops the Monday → short cost UNDERSTATED, the
    # wrong-money direction the PIL guard below also guards). We add the SHORTFALL for
    # the carried gap beyond the base, MEASURED from the held series (so it's correct for
    # weekend-extending AND mid-week holidays AND data gaps), while leaving every normal
    # weekend/Friday-close total byte-identical (gap == base ⇒ zero shortfall).
    _norm_marks = sorted((_as_date(on), close_price) for on, close_price in close_by_date.items())
    per_unit_day = (
        abs(short_shares) * annual_cbr / DAY_COUNT_BASIS
        if (short_shares < 0 and annual_cbr > 0)
        else 0.0
    )
    for idx, (on_date, close_price) in enumerate(_norm_marks):
        days_held += 1
        held_dates.append(on_date)
        total_fee += daily_borrow_fee(short_shares, close_price, annual_cbr, on_date)
        # Shortfall: calendar days this mark carries (to the next mark) beyond its base.
        if idx + 1 < len(_norm_marks) and close_price > 0:
            base_mult = 3 if on_date.weekday() == 4 else 1
            gap_days = (_norm_marks[idx + 1][0] - on_date).days
            shortfall_days = max(0, gap_days - base_mult)
            if shortfall_days:
                total_fee += per_unit_day * close_price * shortfall_days

    # The short was open across the CONTIGUOUS interval [min(held), max(held)] of the
    # daily marks. PIL is owed for any ex-div date the position spanned. Exact-key
    # membership (`ex_div_date in close_by_date`) was wrong-direction unsafe: an ex-div
    # falling on a date the daily-mark series simply did not record (a market-data gap,
    # or an ex-div on a non-marked calendar day) would silently drop a genuinely-owed
    # PIL and UNDERSTATE short cost. We instead test true held-across-ex-div bracketing
    # against the [min, max] window, with both sides normalized to pure `date` so a
    # datetime-keyed dividend cannot slip past on a type mismatch (FAIL-CLOSED toward
    # debiting the owed PIL). A dividend whose ex-div date falls OUTSIDE this window did
    # not touch the short and contributes ZERO PIL.
    total_pil = 0.0
    if dividends and held_dates:
        window_start = min(held_dates)
        window_end = max(held_dates)
        for ex_div_date, div_per_share in dividends.items():
            ex_date = _as_date(ex_div_date)
            if window_start <= ex_date <= window_end:
                total_pil += payment_in_lieu(short_shares, div_per_share)

    return BorrowAccrual(
        symbol=symbol,
        total_borrow_fee=total_fee,
        total_pil=total_pil,
        days_held=days_held,
    )
