"""hermes_quant.reporting.daily_report — daily Markdown brief generator.

ADR-0061. Synthesizes the day's governance events, reflections, hypothesis
status changes, factor verdicts, positions, and open proposals into a single
publishable report. Pure read-only — never mutates state.

The report is consumable in three formats:
    * ``format_markdown(report)`` → full Markdown with tables and sections
      (default; written to disk under ``~/.hermes/quant/reports/{date}.md``).
    * ``format_telegram(report)`` → Markdown-V2 escaped, truncated to fit the
      4096-char Telegram message limit with a "see full file" footer.
    * ``DailyReport`` dataclass → for downstream JSON serialization.

Caller (cron job, Discord slash command, future Telegram bot) is responsible
for actual delivery; this module never calls ``send_message``.

Design notes
------------
1. Inputs are tail-read from JSONL stores with corrupt-line tolerance:
   a malformed audit_log row is skipped, not raised.
2. ``state.db`` reads use a fresh ``PortfolioState`` instance pointed at the
   override path; never opened in write mode.
3. P&L is reported using ``avg_entry_price`` as a proxy for mark when no
   live price feed is available — same approximation as PortfolioState's
   ``equity_total``. Documented in the report's P&L section.
4. ``--asof`` in the past walks the audit log filtered to that calendar day
   in UTC; tomorrow's pending proposals are a "live" snapshot regardless.
5. Telegram MarkdownV2: characters ``_*[]()~`>#+-=|{}.!`` must be backslash-
   escaped except inside fenced code blocks. We escape user-supplied data
   (tickers, reasons, proposal IDs) but leave the report's structural
   characters intact.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

DEFAULT_QUANT_HOME = Path.home() / ".hermes" / "quant"

# Telegram MarkdownV2 reserved characters (bot API doc).
_TG_MD_V2_SPECIALS = r"_*[]()~`>#+-=|{}.!"

# Default Telegram char budget (Telegram caps at 4096; we leave headroom for
# the truncation footer).
DEFAULT_TELEGRAM_LIMIT = 3500


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class DailyReport:
    """Structured daily report payload.

    All numeric fields default to safe zero/empty values so an empty
    quant-home produces a valid (if uninteresting) report.

    Attributes
    ----------
    date:
        Calendar date (UTC) the report covers.
    summary_lines:
        One-line bullet headlines for the top of the report.
    gate_table:
        List of ``{ticker, action, conf, reason, asof}`` dicts for every
        gate decision (approval + rejection) on the report date.
    positions_table:
        List of ``{ticker, qty, cost, mark, unrealized_pnl}`` dicts read
        from ``state.db``.
    pnl_today, pnl_mtd, pnl_ytd:
        Approximate P&L figures using ``equity_total - initial_cash`` as
        the proxy. ``None`` when state.db has no cash row yet.
    reflections_section:
        Bullet lines summarising reflections from the last 24h.
    hypotheses_changes:
        ``{"promoted": [...], "falsified": [...], "new": [...]}``.
    factor_verdicts_today:
        ``{tier: count}`` for every verdict appended on the report date.
    open_proposals:
        List of ``{proposal_id, ticker, ttl_remaining}`` dicts for the
        currently-pending proposals.
    """

    date: date
    summary_lines: list[str] = field(default_factory=list)
    gate_table: list[dict[str, Any]] = field(default_factory=list)
    positions_table: list[dict[str, Any]] = field(default_factory=list)
    pnl_today: float | None = None
    pnl_mtd: float | None = None
    pnl_ytd: float | None = None
    reflections_section: list[str] = field(default_factory=list)
    hypotheses_changes: dict[str, list[str]] = field(
        default_factory=lambda: {"promoted": [], "falsified": [], "new": []}
    )
    factor_verdicts_today: dict[str, int] = field(default_factory=dict)
    open_proposals: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# JSONL tail-read helper (corrupt-tolerant)
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield rows from a JSONL file, skipping malformed lines.

    Mirrors the v0.4-1 ``status.py`` pattern: an audit_log line truncated
    by an unflushed crash must NOT take down the report.
    """
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("daily_report: skipping malformed line in %s", path)
                    continue
                if not isinstance(obj, dict):
                    continue
                yield obj
    except OSError as exc:
        logger.warning("daily_report: could not read %s: %s", path, exc)


