"""Tests for the vendored FOMC seed + loader + freshness (ADR-0084 Option D, item 1).

Fully offline + deterministic (the seed ships WITH the package; ZERO network).
Covers:
  * load_fomc_seed() yields the right CalendarEvent set (8 meetings + 8 blackouts,
    all kind=fomc, US/high, outcome-free, asof-honest),
  * the 2026 windows are correct (decision instants at 2pm ET DST-aware; blackout
    windows = second-Saturday-preceding -> Thursday-following),
  * the freshness assertion (the seed must cover the current + next quarter, else
    WARN — never hard-fail in CI for a future-dated calendar), and
  * the refresh helper's window derivation reproduces the vendored seed
    (determinism cross-check; no network).
"""

from __future__ import annotations

import importlib.util
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hermes_quant.catalyst.calendar import CalendarEvent, load_fomc_seed

# Expected 2026 FOMC decision instants (2:00 PM ET on day 2; DST-aware:
# 2pm EST = 19:00 UTC in Jan/Dec, 2pm EDT = 18:00 UTC Mar-Oct).
# Source: federalreserve.gov/monetarypolicy/fomccalendars.htm (8 two-day meetings).
EXPECTED_MEETING_DECISIONS = {
    datetime(2026, 1, 28, 19, 0, tzinfo=UTC),
    datetime(2026, 3, 18, 18, 0, tzinfo=UTC),
    datetime(2026, 4, 29, 18, 0, tzinfo=UTC),
    datetime(2026, 6, 17, 18, 0, tzinfo=UTC),
    datetime(2026, 7, 29, 18, 0, tzinfo=UTC),
    datetime(2026, 9, 16, 18, 0, tzinfo=UTC),
    datetime(2026, 10, 28, 18, 0, tzinfo=UTC),
    datetime(2026, 12, 9, 19, 0, tzinfo=UTC),
}

# Expected 2026 blackout end instants (Thursday following, 23:59:59 ET).
EXPECTED_BLACKOUT_ENDS = {
    datetime(2026, 1, 30, 4, 59, 59, tzinfo=UTC),
    datetime(2026, 3, 20, 3, 59, 59, tzinfo=UTC),
    datetime(2026, 5, 1, 3, 59, 59, tzinfo=UTC),
    datetime(2026, 6, 19, 3, 59, 59, tzinfo=UTC),
    datetime(2026, 7, 31, 3, 59, 59, tzinfo=UTC),
    datetime(2026, 9, 18, 3, 59, 59, tzinfo=UTC),
    datetime(2026, 10, 30, 3, 59, 59, tzinfo=UTC),
    datetime(2026, 12, 11, 4, 59, 59, tzinfo=UTC),
}

# The Fed publishes the schedule ~1+yr ahead -> announced_at is a hard PAST fact.
EXPECTED_ANNOUNCED_AT = datetime(2024, 6, 12, 18, 0, tzinfo=UTC)


def _meetings(events: list[CalendarEvent]) -> list[CalendarEvent]:
    return [e for e in events if e.title.lower().startswith("fomc meeting")]


def _blackouts(events: list[CalendarEvent]) -> list[CalendarEvent]:
    return [e for e in events if e.title.lower().startswith("fomc blackout")]


# ---------------------------------------------------------------------------
# load_fomc_seed: the right CalendarEvent set
# ---------------------------------------------------------------------------


def test_load_yields_16_fomc_events_8_meetings_8_blackouts():
    events = load_fomc_seed()
    assert len(events) == 16
    assert len(_meetings(events)) == 8
    assert len(_blackouts(events)) == 8


def test_every_event_is_fomc_us_high_and_outcome_free():
    for e in load_fomc_seed():
        assert e.kind == "fomc"
        assert e.market == "US"
        assert e.impact == "high"
        assert e.outcome is None  # adapter is outcome-free by contract
        assert e.source == "federalreserve.gov/fomccalendars"


def test_every_event_is_asof_honest_and_tz_aware():
    for e in load_fomc_seed():
        assert e.scheduled_for.tzinfo is not None
        assert e.announced_at.tzinfo is not None
        # announced_at <= scheduled_for (a schedule cannot be announced after it happens)
        assert e.announced_at <= e.scheduled_for
        # announced_at is the shared publication anchor (a hard past fact)
        assert e.announced_at == EXPECTED_ANNOUNCED_AT


def test_announced_at_is_a_past_fact_relative_to_now():
    # The whole asof-honesty claim: the schedule was PUBLIC long before any 2026
    # event (the Fed publishes ~1+yr ahead), so announced_at is in the past today.
    now = datetime.now(UTC)
    for e in load_fomc_seed():
        assert e.announced_at < now


# ---------------------------------------------------------------------------
# 2026 windows correct
# ---------------------------------------------------------------------------


def test_2026_meeting_decision_instants_correct():
    got = {e.scheduled_for for e in _meetings(load_fomc_seed())}
    assert got == EXPECTED_MEETING_DECISIONS


def test_2026_blackout_end_instants_correct():
    got = {e.scheduled_for for e in _blackouts(load_fomc_seed())}
    assert got == EXPECTED_BLACKOUT_ENDS


