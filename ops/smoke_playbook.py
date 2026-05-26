"""Live smoke run for hermes_quant.playbook scorers.

Hits yfinance for ten symbols and prints which plays each is eligible for.

Usage:
    python ops/smoke_playbook.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from hermes_quant.playbook import compute_play_snapshot, score_all

SYMBOLS = ["NVDA", "AAPL", "MSFT", "AMZN", "AMD", "NET", "CRWD", "MRNA", "PLTR", "GOOGL"]


def fmt_pct(x: float | None) -> str:
    if x is None:
        return "  N/A"
    return f"{x*100:+6.1f}%"


def fmt_num(x: float | None, scale: float = 1.0, fmt: str = "8.2f") -> str:
    if x is None:
        return "    N/A"
    return f"{x * scale:{fmt}}"


def main() -> int:
    asof = datetime.now(tz=UTC)
    print(f"# hermes-quant playbook smoke run @ {asof.isoformat()}")
    print(f"# symbols: {SYMBOLS}\n")

    rows: list[tuple[str, dict, dict]] = []
    for sym in SYMBOLS:
        print(f"[{sym}] fetching snapshot...", flush=True)
        try:
            snap = compute_play_snapshot(sym, asof=asof)
            fits = score_all(snap)
        except Exception as e:  # noqa: BLE001
            print(f"  ! error: {type(e).__name__}: {e}")
            continue
        rows.append((sym, snap, fits))

    print()
    print("=" * 100)
    print(
        f"{'sym':<6} {'last':>7} {'mcap($B)':>9} {'adv30($M)':>10} {'rvol':>6} "
        f"{'rsi':>5} {'52wHi':>7} {'CC':>5} {'CSP':>5} {'WHL':>5} {'LPS':>5} {'SWG':>5}"
    )
    print("-" * 100)
    for sym, snap, fits in rows:
        cc = fits["covered_call"]
        csp = fits["csp"]
        wh = fits["wheel"]
        lp = fits["leaps"]
        sw = fits["swing"]
        print(
            f"{sym:<6} "
            f"{fmt_num(snap['last_close'], fmt='7.2f')} "
            f"{fmt_num(snap['market_cap_usd'], scale=1/1e9, fmt='9.1f')} "
            f"{fmt_num(snap['avg_dollar_volume_30d'], scale=1/1e6, fmt='10.1f')} "
            f"{fmt_num(snap['realized_vol_30d'], fmt='6.2f')} "
            f"{fmt_num(snap['rsi_14'], fmt='5.1f')} "
            f"{fmt_pct(snap['distance_from_52w_high_pct']):>7} "
            f"{cc.score:>5.2f} {csp.score:>5.2f} {wh.score:>5.2f} "
            f"{lp.score:>5.2f} {sw.score:>5.2f}"
        )
    print("=" * 100)
    print()

    # Eligibility summary
    print("Eligibility (✓ = score>=0.65 AND pass_hard AND not evicted):")
    print(f"{'sym':<6} {'covered_call':<13} {'csp':<5} {'wheel':<7} {'leaps':<7} {'swing':<7}")
    print("-" * 60)
    for sym, _snap, fits in rows:
        marks = []
        for play in ("covered_call", "csp", "wheel", "leaps", "swing"):
            marks.append("✓" if fits[play].eligible else "✗")
        print(
            f"{sym:<6} {marks[0]:^13} {marks[1]:^5} {marks[2]:^7} "
            f"{marks[3]:^7} {marks[4]:^7}"
        )

    # Per-symbol detail
    print("\nPer-symbol failed-rule highlights:")
    for sym, _snap, fits in rows:
        print(f"\n[{sym}]")
        for play in ("covered_call", "csp", "wheel", "leaps", "swing"):
            f = fits[play]
            tag = "ELIGIBLE" if f.eligible else "—"
            failed = ", ".join(f.failed_rules[:4]) or "(none)"
            print(f"  {play:<13} score={f.score:.2f} hard={f.pass_hard} {tag}")
            if f.failed_rules:
                print(f"    failed: {failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