def _parse_iso(s: str | None) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        # Tolerate trailing Z.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None


def _on_date(dt: datetime | None, target: date) -> bool:
    if dt is None:
        return False
    return dt.astimezone(UTC).date() == target


# ---------------------------------------------------------------------------
# Per-store readers
# ---------------------------------------------------------------------------


def _read_gate_decisions(audit_path: Path, asof: date) -> list[dict[str, Any]]:
    """Return every gate_approval / gate_rejection event landing on ``asof``.

    Sorted ascending by event timestamp so callers see the day's narrative
    in order.
    """
    rows: list[dict[str, Any]] = []
    for raw in _read_jsonl(audit_path):
        kind = raw.get("kind")
        if kind not in ("gate_approval", "gate_rejection"):
            continue
        dt = _parse_iso(raw.get("asof"))
        if not _on_date(dt, asof):
            continue
        payload = raw.get("payload") or {}
        ticker = (
            payload.get("asset")
            or payload.get("ticker")
            or payload.get("symbol")
            or "?"
        )
        # action: gate_approval → APPROVE, gate_rejection → REJECT.
        action = "APPROVE" if kind == "gate_approval" else "REJECT"
        conf = payload.get("confidence")
        if conf is None:
            conf = payload.get("kelly_fraction")
        if conf is None:
            conf = payload.get("conviction")
        # Coerce to float when possible; leave as None for missing data so
        # the markdown renderer can show "—".
        try:
            conf_f: float | None = float(conf) if conf is not None else None
        except (TypeError, ValueError):
            conf_f = None
        reason = (
            payload.get("reason")
            or payload.get("gated_reason")
            or payload.get("rejection_reason")
            or ""
        )
        rows.append(
            {
                "ticker": str(ticker),
                "action": action,
                "conf": conf_f,
                "reason": str(reason),
                "asof": dt,
            }
        )
    # Sort ASCENDING by time so the table reads chronologically.
    rows.sort(key=lambda r: r["asof"] or datetime.min.replace(tzinfo=UTC))
    return rows


def _read_factor_verdicts(verdicts_path: Path, asof: date) -> dict[str, int]:
    """Group factor_verdicts.jsonl rows by tier for ``asof``."""
    counter: Counter[str] = Counter()
    for raw in _read_jsonl(verdicts_path):
        dt = _parse_iso(raw.get("reviewed_at"))
        if not _on_date(dt, asof):
            continue
        tier = raw.get("tier") or "unknown"
        counter[str(tier)] += 1
    return dict(counter)


def _read_reflections(refl_path: Path, since_dt: datetime) -> list[str]:
    """Return reflection_text bullet lines for entries in last 24h."""
    lines: list[str] = []
    for raw in _read_jsonl(refl_path):
        dt = _parse_iso(raw.get("asof_resolution"))
        if dt is None or dt < since_dt:
            continue
        ticker = raw.get("ticker", "?")
        text = raw.get("reflection_text", "").strip()
        if not text:
            continue
        # Preserve the ticker prefix for context.
        lines.append(f"[{ticker}] {text}")
    return lines


def _read_hypothesis_changes(
    hyp_path: Path, since_dt: datetime
) -> dict[str, list[str]]:
    """Diff hypotheses.jsonl over the last 24h.

    Returns three buckets:
    - ``promoted``: status_change rows where ``new_status == "validated"``.
    - ``falsified``: status_change rows where ``new_status == "falsified"``.
    - ``new``: registration rows whose ``created_at`` falls in the window.
    """
    out: dict[str, list[str]] = {"promoted": [], "falsified": [], "new": []}
    for raw in _read_jsonl(hyp_path):
        kind = raw.get("kind")
        if kind == "hypothesis":
            dt = _parse_iso(raw.get("created_at"))
            if dt is not None and dt >= since_dt:
                hyp_id = raw.get("hypothesis_id", "?")
                claim = raw.get("claim", "").strip()
                out["new"].append(f"{hyp_id}: {claim[:120]}")
        elif kind == "status_change":
            dt = _parse_iso(raw.get("asof"))
            if dt is None or dt < since_dt:
                continue
            new_status = raw.get("new_status")
            hyp_id = raw.get("hypothesis_id", "?")
            if new_status == "validated":
                out["promoted"].append(hyp_id)
            elif new_status == "falsified":
                out["falsified"].append(hyp_id)
    return out


