"""Concurrency + fail-open tests for the ADR-0078 per-symbol tick lock.

The race (ra10 / ADR-0078): the fire sequence reconstructs the book, decides,
appends to executions.jsonl, then updates state.db — with NO lock held across
that whole window. Two armed crons (autonomous + playbook) can both reconstruct
the SAME pre-fire book, both decide to fire the SAME symbol, and both append:
a read-modify-write (TOCTOU) race on the money ledger (the 880%-gross mechanism).

These tests interleave TWO real writers (separate PROCESSES via multiprocessing)
hammering the SAME symbol with distinct proposal_ids and the SAME target, against
ONE shared tmp ledger.

  * RED  (HERMES_QUANT_TICK_LOCK=0): the unlocked path can append BOTH fills in an
    interleaved/torn way — two lines for one intended position, or a torn JSONL
    line. We assert the unlocked path EXHIBITS contention damage (≥1 of: torn
    line, or both distinct proposals landing as separate position-moving fills),
    which is exactly the bug.
  * GREEN (lock ON, default): the per-symbol lock serializes the two writers; the
    final reconstructed net is the single intended target, there is no torn JSONL
    line, and at most one position-moving fill per proposal landed serialized
    (never interleaved into a torn write).

We use a real OS barrier (multiprocessing.Barrier) so both processes hit the
fire seam at the same instant, maximizing the interleave window. To make the
race observable WITHOUT the lock, each writer takes a slow path INSIDE the
critical section (a monkeypatched apply_execution that sleeps), so a torn /
interleaved append is overwhelmingly likely on the unlocked path.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# Use a fork start method so the worker can rely on the parent's import state.
# Linux default is already fork; we set it explicitly for determinism.
_MP_CTX = mp.get_context("fork")


def _fire_one(
    *,
    quant_home: str,
    executions_path: str,
    state_db_path: str,
    proposal_id: str,
    symbol: str,
    target: float,
    tick_lock_on: bool,
    slow_inside_critical_s: float,
    lock_timeout_s: float,
    barrier: Any,  # multiprocessing.synchronize.Barrier (factory, not a public type)
) -> None:
    """Worker process: fire one PaperReactor.execute for `symbol` at `target`.

    Runs in a CHILD process — it must rebuild every path from arguments, since
    monkeypatched module attributes in the parent do NOT cross the process
    boundary. The lock directory is resolved from HERMES_QUANT_HOME (env), so
    both children agree on the same lock files.
    """
    # Point QUANT_HOME (and thus the lock dir) + all flags from the args.
    os.environ["HERMES_QUANT_HOME"] = quant_home
    os.environ["HERMES_QUANT_TICK_LOCK"] = "1" if tick_lock_on else "0"
    # Short acquire timeout so the LOSER of a contended same-symbol race times out
    # and SKIPS (silence-by-default) rather than waiting out a long critical section.
    os.environ["HERMES_QUANT_TICK_LOCK_TIMEOUT_S"] = str(lock_timeout_s)
    # Keep the reactor lean + deterministic: no slippage, no reflection, no caps.
    os.environ["HERMES_QUANT_PAPER_SLIPPAGE_MODEL"] = "v0.1"
    os.environ["HERMES_QUANT_REFLECTION"] = "0"
    os.environ.pop("HERMES_QUANT_PORTFOLIO_CAPS", None)
    os.environ.pop("HERMES_QUANT_ADMISSIBILITY", None)

    from hermes_quant.react.paper import PaperReactor
    from hermes_quant.state import portfolio_state as ps_mod

    # Force the state.db singleton to the shared tmp db for this child.
    ps_mod.DEFAULT_STATE_DB = Path(state_db_path)
    with ps_mod._singleton_lock:
        ps_mod._singleton = None

    # Slow the critical section so an unlocked interleave is observable. We wrap
    # apply_execution to sleep AFTER the bus append has happened but while the
    # "writer" is still in its read-modify-write window.
    orig_apply = ps_mod.PortfolioState.apply_execution

    def _slow_apply(self, record):  # type: ignore[no-untyped-def]
        time.sleep(slow_inside_critical_s)
        return orig_apply(self, record)

    ps_mod.PortfolioState.apply_execution = _slow_apply  # type: ignore[assignment]

    proposal = SimpleNamespace(
        proposal_id=proposal_id,
        symbol=symbol,
        asset_class="equity",
        timeframe="1d",
        advisor_result={"decision_price": 100.0, "as_of": "2026-06-12T10:00:00Z"},
        reactor_metadata=None,
    )

    reactor = PaperReactor(executions_path=Path(executions_path))

    # Synchronize both writers to the fire seam at the same instant.
    barrier.wait(timeout=30)
    reactor.execute(proposal, fill_size_pct=target, play_tag="autonomous")


def _run_two_writers(
    tmp_path: Path,
    *,
    tick_lock_on: bool,
    target: float = 0.05,
    slow_inside_critical_s: float = 1.0,
    lock_timeout_s: float = 0.3,
) -> tuple[list[dict], list[str], dict]:
    """Spawn two writer processes firing the SAME symbol; return parsed results.

    Returns:
        (records, raw_lines, reconstructed_positions)
        - records: parsed JSONL dicts from the shared ledger (torn lines excluded).
        - raw_lines: every non-empty raw line (to detect torn/partial appends).
        - reconstructed_positions: final {symbol: target} from reconstruct_portfolio_state.
    """
    quant_home = tmp_path / "quant_home"
    quant_home.mkdir(parents=True, exist_ok=True)
    executions_path = quant_home / "executions.jsonl"
    executions_path.touch()
    state_db_path = quant_home / "state.db"

    barrier = _MP_CTX.Barrier(2)
    procs: list[mp.Process] = []
    for i in range(2):
        p = _MP_CTX.Process(
            target=_fire_one,
            kwargs=dict(
                quant_home=str(quant_home),
                executions_path=str(executions_path),
                state_db_path=str(state_db_path),
                proposal_id=f"prop_race_{i}",
                symbol="AAPL",
                target=target,
                tick_lock_on=tick_lock_on,
                slow_inside_critical_s=slow_inside_critical_s,
                lock_timeout_s=lock_timeout_s,
                barrier=barrier,
            ),
        )
        procs.append(p)

    for p in procs:
        p.start()
    # Join with a generous timeout — a hung tick (the thing we must NEVER cause)
    # would blow this and fail the test loudly rather than hang the suite.
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode is not None, "writer process hung — tick lock must never deadlock"
        assert p.exitcode == 0, f"writer process crashed (exitcode={p.exitcode})"

    raw_lines = [ln for ln in executions_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    records: list[dict] = []
    torn = 0
    for ln in raw_lines:
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            torn += 1
            continue
        if isinstance(rec, dict):
            records.append(rec)

    # Reconstruct the final net position from the shared ledger.
    from hermes_quant.portfolio.state import reconstruct_portfolio_state

    state = reconstruct_portfolio_state(executions_path)
    return records, raw_lines, {"positions": state.positions, "torn": torn}


# ---------------------------------------------------------------------------
# RED: with the lock OFF, two writers on one symbol corrupt the ledger.
# ---------------------------------------------------------------------------
@pytest.mark.timeout(120)
def test_red_unlocked_two_writers_one_symbol_double_fires(tmp_path: Path) -> None:
    """RED: HERMES_QUANT_TICK_LOCK=0 — two distinct proposals racing on one symbol
    both append a POSITION-MOVING fill (double-fire), which is the bug.

    Without the lock there is NO serialization across the read-decide-append-store
    window, so both writers append their own +0.05 AAPL line. The intended state
    is a SINGLE +0.05 AAPL position; two distinct fills on the bus for one intended
    position is the inflation mechanism (each is a separate proposal_id, so the
    processed_fills idempotency guard does NOT dedup them).
    """
    records, raw_lines, info = _run_two_writers(tmp_path, tick_lock_on=False)

    position_moving = [r for r in records if r.get("fill_size_pct", 0) != 0.0]
    distinct_props = {r.get("proposal_id") for r in position_moving}

    # The bug: both distinct proposals landed as position-moving fills (double-fire)
    # OR a torn JSONL line was produced. Either is contention damage the lock fixes.
    assert info["torn"] >= 1 or len(distinct_props) == 2, (
        "expected the UNLOCKED path to double-fire (2 distinct position-moving "
        f"proposals) or tear a line; got distinct_props={distinct_props} "
        f"torn={info['torn']} raw_lines={raw_lines}"
    )


# ---------------------------------------------------------------------------
# GREEN: with the lock ON (default), the two writers serialize cleanly.
# ---------------------------------------------------------------------------
@pytest.mark.timeout(120)
def test_green_locked_two_writers_one_symbol_no_double_fire(tmp_path: Path) -> None:
    """GREEN: lock ON — the per-symbol tick lock serializes the two writers.

    Exactly ONE writer wins the lock and fires; the contending writer SKIPS the
    symbol this tick (silenced, NOT appended). The final reconstructed net is the
    single intended +0.05 AAPL position, there is no torn JSONL line, and the bus
    carries exactly ONE position-moving fill.
    """
    records, raw_lines, info = _run_two_writers(tmp_path, tick_lock_on=True)

    # No torn / partial line — every raw line parsed as a dict.
    assert info["torn"] == 0, f"locked path produced a torn line: {raw_lines}"

    position_moving = [r for r in records if r.get("fill_size_pct", 0) != 0.0]
    distinct_props = {r.get("proposal_id") for r in position_moving}

    # Exactly one position-moving fill landed (the lock winner). The loser was
    # silenced (tick_lock_contended) and NOT appended to the bus.
    assert len(position_moving) == 1, (
        f"expected exactly ONE position-moving fill under the lock, got "
        f"{len(position_moving)}: {position_moving}"
    )
    assert len(distinct_props) == 1, (
        f"two distinct proposals double-fired under the lock: {distinct_props}"
    )

    # The final reconstructed net is the single intended target, not inflated.
    positions = info["positions"]
    assert positions.get("AAPL") == pytest.approx(0.05), (
        f"final net should be the single intended +0.05, got {positions}"
    )


# ---------------------------------------------------------------------------
# FAIL-OPEN: a lock that cannot be acquired degrades to a logged skip; the tick
# never hangs or crashes. (Single-process, deterministic — no race needed.)
# ---------------------------------------------------------------------------
def test_fail_open_on_lock_open_error_does_not_hang_or_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """If the lock file cannot be opened (e.g. a filesystem with no flock), the
    reactor must FAIL OPEN: proceed with the fire (today's behavior) with a
    WARNING, NEVER hang and NEVER crash.

    We force the failure by patching os.open INSIDE the tick_lock module to raise
    OSError on the lock path. The contextmanager yields fail_open=True and the
    reactor runs _execute_fired anyway — proving the degrade path is wired and
    safe.
    """
    monkeypatch.setenv("HERMES_QUANT_TICK_LOCK", "1")
    monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.1")
    monkeypatch.setenv("HERMES_QUANT_REFLECTION", "0")

    import hermes_quant.daemon.tick_lock as tl

    real_os_open = os.open

    def _boom_open(path, *a, **k):  # type: ignore[no-untyped-def]
        if str(path).endswith(".lock"):
            raise OSError(95, "Operation not supported")  # ENOTSUP-style
        return real_os_open(path, *a, **k)

    monkeypatch.setattr(tl.os, "open", _boom_open)

    from hermes_quant.react.paper import PaperReactor
    from hermes_quant.state import portfolio_state as ps_mod

    ps_mod.DEFAULT_STATE_DB = tmp_path / "state.db"
    with ps_mod._singleton_lock:
        ps_mod._singleton = None

    proposal = SimpleNamespace(
        proposal_id="prop_failopen",
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        advisor_result={"decision_price": 100.0, "as_of": "2026-06-12T10:00:00Z"},
        reactor_metadata=None,
    )
    executions_path = tmp_path / "executions.jsonl"
    reactor = PaperReactor(executions_path=executions_path)

    # Hard guard against a hang: wall-clock bound the call.
    start = time.monotonic()
    with caplog.at_level("WARNING"):
        record = reactor.execute(proposal, fill_size_pct=0.05, play_tag="autonomous")
    elapsed = time.monotonic() - start

    assert elapsed < 10.0, f"fail-open path must not hang (took {elapsed:.1f}s)"

    # The fire STILL happened (degraded to today's behavior) — not silenced.
    assert record.fill_size_pct == pytest.approx(0.05)
    assert not (record.reactor_metadata or {}).get("silenced")

    # The fill landed on the bus (the tick was not blocked).
    lines = [ln for ln in executions_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected the fail-open fill to land, got {lines}"

    # And a fail-open WARNING was logged so the degrade is visible.
    assert any("FAILING OPEN" in r.message for r in caplog.records), (
        "fail-open path must emit a visible WARNING"
    )


# ---------------------------------------------------------------------------
# FLAG-OFF: HERMES_QUANT_TICK_LOCK=0 bypasses the lock entirely (byte-identical
# to the pre-ADR-0078 path — the lock module is never even touched).
# ---------------------------------------------------------------------------
def test_flag_off_never_touches_the_lock_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With HERMES_QUANT_TICK_LOCK=0 the fire path must NOT call symbol_tick_lock.

    Guardrail: patch symbol_tick_lock to blow up if invoked. The fill must land
    exactly as today, proving the flag-off path is byte-identical (no lock seam).
    """
    monkeypatch.setenv("HERMES_QUANT_TICK_LOCK", "0")
    monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.1")
    monkeypatch.setenv("HERMES_QUANT_REFLECTION", "0")

    import hermes_quant.react.paper as paper_mod

    def _should_not_be_called(*a, **k):  # type: ignore[no-untyped-def]
        raise AssertionError("symbol_tick_lock must NOT be called when flag is OFF")

    monkeypatch.setattr(paper_mod, "symbol_tick_lock", _should_not_be_called)

    from hermes_quant.state import portfolio_state as ps_mod

    ps_mod.DEFAULT_STATE_DB = tmp_path / "state.db"
    with ps_mod._singleton_lock:
        ps_mod._singleton = None

    proposal = SimpleNamespace(
        proposal_id="prop_flagoff",
        symbol="MSFT",
        asset_class="equity",
        timeframe="1d",
        advisor_result={"decision_price": 100.0, "as_of": "2026-06-12T10:00:00Z"},
        reactor_metadata=None,
    )
    executions_path = tmp_path / "executions.jsonl"
    reactor = paper_mod.PaperReactor(executions_path=executions_path)

    record = reactor.execute(proposal, fill_size_pct=0.07, play_tag="autonomous")

    assert record.fill_size_pct == pytest.approx(0.07)
    assert not (record.reactor_metadata or {}).get("silenced")
    lines = [ln for ln in executions_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
