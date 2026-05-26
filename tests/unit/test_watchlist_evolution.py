"""Unit tests for hermes_quant.playbook.watchlist_evolution.

Covers:
* Sticky onboarding (1 day above floor != onboard; 3 consecutive → onboard)
* Fast eviction (1 day below evict_floor → evict)
* Slow eviction (7 runs below onboard_floor but above evict_floor → evict)
* max_per_play cap (51st candidate isn't onboarded)
* Journal append-only + JSON-line valid
* Atomic-write crash safety (mocked rename)
* Scorer dependency injection
* get_active_watchlist helper

All tests use deterministic in-memory state; no network, no live data.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import pandas as pd
import pytest

from hermes_quant.playbook.watchlist_evolution import (
    PLAY_NAMES,
    SLOW_EVICT_RUNS,
    STATE_ACTIVE,
    STATE_EVICTED,
    WatchlistEntry,
    _atomic_write_json,
    evolve_watchlist,
    get_active_watchlist,
    stub_scorer,
)

# --------------------------------------------------------------------------- #
# Fixtures + helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def universe_file(tmp_path: Path) -> Path:
    """Tiny universe of three symbols for fast iteration."""
    path = tmp_path / "universe.json"
    path.write_text(
        json.dumps(
            {
                "asof": "2026-01-01T00:00:00+00:00",
                "count": 3,
                "symbols": [
                    {"symbol": "AAA", "last_close": 10.0},
                    {"symbol": "BBB", "last_close": 20.0},
                    {"symbol": "CCC", "last_close": 30.0},
                ],
            }
        )
    )
    return path


@pytest.fixture
def state_paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "play-fit.json", tmp_path / "journal.jsonl"


def _run(
    universe_file: Path,
    state_paths: tuple[Path, Path],
    *,
    score: float | dict[str, float] = 0.7,
    asof: pd.Timestamp,
    plays: tuple[str, ...] = ("covered_call",),
    max_per_play: int = 50,
    onboard_floor: float = 0.65,
    evict_floor: float = 0.45,
    sticky_onboard_days: int = 3,
    eviction_rules: Callable[[WatchlistEntry, float], str | None] | None = None,
) -> dict:
    """Convenience wrapper that pins one play and a per-symbol scorer."""
    if isinstance(score, dict):
        scorer_fn = lambda sym, _play: score.get(sym, 0.0)  # noqa: E731
    else:
        scorer_fn = stub_scorer(score)
    watchlist_path, journal_path = state_paths
    return evolve_watchlist(
        universe_path=universe_file,
        watchlist_path=watchlist_path,
        journal_path=journal_path,
        max_per_play=max_per_play,
        onboard_floor=onboard_floor,
        evict_floor=evict_floor,
        sticky_onboard_days=sticky_onboard_days,
        scorer=scorer_fn,
        eviction_rules=eviction_rules,
        asof=asof,
        plays=plays,
    )


def _read_state(watchlist_path: Path) -> dict:
    return json.loads(watchlist_path.read_text())


def _read_journal(journal_path: Path) -> list[dict]:
    if not journal_path.exists():
        return []
    return [json.loads(line) for line in journal_path.read_text().splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# Sticky onboarding
# --------------------------------------------------------------------------- #


def test_one_day_above_floor_does_not_onboard(universe_file, state_paths):
    """1 run above the onboard floor → still candidate, no onboard event."""
    summary = _run(
        universe_file,
        state_paths,
        score=0.7,
        asof=pd.Timestamp("2026-01-01", tz="UTC"),
        sticky_onboard_days=3,
    )
    assert summary["per_play"]["covered_call"]["n_active"] == 0
    assert summary["per_play"]["covered_call"]["n_onboarded_today"] == 0

    state = _read_state(state_paths[0])
    rows = state["plays"]["covered_call"]
    assert all(r["state"] == "candidate" for r in rows)
    assert all(r["consecutive_days_above_floor"] == 1 for r in rows)


def test_three_days_above_floor_triggers_onboard(universe_file, state_paths):
    """3 consecutive runs above the onboard floor → onboard event fires."""
    sticky = 3
    for i in range(sticky):
        summary = _run(
            universe_file,
            state_paths,
            score=0.7,
            asof=pd.Timestamp(f"2026-01-{i + 1:02d}", tz="UTC"),
            sticky_onboard_days=sticky,
        )

    # On day 3 all 3 universe symbols cross the threshold simultaneously.
    assert summary["per_play"]["covered_call"]["n_onboarded_today"] == 3
    assert summary["per_play"]["covered_call"]["n_active"] == 3

    journal = _read_journal(state_paths[1])
    onboard_events = [e for e in journal if e["action"] == "onboard"]
    assert len(onboard_events) == 3
    assert {e["symbol"] for e in onboard_events} == {"AAA", "BBB", "CCC"}


def test_streak_breaks_on_score_below_floor(universe_file, state_paths):
    """A single run below floor resets the consecutive-above streak to 0."""
    # Day 1 + 2 above floor.
    _run(universe_file, state_paths, score=0.7, asof=pd.Timestamp("2026-01-01", tz="UTC"))
    _run(universe_file, state_paths, score=0.7, asof=pd.Timestamp("2026-01-02", tz="UTC"))
    # Day 3 dips below floor (but still above evict floor — won't fast-evict).
    _run(universe_file, state_paths, score=0.55, asof=pd.Timestamp("2026-01-03", tz="UTC"))

    state = _read_state(state_paths[0])
    rows = state["plays"]["covered_call"]
    for r in rows:
        # streak reset to 0
        assert r["consecutive_days_above_floor"] == 0
        # still a candidate (not evicted, not active)
        assert r["state"] == "candidate"
        # below-onboard counter ticked
        assert r["consecutive_days_below_onboard"] == 1


# --------------------------------------------------------------------------- #
# Eviction
# --------------------------------------------------------------------------- #


def test_fast_eviction_one_day_below_evict_floor(universe_file, state_paths):
    """One run with score < evict_floor evicts immediately."""
    # First, onboard.
    for i in range(3):
        _run(
            universe_file,
            state_paths,
            score=0.7,
            asof=pd.Timestamp(f"2026-01-{i + 1:02d}", tz="UTC"),
        )
    state = _read_state(state_paths[0])
    assert sum(1 for r in state["plays"]["covered_call"] if r["state"] == "active") == 3

    # Now collapse the score.
    summary = _run(
        universe_file,
        state_paths,
        score=0.10,  # well below evict_floor=0.45
        asof=pd.Timestamp("2026-01-04", tz="UTC"),
    )
    assert summary["per_play"]["covered_call"]["n_evicted_today"] == 3
    assert summary["per_play"]["covered_call"]["n_active"] == 0

    state = _read_state(state_paths[0])
    for r in state["plays"]["covered_call"]:
        assert r["state"] == STATE_EVICTED
        assert r["eviction_reason"] is not None
        assert "fast" in r["eviction_reason"]

    journal = _read_journal(state_paths[1])
    evict_events = [e for e in journal if e["action"] == "evict"]
    assert len(evict_events) == 3


def test_slow_eviction_seven_runs_below_onboard_floor(universe_file, state_paths):
    """7 consecutive runs in the [evict_floor, onboard_floor) band → evict."""
    # Onboard first.
    for i in range(3):
        _run(
            universe_file,
            state_paths,
            score=0.7,
            asof=pd.Timestamp(f"2026-01-{i + 1:02d}", tz="UTC"),
        )

    # Now drift in the slow-decay zone for SLOW_EVICT_RUNS runs.
    mid_score = 0.55  # > evict_floor=0.45, < onboard_floor=0.65
    for k in range(SLOW_EVICT_RUNS):
        summary = _run(
            universe_file,
            state_paths,
            score=mid_score,
            asof=pd.Timestamp(f"2026-02-{k + 1:02d}", tz="UTC"),
        )

    # The run that hits SLOW_EVICT_RUNS should evict.
    assert summary["per_play"]["covered_call"]["n_evicted_today"] == 3
    state = _read_state(state_paths[0])
    for r in state["plays"]["covered_call"]:
        assert r["state"] == STATE_EVICTED
        assert "slow" in (r["eviction_reason"] or "")


def test_explicit_eviction_rule_fires(universe_file, state_paths):
    """Caller-supplied eviction_rules callback fires and is journalled."""
    # Onboard.
    for i in range(3):
        _run(
            universe_file,
            state_paths,
            score=0.7,
            asof=pd.Timestamp(f"2026-01-{i + 1:02d}", tz="UTC"),
        )

    def evict_only_aaa(row: WatchlistEntry, score: float) -> str | None:  # noqa: ARG001
        if row.symbol == "AAA":
            return "earnings_window"
        return None

    summary = _run(
        universe_file,
        state_paths,
        score=0.7,
        asof=pd.Timestamp("2026-01-10", tz="UTC"),
        eviction_rules=evict_only_aaa,
    )
    assert summary["per_play"]["covered_call"]["n_evicted_today"] == 1
    assert summary["per_play"]["covered_call"]["n_active"] == 2

    journal = _read_journal(state_paths[1])
    evict_events = [e for e in journal if e["action"] == "evict"]
    assert any(e["symbol"] == "AAA" and e["reason"] == "earnings_window" for e in evict_events)


# --------------------------------------------------------------------------- #
# max_per_play cap
# --------------------------------------------------------------------------- #


def test_max_per_play_cap(tmp_path: Path):
    """The 51st qualifying candidate is NOT onboarded; cap holds."""
    universe_path = tmp_path / "universe.json"
    universe_path.write_text(
        json.dumps(
            {
                "symbols": [{"symbol": f"S{i:03d}"} for i in range(60)],
            }
        )
    )
    state_paths_pair = (tmp_path / "play-fit.json", tmp_path / "journal.jsonl")

    # Run sticky_onboard_days times so all 60 symbols qualify simultaneously.
    for i in range(3):
        summary = _run(
            universe_path,
            state_paths_pair,
            score=0.9,
            asof=pd.Timestamp(f"2026-01-{i + 1:02d}", tz="UTC"),
            max_per_play=50,
        )

    assert summary["per_play"]["covered_call"]["n_active"] == 50
    assert summary["per_play"]["covered_call"]["n_onboarded_today"] == 50

    # The other 10 stayed in candidate because the cap fired.
    state = _read_state(state_paths_pair[0])
    rows = state["plays"]["covered_call"]
    n_candidate = sum(1 for r in rows if r["state"] == "candidate")
    assert n_candidate == 10


# --------------------------------------------------------------------------- #
# Journal invariants
# --------------------------------------------------------------------------- #


def test_journal_is_append_only_and_jsonl_valid(universe_file, state_paths):
    """Each line is independently parseable JSON; later runs never rewrite."""
    # Onboard run 1–3.
    for i in range(3):
        _run(
            universe_file,
            state_paths,
            score=0.7,
            asof=pd.Timestamp(f"2026-01-{i + 1:02d}", tz="UTC"),
        )

    journal_path = state_paths[1]
    snapshot1 = journal_path.read_text()

    # Run another step that triggers more events.
    _run(
        universe_file,
        state_paths,
        score=0.10,
        asof=pd.Timestamp("2026-01-04", tz="UTC"),
    )

    snapshot2 = journal_path.read_text()
    assert snapshot2.startswith(snapshot1), "journal is append-only"

    # Every line is valid JSON with the expected schema.
    required_keys = {
        "event_id",
        "asof",
        "play",
        "symbol",
        "action",
        "reason",
        "score_before",
        "score_after",
    }
    for line in snapshot2.splitlines():
        if not line.strip():
            continue
        parsed = json.loads(line)
        assert required_keys.issubset(parsed.keys())
        assert parsed["action"] in {"onboard", "evict", "score_update"}


# --------------------------------------------------------------------------- #
# Atomic write
# --------------------------------------------------------------------------- #


def test_atomic_write_no_partial_state_on_crash(tmp_path: Path):
    """If os.replace fails mid-write, the existing state file stays intact."""
    target = tmp_path / "state.json"
    # Establish a known-good baseline.
    _atomic_write_json(target, {"version": 1})
    baseline = target.read_text()

    # Simulate a crash inside os.replace — the temp file must not become the
    # canonical artifact.
    with mock.patch("hermes_quant.playbook.watchlist_evolution.os.replace") as m_replace:
        m_replace.side_effect = OSError("simulated crash")
        with pytest.raises(OSError, match="simulated"):
            _atomic_write_json(target, {"version": 2})

    # Original content unchanged.
    assert target.read_text() == baseline

    # No leftover .tmp files in the directory.
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_evolve_watchlist_silent_when_universe_missing(tmp_path: Path):
    """Missing universe → empty summary, no crash, no state written."""
    summary = evolve_watchlist(
        universe_path=tmp_path / "does-not-exist.json",
        watchlist_path=tmp_path / "play-fit.json",
        journal_path=tmp_path / "journal.jsonl",
        scorer=stub_scorer(0.9),
    )
    assert summary["events_written"] == 0
    for play in PLAY_NAMES:
        assert summary["per_play"][play]["n_active"] == 0
    assert not (tmp_path / "play-fit.json").exists()
    assert not (tmp_path / "journal.jsonl").exists()


# --------------------------------------------------------------------------- #
# Scorer DI
# --------------------------------------------------------------------------- #


def test_scorer_is_dependency_injected(universe_file, state_paths):
    """The scorer is called once per (symbol, play) per run."""
    calls: list[tuple[str, str]] = []

    def tracking_scorer(symbol: str, play: str) -> float:
        calls.append((symbol, play))
        return 0.5

    evolve_watchlist(
        universe_path=universe_file,
        watchlist_path=state_paths[0],
        journal_path=state_paths[1],
        scorer=tracking_scorer,
        plays=("covered_call", "csp"),
        asof=pd.Timestamp("2026-01-01", tz="UTC"),
    )

    # 3 universe symbols × 2 plays = 6 calls.
    assert len(calls) == 6
    assert {p for _s, p in calls} == {"covered_call", "csp"}
    assert {s for s, _p in calls} == {"AAA", "BBB", "CCC"}


# --------------------------------------------------------------------------- #
# get_active_watchlist helper
# --------------------------------------------------------------------------- #


def test_get_active_watchlist_returns_only_active(universe_file, state_paths):
    """Helper filters to STATE_ACTIVE only and supports per-play / union."""
    # Onboard against covered_call only.
    for i in range(3):
        _run(
            universe_file,
            state_paths,
            score=0.7,
            asof=pd.Timestamp(f"2026-01-{i + 1:02d}", tz="UTC"),
            plays=("covered_call",),
        )

    per_play = get_active_watchlist(
        play="covered_call",
        watchlist_path=state_paths[0],
    )
    assert sorted(per_play) == ["AAA", "BBB", "CCC"]

    # csp has nothing active → empty list.
    assert get_active_watchlist(play="csp", watchlist_path=state_paths[0]) == []

    # Union with no plays specified — should equal covered_call's set.
    union = get_active_watchlist(watchlist_path=state_paths[0])
    assert sorted(union) == ["AAA", "BBB", "CCC"]


def test_get_active_watchlist_missing_file(tmp_path: Path):
    """Missing watchlist file → empty list, no crash."""
    assert get_active_watchlist(watchlist_path=tmp_path / "missing.json") == []
    assert (
        get_active_watchlist(
            play="covered_call",
            watchlist_path=tmp_path / "missing.json",
        )
        == []
    )


# --------------------------------------------------------------------------- #
# Schema round-trip
# --------------------------------------------------------------------------- #


def test_watchlist_entry_round_trip():
    """to_dict / from_dict survives a JSON round trip with NaT."""
    entry = WatchlistEntry(
        symbol="AAPL",
        play="covered_call",
        onboarded_at=pd.Timestamp("2026-01-01", tz="UTC"),
        last_seen_at=pd.Timestamp("2026-01-05", tz="UTC"),
        last_score=0.72,
        consecutive_days_above_floor=4,
        state=STATE_ACTIVE,
        eviction_reason=None,
    )
    blob = json.dumps(entry.to_dict())
    restored = WatchlistEntry.from_dict(json.loads(blob))
    assert restored.symbol == "AAPL"
    assert restored.last_score == 0.72
    assert restored.state == STATE_ACTIVE
    assert restored.consecutive_days_above_floor == 4
    assert restored.onboarded_at == pd.Timestamp("2026-01-01", tz="UTC")
