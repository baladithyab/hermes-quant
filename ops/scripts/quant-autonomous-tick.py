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
quant.pdr.mode=autonomous in config.yaml. This script passes
mode_override="autonomous" EXPLICITLY to tick() (Wave 5a) so the underlying PDR
pipeline always runs regardless of config — replacing the old process-scope
monkeypatch of auto._read_pdr_mode (which leaked to every importer in the
process and broke silently on a rename). The actual safety lives in --dry-run +
halt_state + idempotency. This keeps the user's config.yaml clean and unchanged.

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

# ADR-0075: per-symbol admitted_via tag captured from play-fit.json extras during
# watchlist load, copied into each decision audit row for catalyst attribution.
# Empty when no catalyst-onboarded names are active (today's default behavior).
_ADMITTED_VIA: dict[str, str] = {}


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
            # ADR-0075: capture the admitted_via tag (rides WatchlistEntry.extras
            # -> play-fit.json) so the decision audit row attributes catalyst-
            # onboarded trades distinctly. Absent extras -> no tag (today's rows).
            extras = e.get("extras") or {}
            via = extras.get("admitted_via")
            if via:
                _ADMITTED_VIA[sym] = via

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
                if not isinstance(row, dict):
                    continue  # silence-by-default: a valid-JSON non-dict line (corrupt append)
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
        "direction_bias_mismatch": 0,
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

    # --- Lazy import + explicit mode override (Wave 5a) ---
    # The user's config.yaml does NOT set quant.pdr.mode=autonomous (that's a
    # bigger live-go decision). We still want to run the PDR pipeline because
    # the actual safety lives in --dry-run + halt_state + idempotency, all of
    # which we own here. We pass mode_override="autonomous" EXPLICITLY to
    # auto.tick (below) instead of monkey-patching auto._read_pdr_mode at
    # process scope — the old patch mutated module state for every other
    # importer in the process and broke silently if the reader was renamed
    # (Codex P1 3a). config.yaml stays untouched.
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

    from hermes_quant.watchlist import WatchlistEntry  # type: ignore

    # B04 / A5 direction-vs-play-bias guard. The advisor's signal is propagated
    # through whichever play the symbol is ELIGIBLE for, but the plays are not
    # all direction-agnostic: covered_call / csp / wheel / leaps are BULLISH-bias
    # structures. A SHORT signal routed through e.g. csp is structurally
    # incoherent (the live AXP defect). We screen direction-vs-bias BEFORE any
    # React fires by neutralizing the advisor result for incompatible symbols.
    try:
        from hermes_quant.playbook.direction_bias import direction_play_compatible
    except Exception:  # noqa: BLE001
        # Silence-by-default: if the predicate can't be imported, treat ALL
        # signals as incompatible so nothing fires through a possibly-wrong play.
        def direction_play_compatible(direction: int, plays: list[str]) -> bool:  # type: ignore[misc]
            return False

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

    # --- Direction-vs-play-bias screen (B04 / A5) ---
    # Sentinel that tags a recommendation neutralized purely because the
    # advisor's direction was incompatible with EVERY eligible play's bias.
    # The downstream advisor risk_gate uses this as gated_reason; the audit
    # loop maps it to gate=DIRECTION_BIAS_MISMATCH (NOT a generic silence).
    direction_bias_gated_reason = "direction_bias_mismatch"

    # DEFAULT-OFF behind a flag, matching the repo pattern for restrictive gates
    # (cf. HERMES_QUANT_PORTFOLIO_CAPS in autonomous.py). The screen can only
    # abstain MORE (never fires/widens/flips), so it is money-safe either way —
    # but flagging it gives an instant observe-only rollout + rollback switch and
    # keeps the AXP-style fix reversible if it ever over-abstains in production.
    _direction_bias_gate_on = (
        os.environ.get("HERMES_QUANT_DIRECTION_BIAS_GATE", "0") == "1"
    )

    from hermes_quant.advisor import recommend as _base_recommend

    def _direction_screened_recommend(**kwargs: Any) -> dict[str, Any]:
        """advisor.recommend wrapper that neutralizes a recommendation when its
        direction can't structurally route through any of the symbol's eligible
        plays. Neutralization sets risk_gate.pass=False so auto.tick never fires
        the React — the order is genuinely prevented, not relabeled after the
        fact. No-op (returns the advisor result untouched) when the flag is OFF.

        ADR-0079 PDR-1 / M17: this wrapper is now a PURE post-processor — it no
        longer injects semantic packets. The PerceptionFrame is built ONCE inside
        autonomous.tick (the producer BOTH this cron and the quant_autonomous_tick
        TOOL path reach), so the tool path perceives the same frame the cron does.
        kwargs (including any perception_frame=) are forwarded to recommend
        verbatim."""
        res = _base_recommend(**kwargs)
        if not _direction_bias_gate_on:
            return res
        sym = kwargs.get("symbol")
        plays = play_map.get(sym, [])
        sig = (res or {}).get("aggregated_signal") or {}
        try:
            direction = int(sig.get("direction", 0))
        except (TypeError, ValueError):
            direction = 0
        # Only screen signals the advisor would actually act on. If direction is
        # 0 or the advisor already gated, leave the result untouched (it won't
        # fire anyway) so we don't mislabel an unrelated silence.
        prior_gate = (res or {}).get("risk_gate") or {}
        already_gated = prior_gate.get("pass") is False
        if direction != 0 and not already_gated and not direction_play_compatible(direction, plays):
            res["risk_gate"] = {
                "pass": False,
                "gated_reason": direction_bias_gated_reason,
                "direction": direction,
                "eligible_plays": plays,
            }
        elif direction != 0 and already_gated and not direction_play_compatible(direction, plays):
            # Already silenced upstream for another reason AND bias-incompatible:
            # preserve the upstream cause so the audit trail records both, rather
            # than masking the original silence with the bias label.
            res["risk_gate"] = {
                **prior_gate,
                "gated_reason": direction_bias_gated_reason,
                "prior_gated_reason": prior_gate.get("gated_reason"),
                "direction": direction,
                "eligible_plays": plays,
            }
        return res

    # --- Run the canonical PDR pipeline tick ---
    # dry_run flips REACT — when True, no PaperReactor.execute call.
    # mode_override="autonomous" (Wave 5a) bypasses the config mode gate
    # explicitly, replacing the old auto._read_pdr_mode monkeypatch.
    try:
        result = auto.tick(
            dry_run=not armed,
            symbols=entries,
            advisor_recommend=_direction_screened_recommend,
            mode_override="autonomous",
        )
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
        details = d.details or {}
        # B04 / A5: a signal neutralized purely because its direction was
        # incompatible with every eligible play's bias surfaces from auto.tick
        # as SILENCE_GATED_BY_ADVISOR with our sentinel gated_reason. Relabel it
        # so the audit trail names the real cause, distinct from generic silence.
        direction_bias_mismatch = (
            gate == "SILENCE_GATED_BY_ADVISOR"
            and details.get("gated_reason") == direction_bias_gated_reason
        )
        if direction_bias_mismatch:
            audit_gate = "DIRECTION_BIAS_MISMATCH"
        # Normalize the gate label so dry-run is unambiguous in the audit log.
        elif gate == "FIRE" and not armed:
            audit_gate = "DRY_RUN_FIRE"
        else:
            audit_gate = gate

        # Counters
        if audit_gate == "DIRECTION_BIAS_MISMATCH":
            # Did NOT fire and won't — count as abstained + a dedicated tally.
            summary["abstained"] += 1
            summary["direction_bias_mismatch"] += 1
        elif audit_gate == "FIRE":
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
            "details": details,
            "armed": armed,
        }
        if d.action is not None:
            rec["action"] = d.action
        if d.execution_id is not None:
            rec["execution_id"] = d.execution_id
        if d.error is not None:
            rec["error"] = d.error
        # ADR-0075: attribute catalyst-onboarded trades distinctly in the audit
        # trail. Only present when the symbol carries an admitted_via tag.
        via = _ADMITTED_VIA.get(sym)
        if via:
            rec["admitted_via"] = via
        append_audit(rec)

    # Append the tick summary itself to close the audit picture.
    append_audit(summary)
    return summary


