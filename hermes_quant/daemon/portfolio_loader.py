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
from hermes_quant.react.base import is_absolute_target_record

logger = logging.getLogger(__name__)

# cs14 Option B (ADR-0091 Option E / docs/design/2026-06-13-cs14-weekly-exit-reader-fork.md):
# the LIVE producer (react.paper.PaperReactor -> _record_to_dict) emits a record shape
# the legacy int-1 `side`/`qty` branch below CANNOT read — it carries
# schema_version=None (or the "absolute-target-v1" sentinel), a signed NAV-fraction
# `target_position_pct`, `reactor_name`, `fill_price`, `asof_execution`, and NO
# top-level qty/side/account_id. The absolute-target reconstruction block (added below)
# consumes that real shape via LATEST-TARGET semantics (mirroring
# portfolio.state.reconstruct_portfolio_state) so the weekly-exit reader reconstructs a
# real book. Only equity-fill reactors are admitted; crypto/other partitions are NOT
# silently absorbed (they must use the legacy int-1 path or a future per-class seam).
EQUITY_FILL_REACTORS = frozenset({"paper", "deterministic-equity", "alpaca_paper"})


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
    # Filter to scope — LEGACY int-1 path (hand-rolled side/qty records; the crypto
    # settlement shape). Unchanged: schema_version == 1 is the int sentinel, which is
    # disjoint from the absolute-target shape (None / "absolute-target-v1") by
    # construction, so the two lists never overlap.
    matching = [
        r
        for r in records
        if r.get("account_id") == account_id
        and r.get("asset_class") == asset_class
        and r.get("schema_version") == 1
    ]

    # cs14: ABSOLUTE-TARGET path — the real live producer shape. A record is admitted
    # iff it is an absolute-target record (schema_version None or the sentinel),
    # its reactor_name is an equity-fill reactor, its asset_class matches scope, AND
    # the account resolves to the requested partition the SAME way the live
    # producer/state-write seam does: top-level account_id (legacy-injected) OR
    # reactor_metadata.account_id OR the "paper-default" sentinel (react.paper:438-441).
    # We do NOT silently admit crypto/other partitions — reactor_name must be in the
    # equity-fill set.
    def _record_account(r: dict) -> str:
        acct = r.get("account_id")
        if acct:
            return str(acct)
        meta_acct = (r.get("reactor_metadata") or {}).get("account_id")
        if meta_acct:
            return str(meta_acct)
        return "paper-default"

    # cs24: account-EQUALITY, NOT a set-OR over {account_id, "paper-default"}.
    #
    # The prior `_record_account(r) in {account_id, "paper-default"}` was a set-OR
    # that admitted the ENTIRE synthetic "paper-default" book (PaperReactor +
    # DeterministicEquityReactor both resolve to "paper-default") into ANY requested
    # account. Empirically, reconstruct_portfolio("alpaca-paper","equity") returned
    # the paper-default fills POOLED with the lone real alpaca-paper position — so a
    # request for the SHADOW book (react.alpaca_paper: a deliberately SEPARATE
    # partition, default-OFF) silently absorbed the real synthetic managed book.
    #
    # This now matches the cs18 sibling reconstruction
    # (portfolio.state.reconstruct_portfolio_state:138 — `_record_account(rec) !=
    # account` skip) AND the strict legacy int-1 path above (:90 `== account_id`).
    # The two reconstructions previously DISAGREED on account semantics (set-OR vs
    # equality); they now agree. A paper-default request still gets the paper-default
    # book; an alpaca-paper request gets ONLY the alpaca-paper shadow book — no pool.
    absolute_matching = [
        r
        for r in records
        if is_absolute_target_record(r)
        and r.get("reactor_name") in EQUITY_FILL_REACTORS
        and r.get("asset_class") == asset_class
        and _record_account(r) == account_id
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
            # Fully closing position (clean path).
            #
            # cs00 sign fix (2026-06-13): the realized P&L of a full close is
            #   (exit_price - avg_entry) * exit_qty_in_the_direction_of_the_lot
            # where the lot being closed has signed size -signed_qty (the close
            # fill is opposite the position). For a LONG (old_qty > 0) the close
            # fill is a sell (signed_qty < 0) so -signed_qty > 0 and the formula
            # is (fill - avg_old) * (+qty) — profit when fill > entry. For a SHORT
            # (old_qty < 0) the close fill is a buy (signed_qty > 0) so
            # -signed_qty < 0 and the formula is (fill - avg_old) * (-qty) —
            # profit when fill < entry (covered cheaper than shorted). The
            # previous trailing `* (1 if old_qty > 0 else -1)` factor INVERTED the
            # short branch (shorting @100 then covering @90 booked -100 instead of
            # +100). Dropping it makes the short branch correct; the long branch is
            # unchanged (the dropped factor was +1 for longs). realized_pnl_total
            # is report-only / not-yet-gate-wired today (the lone live consumer,
            # scripts/quant-playbook-weekly.py, reads pf.positions only), so this
            # is a pure correctness fix to a human-facing number.
            avg_old = (old_cost / old_qty) if old_qty != 0 else 0.0
            realized = (fill - avg_old) * (-signed_qty)
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

    # ------------------------------------------------------------------------
    # cs14 ABSOLUTE-TARGET reconstruction (LATEST-TARGET, never delta-summed).
    #
    # The real live producer emits one absolute signed NAV-fraction target per
    # symbol per fill; later fills SUPERSEDE earlier ones (they do NOT cancel /
    # net out — that is the dual-ledger inflation ADR-0091 forbids). So we keep
    # the single record with the MAX asof_execution per symbol (mirroring
    # portfolio.state.reconstruct_portfolio_state:108-113) and derive ONE Position
    # from it. A latest target of 0.0 means the symbol is closed -> dropped.
    #
    # Derivation of share qty/avg_entry from a NAV fraction:
    #   * reactor_metadata.quantity, when present, is the AUTHORITATIVE signed
    #     absolute filled share count (the det-equity / live-broker reconciliation
    #     anchor per react/base.py:50-52) — used verbatim.
    #   * otherwise qty = target_position_pct * NAV / entry_price, where NAV is the
    #     loader's `initial_cash` (the daemon passes the account NAV here; in tests
    #     it is the 100_000 default). entry_price = fill_price (slipped) or, absent
    #     that, decision_price.
    # The Position's cost-basis cash leg is folded into `cash` (cash -= qty*entry)
    # so equity_total = cash + sum(qty*mark) stays coherent with the legacy legs.
    abs_latest: dict[str, dict] = {}
    for rec in absolute_matching:
        asset = rec.get("asset")
        if asset is None:
            continue
        ts = rec.get("asof_execution") or rec.get("asof")
        if ts is None:
            continue
        prior = abs_latest.get(asset)
        if prior is None or ts >= (prior.get("asof_execution") or prior.get("asof") or ""):
            abs_latest[asset] = rec

    for asset, rec in abs_latest.items():
        try:
            target_pct = float(rec.get("target_position_pct"))
        except (TypeError, ValueError):
            logger.warning("absolute-target record missing target_position_pct: %s", rec)
            continue
        if abs(target_pct) < 1e-12:
            # Latest target is flat -> the position is closed. Drop it.
            continue

        # Entry price: slipped fill_price preferred, else decision_price.
        try:
            entry_price = float(rec.get("fill_price"))
        except (TypeError, ValueError):
            entry_price = 0.0
        if entry_price <= 0.0:
            try:
                entry_price = float(rec.get("decision_price"))
            except (TypeError, ValueError):
                entry_price = 0.0
        if entry_price <= 0.0:
            logger.warning(
                "absolute-target record for %s has no usable entry price; skipped: %s",
                asset,
                rec,
            )
            continue

        meta = rec.get("reactor_metadata") or {}
        qty: float
        meta_qty = meta.get("quantity")
        if meta_qty is not None:
            # Authoritative signed absolute backend share count (det-equity / live).
            try:
                qty = float(meta_qty)
            except (TypeError, ValueError):
                qty = (target_pct * initial_cash) / entry_price
        else:
            # NAV-fraction fallback: shares = (fraction-of-NAV * NAV) / price.
            qty = (target_pct * initial_cash) / entry_price

        if abs(qty) < 1e-12:
            continue

        mark = mark_prices.get(asset, entry_price)
        unrealized = (mark - entry_price) * qty
        positions[asset] = Position(
            asset=asset,
            qty=qty,
            avg_entry_price=entry_price,
            mark_price=mark,
            unrealized_pnl=unrealized,
            realized_fees=0.0,
        )
        # Fold the absolute leg's cost basis into cash so equity stays coherent.
        cash -= qty * entry_price

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
