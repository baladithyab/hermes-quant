"""tests/unit/test_watchlist_cap_trim.py — seed d641 cap-trim enforcement.

`max_per_play` gated ONBOARDS only (active_count < max_per_play). It NEVER
trimmed an already-over-cap active set: lowering the cap or loading over-cap
state left rows above the cap indefinitely (operationally confirmed 2026-06-04).

This keeps the deterministic post-scoring trim helper tested, but the evolution
seam applies it only when HERMES_QUANT_WATCHLIST_CAP_TRIM=1. The unset path must
preserve already-over-cap persisted state so the migration is explicit.

Covered:
  * helper `_enforce_cap_trim`: over-cap -> exactly cap, keeps top-by-score,
    deterministic tie-break, protection, at/under-cap no-op, never grows.
  * end-to-end `evolve_watchlist`: flag-off keeps over-cap active rows; flag-on
    trims a seeded 60 active rows, cap 50 -> 50 active, 10 evict events.
"""
from __future__ import annotations

import json

import pandas as pd

from hermes_quant.playbook.watchlist_evolution import (
    ACTION_EVICT,
    STATE_ACTIVE,
    STATE_EVICTED,
    WatchlistEntry,
    _enforce_cap_trim,
    evolve_watchlist,
    stub_scorer,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ASOF = pd.Timestamp("2026-06-04T12:00:00Z")


def _active_row(symbol: str, score: float, *, extras=None, onboarded=None) -> WatchlistEntry:
    return WatchlistEntry(
        symbol=symbol,
        play="swing",
        onboarded_at=onboarded if onboarded is not None else pd.Timestamp("2026-01-01T00:00:00Z"),
        last_seen_at=_ASOF,
        last_score=score,
        consecutive_days_above_floor=1,
        state=STATE_ACTIVE,
        consecutive_days_below_onboard=0,
        extras=extras or {},
    )


def _sym(i: int) -> str:
    return f"S{i:02d}"


# ---------------------------------------------------------------------------
# Helper-level tests: _enforce_cap_trim
# ---------------------------------------------------------------------------


def test_over_cap_trims_to_exactly_cap():
    """60 active, cap 50 -> exactly 50 active, 10 evict events."""
    rows = [_active_row(_sym(i), 0.500 + i * 0.001) for i in range(60)]
    new_rows, events = _enforce_cap_trim(
        rows, play="swing", asof=_ASOF, max_per_play=50, position_lookup=None
    )
    active = [r for r in new_rows if r.state == STATE_ACTIVE]
    assert len(active) == 50
    assert len(events) == 10
    assert all(ev["action"] == ACTION_EVICT for ev in events)


def test_trim_keeps_top_by_score():
    """The 50 kept rows are the highest-scored; the 10 lowest are evicted."""
    rows = [_active_row(_sym(i), 0.500 + i * 0.001) for i in range(60)]
    new_rows, events = _enforce_cap_trim(
        rows, play="swing", asof=_ASOF, max_per_play=50, position_lookup=None
    )
    kept = {r.symbol for r in new_rows if r.state == STATE_ACTIVE}
    evicted = {r.symbol for r in new_rows if r.state == STATE_EVICTED}
    # S00..S09 are the lowest scored -> trimmed; S10..S59 kept.
    assert evicted == {_sym(i) for i in range(10)}
    assert kept == {_sym(i) for i in range(10, 60)}
    assert {ev["symbol"] for ev in events} == {_sym(i) for i in range(10)}


def test_trim_tie_break_by_symbol_deterministic():
    """All-equal scores -> tie-break by symbol ascending; lowest symbols trimmed."""
    rows = [_active_row(_sym(i), 0.55) for i in range(60)]  # identical scores
    new_rows, events = _enforce_cap_trim(
        rows, play="swing", asof=_ASOF, max_per_play=50, position_lookup=None
    )
    # With equal scores, keep is the top-50 by symbol-ascending tie-break, i.e.
    # symbols S00..S49 sort first -> kept; S50..S59 are the over-cap tail -> trimmed.
    evicted = {r.symbol for r in new_rows if r.state == STATE_EVICTED}
    assert evicted == {_sym(i) for i in range(50, 60)}


def test_trim_deterministic_across_two_runs():
    """Same input -> identical trimmed set (no RNG / wall-clock in the decision)."""
    rows = [_active_row(_sym(i), 0.500 + (i % 7) * 0.01) for i in range(60)]
    _, ev_a = _enforce_cap_trim(rows, play="swing", asof=_ASOF, max_per_play=50, position_lookup=None)
    _, ev_b = _enforce_cap_trim(rows, play="swing", asof=_ASOF, max_per_play=50, position_lookup=None)
    assert {e["symbol"] for e in ev_a} == {e["symbol"] for e in ev_b}
    assert len(ev_a) == len(ev_b) == 10


def test_protected_row_counts_against_cap_and_backfills_eviction():
    """cap=3, 5 active, lowest protected -> keep protected + two best unprotected."""
    rows = [
        _active_row("TOP", 0.90),
        _active_row("MID", 0.80),
        _active_row("DROP1", 0.70),
        _active_row("DROP2", 0.60),
        _active_row(
            "CAT",
            0.10,
            extras={"admitted_via": "catalyst", "catalyst_horizon": "1w"},
            onboarded=_ASOF - pd.Timedelta(days=2),  # 2 days into a 1w horizon
        ),
    ]
    new_rows, events = _enforce_cap_trim(
        rows, play="swing", asof=_ASOF, max_per_play=3,
        position_lookup=lambda s: True,  # open position -> protected
    )
    active = {r.symbol for r in new_rows if r.state == STATE_ACTIVE}
    evicted = {r.symbol for r in new_rows if r.state == STATE_EVICTED}

    assert len(active) <= 3
    assert "CAT" in active, "protected row must NOT be trimmed even if lowest-scored"
    assert active == {"CAT", "TOP", "MID"}
    assert evicted == {"DROP1", "DROP2"}
    assert {ev["symbol"] for ev in events} == {"DROP1", "DROP2"}


def test_protected_row_not_trimmed_even_if_low_scored():
    """A low-scored catalyst-protected row survives while total active lands at cap."""
    rows = [_active_row(_sym(i), 0.500 + i * 0.001) for i in range(60)]
    # Make the LOWEST-scored row (S00) catalyst-protected + within horizon.
    rows[0] = _active_row(
        _sym(0),
        0.500,
        extras={"admitted_via": "catalyst", "catalyst_horizon": "1w"},
        onboarded=_ASOF - pd.Timedelta(days=2),  # 2 days into a 1w horizon
    )
    new_rows, events = _enforce_cap_trim(
        rows, play="swing", asof=_ASOF, max_per_play=50,
        position_lookup=lambda s: True,  # open position -> protected
    )
    active = {r.symbol for r in new_rows if r.state == STATE_ACTIVE}
    assert _sym(0) in active, "protected row must NOT be trimmed even if lowest-scored"
    # S00 counts as an occupied slot, so the 10 next-lowest unprotected rows
    # (S01..S10) are trimmed instead and the active set lands back at cap.
    assert len(active) == 50
    assert len(events) == 10
    assert {ev["symbol"] for ev in events} == {_sym(i) for i in range(1, 11)}


def test_trim_nan_score_is_deterministic():
    """A NaN last_score must not make the trim non-deterministic (adversarial B1).

    NaN breaks Python's sort ordering (NaN comparisons are all False), so a naive
    `sorted(key=(-score, symbol))` produced input-order-dependent eviction sets.
    NaN rows must be treated as lowest (trimmed first) deterministically.
    """
    import random

    base = [_active_row(_sym(i), 0.500 + i * 0.001) for i in range(58)]
    nan_rows = [_active_row("NANA", float("nan")), _active_row("NANB", float("nan"))]
    rows = base + nan_rows  # 60 active, cap 50

    evicted_sets = []
    for seed in range(6):
        shuffled = list(rows)
        random.Random(seed).shuffle(shuffled)
        new_rows, events = _enforce_cap_trim(
            shuffled, play="swing", asof=_ASOF, max_per_play=50, position_lookup=None
        )
        evicted_sets.append(frozenset(ev["symbol"] for ev in events))

    assert len(set(evicted_sets)) == 1, (
        f"NaN scores produced non-deterministic eviction sets: {evicted_sets}"
    )
    # NaN rows are lowest -> always among the trimmed.
    assert {"NANA", "NANB"} <= evicted_sets[0]


def test_at_cap_untouched():
    """Exactly cap active -> no trim, no events."""
    rows = [_active_row(_sym(i), 0.500 + i * 0.001) for i in range(50)]
    new_rows, events = _enforce_cap_trim(
        rows, play="swing", asof=_ASOF, max_per_play=50, position_lookup=None
    )
    assert events == []
    assert [r.symbol for r in new_rows] == [r.symbol for r in rows]
    assert all(r.state == STATE_ACTIVE for r in new_rows)


def test_under_cap_untouched():
    """Below cap -> identity, no events."""
    rows = [_active_row(_sym(i), 0.500 + i * 0.001) for i in range(30)]
    new_rows, events = _enforce_cap_trim(
        rows, play="swing", asof=_ASOF, max_per_play=50, position_lookup=None
    )
    assert events == []
    assert new_rows == rows


def test_trim_ignores_non_active_rows():
    """Candidate/evicted rows are not counted toward the cap nor trimmed."""
    rows = [_active_row(_sym(i), 0.500 + i * 0.001) for i in range(50)]
    # add some non-active rows; they must not affect the active cap.
    cand = WatchlistEntry(
        symbol="CAND", play="swing", onboarded_at=pd.NaT, last_seen_at=_ASOF,
        last_score=0.99, consecutive_days_above_floor=0, state="candidate",
        consecutive_days_below_onboard=0, extras={},
    )
    rows_with_cand = rows + [cand]
    new_rows, events = _enforce_cap_trim(
        rows_with_cand, play="swing", asof=_ASOF, max_per_play=50, position_lookup=None
    )
    assert events == [], "50 active + 1 candidate is at cap -> no trim"
    assert any(r.symbol == "CAND" and r.state == "candidate" for r in new_rows)


def test_trim_never_grows_active_set():
    """Property: trim shrinks-or-equals, and (unprotected) lands at min(before, cap)."""
    for n in (10, 50, 51, 60, 120):
        rows = [_active_row(_sym(i), 0.5 + i * 0.001) for i in range(n)]
        before = sum(1 for r in rows if r.state == STATE_ACTIVE)
        new_rows, _ = _enforce_cap_trim(
            rows, play="swing", asof=_ASOF, max_per_play=50, position_lookup=None
        )
        after = sum(1 for r in new_rows if r.state == STATE_ACTIVE)
        assert after <= before, "trim must never grow the active set"
        # No protected rows here, so the cap is a hard ceiling.
        assert after == min(before, 50)


# ---------------------------------------------------------------------------
# End-to-end: evolve_watchlist applies the cap-trim
# ---------------------------------------------------------------------------


def _write_universe(path, symbols):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"symbols": list(symbols)}))


