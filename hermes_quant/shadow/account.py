"""hermes_quant.shadow.account — ShadowAccount: isolated SQLite-backed shadow portfolio.

Wave 8b / ADR-0049.

Each ShadowAccount mirrors the real portfolio structure but is driven by a
single ShadowRule instead of the production approval pipeline.  State is
persisted to an isolated SQLite database at:

    ~/.hermes/quant/shadow/<rule_name>.db

Design constraints (see ADR-0049):
- READ-ONLY relationship to production audit_log.jsonl.
- Isolated DB per rule — no shared state across rules.
- Cost model: conservative 10 bps default drag so no shadow account gets a
  free pass.  A rule must genuinely outperform to beat the real portfolio.
- pnl_history tracks end-of-day mark-to-market snapshots.
"""
from __future__ import annotations

import logging
import math
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from hermes_quant.shadow.rules import ShadowDecision, ShadowRule

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

_SHADOW_HOME = Path.home() / ".hermes" / "quant" / "shadow"

# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_positions (
    ticker          TEXT NOT NULL,
    quantity        REAL NOT NULL,
    avg_entry_price REAL NOT NULL,
    last_update_at  TEXT NOT NULL,
    PRIMARY KEY (ticker)
);

CREATE TABLE IF NOT EXISTS shadow_cash (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    balance         REAL NOT NULL,
    last_update_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_pnl_history (
    asof            TEXT NOT NULL PRIMARY KEY,
    equity_total    REAL NOT NULL,
    cash            REAL NOT NULL,
    positions_value REAL NOT NULL,
    pnl_today       REAL NOT NULL,
    pnl_total       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_fills (
    event_id        TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    action          TEXT NOT NULL,
    size_fraction   REAL NOT NULL,
    fill_price      REAL NOT NULL,
    cost_bps        REAL NOT NULL,
    asof            TEXT NOT NULL,
    PRIMARY KEY (event_id)
);
"""


# ---------------------------------------------------------------------------
# ShadowAccount
# ---------------------------------------------------------------------------


class ShadowAccount:
    """Isolated shadow portfolio driven by a single ShadowRule.

    Parameters
    ----------
    rule:
        The ShadowRule that decides when and how to trade.
    initial_cash:
        Starting cash balance (default 100 000 USD).
    cost_model_bps:
        One-way transaction cost in basis points applied to every simulated
        fill (default 10 bps = 0.10%).
    db_path:
        Path to the SQLite database.  Defaults to
        ``~/.hermes/quant/shadow/<rule.name>.db``.
    """

    def __init__(
        self,
        rule: ShadowRule,
        *,
        initial_cash: float = 100_000.0,
        cost_model_bps: float = 10.0,
        db_path: Optional[Path] = None,
    ) -> None:
        self.rule = rule
        self.initial_cash = initial_cash
        self.cost_model_bps = cost_model_bps
        self.db_path: Path = db_path or (_SHADOW_HOME / f"{rule.name}.db")
        self._lock = threading.RLock()

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._bootstrap_cash_if_missing()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    def _bootstrap_cash_if_missing(self) -> None:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT balance FROM shadow_cash WHERE id = 1").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO shadow_cash (id, balance, last_update_at) VALUES (1, ?, ?)",
                    (self.initial_cash, _utc_now_iso()),
                )

    # ------------------------------------------------------------------
    # Properties (live reads from DB)
    # ------------------------------------------------------------------

    @property
    def cash(self) -> float:
        with self._conn() as conn:
            row = conn.execute("SELECT balance FROM shadow_cash WHERE id = 1").fetchone()
            return float(row["balance"]) if row else self.initial_cash

    @property
    def positions(self) -> dict[str, dict[str, float]]:
        """Returns {ticker: {quantity, avg_entry_price}}."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ticker, quantity, avg_entry_price FROM shadow_positions"
            ).fetchall()
        return {
            r["ticker"]: {
                "quantity": float(r["quantity"]),
                "avg_entry_price": float(r["avg_entry_price"]),
            }
            for r in rows
            if abs(float(r["quantity"])) > 1e-12
        }

    @property
    def pnl_history(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT asof, equity_total, cash, positions_value, pnl_today, pnl_total "
                "FROM shadow_pnl_history ORDER BY asof"
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # apply_signal — consume one audit event
    # ------------------------------------------------------------------

    def apply_signal(
        self,
        audit_event: dict,
        prices: dict[str, float],
    ) -> Optional[ShadowDecision]:
        """Apply the rule to one audit event, simulate a fill if the rule fires.

        Parameters
        ----------
        audit_event:
            A dict representation of a governance audit event.
        prices:
            Current-day prices keyed by ticker (e.g. {"AAPL": 195.50}).
            Used both to determine decision_price and for mark-to-market.

        Returns
        -------
        ShadowDecision if the rule fired and a fill was simulated, else None.
        """
        decision = self.rule.evaluate(audit_event)
        if decision is None:
            return None

        ticker = decision.ticker
        decision_price = prices.get(ticker)
        if decision_price is None or decision_price <= 0:
            logger.debug(
                "ShadowAccount[%s]: no price for %s; skipping fill",
                self.rule.name,
                ticker,
            )
            return None

        event_id = audit_event.get("event_id", _utc_now_iso())

        # Idempotency: skip if this event_id already processed
        with self._lock, self._conn() as conn:
            existing = conn.execute(
                "SELECT event_id FROM shadow_fills WHERE event_id = ?", (event_id,)
            ).fetchone()
            if existing is not None:
                return decision  # already applied

        sign = 1 if decision.action == "buy" else -1

        # Cost model: slippage + explicit cost_model_bps
        # fill_price = market_price * (1 + sign * 0.0005) * (1 + sign * cost_bps / 10000)
        slippage_fraction = sign * 0.0005  # 5 bps directional slippage
        cost_fraction = sign * (self.cost_model_bps / 10_000.0)
        fill_price = decision_price * (1.0 + slippage_fraction + cost_fraction)

        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cash_row = conn.execute(
                    "SELECT balance FROM shadow_cash WHERE id = 1"
                ).fetchone()
                current_cash = float(cash_row["balance"]) if cash_row else self.initial_cash

                pos_row = conn.execute(
                    "SELECT quantity, avg_entry_price FROM shadow_positions WHERE ticker = ?",
                    (ticker,),
                ).fetchone()
                old_qty = float(pos_row["quantity"]) if pos_row else 0.0
                old_avg = float(pos_row["avg_entry_price"]) if pos_row else 0.0

                # Position size: size_fraction of current equity
                equity = self._equity_from_conn(conn, current_cash, prices)
                trade_notional = equity * decision.size_fraction
                shares = trade_notional / fill_price  # fractional shares ok in shadow

                if decision.action == "sell":
                    shares = -abs(shares)

                new_qty, new_avg = _update_position(old_qty, old_avg, shares, fill_price)

                # Cash impact
                new_cash = current_cash - (shares * fill_price)

                # Persist fill
                conn.execute(
                    "INSERT OR IGNORE INTO shadow_fills "
                    "(event_id, ticker, action, size_fraction, fill_price, cost_bps, asof) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        event_id,
                        ticker,
                        decision.action,
                        decision.size_fraction,
                        fill_price,
                        self.cost_model_bps,
                        _utc_now_iso(),
                    ),
                )

                # Persist position
                if abs(new_qty) < 1e-12:
                    conn.execute(
                        "DELETE FROM shadow_positions WHERE ticker = ?", (ticker,)
                    )
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO shadow_positions "
                        "(ticker, quantity, avg_entry_price, last_update_at) "
                        "VALUES (?, ?, ?, ?)",
                        (ticker, new_qty, new_avg, _utc_now_iso()),
                    )

                # Persist cash
                conn.execute(
                    "UPDATE shadow_cash SET balance = ?, last_update_at = ? WHERE id = 1",
                    (new_cash, _utc_now_iso()),
                )

                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        return decision

    # ------------------------------------------------------------------
    # mark_to_market
    # ------------------------------------------------------------------

    def mark_to_market(self, prices: dict[str, float]) -> dict[str, Any]:
        """Mark the portfolio to current prices and persist a P&L snapshot.

        Parameters
        ----------
        prices:
            Current prices keyed by ticker.

        Returns
        -------
        dict with keys: equity_total, cash, positions_value, pnl_today, pnl_total.
        """
        with self._lock, self._conn() as conn:
            cash_row = conn.execute(
                "SELECT balance FROM shadow_cash WHERE id = 1"
            ).fetchone()
            current_cash = float(cash_row["balance"]) if cash_row else self.initial_cash

            pos_rows = conn.execute(
                "SELECT ticker, quantity, avg_entry_price FROM shadow_positions"
            ).fetchall()

            positions_value = 0.0
            for row in pos_rows:
                qty = float(row["quantity"])
                if abs(qty) < 1e-12:
                    continue
                price = prices.get(row["ticker"], float(row["avg_entry_price"]))
                positions_value += qty * price

            equity_total = current_cash + positions_value

            # Prior equity from last history entry
            last_row = conn.execute(
                "SELECT equity_total FROM shadow_pnl_history ORDER BY asof DESC LIMIT 1"
            ).fetchone()
            prior_equity = float(last_row["equity_total"]) if last_row else self.initial_cash

            pnl_today = equity_total - prior_equity
            pnl_total = equity_total - self.initial_cash

            asof_str = _utc_now_iso()
            conn.execute(
                "INSERT OR REPLACE INTO shadow_pnl_history "
                "(asof, equity_total, cash, positions_value, pnl_today, pnl_total) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (asof_str, equity_total, current_cash, positions_value, pnl_today, pnl_total),
            )

        return {
            "equity_total": equity_total,
            "cash": current_cash,
            "positions_value": positions_value,
            "pnl_today": pnl_today,
            "pnl_total": pnl_total,
        }

    # ------------------------------------------------------------------
    # pnl_curve
    # ------------------------------------------------------------------

    def pnl_curve(self) -> pd.Series:
        """Return a pandas Series of daily P&L indexed by asof date string.

        Returns
        -------
        pd.Series[float] with index = asof timestamps, values = pnl_today.
        """
        history = self.pnl_history
        if not history:
            return pd.Series(dtype=float, name=self.rule.name)
        index = [h["asof"] for h in history]
        values = [h["pnl_today"] for h in history]
        return pd.Series(values, index=index, name=self.rule.name)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _equity_from_conn(
        self, conn: sqlite3.Connection, cash: float, prices: dict[str, float]
    ) -> float:
        pos_rows = conn.execute(
            "SELECT ticker, quantity, avg_entry_price FROM shadow_positions"
        ).fetchall()
        positions_value = 0.0
        for row in pos_rows:
            qty = float(row["quantity"])
            if abs(qty) < 1e-12:
                continue
            price = prices.get(row["ticker"], float(row["avg_entry_price"]))
            positions_value += qty * price
        return cash + positions_value

    def reset(self) -> None:
        """Clear all state and reset to initial_cash. Used in tests."""
        with self._lock, self._conn() as conn:
            conn.executescript(
                "DELETE FROM shadow_positions;"
                "DELETE FROM shadow_cash;"
                "DELETE FROM shadow_pnl_history;"
                "DELETE FROM shadow_fills;"
            )
        self._bootstrap_cash_if_missing()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _update_position(
    old_qty: float,
    old_avg: float,
    delta_qty: float,
    fill_price: float,
) -> tuple[float, float]:
    """Compute new (quantity, avg_entry_price) after a fill.

    Uses FIFO-compatible weighted-average entry price.
    """
    new_qty = old_qty + delta_qty
    if abs(new_qty) < 1e-12:
        return 0.0, 0.0
    if math.copysign(1, old_qty) == math.copysign(1, delta_qty) or old_qty == 0.0:
        # Adding to or initiating a position: weighted average
        total_cost = old_qty * old_avg + delta_qty * fill_price
        new_avg = total_cost / new_qty
    else:
        # Reducing a position: avg doesn't change
        new_avg = old_avg
    return new_qty, new_avg
