"""hermes_quant.daemon.portfolio_loader — Reconstruct mark-to-market Portfolio.

Per ADR-0009 §P0-3 + §P1-9: state is sourced from executions.jsonl (broker
reality), NOT from internal P&L log. Partition per (account_id, asset_class).

The settlement loop calls reconstruct_portfolio() at the start of each tick
to refresh the Portfolio dataclass from the executions back-channel.

Execution record schema (in executions.jsonl):
  {
    "schema_version": 1,
    "exec_id": "exec-...",
    "asof": "2026-05-13T...",
    "asset": "BTC/USDT",
    "side": "buy" | "sell",
    "qty": 0.5,
    "fill_price": 67234.50,
    "decision_price": 67200.00,    # signal asof price for slippage attribution
    "fees": 5.42,
    "account_id": "alpaca-paper",
    "asset_class": "crypto",
    "signal_id": "sig-...",         # optional, ties back to signal
    "realized_pnl": null            # null if entry; computed at exit
  }
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path

import pandas as pd

from hermes_quant.daemon.signal_bus import EXECUTION_BUS_PATH, read_jsonl_tail
from hermes_quant.protocol import Portfolio, Position

logger = logging.getLogger(__name__)


def reconstruct_portfolio(
    account_id: str,
    asset_class: str,
    *,
    initial_cash: float = 100_000.0,
    asof: pd.Timestamp | None = None,
    bus_path: Path = EXECUTION_BUS_PATH,
    n_records: int = 100_000,
    mark_prices: dict[str, float] | None = None,
) -> Portfolio:
    """Reconstruct a Portfolio from executions.jsonl + current marks.

    Args:
        account_id: filter executions to this account.
        asset_class: filter to this asset class.
        initial_cash: starting cash (before any fills).
        asof: snapshot timestamp; default now.
        bus_path: executions.jsonl path.
        n_records: max records to read from tail.
        mark_prices: per-asset current price for mark-to-market. If missing,
            uses the most recent fill price for that asset.

    Returns:
        Portfolio with mark-to-market accounting.
    """
    asof = asof if asof is not None else pd.Timestamp.utcnow()
    mark_prices = mark_prices or {}

    records = read_jsonl_tail(bus_path, n=n_records)
    # Filter to scope
    matching = [
        r
        for r in records
        if r.get("account_id") == account_id
        and r.get("asset_class") == asset_class
        and r.get("schema_version") == 1
    ]

    cash = initial_cash
    positions_qty: dict[str, float] = defaultdict(float)
    positions_cost: dict[str, float] = defaultdict(float)  # accumulated cost basis
    positions_last_fill: dict[str, float] = {}
    realized_pnl_total = 0.0
    realized_fees_total = 0.0
    daily_open_equity = initial_cash
    peak_equity = initial_cash

    for rec in matching:
        try:
            asset = rec["asset"]
            side = rec["side"]
            qty = float(rec["qty"])
            fill = float(rec["fill_price"])
            fees = float(rec.get("fees", 0.0))
        except (KeyError, ValueError, TypeError) as e:
            logger.warning("malformed exec record skipped: %s (%s)", rec, e)
            continue

        signed_qty = qty if side == "buy" else -qty
        notional = signed_qty * fill

        # Update position with running average cost basis
        old_qty = positions_qty[asset]
        old_cost = positions_cost[asset]
        new_qty = old_qty + signed_qty

        # Phase-8 P1-α (synthesis 2026-05-13): the direction-flip and
        # partial-close branches below have known sign-convention bugs
        # caught by Claude (P1) and DeepSeek (P0). v0.1.1 GATES OFF the
        # bug-prone codepaths and only supports two clean cases:
        #   (1) opening / adding to position (same direction)
        #   (2) full close (new_qty exactly 0)
        # Any partial close, partial reduction, or direction flip raises
        # NotImplementedError so the daemon refuses to silently corrupt
        # equity/drawdown computations. v0.1.2 will rewrite this with
        # explicit case handling + 8+ unit tests covering all
        # buy/sell × long/short × partial/full × flip combinations.
        is_full_close = abs(new_qty) < 1e-12
        is_same_direction = (old_qty == 0) or (old_qty * signed_qty > 0)
        if not (is_full_close or is_same_direction):
            raise NotImplementedError(
                "portfolio_loader.reconstruct_portfolio v0.1.1 does not "
                "support partial closes or direction flips. Phase-8 P1-α "
                f"caught sign-convention bugs in those branches. "
                f"Got old_qty={old_qty} signed_qty={signed_qty} "
                f"new_qty={new_qty} for asset={asset}. v0.1.2 will land "
                f"the rewrite. To unblock for now: configure freqtrade "
                f"with at most one open position per pair, no scale-out, "
                f"no shorts after longs (or vice versa) without a flat "
                f"transition."
            )

        if is_full_close:
            # Fully closing position (clean path)
            avg_old = (old_cost / old_qty) if old_qty != 0 else 0.0
            realized = (fill - avg_old) * (-signed_qty) * (1 if old_qty > 0 else -1)
            realized_pnl_total += realized
            positions_qty[asset] = 0.0
            positions_cost[asset] = 0.0
        else:
            # Adding to position (or opening new) — clean path
            positions_qty[asset] = new_qty
            positions_cost[asset] = old_cost + notional

        cash -= notional + fees
        realized_fees_total += fees
        positions_last_fill[asset] = fill

    # Build Position objects
    positions: dict[str, Position] = {}
    for asset, qty in positions_qty.items():
        if abs(qty) < 1e-12:
            continue
        avg_entry = positions_cost[asset] / qty if qty != 0 else 0.0
        mark = mark_prices.get(asset, positions_last_fill.get(asset, avg_entry))
        unrealized = (mark - avg_entry) * qty
        positions[asset] = Position(
            asset=asset,
            qty=qty,
            avg_entry_price=avg_entry,
            mark_price=mark,
            unrealized_pnl=unrealized,
            realized_fees=0.0,
        )

    equity_total = cash + sum(p.qty * p.mark_price for p in positions.values())
    peak_equity = max(peak_equity, equity_total)

    return Portfolio(
        account_id=account_id,
        asset_class=asset_class,
        asof=asof,
        positions=positions,
        cash=cash,
        equity_total=equity_total,
        realized_pnl_total=realized_pnl_total,
        realized_fees_total=realized_fees_total,
        peak_equity=peak_equity,
        daily_open_equity=daily_open_equity,
    )