def _seed_over_cap_state(path, n=60):
    """Seed play-fit.json with n already-active 'swing' rows (over a cap of 50)."""
    rows = []
    for i in range(n):
        r = _active_row(_sym(i), 0.50 + i * 0.001)
        rows.append(r.to_dict())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"as_of": _ASOF.isoformat(), "plays": {"swing": rows}}))


def test_evolve_watchlist_over_cap_flag_off_keeps_active_universe(tmp_path, monkeypatch):
    """Unset migration flag: pre-existing over-cap active rows are not evicted."""
    monkeypatch.delenv("HERMES_QUANT_WATCHLIST_CAP_TRIM", raising=False)
    universe = tmp_path / "universe.json"
    watchlist = tmp_path / "play-fit.json"
    journal = tmp_path / "journal.jsonl"
    syms = [_sym(i) for i in range(60)]
    _write_universe(universe, syms)
    _seed_over_cap_state(watchlist, n=60)

    summary = evolve_watchlist(
        universe_path=universe, watchlist_path=watchlist, journal_path=journal,
        scorer=lambda s, p: 0.50 + int(s[1:]) * 0.001, asof=_ASOF,
        max_per_play=50, plays=("swing",),
    )

    data = json.loads(watchlist.read_text())
    active = [r for r in data["plays"]["swing"] if r["state"] == STATE_ACTIVE]
    assert len(active) == 60
    assert {r["symbol"] for r in active} == set(syms)
    assert summary["per_play"]["swing"]["n_evicted_today"] == 0
    assert summary["events_written"] == 0
    assert not journal.exists()