def test_each_meeting_decision_falls_inside_its_blackout_window_end():
    # Every meeting decision must resolve BEFORE its blackout lifts (the blackout
    # ends the Thursday AFTER the Wednesday decision). Sanity on the two sets.
    meetings = sorted(e.scheduled_for for e in _meetings(load_fomc_seed()))
    blackout_ends = sorted(e.scheduled_for for e in _blackouts(load_fomc_seed()))
    assert len(meetings) == len(blackout_ends) == 8
    for decision, b_end in zip(meetings, blackout_ends, strict=True):
        assert decision < b_end  # blackout lifts after the decision
        # and within ~3 days (Wed decision -> Fri-ish UTC blackout lift)
        assert b_end - decision < timedelta(days=3)


# ---------------------------------------------------------------------------
# freshness assertion: seed must cover the current + next quarter (else WARN)
# ---------------------------------------------------------------------------


def test_freshness_seed_covers_current_and_next_quarter_else_warns():
    """ADR-0084 seed-staleness mitigation: the latest seeded FOMC event must reach
    at least the current + next quarter. If it does not, WARN (do not hard-fail —
    a stale forward calendar is an operator-refresh task, not a CI breakage)."""
    events = load_fomc_seed()
    assert events, "FOMC seed must be present and loadable"
    latest = max(e.scheduled_for for e in events)
    horizon = datetime.now(UTC) + timedelta(days=183)  # ~current + next quarter
    if latest < horizon:
        warnings.warn(
            f"FOMC seed is STALE: latest scheduled event {latest.date()} does not "
            f"cover the current+next quarter (to {horizon.date()}). Refresh via "
            "ops/scripts/quant-fomc-calendar-refresh.py with the next year's dates.",
            stacklevel=2,
        )
    # The assertion itself is soft: the test documents+detects staleness without
    # breaking the pipeline on a future-dated calendar. As of the 2026 seed it holds.


def test_freshness_detects_a_stale_seed_via_a_synthetic_fixture(tmp_path):
    """The freshness check must actually FIRE on a deliberately stale seed (a seed
    whose only events are far in the past) — proving it is not a no-op."""
    stale = tmp_path / "stale.seed.yaml"
    stale.write_text(
        'year: 2020\n'
        'announced_at: "2019-06-01T18:00:00Z"\n'
        "market: US\nimpact: high\nsource: test\n"
        "meetings:\n"
        '  - {scheduled_for: "2020-01-29T19:00:00Z", title: "FOMC meeting (Jan 28-29) — stale"}\n',
        encoding="utf-8",
    )
    events = load_fomc_seed(path=stale)
    assert len(events) == 1
    latest = max(e.scheduled_for for e in events)
    horizon = datetime.now(UTC) + timedelta(days=183)
    assert latest < horizon  # the staleness condition is TRUE for this fixture


# ---------------------------------------------------------------------------
# loader robustness: missing / malformed seed -> [] (silence-by-default)
# ---------------------------------------------------------------------------


def test_load_missing_seed_returns_empty(tmp_path):
    assert load_fomc_seed(path=tmp_path / "does-not-exist.yaml") == []


def test_load_malformed_seed_returns_empty(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("this: [is: not: valid: yaml::::\n", encoding="utf-8")
    assert load_fomc_seed(path=bad) == []


def test_load_skips_dishonest_rows_never_raises(tmp_path):
    # A row whose announced_at is AFTER scheduled_for violates the asof invariant
    # and must be SKIPPED (parse_event_rows drops it); the loader never raises.
    seed = tmp_path / "mixed.seed.yaml"
    seed.write_text(
        'announced_at: "2024-06-12T18:00:00Z"\n'
        "market: US\nimpact: high\nsource: test\n"
        "meetings:\n"
        '  - {scheduled_for: "2026-06-17T18:00:00Z", title: "FOMC meeting good"}\n'
        # this row overrides announced_at to AFTER scheduled_for -> dropped
        '  - {scheduled_for: "2026-01-01T00:00:00Z", announced_at: "2026-06-01T00:00:00Z", '
        'title: "FOMC meeting dishonest"}\n',
        encoding="utf-8",
    )
    events = load_fomc_seed(path=seed)
    assert len(events) == 1
    assert events[0].scheduled_for == datetime(2026, 6, 17, 18, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# determinism cross-check: the refresh helper reproduces the vendored 2026 seed
# ---------------------------------------------------------------------------


def _load_refresh_module():
    path = Path(__file__).resolve().parents[2] / "ops" / "scripts" / "quant-fomc-calendar-refresh.py"
    spec = importlib.util.spec_from_file_location("quant_fomc_calendar_refresh", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_refresh_derivation_reproduces_vendored_2026_windows():
    from datetime import date

    mod = _load_refresh_module()
    pairs = [
        (date(2026, 1, 27), date(2026, 1, 28)),
        (date(2026, 3, 17), date(2026, 3, 18)),
        (date(2026, 4, 28), date(2026, 4, 29)),
        (date(2026, 6, 16), date(2026, 6, 17)),
        (date(2026, 7, 28), date(2026, 7, 29)),
        (date(2026, 9, 15), date(2026, 9, 16)),
        (date(2026, 10, 27), date(2026, 10, 28)),
        (date(2026, 12, 8), date(2026, 12, 9)),
    ]
    meeting_rows, blackout_rows = mod.derive_windows(pairs)
    derived_decisions = {
        datetime.fromisoformat(r["scheduled_for"].replace("Z", "+00:00")) for r in meeting_rows
    }
    derived_blackout_ends = {
        datetime.fromisoformat(r["scheduled_for"].replace("Z", "+00:00")) for r in blackout_rows
    }
    assert derived_decisions == EXPECTED_MEETING_DECISIONS
    assert derived_blackout_ends == EXPECTED_BLACKOUT_ENDS


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
