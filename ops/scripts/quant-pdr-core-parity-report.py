#!/usr/bin/env python3
"""quant-pdr-core-parity-report.py — OFFLINE pdr_core-vs-shell parity report.

ADR-0092 Phase-4 (parity proof). The shadow seam
(``hermes_quant.pdr_core_adapter.run_shadow_gate``, flag
``HERMES_QUANT_PDR_CORE_SHADOW=1``) APPENDS one JSONL line per tick to
``<quant_home>/pdr-core-shadow-divergence.jsonl``::

    {"asof": "<UTC ISO>", "diverged": bool, "fields": [..],
     "live": <action primitives | null>, "shadow": <action primitives | null>}

This script READS that log and summarizes how often the ported core gate agrees
with the LIVE shell gate, which Action fields diverge most, and over what window
— so the operator can drive divergences to ZERO before building the cutover
(``HERMES_QUANT_PDR_CORE_LIVE``, which this script does NOT touch). The report is
the evidence the operator weighs; it makes no decision and flips no flag.

SAFETY (money software):
  * READ-ONLY. It opens exactly one file for reading and mutates nothing — no
    write, no rename, no market data, no lookahead.
  * SILENCE-BY-DEFAULT. A missing or empty log => "no divergence records yet",
    exit 0. An absent sample is a valid, honest state (the shadow may simply not
    have run yet), NOT an error.
  * BEST-EFFORT READ. A torn / non-JSON line is skipped line-by-line; the valid
    records still summarize. The harness never crashes on a partial write.

The home is resolved the SAME way the sink writes it: ``hermes_quant.home``'s
``quant_home()`` (precedence: explicit --home override > HERMES_QUANT_HOME >
HERMES_HOME/quant > ~/.hermes/quant), evaluated at call time. An explicit --home
is threaded as the override so the report reads THIS home's own log.

Usage::

    quant-pdr-core-parity-report.py                 # default home, human summary
    quant-pdr-core-parity-report.py --home /tmp/q   # a pinned quant home
    quant-pdr-core-parity-report.py --json          # machine summary

Exit 0 always (a clean / empty sample is a valid outcome, not an error).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

_SHADOW_DIVERGENCE_FILE = "pdr-core-shadow-divergence.jsonl"


def _resolve_log_path(home: str | Path | None) -> Path:
    """Resolve the shadow-divergence log path at call time, home-decouple honest.

    Uses the ADR-0092 ph3 resolver (the SAME one the sink writes through) so a
    threaded --home or an env override (HERMES_QUANT_HOME / HERMES_HOME) reads the
    log the shadow seam actually wrote. quant_home() returns the quant root
    directly (no /quant suffix); the log sits at its top level.
    """
    from hermes_quant.home import quant_home

    return quant_home(home) / _SHADOW_DIVERGENCE_FILE


def summarize_log(log_path: Path) -> dict:
    """Summarize the shadow-divergence JSONL into a parity report dict (pure).

    Returns a machine summary::

        {
          "log_path": str,
          "total": int,            # valid JSONL records read
          "diverged": int,         # records with diverged == True
          "agreed": int,           # total - diverged
          "agreement_rate": float|None,   # agreed/total, None over zero records
          "field_tally": {field: count},  # ranked most-divergent first
          "first_asof": str|None,
          "last_asof": str|None,
        }

    Silence-by-default: a missing or empty log => total 0, agreement_rate None,
    empty field_tally — never crashes. A torn / non-JSON line is skipped.
    """
    total = 0
    diverged = 0
    field_counter: Counter[str] = Counter()
    asofs: list[str] = []

    if log_path.exists():
        try:
            with log_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (ValueError, json.JSONDecodeError):
                        # torn / non-JSON line — skip, best-effort (never crash)
                        continue
                    if not isinstance(rec, dict):
                        continue
                    total += 1
                    if rec.get("diverged"):
                        diverged += 1
                        for f in rec.get("fields") or []:
                            field_counter[str(f)] += 1
                    asof = rec.get("asof")
                    if isinstance(asof, str):
                        asofs.append(asof)
        except OSError:
            # unreadable log => treat as empty (silence-by-default, never crash)
            pass

    agreed = total - diverged
    agreement_rate = round(agreed / total, 6) if total else None

    # rank the field tally most-divergent first (which Action field diverges most)
    field_tally = {
        f: c for f, c in sorted(field_counter.items(), key=lambda kv: (-kv[1], kv[0]))
    }

    return {
        "log_path": str(log_path),
        "total": total,
        "diverged": diverged,
        "agreed": agreed,
        "agreement_rate": agreement_rate,
        "field_tally": field_tally,
        "first_asof": min(asofs) if asofs else None,
        "last_asof": max(asofs) if asofs else None,
    }


def _print_human(summary: dict) -> None:
    print("AEGIS pdr_core parity report (ADR-0092 Phase-4, shadow-vs-live)")
    print(f"  log: {summary['log_path']}")
    if summary["total"] == 0:
        print("  no divergence records yet "
              "(the shadow seam has not appended any samples — clean / un-run).")
        return
    rate = summary["agreement_rate"]
    rate_pct = f"{rate * 100:.2f}%" if rate is not None else "n/a"
    print(f"  records: total={summary['total']}  agreed={summary['agreed']}  "
          f"diverged={summary['diverged']}")
    print(f"  agreement rate: {rate_pct}  ({summary['agreed']}/{summary['total']})")
    print(f"  window: {summary['first_asof']}  ->  {summary['last_asof']}")
    if summary["field_tally"]:
        print("  per-field divergence tally (most-divergent first):")
        for field, count in summary["field_tally"].items():
            print(f"    {field}: {count}")
    else:
        print("  per-field divergence tally: (none — full agreement)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--home",
        default=None,
        help="quant home override (default: HERMES_QUANT_HOME / HERMES_HOME / ~/.hermes/quant)",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="emit the machine summary dict as JSON instead of the human summary",
    )
    args = ap.parse_args(argv)

    log_path = _resolve_log_path(args.home)
    summary = summarize_log(log_path)

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        _print_human(summary)

    # Exit 0 always: an empty / clean sample is a valid, honest outcome — this is
    # a read-only reporter, it never fails on the absence of divergence data.
    return 0


if __name__ == "__main__":
    sys.exit(main())