def test_evolve_watchlist_trims_over_cap_active_set(tmp_path, monkeypatch):
    """End-to-end: a seeded 60-active set with cap 50 trims to 50 + 10 evicts."""
    monkeypatch.setenv("HERMES_QUANT_WATCHLIST_CAP_TRIM", "1")
    universe = tmp_path / "universe.json"
    watchlist = tmp_path / "play-fit.json"
    journal = tmp_path / "journal.jsonl"
    syms = [_sym(i) for i in range(60)]
    _write_universe(universe, syms)
    _seed_over_cap_state(watchlist, n=60)

    # Scorer keeps every row active: above evict_floor (0.45), below onboard_floor
    # (0.65) so no re-onboard noise; distinct per-symbol so ranking is clean.
    def scorer(sym, play):
        return 0.50 + int(sym[1:]) * 0.002  # 0.500 .. 0.618

    summary = evolve_watchlist(
        universe_path=universe, watchlist_path=watchlist, journal_path=journal,
        scorer=scorer, asof=_ASOF, max_per_play=50, plays=("swing",),
    )
    data = json.loads(watchlist.read_text())
    rows = data["plays"]["swing"]
    active = [r for r in rows if r["state"] == STATE_ACTIVE]
    assert len(active) == 50, f"expected exactly 50 active after trim, got {len(active)}"
    assert summary["per_play"]["swing"]["n_evicted_today"] == 10
    assert summary["per_play"]["swing"]["n_active"] == 50


