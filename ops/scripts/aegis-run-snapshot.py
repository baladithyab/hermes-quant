#!/usr/bin/env python3
"""aegis-run-snapshot.py — one daily performance line for an AEGIS paper run.

Purpose: during a multi-day autonomous-paper observation window, capture "how is it
performing" into a self-contained run-journal directory so the operator can review the
record WITHOUT digging through raw executions.jsonl. Discord stays the live heartbeat;
this is the durable, greppable daily snapshot.

It REUSES the canonical money-path functions (no reimplementation of P&L):
  * realized P&L  : hermes_quant.autonomous.compute_cumulative_realized_pnl_pct
                    (the SAME basis the ADR-0016 kill-switch + ADR-0125 promotion gate use)
  * settled trips : hermes_quant.daemon.settlement_loop.join_exit_fills (paper-default only)
  * open book     : hermes_quant.portfolio.state.reconstruct_portfolio_state(reactor_filter="paper")

READ-ONLY: it never mutates money state. Writes only to the run-journal dir.

Usage:
  # append today's snapshot to a run (creates the run dir + run-card on first call):
  aegis-run-snapshot.py --run-id 2026-06-18-paper-window
  # custom dir / bus (for testing):
  aegis-run-snapshot.py --run-id test --home /tmp/q --bus /tmp/q/executions.jsonl
  # also write the armed-flag run-card (call once at the start of a run):
  aegis-run-snapshot.py --run-id <id> --write-run-card

Output: ~/.hermes/quant/aegis-runs/<run-id>/perf.jsonl  (one JSON line per call)
        ~/.hermes/quant/aegis-runs/<run-id>/run-card.json (armed flags + start, once)
        + a human summary to stdout (Discord-friendly when delivered).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# The flags whose ON/OFF state defines whether the safety rails are armed for this run.
# The run-card records what was actually set in the process env at run start, so a
# later review can tell whether a window ran armed or disarmed.
_RAIL_FLAGS = [
    "HERMES_QUANT_PORTFOLIO_CAPS",
    "HERMES_QUANT_PAPER_SLIPPAGE_MODEL",
    "HERMES_QUANT_DETERMINISTIC_EQUITY",
    "HERMES_QUANT_DURABLE_DRAWDOWN_BASELINE",
    "HERMES_QUANT_PER_POSITION_STOP",
    "HERMES_QUANT_POST_LOSS_COOLDOWN",
    "HERMES_QUANT_DELTA_NORMALIZER",
    "HERMES_QUANT_ACCOUNT_LOCK",
    "HERMES_QUANT_REFLECTION",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runs_root(home: Path) -> Path:
    return home / "aegis-runs"


def compute_snapshot(home: Path, bus: Path) -> dict:
    """Return the daily perf snapshot dict (read-only)."""
    # Import here so the script can resolve QUANT_HOME overrides before the module
    # binds its module-level QUANT_HOME (we pass the bus path explicitly regardless).
    from hermes_quant.autonomous import compute_cumulative_realized_pnl_pct
    from hermes_quant.daemon.settlement_loop import join_exit_fills

    snap: dict = {"asof": _now(), "bus": str(bus)}

    if not bus.exists():
        snap["error"] = f"bus not found at {bus}"
        return snap

    # --- realized P&L via the canonical kill-switch basis (paper-default) ---
    try:
        cum_frac = compute_cumulative_realized_pnl_pct(executions_path=bus)
        snap["realized_pnl_frac_nav"] = round(cum_frac, 6)
    except Exception as exc:  # noqa: BLE001 - snapshot must never crash the run
        snap["realized_error"] = str(exc)[:200]

    # --- settled round-trips (paper-default) for win-rate / count ---
    try:
        recs = []
        for line in bus.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict) and r.get("reactor_name") == "paper":
                recs.append(r)
        rts, open_lots = join_exit_fills(recs)
        paper_rts = [rt for rt in rts if getattr(rt, "account_id", None) == "paper-default"]
        n = len(paper_rts)
        wins = sum(1 for rt in paper_rts if getattr(rt, "realized_return", 0.0) > 0)
        returns = [rt.realized_return for rt in paper_rts if math.isfinite(rt.realized_return)]
        snap["n_settled_roundtrips"] = n
        snap["win_rate"] = round(wins / n, 4) if n else None
        snap["mean_return_pct"] = round(100.0 * sum(returns) / len(returns), 4) if returns else None
        # open book (paper-default) — symbol -> held NAV-fraction
        open_book = {}
        for (acct, _ac, asset), lots in open_lots.items():
            if acct != "paper-default":
                continue
            net = sum(l["qty"] if l["side"] == "buy" else -l["qty"] for l in lots)
            if abs(net) > 1e-9:
                open_book[asset] = round(net, 4)
        snap["open_positions"] = open_book
        snap["n_open"] = len(open_book)
    except Exception as exc:  # noqa: BLE001
        snap["settlement_error"] = str(exc)[:200]

    return snap


def write_run_card(run_dir: Path) -> dict:
    """Record which rail flags were armed in this process env (call once at run start)."""
    card = {
        "run_started": _now(),
        "rail_flags": {f: os.environ.get(f) for f in _RAIL_FLAGS},
        "pdr_mode_note": "pdr.mode is read from ~/.hermes/config.yaml at tick time, not env",
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run-card.json").write_text(json.dumps(card, indent=2) + "\n", encoding="utf-8")
    return card


def human_summary(snap: dict) -> str:
    if snap.get("error"):
        return f"AEGIS run snapshot: ERROR — {snap['error']}"
    rp = snap.get("realized_pnl_frac_nav")
    rp_s = f"{rp * 100:+.3f}% NAV" if isinstance(rp, (int, float)) else "n/a"
    wr = snap.get("win_rate")
    wr_s = f"{wr * 100:.0f}%" if isinstance(wr, (int, float)) else "n/a"
    n = snap.get("n_settled_roundtrips", "?")
    nopen = snap.get("n_open", "?")
    book = snap.get("open_positions", {})
    book_s = ", ".join(f"{k} {v:+.2f}" for k, v in book.items()) or "flat"
    return (
        f"AEGIS paper snapshot {snap.get('asof', '')[:19]}Z\n"
        f"  realized P&L: {rp_s}  | settled round-trips: {n}  | win-rate: {wr_s}\n"
        f"  open positions ({nopen}): {book_s}\n"
        f"  (realized basis = the canonical kill-switch path; honest forward-only record)"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-id", required=True, help="run-journal subdir name, e.g. 2026-06-18-paper-window")
    ap.add_argument("--home", default=str(Path.home() / ".hermes" / "quant"), help="QUANT_HOME (default ~/.hermes/quant)")
    ap.add_argument("--bus", default=None, help="executions.jsonl path (default <home>/executions.jsonl)")
    ap.add_argument("--write-run-card", action="store_true", help="also write run-card.json (call once at run start)")
    ap.add_argument("--json", action="store_true", help="emit the snapshot dict as JSON instead of the human summary")
    args = ap.parse_args(argv)

    home = Path(args.home)
    bus = Path(args.bus) if args.bus else home / "executions.jsonl"
    run_dir = _runs_root(home) / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    if args.write_run_card:
        card = write_run_card(run_dir)
        if not args.json:
            armed = [f for f, v in card["rail_flags"].items() if v]
            print(f"AEGIS run-card written: {run_dir / 'run-card.json'}")
            print(f"  armed flags: {', '.join(armed) or 'NONE (rails disarmed!)'}")

    snap = compute_snapshot(home, bus)

    # Append the daily line (durable, greppable).
    perf = run_dir / "perf.jsonl"
    with perf.open("a", encoding="utf-8") as f:
        f.write(json.dumps(snap, default=str) + "\n")

    if args.json:
        print(json.dumps(snap, default=str, indent=2))
    else:
        print(human_summary(snap))
        print(f"  -> appended to {perf}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