# ---------------------------------------------------------------------------
# state.db read view
# ---------------------------------------------------------------------------


@contextmanager
def _ro_conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a state.db in shared/read mode that won't trigger schema init."""
    conn = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def _read_positions_and_cash(
    state_db: Path, account_id: str = "paper-default"
) -> tuple[list[dict[str, Any]], float | None, float | None]:
    """Read open positions + (balance, equity) from state.db.

    Returns
    -------
    (positions_table, balance_usd, equity_total).
    Missing tables / missing db return ``([], None, None)``.
    """
    if not state_db.exists():
        return [], None, None
    positions: list[dict[str, Any]] = []
    balance: float | None = None
    equity: float | None = None
    try:
        with _ro_conn(state_db) as conn:
            try:
                rows = conn.execute(
                    "SELECT symbol, quantity, avg_entry_price "
                    "FROM positions "
                    "WHERE account_id = ? AND ABS(quantity) >= 1e-12",
                    (account_id,),
                ).fetchall()
                for r in rows:
                    qty = float(r["quantity"])
                    cost = float(r["avg_entry_price"])
                    # v0.1: mark = cost (no live feed), so unrealized P&L = 0.
                    mark = cost
                    unrealized = (mark - cost) * qty
                    positions.append(
                        {
                            "ticker": r["symbol"],
                            "qty": qty,
                            "cost": cost,
                            "mark": mark,
                            "unrealized_pnl": unrealized,
                        }
                    )
            except sqlite3.OperationalError:
                # Table missing — empty quant-home.
                pass
            try:
                row = conn.execute(
                    "SELECT balance_usd, equity_total FROM cash WHERE account_id = ?",
                    (account_id,),
                ).fetchone()
                if row is not None:
                    balance = float(row["balance_usd"])
                    equity = float(row["equity_total"])
            except sqlite3.OperationalError:
                pass
    except sqlite3.DatabaseError as exc:
        logger.warning("daily_report: could not read state.db: %s", exc)
    return positions, balance, equity


