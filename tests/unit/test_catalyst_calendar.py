"""Tests for hermes_quant.catalyst.calendar (ADR-0084 scheduled-event adapter).

Fully offline (injected fetchers / in-memory fixtures, NO network). Covers the
two-timestamp asof invariant, recorded-sample parse, malformed -> [], and the
silence-by-default fetch contract. This seed is the ADAPTER + dataclass only;
the FOMC seed YAML, the event_risk field, and the guard are separate seeds.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hermes_quant.catalyst.calendar import (
    CalendarEvent,
    ingest_ical,
    ingest_rows,
    parse_event_rows,
    parse_ical,
    visible_at,
)

UTC = timezone.utc
ANNOUNCED = datetime(2026, 1, 2, 14, 0, tzinfo=UTC)  # schedule went public
SCHEDULED = datetime(2026, 6, 17, 18, 0, tzinfo=UTC)  # FOMC decision day


# ---------------------------------------------------------------------------
# dataclass: two-timestamp asof invariant
# ---------------------------------------------------------------------------


def test_event_holds_two_timestamps_and_is_outcome_free():
    ev = CalendarEvent(
        kind="fomc",
        scheduled_for=SCHEDULED,
        announced_at=ANNOUNCED,
        market="US",
        impact="high",
    )
    assert ev.announced_at == ANNOUNCED  # asof anchor (when schedule went public)
    assert ev.scheduled_for == SCHEDULED  # forward payload (when it happens)
    assert ev.announced_at <= ev.scheduled_for
    assert ev.outcome is None  # adapter is outcome-free by contract
    assert ev.scheduled_for > ev.announced_at  # genuinely forward-looking


def test_event_rejects_announced_after_scheduled():
    # A schedule cannot become public AFTER the event already happened.
    with pytest.raises(ValueError, match="asof violation"):
        CalendarEvent(
            kind="cpi",
            scheduled_for=ANNOUNCED,  # earlier
            announced_at=SCHEDULED,  # later -> violation
            market="US",
        )


def test_event_rejects_naive_timestamps():
    with pytest.raises(ValueError, match="tz-aware"):
        CalendarEvent(
            kind="fomc",
            scheduled_for=datetime(2026, 6, 17, 18, 0),  # naive
            announced_at=ANNOUNCED,
        )
    with pytest.raises(ValueError, match="tz-aware"):
        CalendarEvent(
            kind="fomc",
            scheduled_for=SCHEDULED,
            announced_at=datetime(2026, 1, 2, 14, 0),  # naive
        )


def test_event_to_dict_roundtrips_iso():
    ev = CalendarEvent(kind="nfp", scheduled_for=SCHEDULED, announced_at=ANNOUNCED, market="US")
    d = ev.to_dict()
    assert d["scheduled_for"] == SCHEDULED.isoformat()
    assert d["announced_at"] == ANNOUNCED.isoformat()
    assert d["outcome"] is None


# ---------------------------------------------------------------------------
# parse_event_rows: recorded sample + asof-honest skipping
# ---------------------------------------------------------------------------

# A recorded sample resembling a vendored seed / vendor-JSON shape.
_SAMPLE_ROWS = [
    {
        "kind": "FOMC",
        "scheduled_for": "2026-06-17T18:00:00Z",
        "announced_at": "2026-01-02T14:00:00Z",
        "market": "US",
        "importance": "high",
        "title": "FOMC rate decision",
    },
    {
        "kind": "earnings",
        "scheduled_for": "2026-07-31T20:00:00+00:00",
        "announced_at": "2026-07-01T13:30:00+00:00",
        "symbol": "AAPL",
        "impact": "3",  # vendor numeric tier -> high
    },
    {  # NO announced_at -> must be SKIPPED (never defaulted to now())
        "kind": "cpi",
        "scheduled_for": "2026-06-10T12:30:00Z",
        "market": "US",
    },
    {  # NO scheduled_for -> cannot gate -> dropped
        "kind": "nfp",
        "announced_at": "2026-01-02T14:00:00Z",
        "market": "US",
    },
    {  # asof violation (announced after scheduled) -> dropped, never raises
        "kind": "ppi",
        "scheduled_for": "2026-01-01T00:00:00Z",
        "announced_at": "2026-06-01T00:00:00Z",
        "market": "US",
    },
]


def test_parse_rows_recorded_sample():
    events = parse_event_rows(_SAMPLE_ROWS, source="seed")
    # Two valid rows survive; the no-announced, no-scheduled, and asof-violation drop.
    assert len(events) == 2
    by_kind = {e.kind: e for e in events}
    assert set(by_kind) == {"fomc", "earnings"}
    assert by_kind["fomc"].impact == "high"
    assert by_kind["fomc"].announced_at == ANNOUNCED
    assert by_kind["fomc"].scheduled_for == SCHEDULED
    assert by_kind["earnings"].symbol == "AAPL"
    assert by_kind["earnings"].impact == "high"  # numeric "3" normalized
    assert all(e.source == "seed" for e in events)
    assert all(e.outcome is None for e in events)


def test_parse_rows_skips_missing_announced_at_never_defaults_now():
    """ADR-0084 D-2: an event with no parseable announced_at is SKIPPED, never
    given a now() asof anchor (which would fabricate lookahead-free provenance)."""
    rows = [{"kind": "cpi", "scheduled_for": "2026-06-10T12:30:00Z", "market": "US"}]
    assert parse_event_rows(rows) == []


def test_parse_rows_two_timestamp_invariant_holds_for_all():
    events = parse_event_rows(_SAMPLE_ROWS)
    for e in events:
        assert e.announced_at.tzinfo is not None
        assert e.scheduled_for.tzinfo is not None
        assert e.announced_at <= e.scheduled_for


def test_parse_rows_malformed_returns_empty():
    # Garbage timestamps -> unparseable -> dropped; never raises.
    rows = [
        {"kind": "fomc", "scheduled_for": "not-a-date", "announced_at": "also-bad"},
        {"kind": "cpi", "scheduled_for": "", "announced_at": ""},
        {},  # totally empty
    ]
    assert parse_event_rows(rows) == []


# ---------------------------------------------------------------------------
# parse_ical: government no-key primary feed shape
# ---------------------------------------------------------------------------

_SAMPLE_ICAL = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
DTSTAMP:20260102T140000Z
DTSTART:20260617T180000Z
SUMMARY:FOMC Meeting
END:VEVENT
BEGIN:VEVENT
DTSTAMP:20260102T140000Z
DTSTART:20260729T180000Z
SUMMARY:FOMC Meeting
END:VEVENT
BEGIN:VEVENT
DTSTART:20260910T120000Z
SUMMARY:Event with no DTSTAMP should be skipped
END:VEVENT
END:VCALENDAR"""


