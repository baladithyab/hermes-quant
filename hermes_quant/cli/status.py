"""hermes_quant.cli.status — unified `quant status` observability surface (ADR-0059).

This module surfaces all six append-only event stores plus state.db in a single
read-only view. Per ADR-0031 silence-by-default, every error mode (missing file,
malformed line, invalid JSON, decode error) is degraded into either an empty
section placeholder or a structured ``warnings`` entry — the function never
raises.

Append-only stores covered:
  1. governance/audit_log.jsonl          — gate decisions, kill switches, etc.
  2. memory/decisions.jsonl              — committee decisions
  3. memory/reflections.jsonl            — post-trade reflector output
  4. research/hypotheses.jsonl           — HypothesisRegistry
  5. research/run_cards.jsonl            — RunCardLog
  6. factors/factor_verdicts.jsonl       — FactorOracle verdicts

Plus state.db (positions and cash, read-only via sqlite3).

Tail-read semantics
-------------------
For files larger than ``_TAIL_BYTES`` (256 KiB), this module seeks to
``size - _TAIL_BYTES`` and reads only the tail. The first (potentially partial)
line of the tail is discarded so we never decode a half-line. Files smaller
than the threshold are read whole. We never load the entire JSONL into memory
beyond the tail window.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_QUANT_HOME = Path.home() / ".hermes" / "quant"

_TAIL_BYTES: int = 256 * 1024  # 256 KiB

_AUDIT_LOG_REL = ("governance", "audit_log.jsonl")
_DECISIONS_REL = ("memory", "decisions.jsonl")
_REFLECTIONS_REL = ("memory", "reflections.jsonl")
_HYPOTHESES_REL = ("research", "hypotheses.jsonl")
_RUN_CARDS_REL = ("research", "run_cards.jsonl")
_FACTOR_VERDICTS_REL = ("factors", "factor_verdicts.jsonl")
_STATE_DB_REL = ("state.db",)

_KNOWN_TIERS = ("premium", "standard", "experimental", "rejected")


# ---------------------------------------------------------------------------
# Report dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PositionView:
    """Tabular view of a state.db `positions` row."""

    account_id: str
    asset_class: str
    symbol: str
    quantity: float
    avg_entry_price: float
    last_update_at: str


@dataclass
class CashView:
    """Tabular view of a state.db `cash` row."""

    account_id: str
    balance_usd: float
    last_update_at: str
    equity_total: float


@dataclass
class StatusReport:
    """Aggregated read-only snapshot of every event store + state.db.

    This is the canonical return value of :func:`quant_status`. All counts and
    lists may be zero/empty; missing files do not crash. Failures during
    parsing are recorded in :attr:`warnings`.
    """

    # Window / metadata
    asof: str = ""
    window_hours: float = 24.0
    quant_home: str = ""

    # Audit log (governance)
    audit_summary: dict[str, int] = field(default_factory=dict)
    proposed_today: int = 0
    approved_today: int = 0
    rejected_today: int = 0
    top_rejection_reasons: list[tuple[str, int]] = field(default_factory=list)

    # Memory
    recent_decisions: list[dict[str, Any]] = field(default_factory=list)
    recent_reflections: list[dict[str, Any]] = field(default_factory=list)

    # Research
    open_hypotheses_count: int = 0
    recent_hypotheses: list[dict[str, Any]] = field(default_factory=list)
    recent_run_cards: list[dict[str, Any]] = field(default_factory=list)

    # Factors
    factor_verdict_summary: dict[str, int] = field(default_factory=dict)

    # State
    positions: list[PositionView] = field(default_factory=list)
    cash: list[CashView] = field(default_factory=list)

    # Diagnostics
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tail reader
# ---------------------------------------------------------------------------


def _read_tail_lines(
    path: Path,
    warnings: list[str],
    *,
    tail_bytes: int = _TAIL_BYTES,
) -> list[dict[str, Any]]:
    """Read the tail of a JSONL file and return parsed dict rows.

    Behaviour:
      * If ``path`` does not exist, returns ``[]`` (no warning).
      * If file size <= ``tail_bytes``, the whole file is read.
      * Otherwise, seeks to ``size - tail_bytes`` and discards the first
        (likely partial) line of the tail.
      * Each line is JSON-decoded individually; malformed lines are skipped
        and a warning is appended.
    """
    try:
        if not path.exists():
            return []
    except OSError as e:
        warnings.append(f"{path.name}: stat failed ({e!r})")
        return []

    try:
        size = path.stat().st_size
    except OSError as e:
        warnings.append(f"{path.name}: stat failed ({e!r})")
        return []

    if size == 0:
        return []

    try:
        with open(path, "rb") as fh:
            if size <= tail_bytes:
                blob = fh.read()
                truncated_head = False
            else:
                fh.seek(size - tail_bytes)
                blob = fh.read()
                truncated_head = True
    except OSError as e:
        warnings.append(f"{path.name}: read failed ({e!r})")
        return []

    try:
        text = blob.decode("utf-8", errors="replace")
    except Exception as e:  # extremely defensive — decode('utf-8', errors=replace) shouldn't raise
        warnings.append(f"{path.name}: decode failed ({e!r})")
        return []

    lines = text.splitlines()
    if truncated_head and lines:
        # Discard the first line because it may be a partial record.
        lines = lines[1:]

    rows: list[dict[str, Any]] = []
    for idx, raw in enumerate(lines):
        s = raw.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError as e:
            warnings.append(f"{path.name}: malformed line skipped (line {idx}: {e.msg})")
            continue
        if isinstance(obj, dict):
            rows.append(obj)
        else:
            warnings.append(f"{path.name}: non-object row skipped (line {idx})")
    return rows


# ---------------------------------------------------------------------------
# Time utilities
# ---------------------------------------------------------------------------


def _parse_dt(value: Any) -> datetime | None:
    """Parse an ISO-8601 string (or datetime) into a UTC-aware datetime.

    Naive datetimes are treated as UTC per ADR-0031. Returns ``None`` on any
    parse failure — callers must accept ``None`` and degrade gracefully.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    try:
        # datetime.fromisoformat handles 'YYYY-MM-DDTHH:MM:SS', '+00:00', and
        # since Python 3.11 also a trailing 'Z'. Normalise just in case.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _within_window(asof: datetime | None, *, now: datetime, window: timedelta) -> bool:
    if asof is None:
        return False
    return (now - asof) <= window and asof <= now + timedelta(seconds=1)