def _format_human_summary(summary: dict, *, armed: bool) -> str:
    """Format a tick_summary dict as Discord-friendly markdown.

    Returns "" (empty) when the run was both unremarkable AND uneventful —
    the wrapper's no_agent=True semantics treat empty stdout as silent, so
    routine "scanned 119, fired 0, all silenced" ticks don't spam Discord.

    Returns a non-empty multi-line string when anything notable happened:
      - placed > 0 (a paper trade fired) → 📈 lead with symbol(s)
      - halt_aborted → 🚨 lead with halt warning
      - errors > 0 → ⚠️ lead with error count
      - skipped_idempotent > 0 OR per-tick-cap-hit → context line so the
        operator knows the system saw signals it didn't fire on
    Otherwise the heartbeat is silent.
    """
    scanned = summary.get("scanned", 0)
    decided = summary.get("decided", 0)
    placed = summary.get("placed", 0)
    abstained = summary.get("abstained", 0)
    errors = summary.get("errors", 0)
    skipped = summary.get("skipped_idempotent", 0)
    halt_aborted = summary.get("halt_aborted", False)
    watchlist_size = summary.get("watchlist_size", scanned)
    tick_id = summary.get("tick_id", "")
    asof = tick_id[:16].replace("T", " ") + " UTC" if tick_id else ""
    mode = "📦 paper" if armed else "🧪 dry-run"

    # Decide whether to speak at all.
    notable = bool(placed or halt_aborted or errors)
    has_capped_signals = False
    capped_symbols: list[tuple[str, str]] = []  # (symbol, play) of would-have-fired-but-capped
    placed_symbols: list[tuple[str, str, float]] = []  # (symbol, play, target_pct)

    # Enrich with recent decision rows from autonomous-tick.jsonl for this tick_id.
    if tick_id:
        try:
            for line in AUDIT_LOG_PATH.read_text().splitlines()[-300:]:
                if not line.strip() or tick_id not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("event") != "decision":
                    continue
                if rec.get("tick_id") != tick_id:
                    continue
                gate = rec.get("gate")
                sym = rec.get("symbol", "?")
                plays = rec.get("plays", []) or []
                play = plays[0] if plays else "?"
                if gate in ("FIRE", "DRY_RUN_FIRE"):
                    action = rec.get("action") or {}
                    placed_symbols.append((sym, play, float(action.get("target_position_pct", 0.0))))
                elif gate and "PER_TICK_CAP" in gate:
                    has_capped_signals = True
                    capped_symbols.append((sym, play))
        except Exception:
            pass  # Enrichment is best-effort; never block summary delivery on parse failures.

    if has_capped_signals:
        notable = True

    if not notable:
        return ""  # silence-by-default heartbeat

    lines: list[str] = []

    # Headline
    if halt_aborted:
        lines.append(f"🚨 **autonomous-tick HALT-ABORTED** ({mode}, {asof})")
        lines.append("> Active halts present in halt_state.json — fail-closed. No fires this tick.")
    elif placed > 0:
        if len(placed_symbols) == 1:
            sym, play, pct = placed_symbols[0]
            side = "SHORT" if pct < 0 else "LONG"
            verb = "would have fired" if not armed else "fired"
            lines.append(
                f"📈 **autonomous-tick: 1 paper trade {verb}** "
                f"({sym} {side} {abs(pct):.0%} via `{play}`, {mode}, {asof})"
            )
        else:
            verb = "would have fired" if not armed else "fired"
            lines.append(
                f"📈 **autonomous-tick: {placed} paper trades {verb}** "
                f"({mode}, {asof})"
            )
            for sym, play, pct in placed_symbols[:5]:
                side = "SHORT" if pct < 0 else "LONG"
                lines.append(f"  • {sym} {side} {abs(pct):.0%} via `{play}`")
    elif errors > 0:
        lines.append(f"⚠️ **autonomous-tick: {errors} error(s)** ({mode}, {asof})")
    elif has_capped_signals:
        # Notable but no fire happened: operator should know we saw real signals.
        n = len(capped_symbols)
        sym_list = ", ".join(f"{s}/{p}" for s, p in capped_symbols[:5])
        lines.append(
            f"🔕 **autonomous-tick: {n} signal(s) capped** ({mode}, {asof})"
        )
        lines.append(
            f"> Per-tick open cap reached after the first fire. Held back: {sym_list}"
            + (f" (+{n - 5} more)" if n > 5 else "")
        )

    # Body — tight stat line
    body_parts: list[str] = []
    body_parts.append(f"watchlist={watchlist_size}")
    body_parts.append(f"scanned={scanned}")
    body_parts.append(f"decided={decided}")
    body_parts.append(f"placed={placed}")
    body_parts.append(f"abstained={abstained}")
    if skipped:
        body_parts.append(f"idempotent_skipped={skipped}")
    if errors:
        body_parts.append(f"errors={errors}")
    lines.append("```")
    lines.append(" ".join(body_parts))
    lines.append("```")

    return "\n".join(lines)


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
                        help="Emit single-line JSON summary on stdout (debug).")
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
        # Still emit a loud Discord-side alert.
        print(
            f"⚠️ **autonomous-tick crashed**: `{type(exc).__name__}: {exc}` "
            f"(see ~/.hermes/quant/autonomous-tick.jsonl for trace)",
            flush=True,
        )
        return 1

    if args.json:
        # Debug path — preserve raw shape for investigation.
        print(json.dumps(summary, default=str), flush=True)
    else:
        msg = _format_human_summary(summary, armed=armed)
        if msg:
            print(msg, flush=True)
        # Empty stdout when uneventful → no_agent silent semantics.
    return 0


if __name__ == "__main__":
    sys.exit(main())
