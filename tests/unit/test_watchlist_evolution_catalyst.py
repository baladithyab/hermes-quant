"""Watchlist-evolution catalyst-onboarding seam tests (C2-4, ADR-0075).

Covers the evolve_watchlist additions:
  * fast_track_symbols -> same-day onboard (sticky_onboard_days bypassed),
  * admission_extras -> admitted_via persisted to disk (to_dict),
  * sticky-removal protection -> an admitted_via=catalyst row with an open
    position is NOT slow-evicted before its horizon closes,
  * flag-OFF / kwargs-None -> bit-identical to today.

Deterministic, no network: scorer + universe + asof are all injected.
"""

from __future__ import annotations

import json

import pandas as pd

from hermes_quant.playbook.watchlist_evolution import (
    SLOW_EVICT_RUNS,
    STATE_ACTIVE,
    WatchlistEntry,
    _catalyst_eviction_protected,
    evolve_watchlist,
    stub_scorer,
)


def _write_universe(path, symbols):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"symbols": list(symbols)}))


def _read_playfit(path):
    return json.loads(path.read_text())


def test_fast_track_onboards_same_day(tmp_path):
    """An admitted symbol with fast_track onboards in ONE run (sticky bypassed);
    a normal symbol at the same score does NOT (needs the 3-run sticky window)."""
    universe = tmp_path / "universe.json"
    watchlist = tmp_path / "play-fit.json"
    journal = tmp_path / "journal.jsonl"
    _write_universe(universe, ["NORMAL", "CATSYM"])
    asof = pd.Timestamp("2026-01-01T12:00:00Z")

    summary = evolve_watchlist(
        universe_path=universe,
        watchlist_path=watchlist,
        journal_path=journal,
        scorer=stub_scorer(0.90),  # well above the 0.65 onboard floor
        asof=asof,
        fast_track_symbols={"CATSYM"},
        admission_extras={"CATSYM": {"admitted_via": "catalyst", "catalyst_horizon": "1d"}},
        plays=("swing",),
    )
    data = _read_playfit(watchlist)
    rows = {r["symbol"]: r for r in data["plays"]["swing"]}
    assert rows["CATSYM"]["state"] == STATE_ACTIVE, "fast-track symbol should onboard same-day"
    # NORMAL needs 3 consecutive runs above floor -> still candidate after run 1.
    assert rows["NORMAL"]["state"] != STATE_ACTIVE
    assert summary["per_play"]["swing"]["n_onboarded_today"] == 1


def test_admission_extras_persisted(tmp_path):
    """admitted_via (+ horizon/asof) survives to play-fit.json via to_dict()."""
    universe = tmp_path / "universe.json"
    watchlist = tmp_path / "play-fit.json"
    journal = tmp_path / "journal.jsonl"
    _write_universe(universe, ["CATSYM"])
    asof = pd.Timestamp("2026-01-01T12:00:00Z")

    evolve_watchlist(
        universe_path=universe,
        watchlist_path=watchlist,
        journal_path=journal,
        scorer=stub_scorer(0.90),
        asof=asof,
        fast_track_symbols={"CATSYM"},
        admission_extras={
            "CATSYM": {
                "admitted_via": "catalyst",
                "catalyst_horizon": "1d",
                "catalyst_asof": "2026-01-01T09:00:00Z",
            }
        },
        plays=("swing",),
    )
    data = _read_playfit(watchlist)
    row = data["plays"]["swing"][0]
    assert row["symbol"] == "CATSYM"
    assert row["extras"]["admitted_via"] == "catalyst"
    assert row["extras"]["catalyst_horizon"] == "1d"
    assert row["extras"]["catalyst_asof"] == "2026-01-01T09:00:00Z"


def test_flag_off_is_byte_identical(tmp_path):
    """With NO catalyst kwargs, the evolved state matches a run with them None."""
    universe = tmp_path / "universe.json"
    _write_universe(universe, ["AAA", "BBB"])
    asof = pd.Timestamp("2026-01-01T12:00:00Z")

    wl_a = tmp_path / "a.json"
    wl_b = tmp_path / "b.json"
    evolve_watchlist(universe_path=universe, watchlist_path=wl_a,
                     journal_path=tmp_path / "ja.jsonl", scorer=stub_scorer(0.90),
                     asof=asof, plays=("swing",))
    evolve_watchlist(universe_path=universe, watchlist_path=wl_b,
                     journal_path=tmp_path / "jb.jsonl", scorer=stub_scorer(0.90),
                     asof=asof, plays=("swing",),
                     fast_track_symbols=None, admission_extras=None,
                     extra_universe_symbols=None)
    a = _read_playfit(wl_a)["plays"]["swing"]
    b = _read_playfit(wl_b)["plays"]["swing"]
    assert a == b