def test_evolve_watchlist_trim_deterministic_across_runs(tmp_path, monkeypatch):
    """Two identical evolves -> identical surviving active symbol set."""
    monkeypatch.setenv("HERMES_QUANT_WATCHLIST_CAP_TRIM", "1")

    def run(dirpath):
        universe = dirpath / "universe.json"
        watchlist = dirpath / "play-fit.json"
        journal = dirpath / "journal.jsonl"
        syms = [_sym(i) for i in range(60)]
        _write_universe(universe, syms)
        _seed_over_cap_state(watchlist, n=60)
        evolve_watchlist(
            universe_path=universe, watchlist_path=watchlist, journal_path=journal,
            scorer=lambda s, p: 0.50 + int(s[1:]) * 0.002, asof=_ASOF,
            max_per_play=50, plays=("swing",),
        )
        data = json.loads(watchlist.read_text())
        return sorted(r["symbol"] for r in data["plays"]["swing"] if r["state"] == STATE_ACTIVE)

    a = run(tmp_path / "a")
    b = run(tmp_path / "b")
    assert a == b
    assert len(a) == 50


def test_evolve_watchlist_under_cap_unset_byte_identical_to_flag_on(tmp_path, monkeypatch):
    """At/under-cap state is byte-identical whether the migration flag is on or off."""
    syms = [_sym(i) for i in range(30)]

    def run(dirpath, flag: str | None):
        universe = dirpath / "universe.json"
        watchlist = dirpath / "play-fit.json"
        journal = dirpath / "journal.jsonl"
        _write_universe(universe, syms)
        _seed_over_cap_state(watchlist, n=30)
        if flag is None:
            monkeypatch.delenv("HERMES_QUANT_WATCHLIST_CAP_TRIM", raising=False)
        else:
            monkeypatch.setenv("HERMES_QUANT_WATCHLIST_CAP_TRIM", flag)
        summary = evolve_watchlist(
            universe_path=universe, watchlist_path=watchlist, journal_path=journal,
            scorer=lambda s, p: 0.50 + int(s[1:]) * 0.002, asof=_ASOF,
            max_per_play=50, plays=("swing",),
        )
        return summary, watchlist.read_text(encoding="utf-8")

    unset_summary, unset_bytes = run(tmp_path / "unset", None)
    on_summary, on_bytes = run(tmp_path / "on", "1")

    assert unset_summary == on_summary
    assert unset_bytes == on_bytes
    assert unset_summary["per_play"]["swing"]["n_evicted_today"] == 0
    assert unset_summary["per_play"]["swing"]["n_active"] == 30