def test_parse_ical_recorded_sample():
    events = parse_ical(_SAMPLE_ICAL, kind="fomc", market="US", impact="high", source="bls")
    # Two well-formed VEVENTs; the DTSTAMP-less one is skipped (no asof anchor).
    assert len(events) == 2
    assert all(e.kind == "fomc" and e.market == "US" and e.impact == "high" for e in events)
    assert events[0].scheduled_for == SCHEDULED
    assert events[0].announced_at == ANNOUNCED
    assert events[0].title == "FOMC Meeting"


def test_parse_ical_malformed_returns_empty():
    assert parse_ical(b"this is not an ical file") == []
    assert parse_ical(b"") == []


# ---------------------------------------------------------------------------
# ingest_*: injectable fetcher, silence-by-default (NO network in test)
# ---------------------------------------------------------------------------


def test_ingest_ical_injected_fetcher():
    def fake_fetch(url, timeout):
        assert url  # caller-supplied url is passed through
        return _SAMPLE_ICAL

    events, latency = ingest_ical("https://example.test/fomc.ics", kind="fomc",
                                  market="US", fetcher=fake_fetch)
    assert len(events) == 2
    assert latency >= 0.0


def test_ingest_ical_fetch_failure_is_silent():
    def boom(url, timeout):
        raise ConnectionError("network down")

    events, lat = ingest_ical("https://example.test/x.ics", fetcher=boom)
    assert events == []  # never raises
    assert lat >= 0.0


def test_ingest_rows_injected_fetcher():
    def fake_rows(timeout):
        return _SAMPLE_ROWS

    events, latency = ingest_rows(fake_rows, source="seed")
    assert len(events) == 2  # same survivors as the direct parse
    assert latency >= 0.0


def test_ingest_rows_fetch_failure_is_silent():
    def boom(timeout):
        raise RuntimeError("seed file missing")

    events, _ = ingest_rows(boom)
    assert events == []  # never raises


# ---------------------------------------------------------------------------
# visible_at: asof gate (an event is unknown before its announcement)
# ---------------------------------------------------------------------------


def test_visible_at_hides_unannounced_events():
    events = parse_event_rows(_SAMPLE_ROWS)  # fomc announced 2026-01-02, earnings 2026-07-01
    # Before the FOMC announcement -> nothing visible.
    before = datetime(2026, 1, 1, tzinfo=UTC)
    assert visible_at(events, before) == []
    # After FOMC announcement but before earnings announcement -> only FOMC.
    mid = datetime(2026, 3, 1, tzinfo=UTC)
    vis = visible_at(events, mid)
    assert [e.kind for e in vis] == ["fomc"]
    # After both announcements -> both visible (even though scheduled_for is future).
    after = datetime(2026, 7, 15, tzinfo=UTC)
    assert {e.kind for e in visible_at(events, after)} == {"fomc", "earnings"}


def test_visible_at_naive_asof_assumed_utc():
    events = parse_event_rows(_SAMPLE_ROWS)
    vis = visible_at(events, datetime(2026, 3, 1))  # naive -> assumed UTC
    assert [e.kind for e in vis] == ["fomc"]
