"""quant-admissibility-restate.py — offline borrow-aware restatement of the short book (ADR-0077).

Reads `state.db` positions, classifies every short through a ShortabilityOracle, accrues a coarse
borrow carry, and prints a per-symbol accept/reject table + the count of NOT_ETB/NOT_SHORTABLE on
the shorts + an estimated borrow-carry total. This is the rollout-phase-2 operator-audit artifact
(ADR-0077 §Rollout step 2; Verification block).

READ-ONLY on `state.db` — this is a measurement, never a mutation. It NEVER writes positions.

Default-OFF posture: this script does NOT change the live decision path. It only measures. The
admissibility/borrow flags gate the daemon; this offline tool always reports the restatement so the
operator can audit it before flipping anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

_VENV = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
# Re-exec into the venv ONLY when run as a script (idiom §1.6). When imported as a module
# (e.g. by the unit suite via importlib) we must NOT replace the process.
if __name__ == "__main__" and _VENV.exists() and sys.executable != str(_VENV):
    import os

    os.execv(str(_VENV), [str(_VENV), __file__, *sys.argv[1:]])

from hermes_quant.admissibility import (  # noqa: E402  (after the venv re-exec guard)
    ETB_DEFAULT_ANNUAL_CBR,
    AdmissibilityContext,
    AlpacaShortabilityOracle,
    ETBSnapshotEntry,
    ShortabilityOracle,
    StaticETBAllowlistOracle,
)

_DEFAULT_BOOK = Path.home() / ".hermes" / "quant" / "state.db"
_DEFAULT_ACCOUNT = "paper-default"


def _parse_asof(value: str) -> datetime:
    """Parse Position.last_update_at (ISO-8601 UTC) into an aware UTC datetime."""
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _snapshot_from_json(snapshot_json: dict) -> tuple[str, dict[str, ETBSnapshotEntry]]:
    """Build a {SYMBOL: ETBSnapshotEntry} map from the --asof-snapshot JSON.

    Shape: {"asof": "2026-05-30", "etb": {SYMBOL: {easy_to_borrow, shortable, marginable, annual_cbr}}}
    """
    asof = str(snapshot_json.get("asof", ""))
    out: dict[str, ETBSnapshotEntry] = {}
    for symbol, fields in (snapshot_json.get("etb") or {}).items():
        out[symbol] = ETBSnapshotEntry(
            symbol=symbol,
            asof=asof,
            easy_to_borrow=bool(fields.get("easy_to_borrow", False)),
            shortable=bool(fields.get("shortable", False)),
            marginable=bool(fields.get("marginable", False)),
            annual_cbr=float(fields.get("annual_cbr", ETB_DEFAULT_ANNUAL_CBR)),
        )
    return asof, out


def restate_book(
    book: Path | str,
    account_id: str,
    snapshot: dict[str, ETBSnapshotEntry],
    oracle: ShortabilityOracle,
    *,
    asof_snapshot: str = "",
    now: datetime | None = None,
) -> dict:
    """Classify every short in `book` through `oracle`; accrue a coarse one-mark borrow estimate.

    READ-ONLY on `book` — uses PortfolioState.get_positions, never a write path.

    The borrow estimate is a deliberately coarse single-mark accrual: it uses avg_entry_price as a
    daily-close proxy and ETB_DEFAULT_ANNUAL_CBR (or the snapshot CBR) over the held-day count
    (now - last_update_at, calendar days). This is NOT a daily mark-to-market (we lack the historical
    bar series here); the JSON `restated_note` documents the caveat.
    """
    from hermes_quant.admissibility import target_pct_to_shares
    from hermes_quant.admissibility.borrow_pnl import DAY_COUNT_BASIS
    from hermes_quant.state.portfolio_state import PortfolioState

    now = now or datetime.now(tz=UTC)
    state = PortfolioState(state_db_path=Path(book))
    positions = state.get_positions(account_id)

    # Account NAV (equity_total) is the basis for the NAV-fraction -> share conversion.
    # state.db stores position.quantity as a NAV FRACTION (cumulative fill_size_pct), NOT
    # shares (portfolio_state §D7). Without NAV we cannot value any short -> fail-closed
    # (every short would report MISSING_ACCOUNT_CONTEXT). 0.0 sentinel makes that explicit.
    cash = state.get_cash(account_id)
    account_nav = float(cash.equity_total) if cash is not None else 0.0

    rows: list[dict] = []
    n_rejected = 0
    n_rejected_not_etb = 0
    n_accepted = 0
    total_carry = 0.0

    shorts = [p for p in positions.values() if p.is_short]
    for pos in shorts:
        asof = _parse_asof(pos.last_update_at)
        snap = snapshot.get(pos.symbol)
        # Convert the stored NAV-fraction position to a whole-share count using the
        # account NAV + avg_entry_price (our only offline price; documented proxy). The
        # oracle's whole-share check needs SHARES, not a fraction — passing abs(quantity)
        # (the fraction) made every short fractional -> blanket FRACTIONAL_SHORT.
        signed_shares = target_pct_to_shares(pos.quantity, account_nav, pos.avg_entry_price)
        qty = abs(signed_shares)
        # Populate the account/quote context the hardened oracle now REQUIRES for an
        # opening short. avg_entry_price is the only offline quote we have (proxy);
        # equity_total fills both account_equity and available_bp for a coarse audit.
        ctx = AdmissibilityContext(
            tradable=True if snap else None,
            marginable=snap.marginable if snap else None,
            shortable=snap.shortable if snap else None,
            easy_to_borrow=snap.easy_to_borrow if snap else None,
            annual_cbr=snap.annual_cbr if snap else None,
            current_ask=pos.avg_entry_price,
            account_equity=account_nav,
            available_bp=account_nav,
        )
        verdict = oracle.verdict(pos.symbol, "short", qty, asof, ctx)

        if verdict.state.value == "REJECTED":
            n_rejected += 1
            if verdict.reason == "NOT_ETB":
                n_rejected_not_etb += 1
        elif verdict.state.value == "ACCEPTED":
            n_accepted += 1

        # Coarse one-mark borrow estimate (audit only, see caveat). Notional is
        # shares*price (quantity is a NAV fraction, NOT shares — see conversion above).
        cbr = verdict.annual_cbr or (snap.annual_cbr if snap else ETB_DEFAULT_ANNUAL_CBR)
        days_held = max(0, (now - asof).days)
        est_carry = qty * pos.avg_entry_price * cbr / DAY_COUNT_BASIS * days_held
        total_carry += est_carry

        rows.append(
            {
                "symbol": pos.symbol,
                "qty": pos.quantity,
                "qty_shares": signed_shares,
                "state": verdict.state.value,
                "reason": verdict.reason,
                "annual_cbr": round(cbr, 6),
                "est_borrow_carry_usd": round(est_carry, 4),
            }
        )

    rows.sort(key=lambda r: r["symbol"])
    return {
        "asof_snapshot": asof_snapshot,
        "account_id": account_id,
        "n_shorts": len(shorts),
        "n_rejected": n_rejected,
        "n_rejected_not_etb": n_rejected_not_etb,
        "n_accepted": n_accepted,
        "total_est_borrow_carry_usd": round(total_carry, 4),
        "rows": rows,
        "restated_note": (
            "Positions are NAV fractions (cumulative fill_size_pct), converted to whole shares "
            "via target_pct_to_shares(quantity, account_equity_total, avg_entry_price); "
            "avg_entry_price is the offline quote proxy and equity_total backs both account_equity "
            "and available_bp. Coarse one-mark borrow estimate over calendar days held; NOT a daily "
            "mark-to-market. Admissibility uses the asof-snapshot (or live get_asset); names absent "
            "from the snapshot are fail-closed REJECT(NOT_ETB). With no cash row (NAV unknown) every "
            "short is fail-closed REJECT (zero shares / MISSING_ACCOUNT_CONTEXT)."
        ),
    }


def _build_oracle(args) -> tuple[ShortabilityOracle, str, dict[str, ETBSnapshotEntry]]:
    snapshot: dict[str, ETBSnapshotEntry] = {}
    asof_snapshot = ""
    if args.asof_snapshot:
        snapshot_json = json.loads(Path(args.asof_snapshot).read_text())
        asof_snapshot, snapshot = _snapshot_from_json(snapshot_json)

    if args.oracle == "alpaca":
        oracle: ShortabilityOracle = AlpacaShortabilityOracle()
    else:
        oracle = StaticETBAllowlistOracle(snapshot)
    return oracle, asof_snapshot, snapshot


def _format_text(result: dict) -> str:
    lines = [
        f"Admissibility restatement — account={result['account_id']} "
        f"asof_snapshot={result['asof_snapshot'] or '(none)'}",
        f"  shorts={result['n_shorts']}  accepted={result['n_accepted']}  "
        f"rejected={result['n_rejected']}  rejected_not_etb={result['n_rejected_not_etb']}",
        f"  total_est_borrow_carry_usd={result['total_est_borrow_carry_usd']}",
        "",
        f"  {'SYMBOL':<10} {'QTY%NAV':>10} {'SHARES':>8} {'STATE':<10} "
        f"{'REASON':<22} {'CBR':>8} {'EST_CARRY':>12}",
    ]
    for row in result["rows"]:
        lines.append(
            f"  {row['symbol']:<10} {row['qty']:>10.4f} {row['qty_shares']:>8d} "
            f"{row['state']:<10} {str(row['reason'] or '-'):<22} {row['annual_cbr']:>8.4f} "
            f"{row['est_borrow_carry_usd']:>12.4f}"
        )
    lines.append("")
    lines.append("  " + result["restated_note"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline borrow-aware short-book restatement (ADR-0077)."
    )
    parser.add_argument("--book", default=str(_DEFAULT_BOOK), help="state.db path")
    parser.add_argument("--account-id", default=_DEFAULT_ACCOUNT)
    parser.add_argument(
        "--asof-snapshot", default="", help="JSON snapshot path (see module docstring)"
    )
    parser.add_argument("--oracle", choices=("static", "alpaca"), default="static")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    oracle, asof_snapshot, snapshot = _build_oracle(args)
    result = restate_book(
        args.book,
        args.account_id,
        snapshot,
        oracle,
        asof_snapshot=asof_snapshot,
    )

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(_format_text(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
