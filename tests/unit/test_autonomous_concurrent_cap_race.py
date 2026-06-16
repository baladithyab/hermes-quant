"""ADR-0016 §D9 concurrent-positions safety rail — cross-process race regression.

The defect (RED-verified): ``hermes_quant.autonomous.tick()`` enforces the
``max_concurrent_positions`` rail by reading the WHOLE open book ONCE at tick
start (``reconstruct_portfolio_state(...).positions``) entirely OUTSIDE any
lock, then enforcing the cap in-loop against the tick-LOCAL counters
``open_positions_at_tick_start + fires_this_tick``. ``tick()`` itself has NO
whole-tick / per-account guard, so the cron path (``hermes quant autonomous
tick``) and the agent TOOL path (``quant_autonomous_tick``) can overlap.

Concrete overshoot reproduced here: book = cap-1 open, cap = N. Two ticks read
the SAME pre-fire book (cap-1 open). Each fires a DISTINCT new symbol
(projected cap-1+0 < cap, so each admits). The two new symbols differ, so the
per-symbol bus serialization (``append_locked`` is per-WRITE, not per-tick) does
NOT serialize the read-decide-fire window. Both append -> book = cap+1 > cap.

The §D9 read+enforce both complete BEFORE ``execute()`` is entered, and any
reaction-layer lock is downstream of the §D9 decision — so it cannot close this
race. The fix is a per-ACCOUNT advisory lock around tick()'s rail-sensitive
region (open-book read through the fire loop), ON by default, fail-open-safe
only on a genuine flock-unsupported infra error.

This is a REAL multi-process test (not threads): only separate processes prove
the file-lock serializes the read-decide-fire window. Threads would serialize
under the GIL and hide the race.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import time
from pathlib import Path
from typing import Any

import pytest

CAP = 5
# book starts with CAP-1 = 4 open positions; one free slot remains.
_PRESEED_SYMBOLS = [f"OPEN{i}" for i in range(CAP - 1)]
# each worker fires a DISTINCT brand-new symbol; both would be admitted against
# the same stale pre-fire count, jointly overshooting the single free slot.
_WORKER_NEW_SYMBOL = {0: "NEWA", 1: "NEWB"}


def _write_config(home: Path) -> None:
    """Write a ~/.hermes/config.yaml that puts the tick into autonomous mode
    with max_concurrent_positions=CAP and max_per_tick_opens high enough that
    the per-tick rail never fires (we want the CONCURRENT rail under test)."""
    cfg_dir = home / ".hermes"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(
        "quant:\n"
        "  pdr:\n"
        "    mode: autonomous\n"
        "  autonomous:\n"
        f"    max_concurrent_positions: {CAP}\n"
        "    max_per_tick_opens: 5\n"
        "    kill_switch_pct: 0.0\n",  # 0 disables the live kill-switch trip
        encoding="utf-8",
    )


def _preseed_book(executions_path: Path) -> None:
    """Seed executions.jsonl with CAP-1 open paper positions (distinct symbols,
    nonzero target_position_pct, reactor_name=paper) so the reconstruction sees
    a book one slot below the cap."""
    executions_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, sym in enumerate(_PRESEED_SYMBOLS):
        rec = {
            "schema_version": 2,
            "proposal_id": f"seed-{sym}",
            "signal_id": f"sig-{sym}",
            "asset": sym,
            "asset_class": "equity",
            "timeframe": "1d",
            "asof_decision": "2026-06-01T00:00:00Z",
            "asof_execution": f"2026-06-01T00:0{i}:00Z",
            "target_position_pct": 0.05,
            "decision_price": 100.0,
            "fill_price": 100.0,
            "fill_size_pct": 0.05,
            "reactor_name": "paper",
        }
        lines.append(json.dumps(rec, separators=(",", ":"), sort_keys=True))
    executions_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fire_advisor(*, decision_price: float = 200.0):
    """An advisor_recommend stub that yields a high-conviction LONG which clears
    the silence-bias gate (conf 0.9, magnitude 0.02, 2 voices => urgency 1.6).
    LONG (kelly>0) avoids the short-only admissibility path."""

    def _recommend(**kwargs: Any) -> dict[str, Any]:
        return {
            "as_of": "2026-06-13T00:00:00Z",
            "decision_price": decision_price,
            "aggregated_signal": {
                "direction": 1,
                "confidence": 0.9,
                "magnitude": 0.02,
            },
            "risk_gate": {
                "pass": True,
                "gated_reason": None,
                "kelly_fraction": 0.05,
                "reason": "test_long",
            },
            "analyst_views": [
                {"metadata": {"atr_relative": 0.01}},
                {"metadata": {"atr_relative": 0.01}},
            ],
            "lessons": [],
        }

    return _recommend


def _count_open_positions(executions_path: Path) -> int:
    """Reconstruct the open-position count from the bus exactly as the §D9 rail
    does (reactor_filter=paper, drop zeros)."""
    latest: dict[str, tuple[str, float]] = {}
    for line in executions_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            continue
        if rec.get("reactor_name") != "paper":
            continue
        asset = rec.get("asset")
        ts = rec.get("asof_execution")
        tgt = rec.get("target_position_pct")
        if asset is None or ts is None or tgt is None:
            continue
        prior = latest.get(asset)
        if prior is None or ts >= prior[0]:
            latest[asset] = (ts, float(tgt))
    return sum(1 for _ts, t in latest.values() if t != 0.0)


def _worker(home_str: str, worker_id: int, barrier: mp.Barrier) -> None:
    """Run a single autonomous tick in this process against the SHARED home.

    The slow critical section is injected via the advisor callback: it waits on
    the Barrier so BOTH workers have already completed the §D9 open-book read
    before EITHER fires. Without a per-account lock around the rail region, each
    sees the same stale pre-fire count and admits its distinct new symbol.
    """
    os.environ["HOME"] = home_str
    # Default-OFF refinement flags must stay OFF (byte-identical path).
    for k in (
        "HERMES_QUANT_PORTFOLIO_CAPS",
        "HERMES_QUANT_ADMISSIBILITY",
        "HERMES_QUANT_REFLECTION",
        "HERMES_QUANT_SEMANTIC_ENABLED",
    ):
        os.environ.pop(k, None)
    os.environ["HERMES_QUANT_SEMANTIC_ENABLED"] = "0"  # no perception fetch
    os.environ["HERMES_QUANT_REFLECTION"] = "0"

    home = Path(home_str)
    quant_home = home / ".hermes" / "quant"
    executions_path = quant_home / "executions.jsonl"

    import hermes_quant.autonomous as auto
    import hermes_quant.daemon.signal_bus as sb
    import hermes_quant.react.paper as paper

    # Rebind every module-level home/path so this forked process targets the
    # shared tmp book (the parent imported these against the real ~/.hermes).
    auto.QUANT_HOME = quant_home
    auto.KILL_SWITCH_PATH = quant_home / "autonomous_kill_switch.json"
    sb.QUANT_HOME = quant_home
    sb.EXECUTION_BUS_PATH = executions_path
    paper.EXECUTION_BUS_PATH = executions_path

    # Force the config readers to re-read from the rebound HOME (the cached
    # _read_config may have memoized the real home at import).
    if hasattr(auto, "_read_config"):
        try:
            auto._read_config.cache_clear()  # type: ignore[attr-defined]
        except AttributeError:
            pass

    new_symbol = _WORKER_NEW_SYMBOL[worker_id]
    from hermes_quant.watchlist import WatchlistEntry

    base_advisor = _fire_advisor()

    def _barrier_advisor(**kwargs: Any) -> dict[str, Any]:
        # Rendezvous: try to meet the OTHER worker here. tick() has already read
        # the open book by the time the advisor is consulted, so in the UNLOCKED
        # (RED) world both ticks rendezvous past the §D9 read with the SAME stale
        # count before either fires -> both admit -> overshoot. A short timeout
        # keeps the LOCKED (GREEN) world from stalling: there the second tick is
        # blocked acquiring the per-account rail lock and never arrives, so the
        # first tick's barrier times out quickly, it fires + releases, and the
        # second re-reads the now-larger book and silences.
        try:
            barrier.wait(timeout=3)
        except Exception:
            pass
        time.sleep(0.2 * (worker_id + 1))
        return base_advisor(**kwargs)

    auto.tick(
        dry_run=False,
        symbols=[WatchlistEntry(symbol=new_symbol, asset_class="equity", timeframe="1d")],
        advisor_recommend=_barrier_advisor,
    )


@pytest.mark.timeout(60)
def test_concurrent_ticks_do_not_exceed_max_concurrent_positions(tmp_path: Path) -> None:
    """Two overlapping autonomous ticks, each firing a DISTINCT new symbol, must
    NOT jointly push the open-position book past max_concurrent_positions.

    RED (pre-fix): the rail reads the pre-fire book (CAP-1) outside any lock and
    enforces against tick-local counters, so BOTH admit -> book ends at CAP+1.
    GREEN (post-fix): the per-account lock serializes the rail's read-decide-fire
    window — the second tick re-reads the now-CAP book and SILENCES via
    SILENCE_CONCURRENT_CAP, so the book ends at exactly CAP.
    """
    home = tmp_path
    _write_config(home)
    executions_path = home / ".hermes" / "quant" / "executions.jsonl"
    _preseed_book(executions_path)

    assert _count_open_positions(executions_path) == CAP - 1, "preseed sanity"

    ctx = mp.get_context("fork")
    barrier = ctx.Barrier(2)
    procs = [
        ctx.Process(target=_worker, args=(str(home), wid, barrier))
        for wid in (0, 1)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=45)
    for p in procs:
        if p.is_alive():
            p.terminate()
            p.join()
            pytest.fail("worker tick hung (possible deadlock)")
        assert p.exitcode == 0, f"worker exited non-zero: {p.exitcode}"

    final = _count_open_positions(executions_path)
    assert final <= CAP, (
        f"ADR-0016 §D9 concurrent-positions cap BREACHED: book ended with "
        f"{final} open positions (cap={CAP}). Two overlapping ticks each admitted "
        f"a distinct new symbol against the same stale pre-fire count."
    )
