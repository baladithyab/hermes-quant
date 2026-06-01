"""Tests for the calendar SOURCE PRODUCERS (ADR-0084 §D.1 source wiring).

Fully offline (injected fetchers / a fake yfinance Ticker, recorded fixtures, NO
network). Covers BLS .ics CPI/NFP/PPI extraction, FRED key-absent => [], FRED
recorded sample + realtime_start asof anchor, malformed => [], yfinance
date-only earnings + asof-honesty (announced_at = observation instant), and the
ingest_calendar aggregator's silence-by-default per-source isolation.
"""

from __future__ import annotations

from datetime import datetime, timezone

from hermes_quant.catalyst.calendar import (
    ingest_bls_ical,
    ingest_calendar,
    ingest_fred_releases,
    ingest_yfinance_earnings,
    parse_fred_release_dates,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# BLS .ics — no-key government primary (CPI / NFP / PPI extraction)
# ---------------------------------------------------------------------------

# A recorded BLS news-release iCal sample. DTSTAMP = announced_at (when the
# schedule entry was published), DTSTART = scheduled_for (release date).
_BLS_ICAL = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//BLS//Schedule//EN
BEGIN:VEVENT
DTSTAMP:20260102T140000Z
DTSTART:20260610T123000Z
SUMMARY:Consumer Price Index
END:VEVENT
BEGIN:VEVENT
DTSTAMP:20260102T140000Z
DTSTART:20260605T123000Z
SUMMARY:Employment Situation
END:VEVENT
BEGIN:VEVENT
DTSTAMP:20260102T140000Z
DTSTART:20260611T123000Z
SUMMARY:Producer Price Index
END:VEVENT
BEGIN:VEVENT
DTSTAMP:20260102T140000Z
DTSTART:20260612T123000Z
SUMMARY:Quarterly Census of Wibble (untracked low-impact release)
END:VEVENT
END:VCALENDAR"""


def test_bls_ical_extracts_cpi_nfp_ppi_and_drops_untracked():
    def fake_fetch(url, timeout):
        assert "bls.gov" in url  # default no-key BLS URL is used
        return _BLS_ICAL

    events, latency = ingest_bls_ical(fetcher=fake_fetch)
    by_kind = {e.kind: e for e in events}
    # CPI/NFP/PPI surface; the untracked "Quarterly Census" release is dropped.
    assert set(by_kind) == {"cpi", "nfp", "ppi"}
    assert all(e.market == "US" and e.impact == "high" and e.source == "bls" for e in events)
    # asof-honest: announced_at from DTSTAMP, scheduled_for from DTSTART.
    assert by_kind["cpi"].announced_at == datetime(2026, 1, 2, 14, 0, tzinfo=UTC)
    assert by_kind["cpi"].scheduled_for == datetime(2026, 6, 10, 12, 30, tzinfo=UTC)
    assert all(e.outcome is None for e in events)
    assert latency >= 0.0


def test_bls_ical_fetch_failure_is_silent():
    def boom(url, timeout):
        raise ConnectionError("bls down")

    events, lat = ingest_bls_ical(fetcher=boom)
    assert events == []  # never raises
    assert lat >= 0.0


def test_bls_ical_malformed_returns_empty():
    events, _ = ingest_bls_ical(fetcher=lambda url, timeout: b"not an ical")
    assert events == []


# ---------------------------------------------------------------------------
# FRED releases/dates — keyed fallback (key-absent => silence)
# ---------------------------------------------------------------------------

# Recorded FRED releases/dates JSON (include_release_dates_with_no_data=true).
_FRED_JSON = (
    b'{"realtime_start": "2026-01-02", "realtime_end": "9999-12-31",'
    b' "release_dates": ['
    b'  {"release_id": 10, "release_name": "Consumer Price Index", "date": "2026-06-10"},'
    b'  {"release_id": 50, "release_name": "Employment Situation", "date": "2026-06-05"},'
    b'  {"release_id": 53, "release_name": "Gross Domestic Product", "date": "2026-06-26"},'
    b'  {"release_id": 99, "release_name": "Some Untracked Release", "date": "2026-06-20"}'
    b' ]}'
)


def test_fred_key_absent_is_silent(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)

    def boom(url, timeout):  # must NEVER be called when key is absent
        raise AssertionError("fetcher must not be invoked without a key")

    events, lat = ingest_fred_releases(fetcher=boom)
    assert events == []  # key-absent => silence, never crash
    assert lat == 0.0


def test_fred_blank_key_env_is_silent(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "   ")  # blank/whitespace => treated absent
    events, _ = ingest_fred_releases(fetcher=lambda url, timeout: _FRED_JSON)
    assert events == []


def test_fred_recorded_sample_with_explicit_key():
    captured = {}

    def fake_fetch(url, timeout):
        captured["url"] = url
        return _FRED_JSON

    events, latency = ingest_fred_releases(api_key="TESTKEY", fetcher=fake_fetch)
    # CPI/NFP/GDP tracked; the untracked release dropped.
    assert {e.kind for e in events} == {"cpi", "nfp", "gdp"}
    # asof anchor: realtime_start is the observed-as-of date (D-2).
    for e in events:
        assert e.announced_at == datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
        assert e.market == "US"
        assert e.source == "fred"
        assert e.impact == "high"
        assert e.outcome is None
        assert e.announced_at <= e.scheduled_for
    # request was built with the future-dates flag + json + the key.
    assert "include_release_dates_with_no_data=true" in captured["url"]
    assert "file_type=json" in captured["url"]
    assert "api_key=TESTKEY" in captured["url"]


def test_fred_prefers_per_row_release_last_updated_as_anchor():
    payload = {
        "realtime_start": "2026-01-02",
        "release_dates": [
            {
                "release_name": "Consumer Price Index",
                "date": "2026-06-10",
                "release_last_updated": "2026-05-01T08:30:00Z",
            }
        ],
    }
    events = parse_fred_release_dates(payload)
    assert len(events) == 1
    # per-row release_last_updated overrides the container realtime_start.
    assert events[0].announced_at == datetime(2026, 5, 1, 8, 30, tzinfo=UTC)


def test_fred_malformed_returns_empty():
    assert parse_fred_release_dates(b"not json") == []
    assert parse_fred_release_dates(b"[]") == []  # not a dict
    assert parse_fred_release_dates({"release_dates": "not-a-list"}) == []
    # missing realtime_start AND no per-row anchor -> no honest asof -> skipped.
    assert parse_fred_release_dates(
        {"release_dates": [{"release_name": "Consumer Price Index", "date": "2026-06-10"}]}
    ) == []


def test_fred_fetch_failure_is_silent():
    def boom(url, timeout):
        raise TimeoutError("fred timeout")

    events, lat = ingest_fred_releases(api_key="K", fetcher=boom)
    assert events == []
    assert lat >= 0.0


# ---------------------------------------------------------------------------
# yfinance earnings — date-only, best-effort, asof = observation instant
# ---------------------------------------------------------------------------


class _FakeTicker:
    def __init__(self, calendar):
        self.calendar = calendar


def _factory_from(mapping):
    return lambda sym: _FakeTicker(mapping.get(sym, {}))


def test_yfinance_earnings_date_only_with_observation_asof():
    import datetime as _dt

    asof = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    mapping = {
        "AAPL": {"Earnings Date": [_dt.date(2026, 7, 31)]},  # future -> kept
        "MSFT": {"Earnings Date": [_dt.date(2026, 5, 1)]},  # past vs asof -> dropped
    }
    events, latency = ingest_yfinance_earnings(
        ["AAPL", "MSFT"], asof=asof, ticker_factory=_factory_from(mapping)
    )
    assert len(events) == 1
    e = events[0]
    assert e.kind == "earnings"
    assert e.symbol == "AAPL"
    assert e.source == "yfinance"
    # date-only -> 00:00 UTC scheduled_for.
    assert e.scheduled_for == datetime(2026, 7, 31, 0, 0, tzinfo=UTC)
    # asof-honesty: announced_at is the OBSERVATION instant (when we fetched).
    assert e.announced_at == asof
    assert e.announced_at <= e.scheduled_for
    assert e.outcome is None
    assert latency >= 0.0


def test_yfinance_earnings_accepts_iso_string_and_datetime_members():
    asof = datetime(2026, 6, 1, tzinfo=UTC)
    mapping = {
        "NVDA": {"Earnings Date": ["2026-08-20"]},  # ISO string
        "TSLA": {"Earnings Date": [datetime(2026, 7, 15, 20, 0, tzinfo=UTC)]},  # datetime
    }
    events, _ = ingest_yfinance_earnings(
        ["NVDA", "TSLA"], asof=asof, ticker_factory=_factory_from(mapping)
    )
    by_sym = {e.symbol: e for e in events}
    assert by_sym["NVDA"].scheduled_for == datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
    assert by_sym["TSLA"].scheduled_for == datetime(2026, 7, 15, 20, 0, tzinfo=UTC)


def test_yfinance_earnings_missing_data_yields_no_event_never_fabricates():
    asof = datetime(2026, 6, 1, tzinfo=UTC)
    mapping = {
        "AAPL": {},  # empty calendar
        "FOO": {"Earnings Date": []},  # empty list
        "BAR": {"Earnings Date": None},  # None
    }
    events, _ = ingest_yfinance_earnings(
        ["AAPL", "FOO", "BAR", "  "], asof=asof, ticker_factory=_factory_from(mapping)
    )
    assert events == []  # missing data => NO blackout fabricated (ADR-0084 Negative)


def test_yfinance_earnings_per_symbol_failure_is_isolated():
    asof = datetime(2026, 6, 1, tzinfo=UTC)
    import datetime as _dt

    good = {"GOOD": {"Earnings Date": [_dt.date(2026, 9, 1)]}}

    def factory(sym):
        if sym == "BAD":
            raise RuntimeError("yfinance exploded for BAD")
        return _FakeTicker(good.get(sym, {}))

    events, _ = ingest_yfinance_earnings(["BAD", "GOOD"], asof=asof, ticker_factory=factory)
    # the BAD symbol's failure is swallowed; GOOD still surfaces.
    assert [e.symbol for e in events] == ["GOOD"]


def test_yfinance_earnings_default_asof_is_now_and_honest():
    import datetime as _dt

    # A far-future date so it is kept regardless of when "now" lands.
    mapping = {"AAPL": {"Earnings Date": [_dt.date(2099, 1, 1)]}}
    before = datetime.now(timezone.utc)
    events, _ = ingest_yfinance_earnings(["AAPL"], ticker_factory=_factory_from(mapping))
    after = datetime.now(timezone.utc)
    assert len(events) == 1
    # default asof = now(): the honest "when we observed the schedule" anchor.
    assert before <= events[0].announced_at <= after


# ---------------------------------------------------------------------------
# ingest_calendar — aggregator (per-source silence isolation)
# ---------------------------------------------------------------------------


def test_ingest_calendar_aggregates_all_sources(monkeypatch):
    import datetime as _dt

    asof = datetime(2026, 6, 1, tzinfo=UTC)
    mapping = {"AAPL": {"Earnings Date": [_dt.date(2026, 7, 31)]}}
    events = ingest_calendar(
        earnings_symbols=["AAPL"],
        asof=asof,
        bls_fetcher=lambda url, timeout: _BLS_ICAL,
        fred_fetcher=lambda url, timeout: _FRED_JSON,
        fred_api_key="K",
        ticker_factory=_factory_from(mapping),
    )
    kinds = sorted(e.kind for e in events)
    # BLS: cpi/nfp/ppi ; FRED: cpi/nfp/gdp ; yfinance: earnings.
    assert "earnings" in kinds
    assert kinds.count("cpi") == 2  # one from BLS, one from FRED
    assert "gdp" in kinds and "ppi" in kinds
    assert all(e.outcome is None for e in events)


def test_ingest_calendar_one_dead_source_does_not_stop_others(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)  # FRED silent (no key)
    import datetime as _dt

    asof = datetime(2026, 6, 1, tzinfo=UTC)
    mapping = {"AAPL": {"Earnings Date": [_dt.date(2026, 7, 31)]}}
    events = ingest_calendar(
        earnings_symbols=["AAPL"],
        asof=asof,
        bls_fetcher=lambda url, timeout: (_ for _ in ()).throw(ConnectionError("bls down")),
        ticker_factory=_factory_from(mapping),
    )
    # BLS raised (silent []), FRED silent (no key) -> only earnings survives.
    assert [e.kind for e in events] == ["earnings"]


def test_ingest_calendar_no_symbols_skips_earnings():
    events = ingest_calendar(
        bls_fetcher=lambda url, timeout: _BLS_ICAL,
        enable_fred=False,
    )
    assert all(e.kind != "earnings" for e in events)
    assert {e.kind for e in events} == {"cpi", "nfp", "ppi"}


def test_ingest_calendar_all_disabled_is_empty():
    assert ingest_calendar(
        enable_bls=False, enable_fred=False, enable_earnings=False
    ) == []
