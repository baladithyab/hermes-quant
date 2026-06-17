"""Cross-process lost-update RED→GREEN test for evolve_watchlist.

``evolve_watchlist`` is a read-modify-write on the live, money-adjacent
tradeable-universe state file ``~/.hermes/quant/watchlist/play-fit.json``:

  1. ``_read_watchlist(play-fit.json)``    (READ current ranked state)
  2. score + onboard/evict per play         (MODIFY)
  3. ``_atomic_write_json(play-fit.json)``  (WRITE the WHOLE new state)

The atomic write is crash-safe (tempfile+fsync+os.replace), but the whole
read-modify-write has **no cross-process lock**. The cron driver
(``scripts/quant-watchlist-evolve.py``) holds no lock either, and the
sibling operator watchlist module (``hermes_quant.watchlist``) — which
manages the SAME class of state — DOES flock. So two ``evolve_watchlist``
processes (a cron tick that overran its 600s budget while the next tick
fires, or an operator manual re-run overlapping the cron) can interleave:
both read the same prior state, each computes its own onboard/evict
transitions, and the second writer's ``os.replace`` clobbers the first
writer's entire state file — a classic lost update.

Polarity: an onboarded (newly tradeable) symbol that one process committed
is silently dropped by the other process's clobber; downstream the
autonomous tick only trades ``state=='active'`` rows, so the lost onboard
means a position the watchlist meant to open is silently not opened (or an
evict the watchlist meant to apply is silently reverted).

This test drives two real threads through ``evolve_watchlist`` with a
barrier that forces both to READ the same stale state before EITHER writes,
exactly reproducing the cross-process interleave. It does NOT edit any
existing test.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pandas as pd
import pytest

import hermes_quant.playbook.watchlist_evolution as we
from hermes_quant.playbook.watchlist_evolution import (
    STATE_ACTIVE,
    evolve_watchlist,
)


def _universe(path: Path, symbols: list[str]) -> Path:
    path.write_text(
        json.dumps({"asof": "2026-01-01T00:00:00+00:00", "symbols": symbols})
    )
    return path


def _active_symbols(state_path: Path) -> set[str]:
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for _play, rows in (payload.get("plays") or {}).items():
        for r in rows:
            if r.get("state") == STATE_ACTIVE:
                out.add(r["symbol"])
    return out


def test_concurrent_evolve_loses_an_onboard(tmp_path: Path) -> None:
    """Two interleaved evolve processes -> the second clobbers the first's onboard.

    Process A scores AAA above the onboard floor (so AAA onboards). Process B
    scores BBB above the floor (so BBB onboards). Each reads the SAME empty
    prior state (the barrier guarantees both read before either writes). A
    correct, serialized store would end with BOTH AAA and BBB active (B reads
    A's committed state). A lost-update store ends with only ONE active — the
    last writer's view wins and the other onboard is silently dropped.
    """
    state_path = tmp_path / "play-fit.json"
    journal_path = tmp_path / "journal.jsonl"
    universe_path = _universe(tmp_path / "universe.json", ["AAA", "BBB"])

    asof = pd.Timestamp("2026-03-02T12:00:00", tz="UTC")

    # A barrier placed INSIDE _read_watchlist forces both threads to complete
    # their READ of the (empty) prior state before EITHER reaches the write —
    # the exact two-process interleave a cron-overrun / operator-rerun produces.
    #
    # On the BUGGY (lock-free) code, both threads reach the read, rendezvous at
    # the barrier, then both write -> the second clobbers the first (lost
    # update). On the FIXED code, the flock now wraps the read too, so only the
    # lock-holder reaches the barrier; the other thread blocks on flock and
    # never arrives. The lone waiter's barrier times out (BrokenBarrierError,
    # tolerated below), the holder commits + releases, and the second thread
    # then reads the COMMITTED state and onboards correctly -> both survive.
    read_barrier = threading.Barrier(2)
    real_read = we._read_watchlist

    def barriered_read(path: Path):
        result = real_read(path)
        try:
            read_barrier.wait(timeout=3)
        except threading.BrokenBarrierError:  # serialized by the flock fix
            pass
        return result

    # Each "process" onboards a different symbol (its own scores above the
    # onboard floor, sticky_onboard_days=1 so a single run onboards) while
    # scoring the OTHER symbol at a neutral HOLD (above evict_floor, below
    # onboard_floor) so it is neither onboarded nor evicted by this process.
    # Serial outcome: A onboards AAA (BBB candidate); B then reads {AAA active,
    # BBB candidate}, onboards BBB, AAA stays active -> both active.
    def make_scorer(winner: str):
        def _scorer(symbol: str, _play: str) -> float:
            return 0.95 if symbol == winner else 0.5

        return _scorer

    results: dict[str, BaseException | None] = {}

    def run(name: str, winner: str) -> None:
        try:
            evolve_watchlist(
                universe_path=universe_path,
                watchlist_path=state_path,
                journal_path=journal_path,
                scorer=make_scorer(winner),
                sticky_onboard_days=1,
                onboard_floor=0.65,
                evict_floor=0.10,
                asof=asof,
            )
            results[name] = None
        except BaseException as exc:  # noqa: BLE001 - capture for assertion
            results[name] = exc

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(we, "_read_watchlist", barriered_read)
        t_a = threading.Thread(target=run, args=("A", "AAA"))
        t_b = threading.Thread(target=run, args=("B", "BBB"))
        t_a.start()
        t_b.start()
        t_a.join(timeout=20)
        t_b.join(timeout=20)

    assert results.get("A") is None, f"thread A raised: {results.get('A')!r}"
    assert results.get("B") is None, f"thread B raised: {results.get('B')!r}"

    active = _active_symbols(state_path)
    # Correct serialized behavior: both onboards survive. Lost-update: only one.
    assert active == {"AAA", "BBB"}, (
        "cross-process lost update: one process's onboard was clobbered by the "
        f"other's whole-file write. Final active set = {active!r} (expected "
        "{'AAA', 'BBB'})"
    )


def test_concurrent_evolve_serialized_is_non_vacuous(tmp_path: Path) -> None:
    """Non-vacuity guard: run the SAME two evolutions strictly serially and
    confirm both onboards land. Proves the {'AAA','BBB'} expectation is the
    correct serialized outcome, so the failure above is the interleave — not an
    impossible assertion."""
    state_path = tmp_path / "play-fit.json"
    journal_path = tmp_path / "journal.jsonl"
    universe_path = _universe(tmp_path / "universe.json", ["AAA", "BBB"])
    asof = pd.Timestamp("2026-03-02T12:00:00", tz="UTC")

    def make_scorer(winner: str):
        def _scorer(symbol: str, _play: str) -> float:
            return 0.95 if symbol == winner else 0.5

        return _scorer

    evolve_watchlist(
        universe_path=universe_path,
        watchlist_path=state_path,
        journal_path=journal_path,
        scorer=make_scorer("AAA"),
        sticky_onboard_days=1,
        onboard_floor=0.65,
        evict_floor=0.10,
        asof=asof,
    )
    evolve_watchlist(
        universe_path=universe_path,
        watchlist_path=state_path,
        journal_path=journal_path,
        scorer=make_scorer("BBB"),
        sticky_onboard_days=1,
        onboard_floor=0.65,
        evict_floor=0.10,
        asof=asof,
    )
    assert _active_symbols(state_path) == {"AAA", "BBB"}