def _row_asof(row: dict[str, Any], *fields: str) -> datetime | None:
    for f in fields:
        if f in row:
            dt = _parse_dt(row.get(f))
            if dt is not None:
                return dt
    return None


# ---------------------------------------------------------------------------
# State.db reader
# ---------------------------------------------------------------------------


def _read_state_db(
    db_path: Path,
    warnings: list[str],
) -> tuple[list[PositionView], list[CashView]]:
    if not db_path.exists():
        return [], []
    try:
        # Read-only-ish: open URI mode='ro' so we never accidentally write.
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error as e:
        warnings.append(f"state.db: open failed ({e!r})")
        return [], []
    try:
        conn.row_factory = sqlite3.Row
        positions: list[PositionView] = []
        cash: list[CashView] = []
        try:
            cur = conn.execute(
                "SELECT account_id, asset_class, symbol, quantity, avg_entry_price, "
                "last_update_at FROM positions"
            )
            for r in cur.fetchall():
                try:
                    positions.append(
                        PositionView(
                            account_id=str(r["account_id"]),
                            asset_class=str(r["asset_class"]),
                            symbol=str(r["symbol"]),
                            quantity=float(r["quantity"]),
                            avg_entry_price=float(r["avg_entry_price"]),
                            last_update_at=str(r["last_update_at"] or ""),
                        )
                    )
                except (TypeError, ValueError, KeyError) as e:
                    warnings.append(f"state.db: position row coerce failed ({e!r})")
        except sqlite3.Error as e:
            warnings.append(f"state.db: positions read failed ({e!r})")
        try:
            cur = conn.execute(
                "SELECT account_id, balance_usd, last_update_at, equity_total FROM cash"
            )
            for r in cur.fetchall():
                try:
                    cash.append(
                        CashView(
                            account_id=str(r["account_id"]),
                            balance_usd=float(r["balance_usd"]),
                            last_update_at=str(r["last_update_at"] or ""),
                            equity_total=float(r["equity_total"]),
                        )
                    )
                except (TypeError, ValueError, KeyError) as e:
                    warnings.append(f"state.db: cash row coerce failed ({e!r})")
        except sqlite3.Error as e:
            warnings.append(f"state.db: cash read failed ({e!r})")
        return positions, cash
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def quant_status(
    asof_window: timedelta = timedelta(hours=24),
    quant_home: Path | None = None,
    *,
    now: datetime | None = None,
) -> StatusReport:
    """Return a :class:`StatusReport` snapshot of all event stores + state.db.

    Args:
        asof_window: Lookback window. Events with ``asof`` within
            ``[now - asof_window, now]`` are counted as "in-window". Defaults
            to 24 hours.
        quant_home: Override the default ``~/.hermes/quant`` root. Useful in
            tests with ``tmp_path``.
        now: Override the reference time for windowing. Defaults to
            ``datetime.now(timezone.utc)``.

    Never raises. All failure modes are surfaced as ``StatusReport.warnings``
    entries or empty sections.
    """
    if quant_home is None:
        quant_home = DEFAULT_QUANT_HOME
    quant_home = Path(quant_home)

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    report = StatusReport(
        asof=now.isoformat(),
        window_hours=asof_window.total_seconds() / 3600.0,
        quant_home=str(quant_home),
    )

    # ── Audit log ────────────────────────────────────────────────────────
    audit_path = quant_home.joinpath(*_AUDIT_LOG_REL)
    audit_rows = _read_tail_lines(audit_path, report.warnings)
    audit_summary: Counter[str] = Counter()
    rejection_reasons: Counter[str] = Counter()
    proposed = approved = rejected = 0
    for row in audit_rows:
        kind = str(row.get("kind", "")) or "unknown"
        asof = _row_asof(row, "asof")
        in_window = _within_window(asof, now=now, window=asof_window)
        if in_window:
            audit_summary[kind] += 1
            if kind == "proposal_emitted":
                proposed += 1
            elif kind == "gate_approval":
                approved += 1
            elif kind == "gate_rejection":
                rejected += 1
                payload = row.get("payload") or {}
                if isinstance(payload, dict):
                    reason = (
                        payload.get("reason")
                        or payload.get("rejection_reason")
                        or payload.get("rule")
                        or "unspecified"
                    )
                    rejection_reasons[str(reason)] += 1
    report.audit_summary = dict(audit_summary)
    report.proposed_today = proposed
    report.approved_today = approved
    report.rejected_today = rejected
    report.top_rejection_reasons = rejection_reasons.most_common(3)

    # ── Decisions ────────────────────────────────────────────────────────
    decisions_path = quant_home.joinpath(*_DECISIONS_REL)
    decision_rows = _read_tail_lines(decisions_path, report.warnings)
    # Only the "decision" kind rows matter for "recent decisions"; resolutions
    # are link records.
    decision_only = [r for r in decision_rows if r.get("kind", "decision") == "decision"]
    report.recent_decisions = decision_only[-5:][::-1]

    # ── Reflections ──────────────────────────────────────────────────────
    reflections_path = quant_home.joinpath(*_REFLECTIONS_REL)
    reflection_rows = _read_tail_lines(reflections_path, report.warnings)
    report.recent_reflections = reflection_rows[-3:][::-1]

    # ── Hypotheses ───────────────────────────────────────────────────────
    hypotheses_path = quant_home.joinpath(*_HYPOTHESES_REL)
    hypothesis_rows = _read_tail_lines(hypotheses_path, report.warnings)
    # Build current-status map from registration + status_change rows.
    status_by_id: dict[str, str] = {}
    initial_by_id: dict[str, dict[str, Any]] = {}
    for row in hypothesis_rows:
        kind = row.get("kind", "hypothesis")
        hyp_id = row.get("hypothesis_id")
        if not isinstance(hyp_id, str) or not hyp_id:
            continue
        if kind == "hypothesis":
            initial_by_id[hyp_id] = row
            status_by_id[hyp_id] = str(row.get("status", "open"))
        elif kind == "status_change":
            new_status = row.get("new_status") or row.get("status")
            if isinstance(new_status, str):
                status_by_id[hyp_id] = new_status
    open_ids = [hid for hid, st in status_by_id.items() if st == "open"]
    report.open_hypotheses_count = len(open_ids)
    # Most-recent 3 hypothesis registrations (in registration order, newest last).
    recent_initials = [
        initial_by_id[hid]
        for hid in initial_by_id
    ][-3:][::-1]
    report.recent_hypotheses = recent_initials

    # ── Run cards ────────────────────────────────────────────────────────
    run_cards_path = quant_home.joinpath(*_RUN_CARDS_REL)
    run_card_rows = _read_tail_lines(run_cards_path, report.warnings)
    report.recent_run_cards = run_card_rows[-3:][::-1]

    # ── Factor verdicts ──────────────────────────────────────────────────
    factor_path = quant_home.joinpath(*_FACTOR_VERDICTS_REL)
    factor_rows = _read_tail_lines(factor_path, report.warnings)
    tier_counts: Counter[str] = Counter()
    # Per ADR-0055 the file is append-only and an "evaluation" can re-emit a
    # verdict. For a single-pane summary we use latest-per-factor_id.
    latest_by_factor: dict[str, dict[str, Any]] = {}
    for row in factor_rows:
        fid = row.get("factor_id")
        if isinstance(fid, str) and fid:
            latest_by_factor[fid] = row
        else:
            # No id → still count it as a one-shot verdict record.
            tier = str(row.get("tier", "")) or "unknown"
            tier_counts[tier] += 1
    for row in latest_by_factor.values():
        tier = str(row.get("tier", "")) or "unknown"
        tier_counts[tier] += 1
    # Ensure all four canonical tiers are present in the summary (with 0 if
    # absent) for a stable shape.
    summary = {t: tier_counts.get(t, 0) for t in _KNOWN_TIERS}
    # Surface any unexpected tiers too.
    for t, c in tier_counts.items():
        if t not in _KNOWN_TIERS:
            summary[t] = c
    report.factor_verdict_summary = summary

    # ── State.db ─────────────────────────────────────────────────────────
    state_db_path = quant_home.joinpath(*_STATE_DB_REL)
    positions, cash = _read_state_db(state_db_path, report.warnings)
    report.positions = positions
    report.cash = cash

    return report


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


