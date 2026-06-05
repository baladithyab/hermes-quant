#!/usr/bin/env python3
"""quant-alpaca-shadow-compare.py — compare Alpaca shadow fills vs the synthetic book.

The Alpaca SHADOW hook (HERMES_QUANT_ALPACA_SHADOW=1) records, for each fill, the
divergence between Alpaca's REAL paper fill and the synthetic PaperReactor fill to
``~/.hermes/quant/alpaca-shadow-divergence.jsonl``. This harness reads that log and
prints a compact per-fill divergence table (fill price, qty/size, resulting cash).

It is READ-ONLY:
  * It reads the shadow divergence log (written by the hook) — it NEVER submits orders.
  * Optionally (--with-broker) it does a READ-ONLY ``get_account()`` + ``get_positions()``
    against Alpaca paper to print the broker's current truth alongside the synthetic
    state.db ``paper-default`` book — purely informational. No orders are ever placed.

Usage:
    python ops/scripts/quant-alpaca-shadow-compare.py                 # table from the log
    python ops/scripts/quant-alpaca-shadow-compare.py --limit 50
    python ops/scripts/quant-alpaca-shadow-compare.py --with-broker   # + live read-only snapshot
    python ops/scripts/quant-alpaca-shadow-compare.py --log /path/to/divergence.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from hermes_quant.react.alpaca_shadow import SHADOW_DIVERGENCE_PATH


def _load_divergences(path: Path, limit: int) -> list[dict[str, Any]]:
    """Read the divergence log (newest last). Tolerant of malformed lines."""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # Skip a corrupt/partial line rather than crash — same posture as the
            # proposals reconcile reader.
            print(f"WARN: skipping malformed divergence line: {line[:80]!r}", file=sys.stderr)
            continue
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    return rows


def _fmt(value: Any, width: int = 10, prec: int = 4) -> str:
    if value is None:
        return "n/a".rjust(width)
    try:
        return f"{float(value):>{width}.{prec}f}"
    except (TypeError, ValueError):
        return str(value)[:width].rjust(width)


def _print_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No shadow divergence records found. Has HERMES_QUANT_ALPACA_SHADOW=1 run yet?")
        return

    header = (
        f"{'asof':<20} {'asset':<8} {'req%':>8} "
        f"{'syn_px':>10} {'alp_px':>10} {'px_div':>10} "
        f"{'syn%':>8} {'alp%':>8} {'sz_div':>9} "
        f"{'alp_qty':>9} {'status':<10}"
    )
    print(header)
    print("-" * len(header))

    n_unfilled = 0
    abs_px_div_sum = 0.0
    abs_sz_div_sum = 0.0
    cash_div_sum = 0.0
    for r in rows:
        unfilled = bool(r.get("alpaca_unfilled_timeout"))
        n_unfilled += 1 if unfilled else 0
        px_div = r.get("fill_price_divergence")
        sz_div = r.get("fill_size_divergence")
        if isinstance(px_div, (int, float)):
            abs_px_div_sum += abs(px_div)
        if isinstance(sz_div, (int, float)):
            abs_sz_div_sum += abs(sz_div)
        # Resulting-cash divergence proxy: difference in signed notional moved.
        # synthetic notional = syn_size * equity ; alpaca notional = filled_notional.
        equity = r.get("alpaca_account_equity") or 0.0
        syn_notional = (r.get("synthetic_fill_size_pct") or 0.0) * float(equity)
        alp_qty = r.get("alpaca_filled_qty") or 0.0
        alp_px = r.get("alpaca_fill_price") or 0.0
        alp_notional = float(alp_qty) * float(alp_px)
        cash_div_sum += abs(alp_notional - syn_notional)

        status = str(r.get("alpaca_status") or ("UNFILLED" if unfilled else "?"))
        print(
            f"{str(r.get('asof', ''))[:20]:<20} "
            f"{str(r.get('asset', ''))[:8]:<8} "
            f"{_fmt(r.get('requested_fill_size_pct'), 8)} "
            f"{_fmt(r.get('synthetic_fill_price'), 10)} "
            f"{_fmt(r.get('alpaca_fill_price'), 10)} "
            f"{_fmt(px_div, 10)} "
            f"{_fmt(r.get('synthetic_fill_size_pct'), 8)} "
            f"{_fmt(r.get('alpaca_fill_size_pct'), 8)} "
            f"{_fmt(sz_div, 9)} "
            f"{_fmt(alp_qty, 9, 2)} "
            f"{status:<10}"
        )

    print("-" * len(header))
    n = len(rows)
    print(
        f"records={n}  unfilled={n_unfilled}  "
        f"mean|price_div|={abs_px_div_sum / n:.4f}  "
        f"mean|size_div|={abs_sz_div_sum / n:.6f}  "
        f"sum|cash_div|=${cash_div_sum:,.2f}"
    )


def _print_broker_snapshot() -> None:
    """READ-ONLY broker + synthetic-book snapshot (no orders submitted)."""
    print("\n=== READ-ONLY broker snapshot (Alpaca paper) vs synthetic paper-default ===")
    try:
        from hermes_quant.react.alpaca_paper import _build_paper_trading_client

        client = _build_paper_trading_client()
        account = client.get_account()
        positions = client.get_all_positions()  # read-only (alpaca-py: get_all_positions)
    except Exception as exc:  # noqa: BLE001 — read-only snapshot is best-effort
        print(f"  (broker read failed — skipping live snapshot): {exc}")
        return

    print(f"  Alpaca equity:      ${float(getattr(account, 'equity', 0.0)):,.2f}")
    print(f"  Alpaca cash:        ${float(getattr(account, 'cash', 0.0)):,.2f}")
    print(f"  Alpaca buying_power:${float(getattr(account, 'buying_power', 0.0)):,.2f}")
    print(f"  Alpaca positions ({len(positions)}):")
    for p in positions:
        sym = getattr(p, "symbol", "?")
        qty = getattr(p, "qty", "?")
        mv = getattr(p, "market_value", "?")
        print(f"    {sym:<8} qty={qty:<12} market_value={mv}")

    # Synthetic book for comparison (state.db paper-default vs alpaca-paper).
    try:
        from hermes_quant.state.portfolio_state import get_portfolio_state

        ps = get_portfolio_state()
        for acct in ("paper-default", "alpaca-paper"):
            cash = ps.get_cash(acct)
            pos = ps.get_positions(acct)
            equity = f"${cash.equity_total:,.2f}" if cash else "n/a"
            print(f"  state.db[{acct}]: equity={equity} positions={len(pos)}")
    except Exception as exc:  # noqa: BLE001
        print(f"  (state.db read failed): {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log",
        type=Path,
        default=SHADOW_DIVERGENCE_PATH,
        help=f"divergence log path (default: {SHADOW_DIVERGENCE_PATH})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="max most-recent records to show (default 50; 0 = all)",
    )
    parser.add_argument(
        "--with-broker",
        action="store_true",
        help="also print a READ-ONLY Alpaca get_account/get_positions snapshot",
    )
    args = parser.parse_args(argv)

    rows = _load_divergences(args.log, args.limit)
    print(f"Alpaca shadow divergence report — {args.log}")
    print()
    _print_table(rows)

    if args.with_broker:
        _print_broker_snapshot()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
