"""hermes_quant.governance.audit_log_query — Read-only predicates over the
governance audit log (ADR-0039).

This module provides canonical predicates and counters operators can run
against `~/.hermes/quant/governance/audit_log.jsonl` to detect known
failure modes (BMA degeneracy, gate-pass coverage gaps, schema-version
mismatch). Predicates are the audit-trail-only replacement for out-of-band
`recommend()` reprobe scripts.

Per ADR-0039: the canonical degeneracy discriminator is
`is_bma_degenerate(event)`. Operators run it via the CLI form below or
import it directly into incident-response notebooks.

CLI:
    python -m hermes_quant.governance.audit_log_query degenerate
    # → prints all gate_approval rows where is_bma_degenerate is True

The module is read-only. It never mutates the audit log. Append-only
discipline (ADR-0031) is preserved by construction — there is no write
path here.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_AUDIT_LOG_PATH = Path.home() / ".hermes" / "quant" / "governance" / "audit_log.jsonl"


def is_bma_degenerate(event: dict[str, Any]) -> bool:
    """Canonical predicate: True iff this event is the BMA n=1 collapse signature.

    Per ADR-0039, the n=1 collapse fires when the BMA aggregator emits a
    signal with a single distinct analyst voice and the calibrator returns
    confidence=1.00 — the surface signature of the 2026-05-26 incident.

    A `gate_approval` event carrying:
        signal_provenance.aggregator_class == "BMAAggregator"  (or "bma")
        signal_provenance.n_distinct_analysts == 1
        payload.confidence == 1.0

    is degenerate. All three conditions must hold; legitimate
    n_distinct_analysts==2 + conf=1.0 (both analysts agree) is NOT
    degenerate and this predicate returns False for it.

    Returns False on any event that:
      - has kind != "gate_approval" (rejections aren't approvable, can't be
        the failure mode this predicate names)
      - lacks a signal_provenance block (pre-ADR-0039 schema_version=1
        events) — operators should treat those as "unknown" and reach for
        the incident-response reference doc, not this predicate.
      - has any malformed/missing field — defensive: better to under-flag
        than over-flag in an incident-response context.
    """
    if event.get("kind") != "gate_approval":
        return False
    payload = event.get("payload") or {}
    sp = payload.get("signal_provenance") or {}
    if not sp:
        return False
    aggregator = sp.get("aggregator_class")
    n_distinct = sp.get("n_distinct_analysts")
    confidence = payload.get("confidence")
    # Both "BMAAggregator" (Python class name) and "bma" (signal.aggregator
    # field convention) are valid; the field is informally named.
    if aggregator not in ("BMAAggregator", "bma"):
        return False
    if n_distinct != 1:
        return False
    if confidence is None:
        return False
    try:
        return float(confidence) >= 0.999  # tolerate FP wobble around 1.0
    except (TypeError, ValueError):
        return False


def is_pre_provenance_schema(event: dict[str, Any]) -> bool:
    """True iff the event is from a pre-ADR-0039 era (no signal_provenance).

    Useful for operators trying to estimate audit-trail observability
    coverage — count of these events / total events = blind-spot ratio.
    """
    if event.get("kind") not in ("gate_approval", "gate_rejection"):
        return False
    payload = event.get("payload") or {}
    return "signal_provenance" not in payload


def iter_events(path: Path = DEFAULT_AUDIT_LOG_PATH) -> Iterator[dict[str, Any]]:
    """Stream events from the audit log, skipping malformed lines.

    Read-only. Does NOT enforce schema_version (the audit_log module
    itself raises AuditLogSchemaMismatch on writer-side schema drift; for
    read-side queries we want best-effort parsing).
    """
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("audit_log_query: skipping malformed line: %s", exc)


def find_degenerate(
    path: Path = DEFAULT_AUDIT_LOG_PATH,
    *,
    asof_prefix: str | None = None,
) -> list[dict[str, Any]]:
    """Return all events flagged as BMA-degenerate.

    Optional `asof_prefix` filters to events whose `payload.asof` starts
    with the given string (e.g., "2026-05-26" for a single-day filter).
    """
    out: list[dict[str, Any]] = []
    for event in iter_events(path):
        if asof_prefix is not None:
            ev_asof = (event.get("payload") or {}).get("asof") or ""
            if not ev_asof.startswith(asof_prefix):
                continue
        if is_bma_degenerate(event):
            out.append(event)
    return out


def coverage_summary(path: Path = DEFAULT_AUDIT_LOG_PATH) -> dict[str, int]:
    """Return counts that quantify audit-trail observability coverage.

    Keys:
        total_events: every JSONL row that parsed
        gate_approvals: count of kind=gate_approval
        gate_rejections: count of kind=gate_rejection
        with_provenance: gate_approval+gate_rejection events that DO carry
            a signal_provenance block
        without_provenance: ditto, lacking the block (pre-ADR-0039 era)
        degenerate: count where is_bma_degenerate is True
    """
    counts = {
        "total_events": 0,
        "gate_approvals": 0,
        "gate_rejections": 0,
        "with_provenance": 0,
        "without_provenance": 0,
        "degenerate": 0,
    }
    for event in iter_events(path):
        counts["total_events"] += 1
        kind = event.get("kind")
        if kind == "gate_approval":
            counts["gate_approvals"] += 1
        elif kind == "gate_rejection":
            counts["gate_rejections"] += 1
        if kind in ("gate_approval", "gate_rejection"):
            payload = event.get("payload") or {}
            if "signal_provenance" in payload:
                counts["with_provenance"] += 1
            else:
                counts["without_provenance"] += 1
        if is_bma_degenerate(event):
            counts["degenerate"] += 1
    return counts


def _format_degenerate_row(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    sp = payload.get("signal_provenance") or {}
    return (
        f"{payload.get('asof','?')} | {payload.get('asset','?')} | "
        f"dir={payload.get('direction','?')} | conf={payload.get('confidence','?')} | "
        f"target={payload.get('target_position_pct','?')} | "
        f"n_views={sp.get('n_views','?')} | "
        f"n_distinct={sp.get('n_distinct_analysts','?')} | "
        f"contrib={sp.get('contributing_analysts',[])}"
    )


def _cli(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hermes_quant.governance.audit_log_query",
        description="Read-only predicates over the governance audit log.",
    )
    parser.add_argument(
        "--path",
        default=str(DEFAULT_AUDIT_LOG_PATH),
        help=f"Audit-log path (default: {DEFAULT_AUDIT_LOG_PATH})",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub_d = sub.add_parser("degenerate", help="List BMA-degenerate gate_approval events")
    sub_d.add_argument(
        "--asof", default=None, help="Filter to events whose asof starts with this prefix"
    )

    sub.add_parser("coverage", help="Print audit-trail observability counts")

    args = parser.parse_args(list(argv) if argv is not None else None)
    path = Path(args.path).expanduser()

    if args.cmd == "degenerate":
        events = find_degenerate(path, asof_prefix=args.asof)
        if not events:
            print(f"# No BMA-degenerate events found in {path}")
            if args.asof:
                print(f"# (asof prefix filter: {args.asof})")
            return 0
        print(f"# Found {len(events)} BMA-degenerate event(s) in {path}")
        if args.asof:
            print(f"# (asof prefix filter: {args.asof})")
        for ev in events:
            print(_format_degenerate_row(ev))
        return 0

    if args.cmd == "coverage":
        c = coverage_summary(path)
        print(json.dumps(c, indent=2))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(_cli())