def _read_open_proposals(prop_db: Path) -> list[dict[str, Any]]:
    """Read proposals where state='pending' from proposals.db.

    Returns ``{proposal_id, ticker, ttl_remaining}`` (TTL as a humanized
    string like ``"23h 45m"``). Empty list if the db is missing.
    """
    if not prop_db.exists():
        return []
    rows: list[dict[str, Any]] = []
    now = datetime.now(UTC)
    try:
        with _ro_conn(prop_db) as conn:
            try:
                results = conn.execute(
                    "SELECT proposal_id, symbol, expires_at FROM proposals "
                    "WHERE state = 'pending' "
                    "ORDER BY created_at DESC"
                ).fetchall()
            except sqlite3.OperationalError:
                return []
            for r in results:
                expires = _parse_iso(r["expires_at"])
                if expires is None:
                    ttl_str = "?"
                else:
                    delta = expires - now
                    if delta.total_seconds() <= 0:
                        # already expired but not swept yet — show negative
                        ttl_str = "expired"
                    else:
                        total_min = int(delta.total_seconds() // 60)
                        h, m = divmod(total_min, 60)
                        if h > 0:
                            ttl_str = f"{h}h {m}m"
                        else:
                            ttl_str = f"{m}m"
                rows.append(
                    {
                        "proposal_id": r["proposal_id"],
                        "ticker": r["symbol"],
                        "ttl_remaining": ttl_str,
                    }
                )
    except sqlite3.DatabaseError as exc:
        logger.warning("daily_report: could not read proposals.db: %s", exc)
    return rows


# ---------------------------------------------------------------------------
# P&L (approximate v0.1)
# ---------------------------------------------------------------------------

def _approx_pnl(equity: float | None) -> float | None:
    """Approximate cumulative P&L = equity - initial_cash.

    No marking-to-market, no realized vs unrealized split — same v0.1
    approximation as PortfolioState.equity_total. Documented in the
    report's P&L section.

    The initial-cash basis is derived from the SAME env-honoring source
    (``portfolio_state._default_initial_cash`` reading
    ``HERMES_QUANT_PAPER_INITIAL_CASH``) that ``state.db``'s ``equity_total``
    was bootstrapped against, so the report's basis and state.db's basis stay
    in lockstep. A previous module-local ``_DEFAULT_INITIAL_CASH = 100_000.0``
    silently ignored the env override and mis-stated cumulative P&L whenever
    the operator configured a non-default initial cash (e.g. a flat 250k book
    reported a fictional +150k). The shared source's ar10 finite-guard rejects
    non-finite / <=0 overrides, so the report inherits the same fail-closed-to-
    100k semantics; byte-identical at the default (env unset → 100_000.0).
    """
    if equity is None:
        return None
    from hermes_quant.state.portfolio_state import _default_initial_cash

    return equity - _default_initial_cash()


# ---------------------------------------------------------------------------
# Top-level: generate_daily_report
# ---------------------------------------------------------------------------


def generate_daily_report(
    asof: date | None = None,
    quant_home: Path | None = None,
    *,
    account_id: str = "paper-default",
) -> DailyReport:
    """Build a :class:`DailyReport` for ``asof`` (default: today UTC).

    Pulls from
    - ``governance/audit_log.jsonl`` — gate decisions
    - ``state.db`` (positions + cash) — open positions, P&L proxy
    - ``memory/reflections.jsonl`` — last-24h reflections
    - ``research/hypotheses.jsonl`` — promoted / falsified / new
    - ``factors/factor_verdicts.jsonl`` — tier counts for the day
    - ``proposals.db`` — pending proposals (live snapshot regardless of asof)
    """
    if asof is None:
        asof = datetime.now(UTC).date()
    home = quant_home or DEFAULT_QUANT_HOME

    audit_path = home / "governance" / "audit_log.jsonl"
    state_db = home / "state.db"
    reflections_path = home / "memory" / "reflections.jsonl"
    hypotheses_path = home / "research" / "hypotheses.jsonl"
    verdicts_path = home / "factors" / "factor_verdicts.jsonl"
    proposals_db = home / "proposals.db"

    # Gate decisions
    gate_table = _read_gate_decisions(audit_path, asof)
    n_total = len(gate_table)
    n_approved = sum(1 for r in gate_table if r["action"] == "APPROVE")
    n_rejected = sum(1 for r in gate_table if r["action"] == "REJECT")

    # Top-3 rejection reasons
    rejection_reasons = Counter(
        r["reason"] or "(no reason given)"
        for r in gate_table
        if r["action"] == "REJECT"
    )
    top_rejections = rejection_reasons.most_common(3)

    # Positions + P&L
    positions_table, _balance, equity = _read_positions_and_cash(
        state_db, account_id=account_id
    )
    # v0.1: pnl_today / pnl_mtd / pnl_ytd are all the same approximate
    # cumulative number. Future versions can split via a daily snapshot
    # series; for now we report the same proxy three times so the
    # contract is stable.
    pnl_proxy = _approx_pnl(equity)
    pnl_today = pnl_proxy
    pnl_mtd = pnl_proxy
    pnl_ytd = pnl_proxy

    # Reflections (last 24h relative to end-of-asof-day UTC)
    end_of_day = datetime.combine(asof, datetime.max.time(), tzinfo=UTC)
    since_24h = end_of_day - timedelta(hours=24)
    reflections = _read_reflections(reflections_path, since_24h)

    # Hypothesis status diff over same 24h window
    hyp_changes = _read_hypothesis_changes(hypotheses_path, since_24h)

    # Factor verdicts grouped by tier on asof
    factor_verdicts = _read_factor_verdicts(verdicts_path, asof)

    # Open proposals (live snapshot, NOT date-filtered — we want what's
    # actionable RIGHT NOW even if the report is for a historical date,
    # because TTL has elapsed for anything older).
    open_proposals = _read_open_proposals(proposals_db)

    # Summary lines
    summary: list[str] = []
    summary.append(
        f"{n_total} gate decision(s): {n_approved} approved, {n_rejected} rejected"
    )
    summary.append(f"{len(positions_table)} position(s) open")
    if pnl_today is not None:
        summary.append(f"P&L (cumulative proxy): ${pnl_today:,.2f}")
    if reflections:
        summary.append(f"{len(reflections)} reflection(s) recorded in last 24h")
    if hyp_changes["promoted"] or hyp_changes["falsified"]:
        summary.append(
            f"Hypotheses: {len(hyp_changes['promoted'])} promoted, "
            f"{len(hyp_changes['falsified'])} falsified"
        )
    if top_rejections:
        top = "; ".join(
            f"{reason[:60]} (×{count})" for reason, count in top_rejections
        )
        summary.append(f"Top rejection reasons: {top}")
    if open_proposals:
        summary.append(f"{len(open_proposals)} proposal(s) awaiting approval")

    return DailyReport(
        date=asof,
        summary_lines=summary,
        gate_table=gate_table,
        positions_table=positions_table,
        pnl_today=pnl_today,
        pnl_mtd=pnl_mtd,
        pnl_ytd=pnl_ytd,
        reflections_section=reflections,
        hypotheses_changes=hyp_changes,
        factor_verdicts_today=factor_verdicts,
        open_proposals=open_proposals,
    )


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


def _fmt_pnl(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"${v:,.2f}"


def _fmt_conf(c: float | None) -> str:
    if c is None:
        return "—"
    return f"{c:.3f}"


def _fmt_time(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.astimezone(UTC).strftime("%H:%M:%SZ")


def _md_escape_pipe(s: str) -> str:
    """Escape pipe characters that would break a markdown table cell."""
    return s.replace("|", "\\|").replace("\n", " ")


def format_markdown(
    report: DailyReport,
    *,
    stage_outputs: list[Any] | None = None,
) -> str:
    """Render a :class:`DailyReport` as plain-Markdown (GitHub-flavored).

    The output is sectioned, table-formatted, and safe to paste into
    Discord, GitHub issues, or persist to disk.

    ``stage_outputs`` (B18 / ADR-0010 §8 wiring) is an OPTIONAL list of
    LLM-stage Pydantic schema objects — ``TraderProposal``, ``ResearchPlan``,
    ``RiskDebateSummary``, ``PortfolioDecision``. When supplied, each is
    rendered via the canonical, pure ``schema_render.render_schema`` dispatcher
    (the single place those renderers live) and appended as a
    "Committee Stage Outputs" section. Default ``None`` ⇒ no section is
    emitted and the rendered report is byte-identical to the pre-B18 output,
    so every existing caller's behavior is preserved.
    """
    out: list[str] = []
    out.append(f"# Hermes-Quant Daily Report — {report.date.isoformat()}")
    out.append("")

    # Summary
    out.append("## Summary")
    if report.summary_lines:
        for line in report.summary_lines:
            out.append(f"- {line}")
    else:
        out.append("- No notable events.")
    out.append("")

    # Gate Decisions
    n_total = len(report.gate_table)
    n_approved = sum(1 for r in report.gate_table if r["action"] == "APPROVE")
    n_rejected = sum(1 for r in report.gate_table if r["action"] == "REJECT")
    out.append(
        f"## Gate Decisions ({n_total} total: "
        f"{n_approved} approved, {n_rejected} rejected)"
    )
    if report.gate_table:
        out.append("| Time | Ticker | Action | Conf | Reason |")
        out.append("|------|--------|--------|------|--------|")
        for r in report.gate_table:
            out.append(
                "| {time} | {ticker} | {action} | {conf} | {reason} |".format(
                    time=_fmt_time(r.get("asof")),
                    ticker=_md_escape_pipe(str(r.get("ticker", "?"))),
                    action=r.get("action", "?"),
                    conf=_fmt_conf(r.get("conf")),
                    reason=_md_escape_pipe(str(r.get("reason", "")))[:120],
                )
            )
    else:
        out.append("_No gate decisions on this date._")
    out.append("")

    # Positions
    n_pos = len(report.positions_table)
    if n_pos == 0:
        out.append("## Positions (none open)")
    else:
        out.append(f"## Positions ({n_pos} open)")
        out.append("| Ticker | Qty | Cost | Mark | Unrealized P&L |")
        out.append("|--------|-----|------|------|----------------|")
        for p in report.positions_table:
            out.append(
                "| {ticker} | {qty:.4f} | ${cost:,.2f} | "
                "${mark:,.2f} | ${pnl:,.2f} |".format(
                    ticker=_md_escape_pipe(str(p.get("ticker", "?"))),
                    qty=float(p.get("qty", 0.0)),
                    cost=float(p.get("cost", 0.0)),
                    mark=float(p.get("mark", 0.0)),
                    pnl=float(p.get("unrealized_pnl", 0.0)),
                )
            )
    out.append("")

    # P&L (approximate)
    out.append("## P&L (approximate; mark = avg cost in v0.1)")
    out.append(f"- Today: {_fmt_pnl(report.pnl_today)}")
    out.append(f"- MTD:   {_fmt_pnl(report.pnl_mtd)}")
    out.append(f"- YTD:   {_fmt_pnl(report.pnl_ytd)}")
    out.append("")

    # Reflections
    out.append("## Lessons Learned (last 24h reflections)")
    if report.reflections_section:
        for line in report.reflections_section:
            out.append(f"- {line}")
    else:
        out.append("_No reflections in the last 24 hours._")
    out.append("")

    # Hypothesis Changes
    out.append("## Hypothesis Changes")
    promoted = report.hypotheses_changes.get("promoted", [])
    falsified = report.hypotheses_changes.get("falsified", [])
    new = report.hypotheses_changes.get("new", [])
    out.append(f"- Promoted: {', '.join(promoted) if promoted else '_none_'}")
    out.append(f"- Falsified: {', '.join(falsified) if falsified else '_none_'}")
    if new:
        out.append("- New:")
        for n in new:
            out.append(f"    - {n}")
    else:
        out.append("- New: _none_")
    out.append("")

    # Factor Verdicts
    out.append("## Factor Verdicts")
    if report.factor_verdicts_today:
        out.append("| Tier | Count |")
        out.append("|------|-------|")
        # Stable order: premium → standard → experimental → rejected → others.
        order = ["premium", "standard", "experimental", "rejected"]
        seen: set[str] = set()
        for tier in order:
            if tier in report.factor_verdicts_today:
                out.append(f"| {tier} | {report.factor_verdicts_today[tier]} |")
                seen.add(tier)
        for tier, count in report.factor_verdicts_today.items():
            if tier in seen:
                continue
            out.append(f"| {tier} | {count} |")
    else:
        out.append("_No factor verdicts on this date._")
    out.append("")

    # Open Proposals
    out.append("## Open Proposals (awaiting approval)")
    if report.open_proposals:
        out.append("| Proposal ID | Ticker | TTL Remaining |")
        out.append("|-------------|--------|---------------|")
        for p in report.open_proposals:
            out.append(
                "| `{pid}` | {ticker} | {ttl} |".format(
                    pid=_md_escape_pipe(str(p.get("proposal_id", "?"))),
                    ticker=_md_escape_pipe(str(p.get("ticker", "?"))),
                    ttl=_md_escape_pipe(str(p.get("ttl_remaining", "?"))),
                )
            )
    else:
        out.append("_No proposals awaiting approval._")
    out.append("")

    # Committee Stage Outputs (B18 / ADR-0010 §8) — OPTIONAL, default-OFF.
    # When the caller passes through the day's LLM-stage schema objects we
    # surface them via the canonical per-schema renderers instead of
    # re-authoring the markdown here. Absent / empty ⇒ no section, so the
    # default report stays byte-identical.
    if stage_outputs:
        from hermes_quant.agents.schema_render import render_schema

        out.append("## Committee Stage Outputs")
        for obj in stage_outputs:
            out.append(render_schema(obj))
            out.append("")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Telegram MarkdownV2 renderer
# ---------------------------------------------------------------------------


def _escape_telegram_md_v2(s: str) -> str:
    """Backslash-escape every MarkdownV2 special character.

    Per Telegram bot API: ``_*[]()~`>#+-=|{}.!`` must be escaped outside of
    code blocks. Escape order matters — escape the backslash itself first
    so we don't double-escape.
    """
    if not s:
        return ""
    # Backslash first (would otherwise duplicate later).
    s = s.replace("\\", "\\\\")
    for ch in _TG_MD_V2_SPECIALS:
        s = s.replace(ch, "\\" + ch)
    return s


def format_telegram(
    report: DailyReport,
    max_chars: int = DEFAULT_TELEGRAM_LIMIT,
    quant_home: Path | None = None,
) -> str:
    """Render a compact MarkdownV2 brief for Telegram delivery.

    Truncates to ``max_chars`` (default 3500, leaving headroom for the
    truncation footer; Telegram's hard limit is 4096). User-supplied
    fields (tickers, reasons, proposal IDs) are escaped with
    :func:`_escape_telegram_md_v2`.
    """
    home = quant_home or DEFAULT_QUANT_HOME
    full_path = home / "reports" / f"{report.date.isoformat()}.md"

    e = _escape_telegram_md_v2  # alias

    out: list[str] = []
    out.append(f"*Hermes\\-Quant Daily Report — {e(report.date.isoformat())}*")
    out.append("")
    if report.summary_lines:
        for line in report.summary_lines:
            out.append(f"• {e(line)}")
    else:
        out.append("• No notable events\\.")
    out.append("")

    n_total = len(report.gate_table)
    n_approved = sum(1 for r in report.gate_table if r["action"] == "APPROVE")
    n_rejected = sum(1 for r in report.gate_table if r["action"] == "REJECT")
    out.append(
        f"*Gates:* {n_total} total \\({n_approved} approved, {n_rejected} rejected\\)"
    )
    # Inline first 5 gate rows max
    for r in report.gate_table[:5]:
        out.append(
            f"  • {e(str(r.get('ticker', '?')))} — "
            f"{e(str(r.get('action', '?')))} "
            f"\\(conf={e(_fmt_conf(r.get('conf')))}\\)"
        )
    if len(report.gate_table) > 5:
        out.append(f"  • \\(\\+{len(report.gate_table) - 5} more\\)")
    out.append("")

    # Positions
    if report.positions_table:
        out.append(f"*Positions:* {len(report.positions_table)} open")
        for p in report.positions_table[:5]:
            qty_val = float(p.get("qty", 0.0))
            qty_str = f"{qty_val:.3f}"
            out.append(
                f"  • {e(str(p.get('ticker', '?')))} qty={e(qty_str)}"
            )
    else:
        out.append("*Positions:* none open")
    out.append("")

    # P&L
    out.append("*P&L \\(proxy\\):*")
    out.append(f"  • Today: {e(_fmt_pnl(report.pnl_today))}")
    out.append(f"  • MTD: {e(_fmt_pnl(report.pnl_mtd))}")
    out.append(f"  • YTD: {e(_fmt_pnl(report.pnl_ytd))}")
    out.append("")

    if report.reflections_section:
        out.append(f"*Lessons \\(24h\\):* {len(report.reflections_section)}")
        for r in report.reflections_section[:3]:
            out.append(f"  • {e(r[:200])}")
    out.append("")

    promoted = report.hypotheses_changes.get("promoted", [])
    falsified = report.hypotheses_changes.get("falsified", [])
    if promoted or falsified:
        out.append("*Hypotheses:*")
        if promoted:
            out.append(f"  • Promoted: {e(', '.join(promoted))}")
        if falsified:
            out.append(f"  • Falsified: {e(', '.join(falsified))}")
        out.append("")

    if report.factor_verdicts_today:
        parts = ", ".join(
            f"{tier}={count}"
            for tier, count in report.factor_verdicts_today.items()
        )
        out.append(f"*Factors today:* {e(parts)}")
        out.append("")

    if report.open_proposals:
        out.append(f"*Pending proposals:* {len(report.open_proposals)}")
        for p in report.open_proposals[:5]:
            out.append(
                f"  • {e(str(p.get('ticker', '?')))} "
                f"\\(TTL {e(str(p.get('ttl_remaining', '?')))}\\)"
            )

    body = "\n".join(out)

    # Truncate if necessary
    if len(body) > max_chars:
        marker = (
            "…\n\n_\\(truncated, see "
            f"`{_escape_telegram_md_v2(str(full_path))}` for full\\)_"
        )
        # Reserve room for the marker.
        keep = max_chars - len(marker)
        if keep < 0:
            keep = 0
        body = body[:keep] + marker

    return body
