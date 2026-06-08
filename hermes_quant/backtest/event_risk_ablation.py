"""hermes_quant.backtest.event_risk_ablation — make HERMES_QUANT_EVENT_RISK measurable.

Why this module exists
----------------------
The flag-ablation harness (``hermes_quant/backtest/ablation.py`` +
``cli/ablate.py``) measures a flag's return impact by running the SAME
walk-forward window with the flag OFF vs ON. But it drives ``AdvisorStrategy``,
whose risk-gate call (``DefaultRiskGate.gate``) reads the pre-event blackout
carrier from ``signal.metadata['event_risk']`` (see ``risk/gate.py`` line ~598)
— and ``AdvisorStrategy`` never populates that carrier. So ablating
``HERMES_QUANT_EVENT_RISK`` through the plain advisor path is a guaranteed
FALSE NULL: the flag toggles a guard that never has data to bite on. That is
exactly why ``cli/ablate.py`` historically REFUSED the flag with
``verdict: NOT_MEASURABLE`` — refusing a verdict is more honest than printing a
confident-looking null.

This module closes that gap (ADR-0084 follow-up; the C2a deliverable). It is the
"inject the carrier" path the measurability matrix (``NOTES_ABLATION.md``)
pointed at:

  * ``synthetic_macro_calendar`` builds asof-honest scheduled macro events
    (FOMC/CPI/NFP), reusing the production ``CalendarEvent`` dataclass so the
    asof invariant (``announced_at <= scheduled_for``) is enforced by
    construction. Release schedules are published ~a year ahead, so a calendar
    is asof-honest BY CONSTRUCTION — the existence of "FOMC on 2024-03-20" was
    knowable months earlier; only the *outcome* (the rate decision) must never
    be peeked, and ``CalendarEvent.outcome`` is always None.
  * ``EventRiskAblationStrategy`` subclasses ``AdvisorStrategy`` and overrides
    ONLY the gate seam: before delegating to the parent gate, it stamps the
    asof-filtered event carrier into ``signal.metadata['event_risk']`` so the
    gate's ``in_event_blackout`` predicate actually fires on blackout days.

Discipline (money-software)
---------------------------
* ADDITIVE + READ-ONLY: this is an eval-only strategy. It changes no production
  decision path and no flag default; it only feeds a synthetic, asof-honest
  carrier so the OFF-vs-ON ablation is a TRUE measurement, not a false null.
* ASOF-HONEST (defense in depth): the strategy filters the injected calendar to
  ``announced_at <= asof`` at gate time, mirroring the production calendar
  wiring's contract — so even a mis-built calendar cannot leak a
  not-yet-announced event into a decision. The gate's ``in_event_blackout`` then
  tests only the FORWARD ``scheduled_for`` window (knowable at decision time,
  like the halt/cooldown rules). No outcome is ever read.
* IMMUTABILITY: ``AggregatedSignal`` is frozen, so the carrier is stamped via
  ``dataclasses.replace`` (a new signal), never an in-place mutation.
* DETERMINISM: the calendar is built deterministically from the window; OFF and
  ON legs see the IDENTICAL carrier, so the only difference between legs is the
  flag value — preserving the harness's bit-identical-when-flag-unchanged
  contract.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from hermes_quant.backtest.strategy import AdvisorStrategy
from hermes_quant.catalyst.calendar import CalendarEvent

UTC = timezone.utc

# Tiering mirrors the research note (docs/research/2026-06-08-r-macro-event-risk.md):
# FOMC >= CPI > NFP ~= PPI. Only HIGH-impact events bite the blackout guard
# (in_event_blackout high_impact_only=True), so the tier here maps Tier-1 macro
# to "high" and the rest to "medium" (present in the carrier for realism /
# explainability, but they do not trigger a blackout).
_HIGH_IMPACT_KINDS = frozenset({"fomc", "cpi"})


def synthetic_macro_calendar(
    start: Any,
    end: Any,
    *,
    cadence_days: int = 21,
    announce_lead_days: int = 30,
    kinds: Sequence[str] = ("fomc", "cpi", "nfp"),
) -> list[CalendarEvent]:
    """Build a deterministic, asof-honest macro-event calendar spanning [start, end].

    Events are placed every ``cadence_days`` (default ~21 ≈ the real FOMC/CPI
    monthly-ish cadence), cycling through ``kinds``. Each event is "announced"
    ``announce_lead_days`` before it happens (release schedules are public ~a
    year out; 30 days is a conservative, clearly-honest lead). The production
    ``CalendarEvent`` dataclass enforces ``announced_at <= scheduled_for`` and
    tz-awareness at construction, so every event here is asof-honest by
    construction.

    Deterministic: same window + params => same calendar. Both ablation legs get
    the identical list, so the only OFF-vs-ON difference is the flag.

    Parameters
    ----------
    start, end:
        Window bounds (inclusive). Parsed to tz-aware UTC.
    cadence_days:
        Spacing between consecutive scheduled events.
    announce_lead_days:
        How far before ``scheduled_for`` the schedule became public
        (``announced_at = scheduled_for - lead``).
    kinds:
        Event kinds to cycle through. ``fomc``/``cpi`` are HIGH-impact (bite the
        blackout); others are medium (carried for realism, never trigger).

    Returns
    -------
    list[CalendarEvent]
        Sorted by ``scheduled_for``.
    """
    start_ts = _to_utc(start)
    end_ts = _to_utc(end)
    if end_ts < start_ts:
        raise ValueError(f"end ({end_ts}) < start ({start_ts})")

    events: list[CalendarEvent] = []
    cur = start_ts
    i = 0
    lead = timedelta(days=int(announce_lead_days))
    while cur <= end_ts:
        kind = kinds[i % len(kinds)]
        impact = "high" if kind in _HIGH_IMPACT_KINDS else "medium"
        # Schedule at 18:00 UTC (≈ 2pm ET, the FOMC-print hour) for realism;
        # the date is what matters for a 1-day window on daily bars.
        scheduled_for = cur.replace(hour=18, minute=0, second=0, microsecond=0)
        events.append(
            CalendarEvent(
                kind=kind,
                scheduled_for=scheduled_for,
                announced_at=scheduled_for - lead,
                market="US",
                impact=impact,
                title=f"Synthetic {kind.upper()} (ablation)",
                source="synthetic-ablation-calendar",
            )
        )
        cur = cur + timedelta(days=int(cadence_days))
        i += 1

    events.sort(key=lambda e: e.scheduled_for)
    return events


def _to_utc(v: Any) -> datetime:
    """Parse to a tz-aware UTC ``datetime``. Naive inputs are assumed UTC."""
    ts = pd.Timestamp(v)
    if ts is pd.NaT:
        raise ValueError(f"cannot parse a valid timestamp from {v!r}")
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    out = ts.to_pydatetime()
    assert isinstance(out, datetime)  # noqa: S101 - NaT already excluded above
    return out


def build_event_risk_payload(
    calendar: Sequence[CalendarEvent],
    *,
    asof: datetime,
) -> dict[str, Any]:
    """Build the ``ctx.extras['event_risk']``-shaped carrier the gate reads.

    ASOF-HONEST FILTER: keep only events whose ``announced_at <= asof`` — the
    schedule must have been public by the decision time. This mirrors the
    production calendar wiring (which filters to ``announced_at <= decision_asof``
    upstream) and is defense-in-depth: even a mis-built calendar cannot leak a
    not-yet-announced event into a decision. The gate then tests only the forward
    ``scheduled_for`` window.

    The returned dict has the exact shape ``in_event_blackout`` expects:
    ``{"events": [event.to_dict(), ...]}`` sorted by ``scheduled_for`` so the
    predicate's "first qualifying event" return is deterministic.
    """
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=UTC)
    visible = [e for e in calendar if e.announced_at <= asof]
    visible.sort(key=lambda e: e.scheduled_for)
    return {"events": [e.to_dict() for e in visible]}


class EventRiskAblationStrategy(AdvisorStrategy):
    """``AdvisorStrategy`` that injects an asof-honest event-risk carrier so the
    ``HERMES_QUANT_EVENT_RISK`` blackout guard genuinely bites in an ablation.

    Overrides ONLY ``_gate``: it stamps ``signal.metadata['event_risk']`` from
    the injected calendar (filtered to ``announced_at <= asof``) via a frozen-safe
    ``dataclasses.replace`` before delegating to the parent gate. Everything else
    — analyst fan-out, persistent BMA, settlement loop — is inherited unchanged.

    With the flag OFF the gate skips the whole event block (byte-identical to a
    plain ``AdvisorStrategy``); with it ON the gate sees the carrier and silences
    fresh opens inside a blackout window. The OFF-vs-ON return delta is then a
    TRUE measurement of the guard, not a false null.

    Parameters
    ----------
    calendar:
        The macro-event calendar to inject. Build it with
        ``synthetic_macro_calendar(...)`` for an offline ablation, or pass a real
        ``list[CalendarEvent]`` (e.g. the vendored FOMC seed) for a real-data run.
    **kwargs:
        Forwarded to ``AdvisorStrategy.__init__``.
    """

    def __init__(self, universe: list[str], *, calendar: Sequence[CalendarEvent], **kwargs: Any) -> None:
        super().__init__(universe, **kwargs)
        # Store sorted; build_event_risk_payload re-filters per-asof.
        self._calendar = sorted(calendar, key=lambda e: e.scheduled_for)

    def _gate(self, symbol: str, signal, asof: pd.Timestamp):  # type: ignore[override]
        asof_ts = pd.Timestamp(asof)
        asof_dt = asof_ts.to_pydatetime()
        if asof_dt.tzinfo is None:
            asof_dt = asof_dt.replace(tzinfo=UTC)
        payload = build_event_risk_payload(self._calendar, asof=asof_dt)
        # AggregatedSignal is frozen — stamp the carrier via replace (new object),
        # merging onto any existing metadata so we never drop the aggregator's keys.
        merged_meta = dict(signal.metadata or {})
        merged_meta["event_risk"] = payload
        stamped = dataclasses.replace(signal, metadata=merged_meta)
        return super()._gate(symbol, stamped, asof)
