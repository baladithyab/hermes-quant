"""quantcore.calendar_events — asof-honest scheduled-event calendar (ADR-0084).

A forward event has TWO timestamps (the honesty model):

  * ``scheduled_for`` — when the event WILL happen (the forward payload);
  * ``announced_at``  — when the schedule itself became public (the asof anchor).

The system must never see an event's mere EXISTENCE before ``announced_at``.
``upcoming()`` enforces this: rows with ``announced_at > asof`` are invisible.

Determinism rails:
  * stdlib + pydantic only; no network, no clock reads — callers pass ``asof``.
  * Malformed seed data must NEVER fabricate an event blackout: invalid rows
    are DROPPED (collected into a returned warning list), never raised, and a
    dropped row can therefore never trigger the gate's blackout rule.
  * ``upcoming()`` output is JSON-ready and matches the exact shape consumed
    by ``quantcore.gate.in_event_blackout``: ``{kind, impact, scheduled_for}``.

Seed shipping note (per the no-new-packaging-config decision): the vendored
seed ``quantcore/seeds/macro_calendar_2026.json`` is NOT declared as wheel
package-data in pyproject.toml. Callers (the plugin's commands) must pass an
explicit path to ``load_seed`` — commands use the plugin-root path. For
source/editable installs ``DEFAULT_SEED_PATH`` (derived from ``__file__``)
points at the vendored copy and is used by the test suite.

Freshness (ADR-0084 C2, annual-refresh nudge): ``freshness_check`` warns when
the last seeded event is less than 60 days ahead of ``asof`` — time to vendor
next year's schedule.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

UTC = timezone.utc

#: Vendored seed for source/editable installs. Wheel installs may not include
#: package data — callers should pass an explicit path (see module docstring).
DEFAULT_SEED_PATH = Path(__file__).resolve().parent / "seeds" / "macro_calendar_2026.json"

#: freshness_check warns when the last seed event is closer than this (days).
FRESHNESS_MIN_DAYS_AHEAD = 60.0


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class CalendarEvent(BaseModel):
    """One scheduled macro event, asof-honest by construction.

    Validation guarantees ``announced_at <= scheduled_for`` — an event can
    never be known before its schedule was published.
    """

    kind: str = Field(min_length=1, description="e.g. 'fomc_decision', 'cpi_release'")
    impact: str = Field(min_length=1, description="'high' | 'medium' | 'low'")
    scheduled_for: datetime = Field(description="when the event WILL occur (UTC)")
    announced_at: datetime = Field(description="when the schedule became public (UTC)")
    source_url: str = Field(min_length=1, description="provenance of the date")

    @field_validator("kind", "impact")
    @classmethod
    def _normalize(cls, v: str) -> str:
        v = v.strip().lower()
        if not v:
            raise ValueError("must be non-empty")
        return v

    @field_validator("scheduled_for", "announced_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _to_utc(v)

    @model_validator(mode="after")
    def _honesty(self) -> "CalendarEvent":
        if self.announced_at > self.scheduled_for:
            raise ValueError("announced_at must be <= scheduled_for (asof-honesty)")
        return self


def load_seed(path: str | Path) -> tuple[list[CalendarEvent], list[str]]:
    """Load a seed JSON file. NEVER raises.

    Returns ``(events, warnings)``. Invalid rows (missing fields, bad
    timestamps, ``announced_at > scheduled_for``, non-dict entries) are
    dropped and described in ``warnings`` — a malformed row can therefore
    never fabricate an event blackout downstream. A missing/unreadable file
    yields ``([], [warning])``.

    Accepted top-level shapes: ``{"events": [...]}`` (the vendored layout,
    ``_comment`` keys ignored) or a bare JSON list of rows.
    """
    warnings: list[str] = []
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        return [], [f"seed_unreadable:{path}:{exc.__class__.__name__}"]
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError) as exc:
        return [], [f"seed_invalid_json:{path}:{exc.__class__.__name__}"]

    if isinstance(doc, dict):
        rows: Any = doc.get("events", [])
    else:
        rows = doc
    if not isinstance(rows, list):
        return [], [f"seed_events_not_a_list:{path}"]

    events: list[CalendarEvent] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            warnings.append(f"row_{i}_not_a_dict_dropped")
            continue
        try:
            events.append(CalendarEvent.model_validate(row))
        except Exception as exc:  # pydantic ValidationError and anything else
            kind = row.get("kind", "?")
            warnings.append(f"row_{i}_invalid_dropped:kind={kind}:{exc.__class__.__name__}")
    events.sort(key=lambda e: e.scheduled_for)
    return events, warnings


def upcoming(
    events: list[CalendarEvent],
    *,
    asof: datetime,
    window_days: float,
    high_impact_only: bool = True,
) -> list[dict]:
    """Pure asof-honest filter -> JSON-ready dicts for the gate.

    Includes an event iff:
      * ``announced_at <= asof``      (its EXISTENCE is known — asof-honesty);
      * ``asof <= scheduled_for``     (still ahead);
      * ``scheduled_for <= asof + window_days``;
      * impact == 'high' when ``high_impact_only``.

    Output rows are ``{kind, impact, scheduled_for}`` (ISO-8601 UTC string),
    the exact shape ``quantcore.gate.in_event_blackout`` consumes, sorted by
    ``scheduled_for``.
    """
    asof = _to_utc(asof)
    horizon = asof + timedelta(days=window_days)
    out: list[dict] = []
    for ev in sorted(events, key=lambda e: e.scheduled_for):
        if ev.announced_at > asof:
            continue  # not yet public as of `asof` — invisible
        if high_impact_only and ev.impact != "high":
            continue
        if ev.scheduled_for < asof or ev.scheduled_for > horizon:
            continue
        out.append(
            {
                "kind": ev.kind,
                "impact": ev.impact,
                "scheduled_for": ev.scheduled_for.isoformat(),
            }
        )
    return out


def freshness_check(
    events: list[CalendarEvent],
    *,
    asof: datetime,
    min_days_ahead: float = FRESHNESS_MIN_DAYS_AHEAD,
) -> list[str]:
    """Annual-refresh nudge (ADR-0084 C2). NEVER raises.

    Returns warnings if the seed is empty or its LAST event is less than
    ``min_days_ahead`` days ahead of ``asof`` — i.e. the calendar's horizon is
    running out and next year's schedule should be vendored. Empty list means
    the seed is fresh. Advisory only: staleness never blocks, and never
    fabricates a blackout.
    """
    asof = _to_utc(asof)
    if not events:
        return ["calendar_seed_empty:refresh_required"]
    last = max(ev.scheduled_for for ev in events)
    days_ahead = (last - asof).total_seconds() / 86400.0
    if days_ahead < min_days_ahead:
        return [
            "calendar_seed_stale:last_event_"
            f"{last.isoformat()}_is_{days_ahead:.1f}d_ahead_lt_{min_days_ahead:.0f}d:"
            "vendor_next_year_schedule"
        ]
    return []
