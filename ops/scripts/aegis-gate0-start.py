#!/usr/bin/env python3
"""aegis-gate0-start.py — write the clean-window t0 anchor (ADR-0099 GATE-0).

GATE-0 is the operator action that starts the clean-window clock: reset the paper
book, ARM the protective flags, confirm the slippage model + haircut, and stamp the
t0 anchor that compute_gate_metrics filters on (pre-t0 trips are DISCARDED). This
script writes ONLY the anchor — it does NOT reset the book or edit the cron wrapper
(those stay operator-run: quant-reset-paper-book.py + editing the armed wrapper).

It REUSES hermes_quant.eval.clean_window.write_clean_window_start so the anchor JSON
shape is byte-identical to what read_clean_window_start expects (no contract drift):
  {"t0": "<UTC ISO>", "armed_flags": {<the flag snapshot>}, ...}

The t0 it stamps is wall-clock NOW (the moment GATE-0 completes), and the armed_flags
snapshot is read from the CURRENT process env — so run this AFTER you have exported
the armed flags (e.g. `source ~/.hermes/scripts/quant-autonomous-tick-armed.sh`-style
exports, or inline) so the run-card honestly records what was armed.

SAFETY: this writes one small JSON file under ~/.hermes/quant/. It mutates no money
state. It will OVERWRITE a prior anchor (a reset is an explicit operator action).
It REFUSES to stamp t0 unless the four protective flags are armed in the env, UNLESS
--force is passed (so an honest run-card can't silently record a disarmed window).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# The flags that MUST be armed for a clean window to be meaningful (ADR-0099 GATE-0 +
# the safety-rails runbook). PER_POSITION_STOP + TAKE_PROFIT_SWEEP + the slippage
# haircut are the ones this session added; the rest are the pre-existing rails.
_REQUIRED_ARMED = [
    "HERMES_QUANT_DURABLE_DRAWDOWN_BASELINE",
    "HERMES_QUANT_PER_POSITION_STOP",
    "HERMES_QUANT_DELTA_NORMALIZER",
    "HERMES_QUANT_ACCOUNT_LOCK",
]
# Recommended-but-not-blocking (warn if absent).
_RECOMMENDED = [
    "HERMES_QUANT_TAKE_PROFIT_SWEEP",
    "HERMES_QUANT_SLIPPAGE_HAIRCUT",
    "HERMES_QUANT_POST_LOSS_COOLDOWN",
    "HERMES_QUANT_PAPER_SLIPPAGE_MODEL",
]
# The full snapshot recorded in the run-card (everything that shapes the window).
_SNAPSHOT_FLAGS = _REQUIRED_ARMED + _RECOMMENDED + [
    "HERMES_QUANT_PORTFOLIO_CAPS",
    "HERMES_QUANT_DETERMINISTIC_EQUITY",
    "HERMES_QUANT_REFLECTION",
]


def _armed(flag: str) -> bool:
    v = os.environ.get(flag)
    # PAPER_SLIPPAGE_MODEL is a value flag (e.g. "v0.2"), the rest are "1".
    if flag == "HERMES_QUANT_PAPER_SLIPPAGE_MODEL":
        return bool(v)
    return v == "1"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", default=None, help="operator home (default ~/.hermes)")
    ap.add_argument("--force", action="store_true",
                    help="stamp t0 even if a required flag is not armed (records the disarmed state honestly)")
    ap.add_argument("--dry-run", action="store_true", help="print what WOULD be written, write nothing")
    args = ap.parse_args(argv)

    # Resolve the home the clean_window module reads from (it appends quant/clean_window_start.json).
    home = Path(args.home) if args.home else Path.home() / ".hermes"

    missing = [f for f in _REQUIRED_ARMED if not _armed(f)]
    rec_missing = [f for f in _RECOMMENDED if not _armed(f)]
    snapshot = {f: os.environ.get(f) for f in _SNAPSHOT_FLAGS}

    print("AEGIS GATE-0 — clean-window anchor")
    print(f"  home: {home}")
    print(f"  required-armed flags: {'ALL ARMED' if not missing else 'MISSING ' + ', '.join(missing)}")
    if rec_missing:
        print(f"  recommended flags NOT set (warn): {', '.join(rec_missing)}")
    print(f"  flag snapshot: { {k: v for k, v in snapshot.items() if v} }")

    if missing and not args.force:
        print(
            "\nREFUSING to stamp t0: a required protective flag is not armed "
            "(arm it in the cron wrapper / export it, then re-run; or pass --force to "
            "record a disarmed window honestly). A clean window measured with the rails "
            "DISARMED is not a valid test.",
            file=sys.stderr,
        )
        return 2

    if args.dry_run:
        print("\nDRY-RUN: would write the t0 anchor + the armed_flags snapshot. Nothing written.")
        return 0

    # Reuse the canonical writer so the JSON shape matches read_clean_window_start.
    from hermes_quant.eval.clean_window import write_clean_window_start

    t0 = datetime.now(tz=timezone.utc)
    path = write_clean_window_start(home, t0, armed_flags=snapshot)
    print(f"\nGATE-0 anchor written: {path}")
    print(f"  t0 = {t0.isoformat()}  (all round-trips BEFORE this are discarded)")
    print("  The clean-window clock has started. Let the armed crons run; track with")
    print("  aegis-run-snapshot.py and the GATE-1/2/3 metrics (eval/clean_window.py).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
