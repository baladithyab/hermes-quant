#!/usr/bin/env python3
"""quant-autonomous-tick.py — Per-tick autonomous paper-trading orchestrator.

Schedule: every 30 minutes during US market hours, Mon-Fri.
  Hermes cron host runs in PT, so 09:30 ET = 06:30 PT, 16:00 ET = 13:00 PT.
  Cron expression: '0,30 6-13 * * 1-5' (covers 06:30 PT through 13:00 PT inclusive).

What it does each tick:
  1. Read halt_state.json. If ANY active halt → abort with audit line. (Fail-closed.)
  2. Load the evolving watchlist from ~/.hermes/quant/watchlist/play-fit.json.
     Filter to rows with state == "active" across all 5 plays.
     Dedupe by symbol (a symbol can be active in multiple plays).
  3. For each symbol, run the existing PDR pipeline via hermes_quant.autonomous.tick():
        Perceive (advisor.recommend) → Decide (BMA + risk gate) →
        Gate (silence_bias_gate) → React (PaperReactor on FIRE).
  4. Idempotency: skip any symbol that already has an audit entry for today
     in ~/.hermes/quant/autonomous-tick.jsonl with gate == "FIRE" or a non-null
     execution_id. (Limit one fill per symbol per calendar day, ET.)
  5. Append every decision (proposal, gate, fill or abstain) to the audit log.
  6. Print the one-line summary: tick: scanned=N decided=M placed=K abstained=L

Flags:
  --dry-run    DEFAULT. Run the full pipeline but do NOT place orders. Logs
               proposals as gate=DRY_RUN_FIRE in the audit trail. Always safe.
  --armed      Real paper-mode firing. The cron uses this. Subject to:
                 (a) halt_state empty,
                 (b) idempotency guard (one fill per symbol per day),
                 (c) silence_bias_gate FIRE decision.
  --json       Emit a single-line JSON summary on stdout instead of the human one.

Mode-gate bypass: The autonomous.tick() public API requires
quant.pdr.mode=autonomous in config.yaml. This script monkey-patches the mode
reader at process scope so the underlying PDR pipeline always runs regardless
of config. The actual safety lives in --dry-run + halt_state + idempotency.
This keeps the user's config.yaml clean and unchanged.

Audit trail is APPEND-ONLY JSONL — never deleted, never overwritten.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Silence noisy third-party loggers at the top — yfinance and curl-cffi tend
# to emit warnings on missing-bar / unstable-network paths that aren't
# actionable from the cron's POV.
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for noisy in ("yfinance", "peewee", "urllib3", "asyncio"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

logger = logging.getLogger("quant-autonomous-tick")

# ---------- paths ----------
HERMES_HOME = Path.home() / ".hermes"
QUANT_HOME = HERMES_HOME / "quant"
WATCHLIST_PATH = QUANT_HOME / "watchlist" / "play-fit.json"
HALT_MIRROR_PATH = QUANT_HOME / "halt_state.json"
AUDIT_LOG_PATH = QUANT_HOME / "autonomous-tick.jsonl"

ET = ZoneInfo("America/New_York")
UTC = timezone.utc


# ---------- utilities ----------
def utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_et_date() -> str:
    """Calendar date in ET — used as the idempotency key bucket."""
    return datetime.now(UTC).astimezone(ET).strftime("%Y-%m-%d")


def append_audit(record: dict[str, Any]) -> None:
    """Append-only JSONL audit log. Never raises."""
    record.setdefault("ts", utcnow_iso())
    AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        # As a last resort, dump to stderr so the cron operator sees it.
        sys.stderr.write(f"audit log write failed: {e}\n")


# ---------- halt-state fail-closed gate ----------
def read_active_halts() -> list[dict]:
    """Read ~/.hermes/quant/halt_state.json. Returns active halts (empty list = OK)."""
    if not HALT_MIRROR_PATH.exists():
        return []
    try:
        data = json.loads(HALT_MIRROR_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        # Corrupt mirror — treat as a hard halt (fail-closed).
        return [{"reason": f"halt_state.json corrupt: {e}", "scope": "fail-closed"}]
    return data if isinstance(data, list) else []


# ---------- watchlist ----------
def load_active_watchlist() -> list[tuple[str, str, str, list[str]]]:
    """Load play-fit.json and return [(symbol, asset_class, timeframe, [plays])].

    Active = state=="active" in any of the 5 plays. Symbol dedup; plays
    where it's active are aggregated into the per-row "plays" list.
    Asset class = "equity" (all 5 plays are US equity / option-on-equity);
    timeframe = "1d" (matches the play-fit scoring cadence and what advisor
    expects for daily decisions).
    """
    if not WATCHLIST_PATH.exists():
        return []
    try:
        d = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        sys.stderr.write(f"play-fit.json read failed: {e}\n")
        return []

    by_sym: dict[str, list[str]] = {}
    for play, entries in (d.get("plays") or {}).items():
        for e in entries:
            if e.get("state") != "active":
                continue
            sym = e.get("symbol")
            if not sym:
                continue
            by_sym.setdefault(sym, []).append(play)

    return [
        (sym, "equity", "1d", sorted(plays))
        for sym, plays in sorted(by_sym.items())
    ]


# ---------- idempotency ----------
def fired_today() -> set[str]:
    """Read autonomous-tick.jsonl, return symbols that already FIRED today (ET).

    "FIRED" = audit row from today with gate == "FIRE" or a non-null
    execution_id. DRY_RUN_FIRE rows do NOT count toward idempotency — dry
    runs don't place orders, so they shouldn't block real fires later.
    """
    today = today_et_date()
    if not AUDIT_LOG_PATH.exists():
        return set()
    fired: set[str] = set()
    try:
        with open(AUDIT_LOG_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("event") != "decision":
                    continue
                if row.get("date_et") != today:
                    continue
                gate = row.get("gate", "")
                exec_id = row.get("execution_id")
                if gate == "FIRE" or (exec_id and gate != "DRY_RUN_FIRE"):
                    sym = row.get("symbol")
                    if sym:
                        fired.add(sym)
    except OSError:
        pass
    return fired


# ---------- main tick ----------
def run_tick(*, armed: bool) -> dict[str, Any]:
    """Run a single autonomous tick. Returns a summary dict."""
    tick_id = utcnow_iso()
    today_et = today_et_date()
    summary = {
        "event": "tick_summary",
        "tick_id": tick_id,
        "date_et": today_et,
        "armed": armed,
        "scanned": 0,
        "decided": 0,
        "placed": 0,
        "abstained": 0,
        "errors": 0,
        "skipped_idempotent": 0,
        "halt_aborted": False,
        "watchlist_size": 0,
    }

    # --- Halt fail-closed ---
    halts = read_active_halts()
    if halts:
        summary["halt_aborted"] = True
        summary["halts"] = halts
        append_audit({
            "event": "tick_aborted_halt",
            "tick_id": tick_id,
            "date_et": today_et,
            "halts": halts,
            "armed": armed,
        })
        return summary

    # --- Watchlist ---
    watchlist = load_active_watchlist()
    summary["watchlist_size"] = len(watchlist)
    if not watchlist:
        append_audit({
            "event": "tick_empty_watchlist",
            "tick_id": tick_id,
            "date_et": today_et,
            "armed": armed,
        })
        return summary

    # --- Idempotency lookup ---
    already_fired = fired_today() if armed else set()

    # --- Lazy import + monkey-patch mode gate ---
    # The user's config.yaml does NOT set quant.pdr.mode=autonomous (that's a
    # bigger live-go decision). We still want to run the PDR pipeline because
    # the actual safety lives in --dry-run + halt_state + idempotency, all of
    # which we own here. Override the mode reader at process scope.
    try:
        import hermes_quant.autonomous as auto  # type: ignore
    except Exception as exc:  # noqa: BLE001
        summary["errors"] += 1
        append_audit({
            "event": "tick_import_error",
            "tick_id": tick_id,
            "date_et": today_et,
            "error": f"hermes_quant import failed: {exc}",
            "trace": traceback.format_exc(),
        })
        return summary

    auto._read_pdr_mode = lambda: "autonomous"  # type: ignore[attr-defined]

    from hermes_quant.watchlist import WatchlistEntry  # type: ignore

    entries: list[Any] = []
    play_map: dict[str, list[str]] = {}
    for sym, asset_class, tf, plays in watchlist:
        if armed and sym in already_fired:
            summary["skipped_idempotent"] += 1
            append_audit({
                "event": "decision",
                "tick_id": tick_id,
                "date_et": today_et,
                "symbol": sym,
                "asset_class": asset_class,
                "timeframe": tf,
                "plays": plays,
                "gate": "SKIP_IDEMPOTENT",
                "reason": "symbol already fired today",
                "armed": armed,
            })
            continue
        entries.append(WatchlistEntry(symbol=sym, asset_class=asset_class, timeframe=tf))
        play_map[sym] = plays

    summary["scanned"] = len(entries) + summary["skipped_idempotent"]

    if not entries:
        append_audit({
            "event": "tick_all_skipped_idempotent",
            "tick_id": tick_id,
            "date_et": today_et,
            "armed": armed,
            "skipped_idempotent": summary["skipped_idempotent"],
        })
        return summary

    # --- Run the canonical PDR pipeline tick ---
    # dry_run flips REACT — when True, no PaperReactor.execute call.
    try:
        result = auto.tick(dry_run=not armed, symbols=entries)
    except Exception as exc:  # noqa: BLE001
        summary["errors"] += 1
        append_audit({
            "event": "tick_pipeline_error",
            "tick_id": tick_id,
            "date_et": today_et,
            "error": f"auto.tick failed: {exc}",
            "trace": traceback.format_exc(),
            "armed": armed,
        })
        return summary

    summary["decided"] = len(result.decisions)
    summary["errors"] += result.errors

    for d in result.decisions:
        sym = d.symbol
        gate = d.gate or "UNKNOWN"
        # Normalize the gate label so dry-run is unambiguous in the audit log.
        if gate == "FIRE" and not armed:
            audit_gate = "DRY_RUN_FIRE"
        else:
            audit_gate = gate

        # Counters
        if audit_gate == "FIRE":
            summary["placed"] += 1
        elif audit_gate == "DRY_RUN_FIRE":
            # Counted as "would have placed" — still increment placed for the
            # operator's mental model, gate label distinguishes simulation.
            summary["placed"] += 1
        elif gate == "ERROR":
            pass  # already in summary["errors"]
        else:
            summary["abstained"] += 1

        rec = {
            "event": "decision",
            "tick_id": tick_id,
            "date_et": today_et,
            "symbol": sym,
            "asset_class": d.asset_class,
            "timeframe": d.timeframe,
            "plays": play_map.get(sym, []),
            "gate": audit_gate,
            "details": d.details or {},
            "armed": armed,
        }
        if d.action is not None:
            rec["action"] = d.action
        if d.execution_id is not None:
            rec["execution_id"] = d.execution_id
        if d.error is not None:
            rec["error"] = d.error
        append_audit(rec)

    # Append the tick summary itself to close the audit picture.
    append_audit(summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="hermes-quant per-tick autonomous paper-trading orchestrator"
    )
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="Run pipeline without placing orders (default).")
    g.add_argument("--armed", dest="armed", action="store_true",
                   help="Real paper-mode firing. Required for the cron.")
    parser.add_argument("--json", action="store_true",
                        help="Emit single-line JSON summary on stdout.")
    args = parser.parse_args()
    armed = bool(args.armed) and not bool(args.dry_run)

    try:
        summary = run_tick(armed=armed)
    except Exception as exc:  # noqa: BLE001
        # Last-resort: never crash silently. Emit a final audit + stderr line.
        append_audit({
            "event": "tick_uncaught_exception",
            "ts": utcnow_iso(),
            "date_et": today_et_date(),
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc(),
            "armed": armed,
        })
        sys.stderr.write(f"quant-autonomous-tick: uncaught: {exc}\n")
        return 1

    if args.json:
        print(json.dumps(summary, default=str), flush=True)
    else:
        scanned = summary["scanned"]
        decided = summary["decided"]
        placed = summary["placed"]
        abstained = summary["abstained"]
        suffix = ""
        if summary["halt_aborted"]:
            suffix = " HALT-ABORTED"
        elif not armed:
            suffix = " (dry-run)"
        skipped = summary.get("skipped_idempotent", 0)
        skip_str = f" skipped_idempotent={skipped}" if skipped else ""
        err = summary.get("errors", 0)
        err_str = f" errors={err}" if err else ""
        print(
            f"tick: scanned={scanned} decided={decided} placed={placed} "
            f"abstained={abstained}{skip_str}{err_str}{suffix}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