_NO_EVENTS = "  (no events yet)"


def _section(title: str, body_lines: list[str]) -> list[str]:
    out = [f"━━ {title} ━━"]
    if not body_lines:
        out.append(_NO_EVENTS)
    else:
        out.extend(body_lines)
    out.append("")
    return out


def format_status_human(report: StatusReport) -> str:
    """Render a multi-section human-readable text view of the status report."""
    lines: list[str] = []
    lines.append(
        f"Hermes Quant Status — asof={report.asof} window={report.window_hours:g}h"
    )
    lines.append(f"  quant_home={report.quant_home}")
    lines.append("")

    # 1. Audit log
    audit_body: list[str] = []
    if report.audit_summary:
        for kind, count in sorted(report.audit_summary.items()):
            audit_body.append(f"  {kind:30s} {count}")
        audit_body.append("")
        audit_body.append(
            f"  proposed={report.proposed_today}  "
            f"approved={report.approved_today}  "
            f"rejected={report.rejected_today}"
        )
        if report.top_rejection_reasons:
            audit_body.append("  top rejection reasons:")
            for reason, count in report.top_rejection_reasons:
                audit_body.append(f"    - {reason}: {count}")
    lines.extend(_section("audit_log (governance)", audit_body))

    # 2. Decisions
    decision_body: list[str] = []
    for d in report.recent_decisions:
        ticker = d.get("ticker", "?")
        side = d.get("side", "?")
        ts = d.get("asof_decision", d.get("asof", "?"))
        did = d.get("decision_id", "?")
        decision_body.append(f"  {ts}  {ticker} {side}  ({did})")
    lines.extend(_section("recent decisions (memory)", decision_body))

    # 3. Reflections
    reflection_body: list[str] = []
    for r in report.recent_reflections:
        ticker = r.get("ticker", "?")
        ts = r.get("asof_resolution", r.get("asof", "?"))
        cat = r.get("lesson_category", r.get("category", ""))
        rid = r.get("reflection_id", "?")
        reflection_body.append(f"  {ts}  {ticker}  [{cat}]  ({rid})")
    lines.extend(_section("recent reflections (memory)", reflection_body))

    # 4. Hypotheses
    hyp_body: list[str] = []
    hyp_body.append(f"  open hypotheses: {report.open_hypotheses_count}")
    if report.recent_hypotheses:
        hyp_body.append("  most recent registrations:")
        for h in report.recent_hypotheses:
            hid = h.get("hypothesis_id", "?")
            title = h.get("title", h.get("statement", "?"))
            status = h.get("status", "?")
            hyp_body.append(f"    - {hid}  [{status}]  {title}")
    lines.extend(_section("hypotheses (research)", hyp_body))

    # 5. Run cards
    rc_body: list[str] = []
    for rc in report.recent_run_cards:
        rid = rc.get("run_id", "?")
        verdict = rc.get("verdict", "?")
        falsified = (verdict == "falsified") or rc.get("falsified") is True
        marker = "‼ FALSIFIED" if falsified else verdict
        strat = rc.get("strategy_name", "?")
        rc_body.append(f"  {rid}  {strat}  → {marker}")
    lines.extend(_section("recent run cards (research)", rc_body))

    # 6. Factor verdicts
    fv_body: list[str] = []
    if report.factor_verdict_summary:
        for tier in _KNOWN_TIERS:
            fv_body.append(f"  {tier:15s} {report.factor_verdict_summary.get(tier, 0)}")
        # Any non-canonical tiers get appended too.
        extras = [
            (t, c) for t, c in report.factor_verdict_summary.items()
            if t not in _KNOWN_TIERS
        ]
        for t, c in extras:
            fv_body.append(f"  {t:15s} {c}  (non-canonical)")
    lines.extend(_section("factor verdicts (factors)", fv_body))

    # 7. Positions / cash
    pos_body: list[str] = []
    for p in report.positions:
        pos_body.append(
            f"  {p.account_id:20s} {p.asset_class:8s} {p.symbol:10s} "
            f"qty={p.quantity:+.4f}  avg={p.avg_entry_price:.4f}"
        )
    if report.cash:
        if pos_body:
            pos_body.append("")
        pos_body.append("  cash:")
        for c in report.cash:
            pos_body.append(
                f"    {c.account_id:20s}  bal={c.balance_usd:.2f}  "
                f"equity={c.equity_total:.2f}"
            )
    lines.extend(_section("positions / cash (state.db)", pos_body))

    # Warnings
    if report.warnings:
        lines.append("━━ warnings ━━")
        for w in report.warnings:
            lines.append(f"  ! {w}")

    return "\n".join(lines)


