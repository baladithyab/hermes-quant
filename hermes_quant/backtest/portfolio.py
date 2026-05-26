"""hermes_quant.backtest.portfolio — Minimal mark-to-market accounting.

Per ADR-0020 §D3. Single-symbol single-book paper portfolio for
backtesting. v0.4+ will unify with `portfolio_loader` (ADR-0011)
once the calibrator-from-fills feedback loop needs both.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PaperPortfolio:
    """Single-symbol mark-to-market book for backtesting.

    Attributes:
        cash: liquid cash balance
        position_qty: signed quantity; positive = long, negative = short
        avg_entry_price: weighted average entry price of current position
            (0.0 when flat)
        realized_pnl: cumulative P&L from closed/reduced positions
        fees_paid: cumulative commission paid
        n_trades: count of trades executed (for reporting)
    """

    cash: float
    position_qty: float = 0.0
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    n_trades: int = 0

    @classmethod
    def fresh(cls, initial_equity: float) -> "PaperPortfolio":
        return cls(cash=initial_equity)

    # ------------------------------------------------------------------
    # Mark-to-market views
    # ------------------------------------------------------------------

    def equity(self, mark_price: float) -> float:
        """Total NAV = cash + position MTM. fees_paid already deducted from cash."""
        return self.cash + self.position_qty * mark_price

    def unrealized_pnl(self, mark_price: float) -> float:
        if self.position_qty == 0:
            return 0.0
        return self.position_qty * (mark_price - self.avg_entry_price)

    # ------------------------------------------------------------------
    # The actuator — apply a target position
    # ------------------------------------------------------------------

    def apply_target(
        self,
        target_position_pct: float,
        bar_close: float,
        *,
        commission: float = 0.001,
        slippage: float = 0.0005,
    ) -> dict:
        """Move portfolio toward target_position_pct of NAV.

        target_position_pct is signed:
          +0.10 -> 10% NAV LONG
          -0.05 -> 5% NAV SHORT
           0.0 -> flat (close any position)

        Returns a dict with the trade details (delta_qty, fill_price,
        commission_paid, realized_pnl_delta) for the audit trail.
        """
        nav = self.equity(bar_close)
        target_dollar = target_position_pct * nav
        target_qty = target_dollar / bar_close if bar_close != 0 else 0.0

        delta_qty = target_qty - self.position_qty
        if abs(delta_qty) < 1e-9:
            return {
                "delta_qty": 0.0,
                "fill_price": bar_close,
                "commission_paid": 0.0,
                "realized_pnl_delta": 0.0,
                "skipped": True,
                "reason": "no_change",
            }

        # Apply slippage in the trade's adverse direction
        if delta_qty > 0:  # buying
            fill_price = bar_close * (1 + slippage)
        else:  # selling
            fill_price = bar_close * (1 - slippage)

        # Compute realized P&L if we're reducing or flipping a position
        realized_pnl_delta = 0.0
        if self.position_qty != 0 and (
            (self.position_qty > 0 and delta_qty < 0) or (self.position_qty < 0 and delta_qty > 0)
        ):
            # Reducing or flipping
            close_qty = min(abs(delta_qty), abs(self.position_qty))
            sign = 1 if self.position_qty > 0 else -1
            realized_pnl_delta = sign * close_qty * (fill_price - self.avg_entry_price)
            self.realized_pnl += realized_pnl_delta

            if abs(delta_qty) > abs(self.position_qty):
                # Flip — new entry on the OTHER side
                new_qty = delta_qty + self.position_qty  # signed
                # avg_entry_price for the new side
                self.avg_entry_price = fill_price
            elif abs(delta_qty) == abs(self.position_qty):
                # Full close
                self.avg_entry_price = 0.0
            else:
                # Partial reduce — avg_entry_price unchanged for remaining position
                pass
        else:
            # Adding to existing position OR opening fresh
            new_total_qty = self.position_qty + delta_qty
            if self.position_qty == 0:
                self.avg_entry_price = fill_price
            else:
                # Weighted average
                self.avg_entry_price = (
                    self.avg_entry_price * self.position_qty + fill_price * delta_qty
                ) / new_total_qty

        # Cash flow: positive delta_qty (buy) reduces cash; negative (sell) increases
        cash_flow = -delta_qty * fill_price
        commission_paid = abs(delta_qty) * fill_price * commission
        self.cash += cash_flow - commission_paid
        self.fees_paid += commission_paid
        self.position_qty += delta_qty
        self.n_trades += 1

        # Tidy: if position is essentially zero, reset avg_entry_price
        if abs(self.position_qty) < 1e-9:
            self.position_qty = 0.0
            self.avg_entry_price = 0.0

        return {
            "delta_qty": delta_qty,
            "fill_price": fill_price,
            "commission_paid": commission_paid,
            "realized_pnl_delta": realized_pnl_delta,
            "new_position_qty": self.position_qty,
            "new_cash": self.cash,
            "skipped": False,
        }
