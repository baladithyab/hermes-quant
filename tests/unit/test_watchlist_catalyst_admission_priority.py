"""tests/unit/test_watchlist_catalyst_admission_priority.py — ar57.

Catalyst admission priority (ADR-0075 symmetry, admission side).

The eviction path already protects a catalyst-admitted name from being
slow-evicted while in-horizon (``_catalyst_eviction_protected``). The
ADMISSION path had NO symmetric priority: a play already at
``max_per_play`` active ordinary names that all re-confirm active keeps the
active_count pinned at the cap. A strong out-of-universe catalyst admission
(fast_track + admitted_via=catalyst) is unioned in but APPENDED LAST, so by
the time the onboard gate (``active_count < max_per_play``) reaches it the
cap is full and the catalyst stays ``state='candidate'``. The downstream
autonomous tick only trades rows with ``state == 'active'``, so the catalyst
— the entire purpose of onboarding — is silently dropped.

Even with HERMES_QUANT_WATCHLIST_CAP_TRIM=1 ON, ``_enforce_cap_trim`` only
operates on already-active rows (it cannot promote a candidate), so cap-trim
cannot rescue the catalyst.

Fix: when a fast_track (admitted_via=catalyst) name would onboard but the
play is at cap, displace the lowest-scored UNPROTECTED active ordinary row
(reason ``displaced_by_catalyst``) so the catalyst takes the slot —
mirroring the eviction-side asymmetry on the admission side. Default-OFF
behavior (empty fast_track) stays bit-identical.

Deterministic, no network: scorer + universe + asof are all injected.
"""

from __future__ import annotations

import json

import pandas as pd

from hermes_quant.playbook.watchlist_evolution import (
    STATE_ACTIVE,
    STATE_CANDIDATE,
    STATE_EVICTED,
    WatchlistEntry,
    evolve_watchlist,
)


def _write_universe(path, symbols):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"symbols": list(symbols)}))


def _read_playfit(path):
    return json.loads(path.read_text())


def _u(i: int) -> str:
    return f"U{i:02d}"


class _MapScorer:
    """Scorer returning a per-symbol score; default for unspecified symbols."""

    def __init__(self, mapping: dict[str, float], default: float = 0.80):
        self._m = mapping
        self._d = default

    def __call__(self, symbol: str, play: str) -> float:  # noqa: ARG002 — protocol shape
        return self._m.get(symbol, self._d)


def _seed_full_play(watchlist, *, onboarded, n=50, score=0.80):
    """Seed `n` already-active ordinary rows U00..U(n-1) all at `score`."""
    rows = []
    for i in range(n):
        rows.append(
            WatchlistEntry(
                symbol=_u(i),
                play="swing",
                onboarded_at=onboarded,
                last_seen_at=onboarded,
                last_score=score,
                consecutive_days_above_floor=10,
                state=STATE_ACTIVE,
                consecutive_days_below_onboard=0,
                extras={},
            )
        )
    watchlist.parent.mkdir(parents=True, exist_ok=True)
    watchlist.write_text(
        json.dumps(
            {
                "as_of": onboarded.isoformat(),
                "plays": {"swing": [r.to_dict() for r in rows]},
            }
        )
    )


def _run_at_cap_with_catalyst(tmp_path, *, cap_trim_env, monkeypatch):
    """Set up the at-cap + strong catalyst scenario and run one evolution step.

    Returns the per-symbol row dict for the swing play.
    """
    universe = tmp_path / "universe.json"
    watchlist = tmp_path / "play-fit.json"
    journal = tmp_path / "journal.jsonl"

    onboarded = pd.Timestamp("2026-01-01T12:00:00Z")
    _seed_full_play(watchlist, onboarded=onboarded, n=50, score=0.80)

    # Universe re-confirms all 50 ordinary names (so they stay active) and the
    # catalyst LUNR is appended LAST via extra_universe_symbols.
    _write_universe(universe, [_u(i) for i in range(50)])

    monkeypatch.setenv("HERMES_QUANT_WATCHLIST_CAP_TRIM", cap_trim_env)

    asof = pd.Timestamp("2026-01-08T12:00:00Z")  # 7 days later, within nothing critical
    evolve_watchlist(
        universe_path=universe,
        watchlist_path=watchlist,
        journal_path=journal,
        scorer=_MapScorer({"LUNR": 0.90}, default=0.80),
        asof=asof,
        max_per_play=50,
        plays=("swing",),
        fast_track_symbols={"LUNR"},
        admission_extras={
            "LUNR": {"admitted_via": "catalyst", "catalyst_horizon": "1w"}
        },
        extra_universe_symbols=["LUNR"],
    )
    data = _read_playfit(watchlist)
    return {r["symbol"]: r for r in data["plays"]["swing"]}


