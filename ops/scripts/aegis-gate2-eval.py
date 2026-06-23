#!/usr/bin/env python3
"""aegis-gate2-eval.py — evaluate the clean-window options-origination GATE-2 and
write the unlock marker (bf76b / ADR-0099 §C).

GATE-2 is the evidence gate the autonomous tick consults before originating an
options play: ``read_options_unlocked`` (clean_window.py) returns True ONLY when
this script has written ``quant/options_unlock.json`` with ``gate2_cleared: true``.
It is the EXECUTABLE half of the bf76 read-guard — bf76 landed the reader; bf76b
(this) is the writer the eval cron runs.

It REUSES the canonical, already-tested pieces so there is NO metric drift from
what the gate's own tests assert (the same recurring lesson as aegis-gate0-start.py,
which reuses write_clean_window_start):

  read_clean_window_start(home)              -> t0 anchor (pre-t0 trips discarded)
  promotion._settle_paper_round_trips_in_window(t0, now)  -> settled paper book
  clean_window.compute_gate_metrics(round_trips, t0=...)  -> the metric suite
  clean_window.evaluate_gate(metrics, gate_level=2)       -> the GATE-2 verdict

The marker shape is byte-identical to what read_options_unlocked reads
(``{"gate2_cleared": <bool>, "evaluated_at": "<UTC ISO>", ...}``).

FAIL-CLOSED: if t0 is absent, the book is thin, or the gate is NOT cleared, the
marker is written with ``gate2_cleared: false`` (LOCKED) — the conservative,
honest direction. The reader treats absent/malformed/false identically as LOCKED,
so a write failure also leaves origination locked.

SAFETY: this writes one small JSON file under ~/.hermes/quant/. It mutates no money
state and originates no trade — it only records whether the EVIDENCE cleared the gate.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

# cx2-P1 (codex PR#91): the ONE resolver. The GATE-0 anchor, the settled BOOK, and
# the unlock MARKER must all live in the SAME quant root — the one the autonomous
# tick reads via read_options_unlocked() and signal_bus writes the book to. Importing
# clean_window._quant_root (which routes through quant_home(): override >
# HERMES_QUANT_HOME > HERMES_HOME/quant > ~/.hermes/quant) keeps this script and the
# live tick on one resolver, so a multi-home / HERMES_QUANT_HOME-only run can never
# write the marker to a home the tick doesn't read. The bare file name lives here.
from hermes_quant.eval.clean_window import _quant_root

_OPTIONS_UNLOCK_FILE = "options_unlock.json"


def _atomic_write_json(path: Path, payload: dict) -> None:
    """tempfile + fsync + os.replace (mirror clean_window / profile_scan atomic writes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def evaluate_gate2(home: str | Path | None, *, asof: datetime | None = None) -> dict:
    """Compute the GATE-2 verdict over the settled clean-window book.

    Returns the marker payload (NOT yet written). Fail-CLOSED to
    ``gate2_cleared=False`` on any missing input. Pure (no write) so it is unit-
    testable and the caller decides whether to persist.
    """
    from hermes_quant.eval.clean_window import (
        RoundTrip,
        compute_gate_metrics,
        evaluate_gate,
        read_clean_window_start,
    )

    now = asof or datetime.now(tz=UTC)
    t0 = read_clean_window_start(home)
    if t0 is None:
        return {
            "gate2_cleared": False,
            "evaluated_at": now.isoformat(),
            "reason": "no_clean_window_anchor (GATE-0 t0 not stamped)",
            "n": 0,
        }

    # Reuse the canonical settled-book loader (same FIFO matcher as the kill-switch
    # + promotion gate — no basis drift). Best-effort: a read failure => empty book
    # => thin => LOCKED.
    #
    # cx2-P1 (codex PR#91): thread the HOME-SCOPED book via the ONE shared resolver.
    # _quant_root routes through quant_home() (= signal_bus.EXECUTION_BUS_PATH's root),
    # so the GATE-0 anchor, the settled BOOK, and the unlock MARKER all co-locate in
    # the SAME quant root the autonomous tick reads — in every home case (default,
    # HERMES_QUANT_HOME-only, HERMES_HOME, explicit --home). Without it the loader would
    # default to the process EXECUTION_BUS_PATH and could unlock THIS home off another.
    executions_path = _quant_root(home) / "executions.jsonl"
    try:
        from hermes_quant.governance.promotion import _settle_paper_round_trips_in_window

        settled = _settle_paper_round_trips_in_window(t0, now, executions_path=executions_path)
    except Exception as exc:  # noqa: BLE001 — fail-CLOSED: unreadable book => LOCKED
        return {
            "gate2_cleared": False,
            "evaluated_at": now.isoformat(),
            "reason": f"settled_book_read_failed: {exc}",
            "n": 0,
        }

    round_trips = [
        RoundTrip(
            asof_exit=srt.asof_exit,
            realized_return=srt.realized_return,
            is_options=getattr(srt, "asset_class", None) in ("us_option", "option", "multi_leg"),
        )
        for srt in settled
    ]

    metrics = compute_gate_metrics(round_trips, t0=t0)
    cleared = evaluate_gate(metrics, gate_level=2)

    return {
        "gate2_cleared": bool(cleared),
        "evaluated_at": now.isoformat(),
        "t0": t0.isoformat(),
        "n": int(getattr(metrics, "n", len(round_trips))),
        "calendar_days": float(getattr(metrics, "calendar_days", 0.0)),
        "reason": "gate2_cleared" if cleared else "gate2_not_cleared (thin/metrics below threshold)",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--home", default=None, help="operator home (default HERMES_HOME or ~/.hermes)")
    ap.add_argument("--dry-run", action="store_true", help="print the verdict, write nothing")
    args = ap.parse_args(argv)

    quant_root = _quant_root(args.home)
    payload = evaluate_gate2(args.home)

    print("AEGIS GATE-2 — options-origination evidence gate")
    print(f"  quant root: {quant_root}")
    print(f"  n={payload.get('n')}  calendar_days={payload.get('calendar_days', 'n/a')}")
    print(f"  verdict: {'UNLOCKED (gate2_cleared)' if payload['gate2_cleared'] else 'LOCKED'}")
    print(f"  reason: {payload.get('reason')}")

    if args.dry_run:
        print("\nDRY-RUN: would write the marker. Nothing written.")
        return 0

    marker = quant_root / _OPTIONS_UNLOCK_FILE
    _atomic_write_json(marker, payload)
    print(f"\nGATE-2 marker written: {marker}")
    print(
        "  read_options_unlocked() will now return "
        f"{payload['gate2_cleared']} (origination "
        f"{'UNLOCKED' if payload['gate2_cleared'] else 'stays LOCKED'} behind the evidence gate)."
    )
    # Exit 0 always: a LOCKED verdict is a valid, honest outcome (not an error).
    return 0


if __name__ == "__main__":
    sys.exit(main())
