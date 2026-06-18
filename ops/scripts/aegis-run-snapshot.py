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
#
# d83b: SLIPPAGE_HAIRCUT is tracked here so the run-card's rail_drift detection
# covers it. The clean-window evidence is live-realistic only while the ADR-0097
# haircut stays armed (compute_gate_metrics applies it behind that flag); a mid-
# window disarm of the haircut silently reverts to paper-optimistic evidence, which
# must surface as drift — not stay invisible while the record still calls itself
# honest/forward-only.
#
# 821d: PORTFOLIO_VARIANCE_SIZING (ag01) is tracked here too so the run-card's
# window-vs-live drift detection covers it. ag01 is not yet a required armed rail,
# but if it is armed at GATE-0 t0 and silently disarmed mid-window, that shifts the
# position-sizing behaviour the window's evidence was recorded under — a drift the
# operator must see on the run-card, not have it stay invisible.
_RAIL_FLAGS = [
    "HERMES_QUANT_PORTFOLIO_CAPS",
    "HERMES_QUANT_PAPER_SLIPPAGE_MODEL",
    "HERMES_QUANT_DETERMINISTIC_EQUITY",
    "HERMES_QUANT_DURABLE_DRAWDOWN_BASELINE",
    "HERMES_QUANT_PER_POSITION_STOP",
    "HERMES_QUANT_POST_LOSS_COOLDOWN",
    "HERMES_QUANT_DELTA_NORMALIZER",
    "HERMES_QUANT_ACCOUNT_LOCK",
    "HERMES_QUANT_SLIPPAGE_HAIRCUT",
    "HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING",
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

    # Anchor the line to the clean-window t0 (GATE-0). A reviewer reading perf.jsonl can
    # see the window the number belongs to; None means the window was not anchored.
    _anchor = _read_anchor(home)
    snap["clean_window_t0"] = (_anchor or {}).get("t0") if _anchor else None

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

        # --- AG-OPT-EV-1: the ADR-0029 evidence-before-options gate, EXECUTABLE ---
        # READ-ONLY / ADDITIVE: a structured record of "have we accumulated the
        # documented options evidence yet" (N_options>=30 settled multi-leg outcomes
        # over >=30 calendar days). Changes no live gate decision; surfaced for the
        # operator. Anchored to the GATE-0 t0 so pre-window options are discarded.
        # SettledRoundTrip duck-types the (asof_exit, realized_return, asset_class)
        # contract, so the paper round-trips feed compute_options_evidence directly.
        try:
            from hermes_quant.eval.clean_window import compute_options_evidence

            t0 = _anchor_t0(home)
            opt_ev = compute_options_evidence(paper_rts, t0=t0)
            snap["options_evidence"] = {
                "n_options": opt_ev.n_options,
                "win_rate": (round(opt_ev.win_rate, 4)
                             if math.isfinite(opt_ev.win_rate) else None),
                "premium_capture_pct": (round(opt_ev.premium_capture_pct, 2)
                                        if math.isfinite(opt_ev.premium_capture_pct) else None),
                "assignment_count": opt_ev.assignment_count,
                "gate_reject_rate": (round(opt_ev.gate_reject_rate, 4)
                                     if math.isfinite(opt_ev.gate_reject_rate) else None),
                "calendar_days": (round(opt_ev.calendar_days, 2)
                                  if math.isfinite(opt_ev.calendar_days) else None),
                "n_threshold_met": opt_ev.n_threshold_met,
                "verdict": "GREEN" if opt_ev.is_green else "RED",
            }
        except Exception as exc:  # noqa: BLE001 — evidence section must never crash the run
            snap["options_evidence_error"] = str(exc)[:200]
    except Exception as exc:  # noqa: BLE001
        snap["settlement_error"] = str(exc)[:200]

    return snap


def _anchor_t0(home: Path) -> datetime | None:
    """The GATE-0 t0 datetime from the clean-window anchor (UTC-aware), or None.

    None => GATE-0 not run => options-evidence is fail-CLOSED RED. Distinct from
    ``_read_anchor`` (which returns the raw dict): this parses the ``t0`` string.
    """
    anchor = _read_anchor(home)
    if not anchor:
        return None
    t0_str = anchor.get("t0")
    if not t0_str:
        return None
    try:
        dt = datetime.fromisoformat(str(t0_str))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _read_anchor(home: Path) -> dict | None:
    """Read the GATE-0 clean_window_start.json anchor (home IS the quant dir here).

    The anchor (written by aegis-gate0-start.py via write_clean_window_start) captured the
    canonical armed-flag snapshot AT t0 — that is the WINDOW's armed state, independent of
    whatever env this snapshot process happens to run under. Returns None if absent.
    """
    p = home / "clean_window_start.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_run_card(run_dir: Path, home: Path | None = None) -> dict:
    """Record the WINDOW's armed flags (from the GATE-0 anchor), the live env, and any DRIFT.

    The prior version recorded only this process's env — which shows NONE when the snapshot
    is run outside the armed cron wrapper, falsely implying a disarmed window. The fix: the
    canonical "was this window armed" answer is the GATE-0 anchor's t0 snapshot. We record
    BOTH the window (anchor) flags and the live env, and compute ``rail_drift`` — any flag
    whose live value differs from the window value (a mid-window disarm is a real hazard the
    operator must see, NOT silently). ``armed_source`` says which we trust.
    """
    live = {f: os.environ.get(f) for f in _RAIL_FLAGS}
    anchor = _read_anchor(home) if home is not None else None
    window_flags = (anchor or {}).get("armed_flags") if anchor else None

    card: dict = {
        "run_started": _now(),
        "clean_window_t0": (anchor or {}).get("t0") if anchor else None,
        "live_env_flags": live,
        "pdr_mode_note": "pdr.mode is read from ~/.hermes/config.yaml at tick time, not env",
    }
    if window_flags:
        # The window's canonical armed state (captured at GATE-0 t0).
        card["window_armed_flags"] = window_flags
        card["armed_source"] = "gate0_anchor"
        # Drift = a rail flag whose LIVE value differs from the WINDOW value (a disarm).
        drift = {
            f: {"window": window_flags.get(f), "live": live.get(f)}
            for f in _RAIL_FLAGS
            if window_flags.get(f) != live.get(f)
        }
        if drift:
            card["rail_drift"] = drift
            card["rail_drift_warning"] = (
                "LIVE rail flags differ from the GATE-0 window flags — a rail may have been "
                "disarmed mid-window. The window's evidence is only valid while the rails "
                "stay as armed at t0."
            )
    else:
        # No anchor -> GATE-0 not run (or wrong home). Fall back to live env, flagged.
        card["window_armed_flags"] = None
        card["armed_source"] = "live_env (NO GATE-0 anchor found — window not anchored)"

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
    lines = [
        f"AEGIS paper snapshot {snap.get('asof', '')[:19]}Z",
        f"  realized P&L: {rp_s}  | settled round-trips: {n}  | win-rate: {wr_s}",
        f"  open positions ({nopen}): {book_s}",
    ]
    # AG-OPT-EV-1 options-evidence line (ADR-0029): only render when the gate ran.
    oe = snap.get("options_evidence")
    if isinstance(oe, dict):
        wr_o = oe.get("win_rate")
        wr_o_s = f"{wr_o * 100:.0f}%" if isinstance(wr_o, (int, float)) else "n/a"
        lines.append(
            f"  AG-OPT-EV-1 (ADR-0029): {oe.get('verdict', 'RED')} — "
            f"N_options={oe.get('n_options', 0)}/30  | "
            f"window={oe.get('calendar_days', 'n/a')}d  | win-rate {wr_o_s}  | "
            f"assignments={oe.get('assignment_count', 0)}"
        )
    lines.append(
        "  (realized basis = the canonical kill-switch path; honest forward-only record)"
    )
    return "\n".join(lines)


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
        card = write_run_card(run_dir, home=home)
        if not args.json:
            win = card.get("window_armed_flags")
            print(f"AEGIS run-card written: {run_dir / 'run-card.json'}  (source: {card.get('armed_source')})")
            if win:
                armed = [f for f, v in win.items() if v]
                print(f"  window armed flags (GATE-0 t0={card.get('clean_window_t0')}): {', '.join(armed) or 'NONE'}")
                if card.get("rail_drift"):
                    print(f"  ⚠️  RAIL DRIFT vs window: {list(card['rail_drift'].keys())} — a rail may be disarmed mid-window")
            else:
                live_armed = [f for f, v in card["live_env_flags"].items() if v]
                print(f"  no GATE-0 anchor; live-env armed flags: {', '.join(live_armed) or 'NONE (rails disarmed!)'}")

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