def test_catalyst_onboards_at_cap_trim_off(tmp_path, monkeypatch):
    """At-cap play: a strong fast-track catalyst onboards by displacing the
    lowest-scored unprotected ordinary row — cap-trim OFF."""
    rows = _run_at_cap_with_catalyst(tmp_path, cap_trim_env="0", monkeypatch=monkeypatch)
    assert rows["LUNR"]["state"] == STATE_ACTIVE, (
        "high-value catalyst must onboard even when the play is at cap"
    )
    # The catalyst displaced exactly one ordinary name; active count stays at cap.
    active = [s for s, r in rows.items() if r["state"] == STATE_ACTIVE]
    assert len(active) == 50, "active count stays at cap (one displaced, one onboarded)"
    # One ordinary name was evicted with the displacement reason.
    displaced = [s for s, r in rows.items() if r["state"] == STATE_EVICTED]
    assert len(displaced) == 1
    assert "displaced_by_catalyst" in (rows[displaced[0]]["eviction_reason"] or "")


def test_catalyst_onboards_at_cap_trim_on(tmp_path, monkeypatch):
    """Same scenario with cap-trim ON: the catalyst still onboards and the
    active set stays at the cap (cannot grow)."""
    rows = _run_at_cap_with_catalyst(tmp_path, cap_trim_env="1", monkeypatch=monkeypatch)
    assert rows["LUNR"]["state"] == STATE_ACTIVE, (
        "high-value catalyst must onboard at cap with cap-trim ON too"
    )
    active = [s for s, r in rows.items() if r["state"] == STATE_ACTIVE]
    assert len(active) == 50, "cap-trim must keep the active set at the cap ceiling"


def test_no_displacement_when_below_cap(tmp_path, monkeypatch):
    """When the play is below cap, the catalyst onboards into an open slot and
    NO ordinary row is displaced."""
    universe = tmp_path / "universe.json"
    watchlist = tmp_path / "play-fit.json"
    journal = tmp_path / "journal.jsonl"
    onboarded = pd.Timestamp("2026-01-01T12:00:00Z")
    _seed_full_play(watchlist, onboarded=onboarded, n=49, score=0.80)  # one slot free
    _write_universe(universe, [_u(i) for i in range(49)])
    monkeypatch.setenv("HERMES_QUANT_WATCHLIST_CAP_TRIM", "0")

    asof = pd.Timestamp("2026-01-08T12:00:00Z")
    evolve_watchlist(
        universe_path=universe, watchlist_path=watchlist, journal_path=journal,
        scorer=_MapScorer({"LUNR": 0.90}, default=0.80), asof=asof,
        max_per_play=50, plays=("swing",),
        fast_track_symbols={"LUNR"},
        admission_extras={"LUNR": {"admitted_via": "catalyst", "catalyst_horizon": "1w"}},
        extra_universe_symbols=["LUNR"],
    )
    rows = {r["symbol"]: r for r in _read_playfit(watchlist)["plays"]["swing"]}
    assert rows["LUNR"]["state"] == STATE_ACTIVE
    # No ordinary row evicted — there was a free slot.
    evicted = [s for s, r in rows.items() if r["state"] == STATE_EVICTED]
    assert evicted == [], "no displacement when a slot is free"