def test_extra_universe_symbols_unions_in(tmp_path):
    """An out-of-file admitted symbol gets scored when passed via extra_universe."""
    universe = tmp_path / "universe.json"
    watchlist = tmp_path / "play-fit.json"
    _write_universe(universe, ["INFILE"])
    asof = pd.Timestamp("2026-01-01T12:00:00Z")

    evolve_watchlist(
        universe_path=universe, watchlist_path=watchlist,
        journal_path=tmp_path / "j.jsonl", scorer=stub_scorer(0.90), asof=asof,
        fast_track_symbols={"OUTSIDE"},
        admission_extras={"OUTSIDE": {"admitted_via": "catalyst", "catalyst_horizon": "1d"}},
        extra_universe_symbols=["OUTSIDE"],
        plays=("swing",),
    )
    syms = {r["symbol"] for r in _read_playfit(watchlist)["plays"]["swing"]}
    assert "OUTSIDE" in syms, "extra_universe_symbols should be scored"


# ---------------------------------------------------------------------------
# Sticky-removal protection
# ---------------------------------------------------------------------------


def _catalyst_row(*, onboarded, horizon="1w"):
    return WatchlistEntry(
        symbol="CATSYM",
        play="swing",
        onboarded_at=onboarded,
        last_seen_at=onboarded,
        last_score=0.50,
        consecutive_days_above_floor=0,
        state=STATE_ACTIVE,
        consecutive_days_below_onboard=SLOW_EVICT_RUNS,
        extras={"admitted_via": "catalyst", "catalyst_horizon": horizon},
    )


def test_protection_holds_within_horizon_with_position():
    """admitted_via=catalyst + open position + within horizon -> protected."""
    onboarded = pd.Timestamp("2026-01-01T12:00:00Z")
    asof = pd.Timestamp("2026-01-03T12:00:00Z")  # 2 days into a 1w horizon
    row = _catalyst_row(onboarded=onboarded, horizon="1w")
    assert _catalyst_eviction_protected(row, asof, position_lookup=lambda s: True) is True


def test_protection_lifts_after_horizon_elapses():
    onboarded = pd.Timestamp("2026-01-01T12:00:00Z")
    asof = pd.Timestamp("2026-01-20T12:00:00Z")  # well past a 1w horizon
    row = _catalyst_row(onboarded=onboarded, horizon="1w")
    assert _catalyst_eviction_protected(row, asof, position_lookup=lambda s: True) is False


def test_protection_failsafe_when_position_unknown():
    """No position feed -> fail-safe toward holding (protected)."""
    onboarded = pd.Timestamp("2026-01-01T12:00:00Z")
    asof = pd.Timestamp("2026-01-02T12:00:00Z")
    row = _catalyst_row(onboarded=onboarded, horizon="1w")
    assert _catalyst_eviction_protected(row, asof, position_lookup=None) is True


def test_protection_not_applied_to_non_catalyst_rows():
    onboarded = pd.Timestamp("2026-01-01T12:00:00Z")
    asof = pd.Timestamp("2026-01-02T12:00:00Z")
    row = WatchlistEntry(
        symbol="NORMAL", play="swing", onboarded_at=onboarded, last_seen_at=onboarded,
        last_score=0.50, consecutive_days_above_floor=0, state=STATE_ACTIVE,
        consecutive_days_below_onboard=SLOW_EVICT_RUNS, extras={},
    )
    assert _catalyst_eviction_protected(row, asof, position_lookup=lambda s: True) is False


def test_admitted_open_position_not_evicted_mid_horizon(tmp_path):
    """End-to-end: a catalyst row that would slow-evict is held while in-horizon
    with an open position."""
    universe = tmp_path / "universe.json"
    watchlist = tmp_path / "play-fit.json"
    journal = tmp_path / "j.jsonl"
    _write_universe(universe, ["CATSYM"])

    # Seed an already-active catalyst row that has been below the onboard floor
    # for SLOW_EVICT_RUNS (so it qualifies for slow-evict) but is within horizon.
    onboarded = pd.Timestamp("2026-01-01T12:00:00Z")
    seeded = WatchlistEntry(
        symbol="CATSYM", play="swing", onboarded_at=onboarded, last_seen_at=onboarded,
        last_score=0.50, consecutive_days_above_floor=0, state=STATE_ACTIVE,
        consecutive_days_below_onboard=SLOW_EVICT_RUNS - 1,
        extras={"admitted_via": "catalyst", "catalyst_horizon": "1w"},
    )
    watchlist.parent.mkdir(parents=True, exist_ok=True)
    watchlist.write_text(json.dumps({
        "as_of": onboarded.isoformat(),
        "plays": {"swing": [seeded.to_dict()]},
    }))

    # Score above evict_floor (0.45) but below onboard_floor (0.65) so the slow
    # path is the only eviction candidate; asof 2 days in (within 1w horizon).
    asof = pd.Timestamp("2026-01-03T12:00:00Z")
    evolve_watchlist(
        universe_path=universe, watchlist_path=watchlist, journal_path=journal,
        scorer=stub_scorer(0.50), asof=asof, plays=("swing",),
        position_lookup=lambda s: True,
    )
    row = {r["symbol"]: r for r in _read_playfit(watchlist)["plays"]["swing"]}["CATSYM"]
    assert row["state"] == STATE_ACTIVE, "catalyst row must not be slow-evicted mid-horizon"
