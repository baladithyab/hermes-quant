"""Tests for quantcore.calendar_events (ADR-0084 asof-honest macro calendar)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from quantcore.calendar_events import (
    DEFAULT_SEED_PATH,
    CalendarEvent,
    freshness_check,
    load_seed,
    upcoming,
)
from quantcore.config import RiskConfig
from quantcore.gate import RiskGate, in_event_blackout

from .conftest import ASOF, make_costs, make_portfolio, make_signal

UTC = timezone.utc


def _event(
    kind="cpi_release",
    impact="high",
    scheduled_for=None,
    announced_at=None,
) -> CalendarEvent:
    scheduled_for = scheduled_for or ASOF + timedelta(days=0.5)
    announced_at = announced_at or scheduled_for - timedelta(days=180)
    return CalendarEvent(
        kind=kind,
        impact=impact,
        scheduled_for=scheduled_for,
        announced_at=announced_at,
        source_url="https://example.gov/schedule",
    )


# ---------------------------------------------------------------- seed loading


def test_seed_loads_and_validates():
    events, warnings = load_seed(DEFAULT_SEED_PATH)
    assert warnings == []
    assert len(events) == 32  # 8 FOMC + 12 CPI + 12 NFP, all verified
    kinds = {e.kind for e in events}
    assert kinds == {"fomc_decision", "cpi_release", "nfp_release"}
    assert sum(e.kind == "fomc_decision" for e in events) == 8
    assert sum(e.kind == "cpi_release" for e in events) == 12
    assert sum(e.kind == "nfp_release" for e in events) == 12
    assert all(e.impact == "high" for e in events)
    assert all(e.source_url.startswith("https://") for e in events)
    assert all(e.scheduled_for.year == 2026 for e in events)
    # load_seed returns rows sorted by scheduled_for
    times = [e.scheduled_for for e in events]
    assert times == sorted(times)


def test_seed_honesty_holds_for_every_row():
    events, _ = load_seed(DEFAULT_SEED_PATH)
    for ev in events:
        assert ev.announced_at <= ev.scheduled_for
        assert ev.announced_at.tzinfo is not None
        assert ev.scheduled_for.tzinfo is not None


# ---------------------------------------------------------- malformed data


def test_malformed_rows_dropped_never_raised(tmp_path):
    seed = {
        "events": [
            # valid
            {
                "kind": "cpi_release",
                "impact": "high",
                "scheduled_for": "2026-06-10T12:30:00+00:00",
                "announced_at": "2025-12-01T00:00:00+00:00",
                "source_url": "https://www.bls.gov/schedule/news_release/cpi.htm",
            },
            # announced AFTER scheduled -> honesty violation, dropped
            {
                "kind": "fomc_decision",
                "impact": "high",
                "scheduled_for": "2026-06-17T18:00:00+00:00",
                "announced_at": "2026-07-01T00:00:00+00:00",
                "source_url": "https://example.gov",
            },
            # missing scheduled_for -> dropped
            {"kind": "nfp_release", "impact": "high", "source_url": "https://example.gov"},
            # garbage timestamp -> dropped
            {
                "kind": "nfp_release",
                "impact": "high",
                "scheduled_for": "not-a-date",
                "announced_at": "2025-12-01T00:00:00+00:00",
                "source_url": "https://example.gov",
            },
            # non-dict row -> dropped
            "junk",
        ]
    }
    p = tmp_path / "bad_seed.json"
    p.write_text(json.dumps(seed), encoding="utf-8")
    events, warnings = load_seed(p)
    assert len(events) == 1
    assert events[0].kind == "cpi_release"
    assert len(warnings) == 4
    # the dropped rows can never fabricate a blackout: the only surviving
    # event drives the gate, not the malformed ones
    asof = datetime(2026, 6, 16, 18, 0, tzinfo=UTC)  # 1d before dropped FOMC row
    fired, _ = in_event_blackout(
        upcoming(events, asof=asof, window_days=1.0), asof=asof, window_days=1.0
    )
    assert fired is False


def test_missing_file_and_bad_json_never_raise(tmp_path):
    events, warnings = load_seed(tmp_path / "nope.json")
    assert events == [] and len(warnings) == 1
    p = tmp_path / "garbage.json"
    p.write_text("{not json", encoding="utf-8")
    events, warnings = load_seed(p)
    assert events == [] and len(warnings) == 1


# ----------------------------------------------------------------- upcoming()


def test_upcoming_is_asof_honest():
    sched = ASOF + timedelta(days=0.5)
    known = _event(scheduled_for=sched, announced_at=ASOF - timedelta(days=30))
    not_yet_public = _event(
        kind="fomc_decision",
        scheduled_for=sched,
        announced_at=ASOF + timedelta(hours=1),  # announced AFTER asof
    )
    out = upcoming([known, not_yet_public], asof=ASOF, window_days=1.0)
    assert [d["kind"] for d in out] == ["cpi_release"]  # fomc row invisible


def test_upcoming_window_and_impact_filters():
    inside = _event(scheduled_for=ASOF + timedelta(days=0.5))
    beyond = _event(kind="nfp_release", scheduled_for=ASOF + timedelta(days=3.0))
    past = _event(kind="fomc_decision", scheduled_for=ASOF - timedelta(hours=1))
    low = _event(kind="jolts", impact="low", scheduled_for=ASOF + timedelta(days=0.5))
    events = [beyond, past, low, inside]
    out = upcoming(events, asof=ASOF, window_days=1.0)
    assert [d["kind"] for d in out] == ["cpi_release"]
    # widen the window / drop the impact filter
    out = upcoming(events, asof=ASOF, window_days=5.0, high_impact_only=False)
    assert [d["kind"] for d in out] == ["jolts", "cpi_release", "nfp_release"] or [
        d["kind"] for d in out
    ] == ["cpi_release", "jolts", "nfp_release"]


def test_upcoming_output_shape_is_gate_ready():
    out = upcoming([_event()], asof=ASOF, window_days=1.0)
    assert len(out) == 1
    row = out[0]
    assert set(row) == {"kind", "impact", "scheduled_for"}
    assert isinstance(row["scheduled_for"], str)
    # ISO-8601 round-trip, tz-aware UTC
    parsed = datetime.fromisoformat(row["scheduled_for"])
    assert parsed.tzinfo is not None


def test_upcoming_against_real_seed_at_conftest_asof():
    # ASOF = 2026-06-09 14:00Z; CPI prints 2026-06-10 12:30Z (~0.94d ahead)
    events, _ = load_seed(DEFAULT_SEED_PATH)
    out = upcoming(events, asof=ASOF, window_days=1.0)
    assert [d["kind"] for d in out] == ["cpi_release"]
    assert out[0]["scheduled_for"] == "2026-06-10T12:30:00+00:00"


# ------------------------------------------------------------ freshness check


def test_freshness_check_fires_on_stale_seed():
    stale = [_event(scheduled_for=ASOF + timedelta(days=10))]
    warns = freshness_check(stale, asof=ASOF)
    assert len(warns) == 1 and "calendar_seed_stale" in warns[0]
    assert freshness_check([], asof=ASOF) == ["calendar_seed_empty:refresh_required"]
    fresh = stale + [_event(scheduled_for=ASOF + timedelta(days=120))]
    assert freshness_check(fresh, asof=ASOF) == []


def test_real_seed_is_fresh_at_conftest_asof():
    events, _ = load_seed(DEFAULT_SEED_PATH)
    assert freshness_check(events, asof=ASOF) == []  # last event 2026-12-10


# ----------------------------------------------- integration with the gate


def test_upcoming_feeds_gate_blackout_predicate():
    ev = _event(scheduled_for=ASOF + timedelta(days=0.5))  # 0.5 days ahead
    event_risk = upcoming([ev], asof=ASOF, window_days=1.0)
    fired, reason = in_event_blackout(event_risk, asof=ASOF, window_days=1.0)
    assert fired is True
    assert reason == "event_blackout_cpi_release_high_impact"


def test_upcoming_feeds_full_gate_rule3_5():
    ev = _event(scheduled_for=ASOF + timedelta(days=0.5))
    event_risk = upcoming([ev], asof=ASOF, window_days=1.0)
    gate = RiskGate(RiskConfig(event_risk_enabled=True, paper_zero_costs=True))
    decision = gate.gate(
        make_signal(direction=1, confidence=0.7, event_risk=event_risk),
        make_costs(),
        make_portfolio(),
    )
    assert decision.verdict == "silence"
    assert decision.rule == "rule3_5_event_blackout"
    # same signal with no event risk trades through
    decision = gate.gate(
        make_signal(direction=1, confidence=0.7, event_risk=[]),
        make_costs(),
        make_portfolio(),
    )
    assert decision.verdict == "action"
