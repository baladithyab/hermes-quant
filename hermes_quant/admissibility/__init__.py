"""hermes_quant.admissibility — pre-trade admissibility + ShortabilityOracle (ADR-0077).

A hard, deterministic, fail-closed precondition upstream of the ADR-0004 risk gate. It can ONLY
REJECT a proposed order (or flatten an inadmissible held short -> 0.0); it never amplifies.

Two independent default-OFF flags:
  HERMES_QUANT_ADMISSIBILITY=1 -> activates the pre-trade gate (else NullShortabilityOracle).
  HERMES_QUANT_BORROW_COST=1   -> activates the daily borrow-carry accrual.

With both unset, behavior is bit-for-bit identical to today.
"""

from __future__ import annotations

from .borrow_pnl import (
    DAY_COUNT_BASIS,
    BorrowAccrual,
    accrue_borrow_carry,
    borrow_cost_enabled,
    daily_borrow_fee,
    payment_in_lieu,
)
from .oracle import (
    ALPACA_SHORT_ASK_MULT,
    ETB_DEFAULT_ANNUAL_CBR,
    MIN_SHORT_ACCOUNT_EQUITY_USD,
    REASON_EQUITY_BELOW_2K,
    REASON_FRACTIONAL_SHORT,
    REASON_INSUFFICIENT_BPR,
    REASON_NOT_ETB,
    REASON_NOT_MARGINABLE,
    REASON_NOT_SHORTABLE,
    REASON_PTP_BLOCKED,
    REASON_SSR_MARKETABLE_SHORT,
    REASON_UNKNOWN_SHORTABILITY,
    REG_T_SHORT_INITIAL_BPR_MULT,
    AdmissibilityContext,
    AdmissibilityState,
    AlpacaShortabilityOracle,
    ETBSnapshotEntry,
    NullShortabilityOracle,
    ShortabilityOracle,
    ShortabilityVerdict,
    StaticETBAllowlistOracle,
    evaluate_admissibility,
    select_oracle,
)
from .order_state import (
    AdmissibilityAdjustment,
    Side,
    apply_verdict_to_target,
    side_of,
    target_pct_to_shares,
)

__all__ = [
    # oracle: enums / dataclasses / protocol
    "AdmissibilityContext",
    "AdmissibilityState",
    "ShortabilityOracle",
    "ShortabilityVerdict",
    "ETBSnapshotEntry",
    # oracle: implementations + factory + core
    "NullShortabilityOracle",
    "AlpacaShortabilityOracle",
    "StaticETBAllowlistOracle",
    "evaluate_admissibility",
    "select_oracle",
    # oracle: constants
    "ETB_DEFAULT_ANNUAL_CBR",
    "MIN_SHORT_ACCOUNT_EQUITY_USD",
    "REG_T_SHORT_INITIAL_BPR_MULT",
    "ALPACA_SHORT_ASK_MULT",
    # oracle: reasons
    "REASON_NOT_SHORTABLE",
    "REASON_NOT_ETB",
    "REASON_FRACTIONAL_SHORT",
    "REASON_NOT_MARGINABLE",
    "REASON_INSUFFICIENT_BPR",
    "REASON_EQUITY_BELOW_2K",
    "REASON_PTP_BLOCKED",
    "REASON_SSR_MARKETABLE_SHORT",
    "REASON_UNKNOWN_SHORTABILITY",
    # order_state
    "AdmissibilityAdjustment",
    "Side",
    "apply_verdict_to_target",
    "side_of",
    "target_pct_to_shares",
    # borrow_pnl
    "BorrowAccrual",
    "DAY_COUNT_BASIS",
    "accrue_borrow_carry",
    "borrow_cost_enabled",
    "daily_borrow_fee",
    "payment_in_lieu",
]
