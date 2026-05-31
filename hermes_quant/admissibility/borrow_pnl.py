"""hermes_quant.admissibility.borrow_pnl — borrow-aware carry for shorts (ADR-0077 D77.3).

Daily borrow fee on short notional + dividend-on-short payment-in-lieu (PIL), so short P&L is
no longer fictitiously free (Alpaca paper does NOT charge borrow fees: "Borrow Fees: Coming Soon").

Gated by HERMES_QUANT_BORROW_COST (default OFF). Pure functions; no I/O. /360 stock-loan basis;
Friday accrues x3 (weekend). UTC dates only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date

DAY_COUNT_BASIS: int = 360  # stock-loan money-market convention (research §3)


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
    for on, close_price in close_by_date.items():
        days_held += 1
        total_fee += daily_borrow_fee(short_shares, close_price, annual_cbr, on)

    # held_dates are the UTC dates the short position was open (the marks we iterate above).
    # A dividend whose ex-div date falls OUTSIDE this window did not touch the short and
    # contributes ZERO PIL — never debit for a dividend the position did not straddle.
    held_dates = close_by_date.keys()
    total_pil = 0.0
    if dividends:
        for ex_div_date, div_per_share in dividends.items():
            if ex_div_date not in held_dates:
                continue
            total_pil += payment_in_lieu(short_shares, div_per_share)

    return BorrowAccrual(
        symbol=symbol,
        total_borrow_fee=total_fee,
        total_pil=total_pil,
        days_held=days_held,
    )
