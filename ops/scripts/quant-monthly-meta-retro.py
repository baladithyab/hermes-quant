"""quant-monthly-meta-retro.py — T3 monthly meta-retro (W3, ADR-0080 / ADR-0081 §3). Default-OFF.

no_agent=True, silence-by-default. When HERMES_QUANT_MONTHLY_META_RETRO != 1 this script
is a no-op (returns 0, EMPTY stdout) so the off-state is byte-identical (gate condition 4c).

PROPOSES ONLY: aggregates the W2 weekly belief digests + research_debate audit rows (O7) +
promotion records, registers CANDIDATE hypotheses status='open' (never run), and emits
persona-calibration TELEMETRY inside the report. It never promotes, never touches a limit,
a size, the risk gate, or the kill-switch. The deterministic OOS backtest + promotion gate
+ operator sign-off stay the SOLE path to live policy (zero auto-promotion to live).

Silence-by-default (no_agent contract): announces only DELTAS (new candidates / promotions
/ expiries). Standing state -> empty stdout -> the watchdog stays silent.
"""
from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

_VENV = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if _VENV.exists() and sys.executable != str(_VENV):
    os.execv(str(_VENV), [str(_VENV), __file__, *sys.argv[1:]])


def main() -> int:
    # The cron is the flag boundary; the library is pure. Default-OFF: silent no-op.
    if os.environ.get("HERMES_QUANT_MONTHLY_META_RETRO", "0") != "1":
        return 0  # OFF -> byte-identical no-op, empty stdout (no Discord message)

    verbose = "--verbose" in sys.argv

    try:
        from hermes_quant.memory.meta_retro import run_meta_retro
    except Exception as exc:  # noqa: BLE001
        # Never raise out of a no_agent cron: a traceback would be a false alarm.
        print(f"monthly-meta-retro: import failed ({exc}); skipping", file=sys.stderr)
        return 0

    try:
        report = run_meta_retro(
            asof=datetime.now(UTC),
            register_candidates=True,  # only reached when the flag is on
        )
    except Exception as exc:  # noqa: BLE001
        print(f"monthly-meta-retro: pass failed ({exc}); skipping", file=sys.stderr)
        return 0

    deltas = (
        len(report.candidate_hypotheses)
        + len(report.beliefs_promoted)
        + len(report.beliefs_expired)
    )

    if verbose:
        print(
            "monthly-meta-retro: "
            f"trends={len(report.lesson_category_trends)} "
            f"personas={len(report.persona_calibration)} "
            f"candidates={len(report.candidate_hypotheses)} "
            f"promoted={len(report.beliefs_promoted)} "
            f"expired={len(report.beliefs_expired)} "
            f"promotion_readiness_flips={report.promotion_readiness_flips} "
            f"(config_hash={report.config_hash[:12]})"
        )
        return 0

    if deltas == 0:
        return 0  # standing state, nothing material changed -> silent (no_agent watchdog)

    print(
        f"📅 monthly-meta-retro: {len(report.candidate_hypotheses)} candidate hyp, "
        f"{len(report.beliefs_promoted)} promoted, {len(report.beliefs_expired)} expired "
        f"(config_hash={report.config_hash[:12]})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
