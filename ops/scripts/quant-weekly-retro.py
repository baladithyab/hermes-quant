"""quant-weekly-retro.py — weekly CVRF pattern-mining retro (W2, ADR-0081).

no_agent cron with the change-detecting silence contract (mirrors
quant-catalyst-profitability.py, commit e4ecad5 lineage). Reads the now-live
reflections.jsonl corpus, distills winners-vs-losers (by realized ALPHA) into a
bounded, decaying set of verbal belief-deltas in beliefs.jsonl, and — on a successful
under-budget pass — emits the single missing producer for the
`weekly_retro_promotion_readiness` gate field (closes O3).

PROPOSE-ONLY + DEFAULT-OFF. Gated by HERMES_QUANT_WEEKLY_RETRO=1; with the flag unset
this is a silent no-op (the library is pure, the cron is the flag boundary). It NEVER
touches the risk gate, the hard limits, the discrete sizing ladder, or the kill-switch.

Silence-by-default (no_agent contract): prints to stdout ONLY when a transition occurs
(a belief distilled, a belief expired, the budget-cap state flipped, or
promotion-readiness toggled). Standing state -> empty stdout -> the watchdog stays
silent. --verbose always prints the full belief table (operator on-demand pull).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

_VENV = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if _VENV.exists() and sys.executable != str(_VENV):
    os.execv(str(_VENV), [str(_VENV), __file__, *sys.argv[1:]])

# State baseline for the change-detecting no_agent watchdog (mirrors the catalyst
# profitability probe). Projects {active_belief_count, beliefs_distilled,
# under_budget, promotion_readiness_emitted}.
_BASELINE = Path.home() / ".hermes" / "quant" / "memory" / "weekly-retro-baseline.json"


def _load_baseline() -> dict:
    """Load the watchdog baseline. Missing/corrupt -> {} (first run)."""
    try:
        return json.loads(_BASELINE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_baseline(state: dict) -> None:
    """Persist the watchdog baseline. Best-effort (never raises)."""
    try:
        _BASELINE.parent.mkdir(parents=True, exist_ok=True)
        _BASELINE.write_text(json.dumps(state, sort_keys=True))
    except OSError:
        pass


def _current_state(result) -> dict:
    return {
        "active_belief_count": result.active_belief_count,
        "beliefs_distilled": result.beliefs_distilled,
        "beliefs_expired": result.beliefs_expired,
        "under_budget": result.under_budget,
        "promotion_readiness_emitted": result.promotion_readiness_emitted,
    }


def _transitions(cur: dict, baseline: dict) -> list[str]:
    """Pure state-transition diff: emit a line ONLY when something material changed.

    Standing state (same active count, same budget/readiness state) -> nothing -> the
    cron stays silent (no_agent contract).
    """
    out: list[str] = []
    if not baseline:
        if cur["beliefs_distilled"] or cur["beliefs_expired"]:
            out.append(
                f"initialized: {cur['beliefs_distilled']} distilled, "
                f"{cur['active_belief_count']} active"
            )
        return out
    if cur["active_belief_count"] != baseline.get("active_belief_count"):
        out.append(
            f"active beliefs {baseline.get('active_belief_count')} "
            f"-> {cur['active_belief_count']}"
        )
    if cur["beliefs_expired"]:
        out.append(f"expired {cur['beliefs_expired']} belief(s) this pass")
    if cur["under_budget"] != baseline.get("under_budget"):
        out.append(f"under_budget {baseline.get('under_budget')} -> {cur['under_budget']}")
    if cur["promotion_readiness_emitted"] != baseline.get("promotion_readiness_emitted"):
        out.append(
            "promotion_readiness "
            f"{baseline.get('promotion_readiness_emitted')} "
            f"-> {cur['promotion_readiness_emitted']}"
        )
    return out


def main() -> int:
    # The cron is the flag boundary; the library is pure. Default-OFF: silent no-op.
    if os.environ.get("HERMES_QUANT_WEEKLY_RETRO", "0") != "1":
        return 0  # no_agent contract — unset/0 is a bit-for-bit no-op

    verbose = "--verbose" in sys.argv

    try:
        from hermes_quant.memory.weekly_retro import run_weekly_retro
    except Exception as exc:  # noqa: BLE001
        # Never raise out of a no_agent cron: a traceback would be a false alarm.
        print(f"weekly-retro: import failed ({exc}); skipping", file=sys.stderr)
        return 0

    try:
        result = run_weekly_retro(datetime.now(UTC))
    except Exception as exc:  # noqa: BLE001
        print(f"weekly-retro: pass failed ({exc}); skipping", file=sys.stderr)
        return 0

    if result.n_reflections_read == 0 and result.active_belief_count == 0:
        return 0  # silence-by-default: empty corpus, nothing to say

    cur = _current_state(result)
    baseline = _load_baseline()
    transitions = _transitions(cur, baseline)
    _save_baseline(cur)

    if verbose:
        print(
            "weekly-retro: "
            f"read={result.n_reflections_read} distilled={result.beliefs_distilled} "
            f"expired={result.beliefs_expired} active={result.active_belief_count} "
            f"under_budget={result.under_budget} "
            f"promotion_readiness_emitted={result.promotion_readiness_emitted}"
        )
        for line in result.transitions:
            print(f"  - {line}")
        return 0

    if not transitions:
        return 0  # standing state, unchanged -> silent (no_agent watchdog)

    print("weekly-retro: " + "; ".join(transitions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
