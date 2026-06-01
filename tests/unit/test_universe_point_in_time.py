"""Unit tests for hermes_quant.universe.point_in_time (B36).

Survivorship-bias guard: universe = listed-at-asof, not currently-listed.
Fully offline / deterministic. No network, no env mutation that leaks (uses
monkeypatch / explicit ``force=``).
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from hermes_quant.universe.point_in_time import (
    ListingRecord,
    filter_listed_at_asof,
    is_point_in_time_active,
)

# A small injected listing/delisting table exercising every branch.
#   AAPL  — listed 1980, never delisted        -> in
#   MSFT  — listed 1986, never delisted         -> in
#   ENRN  — listed 1985, delisted 2001-11-28    -> delisted-before-asof (the bias bug)
#   IPO99 — listed 2099-01-01, never delisted   -> not-yet-listed at any sane as_of
#   SAMED — listed 2010, delisted == as_of      -> same-day delist removes it
#   UNKWN — absent from table entirely          -> excluded (cannot prove listed)
_TABLE = {
    "AAPL": ListingRecord("AAPL", date(1980, 12, 12)),
    "MSFT": (date(1986, 3, 13), None),
    "ENRN": {"listed_at": "1985-01-01", "delisted_at": "2001-11-28"},
    "IPO99": ListingRecord("IPO99", date(2099, 1, 1)),
    "SAMED": ListingRecord("SAMED", date(2010, 1, 1), date(2020, 6, 1)),
}

_CANDIDATES = ["AAPL", "MSFT", "ENRN", "IPO99", "SAMED", "UNKWN"]
_AS_OF = date(2020, 6, 1)


# ---------------------------------------------------------------------------
# Core acceptance test (the task's required assertions)
# ---------------------------------------------------------------------------


def test_delisted_before_asof_excluded_and_listed_after_asof_excluded():
    kept = filter_listed_at_asof(_CANDIDATES, _AS_OF, _TABLE, force=True)
    # Delisted before as_of (ENRN, delisted 2001) must be excluded.
    assert "ENRN" not in kept
    # Listed after as_of (IPO99, lists 2099) must be excluded.
    assert "IPO99" not in kept
    # Survivors stay.
    assert "AAPL" in kept
    assert "MSFT" in kept


def test_same_day_delist_is_excluded():
    # SAMED delists exactly on as_of -> not tradable that day.
    kept = filter_listed_at_asof(_CANDIDATES, _AS_OF, _TABLE, force=True)
    assert "SAMED" not in kept
    # ...but it IS present the day before.
    kept_day_before = filter_listed_at_asof(
        ["SAMED"], date(2020, 5, 31), _TABLE, force=True
    )
    assert kept_day_before == ["SAMED"]


def test_unknown_symbol_excluded_when_filtering_active():
    # A symbol absent from the listing table cannot be proven listed -> excluded
    # (fail-closed on the per-symbol admission decision).
    kept = filter_listed_at_asof(_CANDIDATES, _AS_OF, _TABLE, force=True)
    assert "UNKWN" not in kept


def test_order_preserved_for_survivors():
    kept = filter_listed_at_asof(_CANDIDATES, _AS_OF, _TABLE, force=True)
    assert kept == ["AAPL", "MSFT"]


# ---------------------------------------------------------------------------
# Default-OFF / byte-identical passthrough (RAILS)
# ---------------------------------------------------------------------------


def test_default_off_returns_input_unchanged(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HERMES_QUANT_PIT_UNIVERSE", raising=False)
    out = filter_listed_at_asof(_CANDIDATES, _AS_OF, _TABLE)
    # No filtering at all when the flag is off — even with a table present.
    assert out == _CANDIDATES
    assert out is not _CANDIDATES  # returns a copy, not the same list object


def test_flag_off_explicit_value(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERMES_QUANT_PIT_UNIVERSE", "0")
    out = filter_listed_at_asof(_CANDIDATES, _AS_OF, _TABLE)
    assert out == _CANDIDATES


def test_flag_on_via_env_filters(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERMES_QUANT_PIT_UNIVERSE", "1")
    out = filter_listed_at_asof(_CANDIDATES, _AS_OF, _TABLE)
    assert out == ["AAPL", "MSFT"]


def test_force_false_overrides_enabled_flag(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERMES_QUANT_PIT_UNIVERSE", "1")
    out = filter_listed_at_asof(_CANDIDATES, _AS_OF, _TABLE, force=False)
    assert out == _CANDIDATES


# ---------------------------------------------------------------------------
# Fail-open-to-current-behavior, never silently-safe (RAILS)
# ---------------------------------------------------------------------------


def test_enabled_but_no_table_warns_and_passes_through(
    caplog: pytest.LogCaptureFixture,
):
    with caplog.at_level("WARNING", logger="hermes_quant.universe.point_in_time"):
        out = filter_listed_at_asof(_CANDIDATES, _AS_OF, None, force=True)
    assert out == _CANDIDATES
    assert any("NOT survivorship-safe" in r.message for r in caplog.records)


def test_enabled_but_empty_table_warns_and_passes_through(
    caplog: pytest.LogCaptureFixture,
):
    with caplog.at_level("WARNING", logger="hermes_quant.universe.point_in_time"):
        out = filter_listed_at_asof(_CANDIDATES, _AS_OF, {}, force=True)
    assert out == _CANDIDATES
    assert any("NOT survivorship-safe" in r.message for r in caplog.records)


def test_unparseable_asof_warns_and_passes_through(
    caplog: pytest.LogCaptureFixture,
):
    with caplog.at_level("WARNING", logger="hermes_quant.universe.point_in_time"):
        out = filter_listed_at_asof(_CANDIDATES, "not-a-date", _TABLE, force=True)
    assert out == _CANDIDATES
    assert any("NOT survivorship-safe" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Input coercion / shape tolerance
# ---------------------------------------------------------------------------


def test_accepts_datetime_and_iso_string_asof():
    by_dt = filter_listed_at_asof(_CANDIDATES, datetime(2020, 6, 1, 14, 30), _TABLE, force=True)
    by_str = filter_listed_at_asof(_CANDIDATES, "2020-06-01", _TABLE, force=True)
    by_str_dt = filter_listed_at_asof(_CANDIDATES, "2020-06-01T09:30:00", _TABLE, force=True)
    assert by_dt == by_str == by_str_dt == ["AAPL", "MSFT"]


def test_record_with_no_listed_date_is_excluded(caplog: pytest.LogCaptureFixture):
    table = {"NOLIST": {"delisted_at": "2025-01-01"}}  # no listed_at at all
    with caplog.at_level("WARNING", logger="hermes_quant.universe.point_in_time"):
        out = filter_listed_at_asof(["NOLIST"], _AS_OF, table, force=True)
    assert out == []
    assert any("no listed_at" in r.message for r in caplog.records)


def test_bare_date_value_treated_as_listed_at():
    table = {"BARE": date(2000, 1, 1)}
    assert filter_listed_at_asof(["BARE"], _AS_OF, table, force=True) == ["BARE"]
    # ...and excluded before it listed.
    assert filter_listed_at_asof(["BARE"], date(1999, 1, 1), table, force=True) == []


# ---------------------------------------------------------------------------
# is_point_in_time_active helper
# ---------------------------------------------------------------------------


def test_is_point_in_time_active_requires_flag_and_table(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HERMES_QUANT_PIT_UNIVERSE", raising=False)
    assert is_point_in_time_active(_TABLE) is False
    monkeypatch.setenv("HERMES_QUANT_PIT_UNIVERSE", "1")
    assert is_point_in_time_active(None) is False
    assert is_point_in_time_active({}) is False
    assert is_point_in_time_active(_TABLE) is True


def test_determinism_repeated_calls_identical():
    a = filter_listed_at_asof(_CANDIDATES, _AS_OF, _TABLE, force=True)
    b = filter_listed_at_asof(_CANDIDATES, _AS_OF, _TABLE, force=True)
    assert a == b == ["AAPL", "MSFT"]
