"""hermes_quant.risk.open_guard — advisor-layer intraday open-guard (ADR-0072).

Problem
-------
The advisor layer (``quant-daily-interim.py``) fires once at premarket and again
at midday/EOD. Its only idempotency key is ``proposal_id``, which is fresh per
run by construction (``prop_<ISO_seconds>_<symbol>_<rand6>``, ADR-0015 §D3). So
two runs on the same ET trading day, acting on the same daily bar, mint distinct
proposals and open the *same* ``(symbol, direction)`` twice — doubling exposure
with no new information. (Observed 2026-05-29: 26 names opened at 12:34 UTC and
re-opened identically at 15:04 UTC.)

The autonomous-tick and playbook layers already guard against this via per-day
journals. This module is the equivalent guard for the advisor layer, built on
the *authoritative* sources (``executions.jsonl`` + ``proposals.db``) rather than
a parallel journal that could drift from the real book.

Design
------
* Direction-aware key ``(symbol, sign(target_position_pct))`` — a genuine
  same-day SHORT→LONG flip is a NEW decision, not a duplicate, and is allowed.
* "Already opened today" = filled-today (executions) **OR** pending-proposal-today
  (proposals), OR'd — correct in both autonomy=paper and HITL modes.
* ET trading-day boundary (not UTC) — matches ``today_et_date()`` in the other
  layers and avoids a latent bug for crons running in the 19:00–24:00 PT window.
* ``allow_intraday_add=True`` on a pick bypasses the guard (deliberate scale-in).
* Pure core (injectable ``executions`` / ``pending`` / ``now_et``) + thin disk/DB
  loaders, so the logic is unit-testable without disk, clock, or a live DB.

The guard is a *filter*, not a halt: it skips individual redundant picks while
letting genuinely-new names through. See ADR-0072.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

DEFAULT_ACCOUNT = "alpaca-paper"
QUANT_HOME = Path.home() / ".hermes" / "quant"
DEFAULT_EXECUTIONS_PATH = QUANT_HOME / "executions.jsonl"

# Kill-flag for debugging only (ADR-0072 ships default-ON).
_DISABLE_ENV = "HERMES_QUANT_OPEN_GUARD"


# ---------------------------------------------------------------------------
# Direction helpers
# ---------------------------------------------------------------------------

def _sign(x: float) -> int:
    if x > 1e-12:
        return 1
    if x < -1e-12:
        return -1
    return 0


def pick_direction(pick: dict[str, Any]) -> int:
    """Direction (+1 long / -1 short / 0 flat) of an actionable pick.

    Reads ``target_position_pct`` first (the post-gate signed size), falling
    back to the BMA ``aggregated_signal.direction`` if size is absent.
    """
    tp = pick.get("target_position_pct")
    if tp is not None:
        return _sign(float(tp))
    agg = (pick.get("advisor_result") or {}).get("aggregated_signal") or {}
    return _sign(float(agg.get("direction") or pick.get("direction") or 0))


def _exec_direction(row: dict[str, Any]) -> int:
    return _sign(float(row.get("target_position_pct") or 0.0))


def _pending_direction(prop: dict[str, Any]) -> int:
    """Direction of a pending proposal dict.

    Mirrors the extraction in hermes_quant.proposals (advisor_result.
    aggregated_signal.direction with fallbacks).
    """
    advisor = prop.get("advisor_result") or {}
    aggregated = advisor.get("aggregated_signal") or {}
    direction = aggregated.get("direction")
    if direction is None:
        direction = advisor.get("direction")
    if direction is None:
        # last resort: sign of a signed target if the proposal carries one
        direction = advisor.get("target_size_pct_nav") or 0
    return _sign(float(direction or 0))


# ---------------------------------------------------------------------------
# ET-day boundary
# ---------------------------------------------------------------------------

def _et_date(iso_utc: str, now_et: datetime) -> str | None:
    """ET calendar date (YYYY-MM-DD) for an ISO-UTC timestamp.

    Returns None on unparseable input (fail-open per-row: a bad row does not
    block a pick, it's simply ignored as evidence).
    """
    try:
        dt = datetime.fromisoformat(iso_utc)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ET).strftime("%Y-%m-%d")


def _today_et(now_et: datetime) -> str:
    return now_et.astimezone(ET).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Core predicate
# ---------------------------------------------------------------------------

def already_opened_today(
    symbol: str,
    direction: int,
    account: str,
    *,
    executions: Iterable[dict[str, Any]],
    pending: Iterable[dict[str, Any]],
    now_et: datetime,
) -> tuple[bool, str | None]:
    """Has ``(symbol, direction)`` already been opened today on ``account``?

    Returns ``(blocked, reason)``. ``reason`` is a human string when blocked,
    else None. OR-semantics across two evidence sources:

    1. A fill in ``executions`` with the same asset, same direction, and an
       ``asof_execution`` that falls on today's ET trading day.
    2. A ``pending`` proposal for the same symbol + direction created today (ET).

    A flat pick (direction == 0) never fires, so it is never blocked.
    Rows tagged to a different ``account`` are ignored.
    """
    if direction == 0:
        return False, None

    today = _today_et(now_et)

    # Source 1: filled today
    for row in executions:
        if row.get("asset") != symbol:
            continue
        row_account = row.get("account", account)  # rows lack account today → assume queried
        if row_account != account:
            continue
        if _exec_direction(row) != direction:
            continue
        fill_day = _et_date(row.get("asof_execution") or "", now_et)
        if fill_day == today:
            side = "SHORT" if direction < 0 else "LONG"
            hhmm = (row.get("asof_execution") or "")[11:16]
            return True, f"filled {side} today at {hhmm} UTC"

    # Source 2: pending proposal today
    for prop in pending:
        if prop.get("symbol") != symbol:
            continue
        if prop.get("state") not in (None, "pending"):
            continue
        if _pending_direction(prop) != direction:
            continue
        prop_day = _et_date(prop.get("created_at") or "", now_et)
        if prop_day == today:
            side = "SHORT" if direction < 0 else "LONG"
            return True, f"pending proposal {side} today"

    return False, None


# ---------------------------------------------------------------------------
# Batch filter
# ---------------------------------------------------------------------------

def open_guard_filter(
    picks: list[dict[str, Any]],
    *,
    account: str = DEFAULT_ACCOUNT,
    executions: Iterable[dict[str, Any]],
    pending: Iterable[dict[str, Any]],
    now_et: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition ``picks`` into ``(kept, deduped)``.

    A pick is deduped if it would re-open a ``(symbol, direction)`` already
    opened today (per :func:`already_opened_today`) OR already kept earlier in
    THIS batch (intra-batch dedup). Deduped picks gain a ``dedup_reason`` key.

    ``allow_intraday_add=True`` on a pick bypasses the guard (kept regardless).
    Flat picks (direction 0) are always kept (they never fire).
    """
    # Materialize once — these iterables are scanned per pick.
    execs = list(executions)
    pend = list(pending)

    kept: list[dict[str, Any]] = []
    deduped: list[dict[str, Any]] = []
    seen_this_batch: set[tuple[str, int]] = set()

    for pick in picks:
        if pick.get("allow_intraday_add"):
            kept.append(pick)
            continue

        symbol = str(pick.get("symbol") or "")
        direction = pick_direction(pick)
        if direction == 0 or not symbol:
            kept.append(pick)
            continue

        key = (symbol, direction)

        # Intra-batch: a second same-direction pick for the same symbol in one run.
        if key in seen_this_batch:
            d = dict(pick)
            d["dedup_reason"] = "duplicate within this batch"
            deduped.append(d)
            continue

        blocked, reason = already_opened_today(
            symbol, direction, account,
            executions=execs, pending=pend, now_et=now_et,
        )
        if blocked:
            d = dict(pick)
            d["dedup_reason"] = reason or "already opened today"
            deduped.append(d)
            continue

        seen_this_batch.add(key)
        kept.append(pick)

    return kept, deduped


# ---------------------------------------------------------------------------
# Disk / DB loaders (used by the cron; not exercised by the pure unit tests)
# ---------------------------------------------------------------------------

def load_executions(path: Path | None = None) -> list[dict[str, Any]]:
    """Load executions.jsonl rows. Fail-open: returns [] on any read error.

    The guard treats missing evidence as "not yet opened" → fail-open lets a
    pick through rather than blocking the whole brief on a corrupt file. This
    is the safe direction for a *dedup* guard: worst case a duplicate slips,
    which the portfolio caps (ADR-0071) still bound.
    """
    p = path or DEFAULT_EXECUTIONS_PATH
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue  # valid JSON but not an object (corrupt/partial append) — skip
            rows.append(row)
    except OSError as e:
        logger.warning("open_guard: could not read %s: %s", p, e)
        return []
    return rows


def load_pending_proposals() -> list[dict[str, Any]]:
    """Load pending proposals as dicts. Fail-open: returns [] on any error."""
    try:
        from hermes_quant.proposals import _proposal_to_dict, get_default_store
    except Exception as e:  # pragma: no cover - import guard
        logger.warning("open_guard: proposals module unavailable: %s", e)
        return []
    try:
        store = get_default_store()
        return [_proposal_to_dict(p) for p in store.list_pending(limit=500)]
    except Exception as e:
        logger.warning("open_guard: could not list pending proposals: %s", e)
        return []


def guard_enabled() -> bool:
    """True unless explicitly disabled via HERMES_QUANT_OPEN_GUARD=0."""
    import os
    return os.environ.get(_DISABLE_ENV, "1").strip().lower() not in ("0", "false", "no")


def filter_actionables(
    picks: list[dict[str, Any]],
    *,
    account: str = DEFAULT_ACCOUNT,
    now_et: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Cron-facing convenience: load real evidence + apply the guard.

    Honors the HERMES_QUANT_OPEN_GUARD=0 kill-flag (returns all picks kept,
    none deduped, when disabled).
    """
    if not guard_enabled():
        return list(picks), []
    now = now_et or datetime.now(UTC).astimezone(ET)
    return open_guard_filter(
        picks,
        account=account,
        executions=load_executions(),
        pending=load_pending_proposals(),
        now_et=now,
    )
