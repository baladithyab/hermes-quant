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

    quant-pdr-core-parity-report.py                      # gate (DECIDE) layer, human summary
    quant-pdr-core-parity-report.py --layer aggregate    # aggregate (PERCEIVE) layer
    quant-pdr-core-parity-report.py --home /tmp/q        # a pinned quant home
    quant-pdr-core-parity-report.py --json               # machine summary

The aggregate-layer log marks a tick ``comparable: false`` when the live BMA
diverges from the cold-start core port BY DESIGN (a fitted isotonic calibrator or
a set learning flag); those ticks are EXCLUDED from the agreement rate so a
faithful port is not slandered. The gate log has no ``comparable`` key (every
record counts), so the gate report is byte-identical to the prior behavior.

Exit 0 always (a clean / empty sample is a valid outcome, not an error).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

# ADR-0092 Phase-4 shadow-divergence logs, one per onboarded PDR layer. The DECIDE
# (gate) layer's run_shadow_gate writes the first; the PERCEIVE (aggregate) layer's
# run_shadow_aggregate writes the second. --layer selects which the report reads.
_SHADOW_DIVERGENCE_FILES = {
    "gate": "pdr-core-shadow-divergence.jsonl",
    "aggregate": "pdr-core-shadow-aggregate-divergence.jsonl",
}
_SHADOW_DIVERGENCE_FILE = _SHADOW_DIVERGENCE_FILES["gate"]  # back-compat default


def _resolve_log_path(home: str | Path | None, layer: str = "gate") -> Path:
    """Resolve the shadow-divergence log path at call time, home-decouple honest.

    Uses the ADR-0092 ph3 resolver (the SAME one the sink writes through) so a
    threaded --home or an env override (HERMES_QUANT_HOME / HERMES_HOME) reads the
    log the shadow seam actually wrote. quant_home() returns the quant root
    directly (no /quant suffix); the log sits at its top level. ``layer`` selects
    the gate (DECIDE) or aggregate (PERCEIVE) divergence log.
    """
    from hermes_quant.home import quant_home

    return quant_home(home) / _SHADOW_DIVERGENCE_FILES[layer]


def summarize_log(log_path: Path) -> dict:
    """Summarize the shadow-divergence JSONL into a parity report dict (pure).

    Returns a machine summary::

        {
          "log_path": str,
          "total": int,            # valid JSONL records read
          "comparable": int,       # records on the parity-valid path (comparable != False)
          "not_comparable": int,   # records skipped by-design (fitted calibrator / learning flag)
          "diverged": int,         # comparable records with diverged == True
          "agreed": int,           # comparable - diverged
          "agreement_rate": float|None,   # agreed/comparable, None over zero comparable
          "field_tally": {field: count},  # ranked most-divergent first
          "not_comparable_reasons": {reason: count},  # why ticks were skipped
          "first_asof": str|None,
          "last_asof": str|None,
        }

    The agreement rate is over COMPARABLE records only — the aggregate-layer log
    marks ``comparable: false`` for a tick where the live aggregator diverges from
    the cold-start core port BY DESIGN (a fitted isotonic calibrator or a set
    learning flag), so counting those as ``diverged`` would slander the port. The
    gate log carries no ``comparable`` key => every gate record defaults comparable
    (byte-identical to the prior gate-only behavior).

    Silence-by-default: a missing or empty log => total 0, agreement_rate None,
    empty tallies — never crashes. A torn / non-JSON line is skipped.
    """
    total = 0
    comparable = 0
    not_comparable = 0
    diverged = 0
    field_counter: Counter[str] = Counter()
    reason_counter: Counter[str] = Counter()
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
                    # comparable defaults True (the gate log has no such key); only
                    # an explicit comparable==False (aggregate by-design skip) counts
                    # as not-comparable and is EXCLUDED from the agreement rate.
                    if rec.get("comparable", True) is False:
                        not_comparable += 1
                        reason_counter[str(rec.get("reason") or "unspecified")] += 1
                    else:
                        comparable += 1
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

    agreed = comparable - diverged
    agreement_rate = round(agreed / comparable, 6) if comparable else None

    # rank the field tally most-divergent first (which signal field diverges most)
    field_tally = {
        f: c for f, c in sorted(field_counter.items(), key=lambda kv: (-kv[1], kv[0]))
    }
    not_comparable_reasons = {
        r: c for r, c in sorted(reason_counter.items(), key=lambda kv: (-kv[1], kv[0]))
    }

    return {
        "log_path": str(log_path),
        "total": total,
        "comparable": comparable,
        "not_comparable": not_comparable,
        "diverged": diverged,
        "agreed": agreed,
        "agreement_rate": agreement_rate,
        "field_tally": field_tally,
        "not_comparable_reasons": not_comparable_reasons,
        "first_asof": min(asofs) if asofs else None,
        "last_asof": max(asofs) if asofs else None,
    }


def _print_human(summary: dict, layer: str) -> None:
    print(f"AEGIS pdr_core parity report (ADR-0092 Phase-4, shadow-vs-live) — layer: {layer}")
    print(f"  log: {summary['log_path']}")
    if summary["total"] == 0:
        print("  no divergence records yet "
              "(the shadow seam has not appended any samples — clean / un-run).")
        return
    rate = summary["agreement_rate"]
    rate_pct = f"{rate * 100:.2f}%" if rate is not None else "n/a"
    print(f"  records: total={summary['total']}  comparable={summary['comparable']}  "
          f"not_comparable={summary['not_comparable']}")
    print(f"  comparable: agreed={summary['agreed']}  diverged={summary['diverged']}")
    print(f"  agreement rate: {rate_pct}  ({summary['agreed']}/{summary['comparable']} comparable)")
    print(f"  window: {summary['first_asof']}  ->  {summary['last_asof']}")
    if summary["field_tally"]:
        print("  per-field divergence tally (most-divergent first):")
        for field, count in summary["field_tally"].items():
            print(f"    {field}: {count}")
    else:
        print("  per-field divergence tally: (none — full agreement on comparable ticks)")
    if summary["not_comparable_reasons"]:
        print("  not-comparable (by-design skip) reasons:")
        for reason, count in summary["not_comparable_reasons"].items():
            print(f"    {reason}: {count}")


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
        "--layer",
        default="gate",
        choices=sorted(_SHADOW_DIVERGENCE_FILES),
        help="which PDR layer's shadow log to summarize: gate (DECIDE, default) "
             "or aggregate (PERCEIVE)",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="emit the machine summary dict as JSON instead of the human summary",
    )
    args = ap.parse_args(argv)

    log_path = _resolve_log_path(args.home, args.layer)
    summary = summarize_log(log_path)

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        _print_human(summary, args.layer)

    # Exit 0 always: an empty / clean sample is a valid, honest outcome — this is
    # a read-only reporter, it never fails on the absence of divergence data.
    return 0


if __name__ == "__main__":
    sys.exit(main())
