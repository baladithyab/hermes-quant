"""hermes_quant.state.positions — Position and CashState typed views.

ADR-0039 wave 1c: these dataclasses are the Python-facing read-views over
the positions and cash tables in state.db.

Design notes
------------
- Position.quantity is SIGNED: positive = long, negative = short.
- Position.avg_entry_price uses weighted-average cost basis (v0.1 choice,
  documented in ADR-0039 §D7: FIFO is cleaner for tax lots but more
  complex; deferred to v0.2).
- CashState.equity_total is denormalized for fast reads; rebuilt from
  cash_balance + mark values each time reconstruct_from runs.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Position:
    """A single holding in the portfolio.

    Attributes
    ----------
    account_id:
        Broker/paper account, e.g. "paper-default".
    asset_class:
        One of the AssetClass literals: "equity", "crypto", etc.
    symbol:
        Ticker, e.g. "AAPL" or "BTC/USDT".
    quantity:
        Signed shares/units: positive = long, negative = short.
    avg_entry_price:
        Weighted-average cost basis per unit (v0.1: weighted-average,
        not FIFO — see ADR-0039 §D7).
    last_update_at:
        ISO 8601 UTC of the last fill that touched this position.
    """

    account_id: str
    asset_class: str
    symbol: str
    quantity: float
    avg_entry_price: float
    last_update_at: str

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def is_short(self) -> bool:
        return self.quantity < 0

    @property
    def is_flat(self) -> bool:
        return abs(self.quantity) < 1e-12

    @property
    def notional(self) -> float:
        """Notional value at avg_entry_price (unsigned)."""
        return abs(self.quantity) * self.avg_entry_price


@dataclass
class CashState:
    """Cash balance for a single account.

    Attributes
    ----------
    account_id:
        Broker/paper account identifier.
    balance_usd:
        Free cash in USD after debiting/crediting fills.
    last_update_at:
        ISO 8601 UTC of the last fill that changed cash.
    equity_total:
        Denormalized: balance_usd + sum of open position notionals
        (at avg_entry_price, not marked — mark-to-market lives in the
        Portfolio layer, not here).
    """

    account_id: str
    balance_usd: float
    last_update_at: str
    equity_total: float
