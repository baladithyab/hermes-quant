#!/usr/bin/env python3
"""quant-research-loop.py — W6 hypothesis→backtest→promote driving cron (ADR-0080).

Schedule (proposed): weekly, AFTER the W3 monthly-meta-retro has had a chance to
seed candidate hypotheses. e.g. `0 8 * * 1` (11:00 ET Mon) — see CRON-REGISTRY.

What it does each cycle (default-OFF behind HERMES_QUANT_RESEARCH_LOOP):
  1. Read halt_state.json. If ANY active halt → abort with audit line. (Fail-closed,
     mirrors quant-autonomous-tick.py.)
  2. Drain W3's `open` candidate hypotheses through the OUTER standard-of-truth:
       deterministic OOS backtest + lookahead sentinel + (for clean validated
       candidates only) the PromotionGate.
  3. PRODUCE reproducible Run-Cards + review-only PromotionRecords + a
     research_loop.jsonl audit row.

The INNER/OUTER rail (QuantAgent / FunSearch), made explicit:
  INNER  (advisory, cheap judge): the candidate hypothesis + its LLM/committee
         strategy. Evidence only.
  OUTER  (standard-of-truth, immutable by this cron): the deterministic backtest +
         lookahead sentinel + PromotionGate. Only this path scores truth.

The cron PRODUCES RunCards + PromotionRecords. It NEVER promotes to live and
NEVER flips a flag — promotion to live influence is an explicit operator action
(ADR-0052; promotion_orchestrator.py:354-360). Default-OFF behind
HERMES_QUANT_RESEARCH_LOOP; with the flag unset it exits 0, silent, writing
nothing (byte-identical off-state, no_agent silence-by-default contract).

Flags:
  --dry-run    DEFAULT. Deterministic StubLLMCommittee-backed strategy; ZERO real
               LLM cost; fully reproducible.
  --armed      Inject a real-LLM research strategy (still NEVER promotes to live).
  --json       Emit a single-line JSON summary on stdout.
  --universe   Comma-separated tickers (default: active watchlist or a research sleeve).
  --max-candidates  Per-cycle cap (default 8; bounded — ADR-0080 §D80.3.4).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_VENV = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if _VENV.exists() and sys.executable != str(_VENV):
    os.execv(str(_VENV), [str(_VENV), __file__, *sys.argv[1:]])

logging.basicConfig(
    level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
for _noisy in ("yfinance", "peewee", "urllib3", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

logger = logging.getLogger("quant-research-loop")

# ---------- paths ----------
HERMES_HOME = Path.home() / ".hermes"
QUANT_HOME = HERMES_HOME / "quant"
WATCHLIST_PATH = QUANT_HOME / "watchlist" / "play-fit.json"
HALT_MIRROR_PATH = QUANT_HOME / "halt_state.json"
RESEARCH_HOME = QUANT_HOME / "research"
RESEARCH_LOOP_LOG_PATH = RESEARCH_HOME / "research_loop.jsonl"

# Default research sleeve when no active watchlist is available. Deliberately a
# small, fixed liquid universe — NEVER tuned to maximise pass-rate.
_DEFAULT_SLEEVE = ["AAPL", "MSFT", "SPY"]


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------- halt-state fail-closed gate (read-only; never writes) ----------
def read_active_halts() -> list[dict]:
    """Read ~/.hermes/quant/halt_state.json. Returns active halts (empty = OK).

    Read-only. The kill-switch is a separate process this runtime cannot
    signal; we only READ it, fail-closed (corrupt → treat as a hard halt).
    Mirrors quant-autonomous-tick.py:read_active_halts.
    """
    if not HALT_MIRROR_PATH.exists():
        return []
    try:
        data = json.loads(HALT_MIRROR_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return [{"reason": f"halt_state.json corrupt: {e}", "scope": "fail-closed"}]
    return data if isinstance(data, list) else []


# ---------- universe ----------
def load_universe(arg_universe: str | None) -> list[str]:
    """Resolve the research universe.

    Precedence: explicit --universe > active watchlist (play-fit.json) > default
    research sleeve.
    """
    if arg_universe:
        return [s.strip().upper() for s in arg_universe.split(",") if s.strip()]
    # Try the active watchlist (same loader pattern as quant-autonomous-tick).
    if WATCHLIST_PATH.exists():
        try:
            d = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
            symbols: list[str] = []
            plays = d.get("plays", d) if isinstance(d, dict) else {}
            if isinstance(plays, dict):
                for _play, rows in plays.items():
                    if not isinstance(rows, list):
                        continue
                    for row in rows:
                        if isinstance(row, dict) and row.get("state") == "active":
                            sym = str(row.get("symbol", "")).upper()
                            if sym and sym not in symbols:
                                symbols.append(sym)
            if symbols:
                return symbols
        except (json.JSONDecodeError, OSError) as e:
            sys.stderr.write(f"play-fit.json read failed: {e}\n")
    return list(_DEFAULT_SLEEVE)


# ---------- the cycle ----------
def run_loop(*, armed: bool, universe: list[str], max_candidates: int) -> dict[str, Any]:
    """Run one research-loop cycle and return a JSON-serialisable summary dict.

    Honors the flag default-OFF + halt fail-closed. The flag is checked inside
    ResearchLoop.run_cycle (byte-identical off-state); the halt check is here so
    the cron aborts before constructing any heavy dependency.
    """
    from hermes_quant.research.hypothesis import HypothesisRegistry
    from hermes_quant.research.orchestrator import HypothesisRunner
    from hermes_quant.research.research_loop import (
        ResearchLoop,
        ResearchLoopSummary,
        flag_on,
    )
    from hermes_quant.research.run_card import RunCardLog

    cycle_ts = utcnow_iso()

    # Flag default-OFF: byte-identical silent no-op (no halt read, no reads,
    # no writes). Mirrors the ResearchLoop off-state at the cron boundary.
    if not flag_on():
        return {"flag_on": False, "halt_aborted": False, "candidates_run": 0}

    # Halt fail-closed BEFORE any work.
    halts = read_active_halts()

    registry = HypothesisRegistry()
    run_card_log = RunCardLog()
    runner = HypothesisRunner(registry=registry, run_card_log=run_card_log)

    strategy_factory = None  # None → ResearchLoop default (Stub, dry-run)
    if armed:
        strategy_factory = _armed_strategy_factory()

    loop = ResearchLoop(
        registry=registry,
        runner=runner,
        strategy_factory=strategy_factory,
    )
    summary: ResearchLoopSummary = loop.run_cycle(
        universe=universe,
        dry_run=not armed,
        max_candidates=max_candidates,
        halts=halts,
    )
    return {
        "flag_on": summary.flag_on,
        "halt_aborted": summary.halt_aborted,
        "candidates_seen": summary.candidates_seen,
        "candidates_run": summary.candidates_run,
        "validated": summary.validated,
        "falsified": summary.falsified,
        "inconclusive": summary.inconclusive,
        "contaminated": summary.contaminated,
        "promotion_records": summary.promotion_records,
        "promotions_recommended": summary.promotions_recommended,
        "errors": summary.errors,
        "cycle_ts": cycle_ts,
        # Documented IN-CODE at the cron boundary too: never auto-promotes.
        "auto_promoted_to_live": False,
    }


def _armed_strategy_factory():
    """Return a real-LLM research strategy_factory for --armed runs.

    Placeholder: even armed, the loop NEVER promotes to live — it only produces
    records. A real LLM-backed strategy plugs in here. For now, fall back to the
    deterministic default (the loop default) to avoid an accidental network
    dependency in a cron environment without keys.
    """
    return None


def _is_transition(summary: dict[str, Any]) -> bool:
    """no_agent change-detecting watchdog: print ONLY on a transition.

    A transition = a candidate ran, a promotion was recommended, a contamination
    fired, an error occurred, or a halt aborted. Otherwise stay silent (empty
    stdout = no Discord message), matching CRON-REGISTRY §0/§2.
    """
    if not summary.get("flag_on"):
        return False
    if summary.get("halt_aborted"):
        return True
    return any(
        summary.get(k, 0)
        for k in (
            "candidates_run",
            "promotions_recommended",
            "contaminated",
            "errors",
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="W6 hypothesis→backtest→promote driving cron (ADR-0080). "
        "Produces Run-Cards + PromotionRecords; NEVER promotes to live."
    )
    g = parser.add_mutually_exclusive_group()
    g.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="DEFAULT. Deterministic StubLLMCommittee strategy; zero LLM cost.",
    )
    g.add_argument(
        "--armed",
        dest="armed",
        action="store_true",
        help="Real-LLM research strategy (still NEVER promotes to live).",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit single-line JSON summary on stdout."
    )
    parser.add_argument(
        "--universe",
        default=None,
        help="Comma-separated tickers; default = active watchlist or research sleeve.",
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=8,
        help="Per-cycle candidate cap (bounded; default 8).",
    )
    args = parser.parse_args()
    armed = bool(args.armed) and not bool(args.dry_run)

    try:
        universe = load_universe(args.universe)
        summary = run_loop(
            armed=armed, universe=universe, max_candidates=args.max_candidates
        )
    except Exception as exc:  # noqa: BLE001
        # Last-resort: never crash silently. Append a final audit row + stderr.
        try:
            RESEARCH_LOOP_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(RESEARCH_LOOP_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "kind": "research_loop_uncaught_exception",
                            "ts": utcnow_iso(),
                            "error": f"{type(exc).__name__}: {exc}",
                            "trace": traceback.format_exc(),
                            "armed": armed,
                        },
                        default=str,
                    )
                    + "\n"
                )
        except OSError:
            pass
        sys.stderr.write(f"quant-research-loop: uncaught: {exc}\n")
        return 1

    if args.json:
        # --json always prints the summary line (machine-readable).
        print(json.dumps(summary, default=str), flush=True)
        return 0

    # no_agent silence-by-default: print ONLY on a transition.
    if _is_transition(summary):
        if summary.get("halt_aborted"):
            print("research-loop: HALT-ABORTED", flush=True)
        else:
            print(
                "research-loop: "
                f"candidates={summary.get('candidates_seen', 0)} "
                f"run={summary.get('candidates_run', 0)} "
                f"validated={summary.get('validated', 0)} "
                f"falsified={summary.get('falsified', 0)} "
                f"inconclusive={summary.get('inconclusive', 0)} "
                f"contaminated={summary.get('contaminated', 0)} "
                f"promo_records={summary.get('promotion_records', 0)} "
                f"promo_recommended={summary.get('promotions_recommended', 0)}",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