def _to_jsonable(obj: Any) -> Any:
    """Recursively convert dataclasses / tuples / Counters to JSON-safe types."""
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, tuple):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, list):
        return [_to_jsonable(v) for v in obj]
    return obj


def format_status_json(report: StatusReport) -> str:
    """Return a stable, json.dumps-able representation of ``report``."""
    raw = asdict(report)
    safe = _to_jsonable(raw)
    return json.dumps(safe, sort_keys=True, indent=2, default=str)


# ---------------------------------------------------------------------------
# CLI helper used by scripts/quant-status.py
# ---------------------------------------------------------------------------


def _filter_for_store(report: StatusReport, store: str) -> dict[str, Any]:
    """Return just the slice of ``report`` relevant to ``store``."""
    base = {
        "asof": report.asof,
        "window_hours": report.window_hours,
        "quant_home": report.quant_home,
        "warnings": list(report.warnings),
    }
    if store == "all":
        return _to_jsonable(asdict(report))
    if store == "audit":
        base.update(
            audit_summary=dict(report.audit_summary),
            proposed_today=report.proposed_today,
            approved_today=report.approved_today,
            rejected_today=report.rejected_today,
            top_rejection_reasons=[list(t) for t in report.top_rejection_reasons],
        )
    elif store == "decisions":
        base.update(recent_decisions=list(report.recent_decisions))
    elif store == "reflections":
        base.update(recent_reflections=list(report.recent_reflections))
    elif store == "hypotheses":
        base.update(
            open_hypotheses_count=report.open_hypotheses_count,
            recent_hypotheses=list(report.recent_hypotheses),
        )
    elif store == "run-cards":
        base.update(recent_run_cards=list(report.recent_run_cards))
    elif store == "factors":
        base.update(factor_verdict_summary=dict(report.factor_verdict_summary))
    elif store == "positions":
        base.update(
            positions=[asdict(p) for p in report.positions],
            cash=[asdict(c) for c in report.cash],
        )
    else:
        base["error"] = f"unknown store: {store!r}"
    return _to_jsonable(base)


