"""hermes_quant.risk.per_position_stop — per-position unrealized-loss stop monitor.

WHY THIS EXISTS (the 2026-06-04 ASTS loss, diagnosed 2026-06-17):
The autonomous paper book lost -4.64% realized over 11 round-trips, and ONE trade
dominated it — ASTS bought $118.17, exited $93.44 three days later = -20.93%
position-level = -4.19% of NAV by itself. NONE of the existing rails caught it:
  * the ADR-0016 kill-switch is realized-only by design (autonomous.py:578 —
    "Unrealized (still-open) positions are NOT counted"), so an OPEN position
    bleeding -20% contributes 0.0 to the trip basis until it is closed;
  * the gate's drawdown / daily-loss breakers measure PORTFOLIO drawdown, and a
    single 0.2-NAV position at -20% is only -4% of NAV — under both the 5%
    daily-loss and 15% drawdown thresholds;
  * the existing ``require_stop_loss`` backstop (autonomous.py:1589) gates ENTRY
    SIZE at fire time only — it never watches an open position decline.

So a per-position UNREALIZED-loss monitor is a genuinely new control class: it runs
each tick against the marked-to-market open book and forces an exit when a single
position's loss from its entry cost basis breaches a threshold. Research
(Kaminski & Lo 2014; the 2% risk-per-trade rule; ATR/volatility stops) recommends a
fixed-pct stop for a thin-history systematic equity book: it caps the fat-tail
blowup (ASTS) without churning the small mean-reverting names (the 10 micro-bleed
round-trips were all -0.5% to -2% position-level — well inside an 8% stop).

POSTURE: this module is PURE (no I/O, no clock, no env). It is the testable decision
core; the autonomous tick wires real marks + the existing _react() exit to it behind
the default-OFF flag ``HERMES_QUANT_PER_POSITION_STOP``. Every numeric input is
finite-guarded — a NaN/inf mark or entry price yields NO stop (HOLD), never a
fabricated exit (silence-by-default money posture; a NaN must not fire a stop on a
winning position, nor suppress one on a losing position via a defeated comparison).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# The flag-ON default per-position stop threshold (operator chose 8% over the
# 10%-exact-2%-rule alternative, 2026-06-17): a position-level unrealized loss of
# 8% from entry cost basis forces an exit. At max_position_pct=0.20 that is 1.6%
# of NAV risk per position — inside the canonical 2% risk-per-trade cap, and the
# research-identified crossover between noise (whipsaw) and signal (real
# breakdown). The live value is read from operator YAML
# (quant.autonomous.per_position_stop_loss_pct) and finite-guarded to this default.
DEFAULT_STOP_LOSS_PCT = 0.08


@dataclass(frozen=True)
class StopDecision:
    """The per-position stop verdict for ONE open position.

    ``should_stop`` is True only when the position's unrealized loss from its entry
    cost basis breaches ``threshold_pct`` AND every input was finite. ``loss_pct`` is
    the signed unrealized loss as a positive fraction of the entry basis (0.20 = the
    position is down 20%); it is None when the decision could not be computed (a
    non-finite mark/entry, a zero entry, or a flat position) — in which case
    ``should_stop`` is always False (HOLD).
    """

    symbol: str
    should_stop: bool
    loss_pct: float | None
    reason: str


def position_unrealized_loss_pct(
    *, held_fraction: float, entry_price: float, mark_price: float
) -> float | None:
    """Return the unrealized LOSS as a positive fraction of entry basis, or None.

    Sign convention (the load-bearing correctness point — getting it wrong fires a
    stop on a WINNING short, a fail-open dangerous bug):
      * LONG  (held_fraction > 0): a loss is mark < entry. loss = (entry - mark)/entry
        = -(mark/entry - 1). A positive return (mark > entry) yields a NEGATIVE loss.
      * SHORT (held_fraction < 0): a loss is mark > entry. loss = (mark - entry)/entry
        =  (mark/entry - 1).

    Returns the loss as a SIGNED fraction (positive = losing, negative = winning).
    Returns None when any input is non-finite, the entry price is <= 0 (cannot form a
    return), or the position is flat (held_fraction == 0) — the caller treats None as
    "no usable signal -> HOLD".
    """
    if not (
        math.isfinite(held_fraction)
        and math.isfinite(entry_price)
        and math.isfinite(mark_price)
    ):
        return None
    if entry_price <= 0.0:
        return None
    if held_fraction == 0.0:
        return None
    ret = mark_price / entry_price - 1.0  # signed price return from entry
    # For a long, a loss is a NEGATIVE return; for a short, a loss is a POSITIVE
    # return. Multiply by the position sign so a positive result always means
    # "losing money on this position".
    sign = 1.0 if held_fraction > 0.0 else -1.0
    return -ret * sign


def evaluate_stop(
    *,
    symbol: str,
    held_fraction: float,
    entry_price: float,
    mark_price: float,
    threshold_pct: float = DEFAULT_STOP_LOSS_PCT,
) -> StopDecision:
    """Decide whether ONE open position should be force-exited on a stop breach.

    Fires (``should_stop=True``) only when the position's unrealized loss is finite
    AND ``>= abs(threshold_pct)``. A non-finite threshold is rejected to the module
    default (a NaN/inf/<=0 threshold must NOT silently disable the stop — the
    ar08/ar09/ar12 finite-guard family). Any non-computable loss (None) is a HOLD.
    """
    if not math.isfinite(threshold_pct) or threshold_pct <= 0.0:
        # ar08-family: a defeated threshold disarms the rail silently. Fall back to
        # the safe default rather than trusting a garbage operator value.
        threshold_pct = DEFAULT_STOP_LOSS_PCT
    thr = abs(threshold_pct)

    loss = position_unrealized_loss_pct(
        held_fraction=held_fraction, entry_price=entry_price, mark_price=mark_price
    )
    if loss is None:
        return StopDecision(
            symbol=symbol,
            should_stop=False,
            loss_pct=None,
            reason="non_computable_loss (non-finite mark/entry, zero entry, or flat position) -> HOLD",
        )
    if loss >= thr:
        return StopDecision(
            symbol=symbol,
            should_stop=True,
            loss_pct=loss,
            reason=(
                f"unrealized loss {loss * 100:.2f}% >= stop {thr * 100:.2f}% "
                f"(held_fraction={held_fraction:+.4f})"
            ),
        )
    return StopDecision(
        symbol=symbol,
        should_stop=False,
        loss_pct=loss,
        reason=f"unrealized loss {loss * 100:.2f}% within stop {thr * 100:.2f}%",
    )


def weighted_avg_entry_from_lots(lots: list[dict]) -> float | None:
    """FIFO-consistent weighted-average entry price from settlement open-lots.

    Reuses the open-lot structure returned by
    ``hermes_quant.daemon.settlement_loop.join_exit_fills`` (the SAME canonical FIFO
    matcher the kill-switch basis and settlement use — NOT a script reimplementation),
    each lot carrying ``qty`` (NAV-fraction magnitude) and ``price`` (entry fill).
    Returns the qty-weighted average entry, or None if the lots are empty or carry no
    finite positive qty (caller treats None as "no cost basis -> HOLD").
    """
    num = 0.0
    den = 0.0
    for lot in lots:
        q = lot.get("qty")
        p = lot.get("price")
        if not (isinstance(q, (int, float)) and isinstance(p, (int, float))):
            continue
        if not (math.isfinite(q) and math.isfinite(p)) or q <= 0.0 or p <= 0.0:
            continue
        num += q * p
        den += q
    if den <= 0.0:
        return None
    return num / den
