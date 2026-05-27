#!/usr/bin/env python3
"""Per-play evolving watchlist tick (CLI shim).

Reads the universe at ~/.hermes/quant/universe/alpaca-daily.json, runs the
play-fitness scorers against it, and atomic-writes the evolved watchlist
state to ~/.hermes/quant/watchlist/play-fit.json. Append-only audit log
at ~/.hermes/quant/watchlist/journal.jsonl.

USAGE
-----
    ~/.hermes/scripts/quant-watchlist-evolve.py

CRON SUGGESTION
---------------
Run weekdays at 06:45 America/New_York — after the universe scanner
completes at 06:30 ET — so the watchlist is fresh before the open. Cron
in UTC:

    45 11 * * 1-5  source $HOME/.hermes/secrets/alpaca.env && \\
        $HOME/.hermes/hermes-agent/venv/bin/python3 \\
        $HOME/.hermes/scripts/quant-watchlist-evolve.py \\
        >> $HOME/.hermes/quant/watchlist/evolve.log 2>&1

POSTURE
-------
Silence-by-default: if the universe file is missing the script logs a
warning and exits 0 without modifying any state. The scorer is
dependency-injected — when ``hermes_quant.playbook.scorers`` is available
it is used; otherwise a stub-0.5 scorer keeps the loop running without
onboarding anything (0.5 < 0.65 default floor).

Stdout is silent when no events fire; only summary on changes.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Re-exec under the hermes-agent venv if needed (where hermes_quant is installed).
HERMES_VENV_PY = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if HERMES_VENV_PY.exists() and sys.executable != str(HERMES_VENV_PY):
    os.execv(str(HERMES_VENV_PY), [str(HERMES_VENV_PY), __file__, *sys.argv[1:]])

# Best-effort: source alpaca creds from ~/.hermes/secrets/alpaca.env so that
# any data-dependent scorer can find them. The evolution function itself
# does NOT call alpaca/yfinance — but the injected scorer might.
SECRETS = Path.home() / ".hermes" / "secrets" / "alpaca.env"
if SECRETS.exists() and not (
    os.environ.get("ALPACA_API_KEY") or os.environ.get("ALPACA_API_KEY_ID")
):
    for line in SECRETS.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" in line:
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")  # bash strips shell quotes; Python doesn't
            os.environ.setdefault(k.strip(), v)

logging.basicConfig(
    level=os.environ.get("HERMES_QUANT_LOG_LEVEL", "WARNING"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


from hermes_quant.playbook.watchlist_evolution import evolve_watchlist  # noqa: E402

# Try to import the real scorer. If the scorers module isn't ready yet, fall
# back to None — evolve_watchlist will use its silent stub. This keeps the
# cron green during the transition.
try:
    from hermes_quant.playbook.scorers import score_symbol  # type: ignore[attr-defined]

    _scorer = score_symbol
except (ImportError, AttributeError):
    _scorer = None


def main() -> int:
    # 2026-05-27 hardening (P0-3 from parallel-critique review): if the
    # upstream universe file is stale (>25h old) the entire downstream
    # chain (watchlist → brief → playbook-tick) operates on yesterday's
    # data with no warning. Fail loudly instead — surface the stale-input
    # condition to the cron's stderr so the agent-deliver path catches it.
    universe_path = Path.home() / ".hermes" / "quant" / "universe" / "alpaca-daily.json"
    if universe_path.exists():
        age_hours = (
            datetime.now(timezone.utc).timestamp() - universe_path.stat().st_mtime
        ) / 3600.0
        if age_hours > 25.0:
            print(
                f"WARNING: universe file is {age_hours:.1f}h old (> 25h); "
                f"watchlist evolution skipped to avoid stale-data cascade. "
                f"Path: {universe_path}",
                file=sys.stderr,
            )
            return 1
    summary = evolve_watchlist(scorer=_scorer)

    # Silence-by-default: only print if something happened.
    if summary["events_written"] == 0 and all(
        v["n_active"] == 0 for v in summary["per_play"].values()
    ):
        return 0

    print(f"as_of={summary['as_of']} events={summary['events_written']}")
    for play, stats in summary["per_play"].items():
        if stats["n_active"] or stats["n_onboarded_today"] or stats["n_evicted_today"]:
            top = ", ".join(f"{s}:{sc:.2f}" for s, sc in stats["top5"])
            print(
                f"  {play:14s} active={stats['n_active']:3d} "
                f"+{stats['n_onboarded_today']:2d} -{stats['n_evicted_today']:2d}  "
                f"top5: {top}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