def run_cli(argv: list[str] | None = None) -> int:
    """Entrypoint for ``scripts/quant-status.py``.

    Returns exit code 0 always — read-only commands never fail the shell.
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="quant-status",
        description="Read-only unified status across all hermes-quant event stores.",
    )
    parser.add_argument(
        "--quant-home",
        type=Path,
        default=None,
        help="Override the quant home directory (default: ~/.hermes/quant).",
    )
    parser.add_argument(
        "--window-hours",
        type=int,
        default=24,
        help="Lookback window in hours for in-window counts (default: 24).",
    )
    parser.add_argument(
        "--format",
        choices=("human", "json"),
        default="human",
        help="Output format.",
    )
    parser.add_argument(
        "--store",
        action="append",
        choices=(
            "audit",
            "decisions",
            "reflections",
            "hypotheses",
            "run-cards",
            "factors",
            "positions",
            "all",
        ),
        default=None,
        help=(
            "Restrict output to one or more event stores (repeatable). "
            "Default: all."
        ),
    )

    args = parser.parse_args(argv)
    stores = args.store or ["all"]
    window = timedelta(hours=int(args.window_hours))
    report = quant_status(asof_window=window, quant_home=args.quant_home)

    if args.format == "json":
        if "all" in stores or len(stores) == 0:
            print(format_status_json(report))
        else:
            slices = {s: _filter_for_store(report, s) for s in stores}
            print(json.dumps(slices, sort_keys=True, indent=2, default=str))
    else:
        if "all" in stores or len(stores) == 0:
            sys.stdout.write(format_status_human(report))
            sys.stdout.write("\n")
        else:
            # For human format with --store filters, emit a focused subsection
            # for each requested store.
            for s in stores:
                slice_dict = _filter_for_store(report, s)
                sys.stdout.write(f"=== store: {s} ===\n")
                sys.stdout.write(json.dumps(slice_dict, sort_keys=True, indent=2, default=str))
                sys.stdout.write("\n\n")
    return 0


__all__ = [
    "DEFAULT_QUANT_HOME",
    "PositionView",
    "CashView",
    "StatusReport",
    "format_status_human",
    "format_status_json",
    "quant_status",
    "run_cli",
]