def test_lowest_scored_ordinary_is_displaced(tmp_path, monkeypatch):
    """The displaced row is the LOWEST-scored unprotected ordinary active row
    (deterministic), not an arbitrary one."""
    universe = tmp_path / "universe.json"
    watchlist = tmp_path / "play-fit.json"
    journal = tmp_path / "journal.jsonl"
    onboarded = pd.Timestamp("2026-01-01T12:00:00Z")

    # Seed 50 active rows, one (U49) deliberately lowest.
    rows = []
    for i in range(50):
        score = 0.50 if i == 49 else 0.80
        rows.append(
            WatchlistEntry(
                symbol=_u(i), play="swing", onboarded_at=onboarded, last_seen_at=onboarded,
                last_score=score, consecutive_days_above_floor=10, state=STATE_ACTIVE,
                consecutive_days_below_onboard=0, extras={},
            )
        )
    watchlist.parent.mkdir(parents=True, exist_ok=True)
    watchlist.write_text(json.dumps({
        "as_of": onboarded.isoformat(),
        "plays": {"swing": [r.to_dict() for r in rows]},
    }))
    # Re-confirm all at their seeded scores (U49 stays at 0.50, still >= evict_floor 0.45).
    _write_universe(universe, [_u(i) for i in range(50)])
    monkeypatch.setenv("HERMES_QUANT_WATCHLIST_CAP_TRIM", "0")

    asof = pd.Timestamp("2026-01-08T12:00:00Z")
    score_map = {"LUNR": 0.90, _u(49): 0.50}
    evolve_watchlist(
        universe_path=universe, watchlist_path=watchlist, journal_path=journal,
        scorer=_MapScorer(score_map, default=0.80), asof=asof,
        max_per_play=50, plays=("swing",),
        fast_track_symbols={"LUNR"},
        admission_extras={"LUNR": {"admitted_via": "catalyst", "catalyst_horizon": "1w"}},
        extra_universe_symbols=["LUNR"],
    )
    out = {r["symbol"]: r for r in _read_playfit(watchlist)["plays"]["swing"]}
    assert out["LUNR"]["state"] == STATE_ACTIVE
    assert out[_u(49)]["state"] == STATE_EVICTED, "lowest-scored ordinary row is displaced"
    # Every other ordinary name stays active.
    for i in range(49):
        assert out[_u(i)]["state"] == STATE_ACTIVE


def test_default_off_no_fast_track_unchanged(tmp_path, monkeypatch):
    """With NO fast_track, an at-cap play does NOT onboard a new candidate and
    no ordinary row is displaced — default behavior is bit-identical."""
    universe = tmp_path / "universe.json"
    watchlist = tmp_path / "play-fit.json"
    journal = tmp_path / "journal.jsonl"
    onboarded = pd.Timestamp("2026-01-01T12:00:00Z")
    _seed_full_play(watchlist, onboarded=onboarded, n=50, score=0.80)
    # NEWCOMER is a fresh high-scoring name but NOT fast-tracked.
    _write_universe(universe, [_u(i) for i in range(50)] + ["NEWCOMER"])
    monkeypatch.setenv("HERMES_QUANT_WATCHLIST_CAP_TRIM", "0")

    asof = pd.Timestamp("2026-01-08T12:00:00Z")
    evolve_watchlist(
        universe_path=universe, watchlist_path=watchlist, journal_path=journal,
        scorer=_MapScorer({"NEWCOMER": 0.95}, default=0.80), asof=asof,
        max_per_play=50, plays=("swing",),
    )
    rows = {r["symbol"]: r for r in _read_playfit(watchlist)["plays"]["swing"]}
    # NEWCOMER cannot displace anyone — it is an ordinary name (no priority).
    assert rows["NEWCOMER"]["state"] == STATE_CANDIDATE
    # No ordinary row evicted.
    evicted = [s for s, r in rows.items() if r["state"] == STATE_EVICTED]
    assert evicted == []
