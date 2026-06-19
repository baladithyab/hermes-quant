"""cr04 RED proof: the per-symbol tick lock does NOT serialize DIFFERENT symbols that
share one account's gross-exposure cap, so two symbols firing concurrently both see
the same pre-fire gross headroom and both append => the portfolio gross cap is bypassed.

Mechanism (verified against code):
  * ADR-0078 ``symbol_tick_lock`` keys on the (account, asset_class, symbol) TRIPLE,
    so two DIFFERENT symbols (AAPL, MSFT) on ONE account use DIFFERENT lock files and
    never block each other.
  * ``_portfolio_cap_clip`` (paper.py, default-OFF behind HERMES_QUANT_PORTFOLIO_CAPS)
    reads ``ps.get_positions(account_id)`` — the WHOLE-account book — OUTSIDE any
    account-level lock, computes remaining headroom, and clips THIS fire to it.
  * Two symbols racing both reconstruct the SAME pre-fire book, both see the same
    remaining cash/gross headroom, and both append a full-size fill. Serially the
    second fire would have seen the first's consumed headroom and been clipped; the
    concurrent path bypasses that — combined gross overshoots the cap.

cr04 fix (LANDED 2026-06-14): a DEFAULT-OFF per-account lock (HERMES_QUANT_ACCOUNT_LOCK)
serializes ALL symbols on an account through the cap-read -> state.db write window so
the loser sees the winner's consumed headroom and the gross cap HOLDS. The lock wraps
the per-symbol lock (account-outer/symbol-inner => no deadlock) and is byte-identical to
the pre-cr04 path when the flag is unset.

  * ``test_red_cross_symbol_cap_bypass``                 — flag OFF (default): the race
        is STILL live; both symbols fire full and the cap is bypassed. Documents the
        unfixed default-OFF state.
  * ``test_green_cross_symbol_cap_held_with_account_lock`` — flag ON + generous timeout:
        the loser BLOCKS until the winner commits, sees consumed headroom, and the final
        book gross stays AT/UNDER the cap bound (clipped / silenced / skipped — never
        both full).
  * ``test_account_lock_off_byte_identical``             — single priced fire, flag
        unset: record is field-for-field identical to today's path.
  * ``test_caps_off_account_lock_on_harmless``           — caps OFF + lock ON: serializes
        but there is no cap to enforce; both append full size, no crash/deadlock.

We mirror tests/unit/test_tick_lock_race.py: real OS processes + a multiprocessing
Barrier so both writers hit the cap-read seam at the same instant, with a slow
critical section so the unlocked-across-symbols interleave is overwhelmingly likely.
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

from hermes_quant.risk.portfolio_normalize import PortfolioCaps

_MP_CTX = mp.get_context("fork")

# Existing seed gross (NAV-fraction). Chosen so that under PortfolioCaps.standard()
# (max_gross=2.0, min_cash_reserve=0.20 => cash-reserve-implied gross bound = 0.80)
# each of two +0.05 fires INDIVIDUALLY fits the pre-fire headroom (0.80 - 0.72 = 0.08
# >= 0.05), but the two TOGETHER push gross to 0.82 > 0.80 — the bypassed bound.
_SEED_SYMBOLS = {"SEEDA": 0.30, "SEEDB": 0.42}  # sum |.| = 0.72
_FIRE_TARGET = 0.05
_GROSS_BOUND = 1.0 - PortfolioCaps.standard().min_cash_reserve_pct  # 0.80


def _proposal(symbol: str, proposal_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        proposal_id=proposal_id,
        symbol=symbol,
        asset_class="equity",
        timeframe="1d",
        advisor_result={"decision_price": 100.0, "as_of": "2026-06-12T10:00:00Z"},
        reactor_metadata=None,
    )


def _seed_state_db(state_db_path: str, executions_path: str) -> None:
    """Pre-populate the shared state.db with the seed positions (gross 0.72)."""
    from hermes_quant.state import portfolio_state as ps_mod

    ps_mod.DEFAULT_STATE_DB = Path(state_db_path)
    with ps_mod._singleton_lock:
        ps_mod._singleton = None
    ps = ps_mod.get_portfolio_state()
    for i, (sym, tgt) in enumerate(_SEED_SYMBOLS.items()):
        ps.apply_execution(
            {
                "proposal_id": f"seed_{sym}",
                "asset": sym,
                "asset_class": "equity",
                "timeframe": "1d",
                "asof_execution": f"2026-06-12T09:0{i}:00Z",
                "target_position_pct": tgt,
                "decision_price": 100.0,
                "fill_price": 100.0,
                "fill_size_pct": tgt,
                "reactor_name": "paper",
                "human_in_the_loop": True,
                "account_id": "paper-default",
            }
        )


def _fire_one(
    *,
    quant_home: str,
    executions_path: str,
    state_db_path: str,
    proposal_id: str,
    symbol: str,
    slow_inside_critical_s: float,
    barrier: Any,
    account_lock: str = "0",
    portfolio_caps: str = "1",
    tick_lock_timeout_s: str = "5",
) -> None:
    """Child process: fire ONE PaperReactor.execute for `symbol`."""
    os.environ["HERMES_QUANT_HOME"] = quant_home
    os.environ["HERMES_QUANT_TICK_LOCK"] = "1"  # per-symbol lock ON
    os.environ["HERMES_QUANT_TICK_LOCK_TIMEOUT_S"] = tick_lock_timeout_s
    os.environ["HERMES_QUANT_ACCOUNT_LOCK"] = account_lock  # cr04 per-account lock
    os.environ["HERMES_QUANT_PAPER_SLIPPAGE_MODEL"] = "v0.1"
    os.environ["HERMES_QUANT_REFLECTION"] = "0"
    os.environ["HERMES_QUANT_PORTFOLIO_CAPS"] = portfolio_caps  # cap seam ARMED (default)
    os.environ.pop("HERMES_QUANT_ADMISSIBILITY", None)

    from hermes_quant.react.paper import PaperReactor
    from hermes_quant.state import portfolio_state as ps_mod

    ps_mod.DEFAULT_STATE_DB = Path(state_db_path)
    with ps_mod._singleton_lock:
        ps_mod._singleton = None

    # Slow the read-modify-write window so the cross-symbol interleave is observable:
    # sleep AFTER the cap-read has happened (inside the fire body) but before the
    # state.db write commits.
    orig_apply = ps_mod.PortfolioState.apply_execution

    def _slow_apply(self, record):  # type: ignore[no-untyped-def]
        time.sleep(slow_inside_critical_s)
        return orig_apply(self, record)

    ps_mod.PortfolioState.apply_execution = _slow_apply  # type: ignore[assignment]

    reactor = PaperReactor(executions_path=Path(executions_path))
    barrier.wait(timeout=30)
    reactor.execute(_proposal(symbol, proposal_id), fill_size_pct=_FIRE_TARGET, play_tag="autonomous")


@pytest.mark.timeout(120)
def test_red_cross_symbol_cap_bypass(tmp_path: Path) -> None:
    """RED: two DIFFERENT symbols racing under caps-ON both fire full size, bypassing
    the account gross cap (combined gross overshoots the cash-reserve-implied bound).

    Serially the second symbol would have seen the first's consumed headroom and been
    clipped to land AT the bound; concurrently both read the same pre-fire book and
    both append, so the final gross exceeds the bound — the cross-symbol bypass.
    """
    quant_home = tmp_path / "quant_home"
    quant_home.mkdir(parents=True, exist_ok=True)
    executions_path = quant_home / "executions.jsonl"
    executions_path.touch()
    state_db_path = quant_home / "state.db"

    # Pre-seed the shared state.db with gross 0.72 (in a child so the parent's import
    # state / singleton is not polluted for the racers).
    seed = _MP_CTX.Process(
        target=_seed_state_db, args=(str(state_db_path), str(executions_path))
    )
    seed.start()
    seed.join(timeout=60)
    assert seed.exitcode == 0, "seed process failed"

    barrier = _MP_CTX.Barrier(2)
    procs: list[mp.Process] = []
    for sym, pid in (("AAPL", "prop_aapl"), ("MSFT", "prop_msft")):
        p = _MP_CTX.Process(
            target=_fire_one,
            kwargs=dict(
                quant_home=str(quant_home),
                executions_path=str(executions_path),
                state_db_path=str(state_db_path),
                proposal_id=pid,
                symbol=sym,
                slow_inside_critical_s=1.0,
                barrier=barrier,
                account_lock="0",  # cr04 fix DEFAULT-OFF: race is still live
            ),
        )
        procs.append(p)

    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode is not None, "writer hung — tick lock must never deadlock"
        assert p.exitcode == 0, f"writer crashed (exitcode={p.exitcode})"

    # Parse the bus: both AAPL and MSFT should have landed POSITION-MOVING fills.
    raw_lines = [ln for ln in executions_path.read_text().splitlines() if ln.strip()]
    fired: dict[str, float] = {}
    for ln in raw_lines:
        rec = json.loads(ln)
        if rec.get("fill_size_pct", 0.0) != 0.0:
            fired[rec["asset"]] = rec["fill_size_pct"]

    # Both new symbols fired at (near) full size — neither was clipped by the other's
    # consumed headroom, because the cap-read is not serialized across symbols.
    assert "AAPL" in fired and "MSFT" in fired, (
        f"expected BOTH symbols to fire (cross-symbol bypass), got {fired}"
    )
    assert fired["AAPL"] == pytest.approx(_FIRE_TARGET)
    assert fired["MSFT"] == pytest.approx(_FIRE_TARGET)

    # Final gross of the WHOLE book (the seed positions in state.db PLUS the two new
    # fires) EXCEEDS the cash-reserve-implied bound the cap-clip is supposed to
    # enforce — the bypass. We read state.db, which IS the book the cap-clip
    # reconstructs against (the seed positions were written there, not to the bus).
    # Serially the second symbol would have seen 0.77 gross and been clipped to land
    # AT 0.80; concurrently both fired full so the book lands at ~0.82.
    from hermes_quant.state import portfolio_state as ps_mod

    ps_mod.DEFAULT_STATE_DB = state_db_path
    with ps_mod._singleton_lock:
        ps_mod._singleton = None
    book = ps_mod.get_portfolio_state().get_positions("paper-default")
    gross = sum(abs(p.quantity) for p in book.values())
    assert gross > _GROSS_BOUND + 1e-9, (
        f"expected combined book gross {gross:.4f} to OVERSHOOT the cap bound "
        f"{_GROSS_BOUND:.4f} (cross-symbol cap bypass); book="
        f"{ {k: p.quantity for k, p in book.items()} }"
    )


def _run_two_symbol_race(
    tmp_path: Path,
    *,
    account_lock: str,
    portfolio_caps: str = "1",
    slow_inside_critical_s: float = 1.0,
    tick_lock_timeout_s: str = "10",
    seed: bool = True,
) -> tuple[dict[str, float], dict[tuple[str, str], float]]:
    """Fire AAPL+MSFT concurrently and return (fired_map, final_book_quantities).

    fired_map: asset -> fill_size_pct for the POSITION-MOVING fills on the bus.
    final_book_quantities: (asset_class, symbol) -> quantity from state.db.
    """
    quant_home = tmp_path / "quant_home"
    quant_home.mkdir(parents=True, exist_ok=True)
    executions_path = quant_home / "executions.jsonl"
    executions_path.touch()
    state_db_path = quant_home / "state.db"

    if seed:
        seed_p = _MP_CTX.Process(
            target=_seed_state_db, args=(str(state_db_path), str(executions_path))
        )
        seed_p.start()
        seed_p.join(timeout=60)
        assert seed_p.exitcode == 0, "seed process failed"

    barrier = _MP_CTX.Barrier(2)
    procs: list[mp.Process] = []
    for sym, pid in (("AAPL", "prop_aapl"), ("MSFT", "prop_msft")):
        p = _MP_CTX.Process(
            target=_fire_one,
            kwargs=dict(
                quant_home=str(quant_home),
                executions_path=str(executions_path),
                state_db_path=str(state_db_path),
                proposal_id=pid,
                symbol=sym,
                slow_inside_critical_s=slow_inside_critical_s,
                barrier=barrier,
                account_lock=account_lock,
                portfolio_caps=portfolio_caps,
                tick_lock_timeout_s=tick_lock_timeout_s,
            ),
        )
        procs.append(p)

    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode is not None, "writer hung — the lock must never deadlock"
        assert p.exitcode == 0, f"writer crashed (exitcode={p.exitcode})"

    fired: dict[str, float] = {}
    for ln in executions_path.read_text().splitlines():
        if not ln.strip():
            continue
        rec = json.loads(ln)
        if rec.get("fill_size_pct", 0.0) != 0.0:
            fired[rec["asset"]] = rec["fill_size_pct"]

    from hermes_quant.state import portfolio_state as ps_mod

    ps_mod.DEFAULT_STATE_DB = state_db_path
    with ps_mod._singleton_lock:
        ps_mod._singleton = None
    book = ps_mod.get_portfolio_state().get_positions("paper-default")
    return fired, {k: p.quantity for k, p in book.items()}


@pytest.mark.timeout(120)
def test_green_cross_symbol_cap_held_with_account_lock(tmp_path: Path) -> None:
    """GREEN (cr04 fix): with HERMES_QUANT_ACCOUNT_LOCK=1 the per-account lock serializes
    the two symbols' cap-read -> state.db-write window. The loser BLOCKS (generous
    timeout >> the slow critical section) until the winner's headroom is committed, so
    the final book gross stays AT/UNDER the cash-reserve-implied bound — the cap HOLDS.

    The loser may be clipped, silenced (no headroom), or account-lock-skipped; all three
    keep gross <= bound. What must NOT happen is both firing full size (gross overshoot).
    """
    fired, book = _run_two_symbol_race(
        tmp_path,
        account_lock="1",  # cr04 fix ARMED
        portfolio_caps="1",
        slow_inside_critical_s=1.0,
        tick_lock_timeout_s="20",  # >> 1.0s critical section: loser BLOCKS, never skips on timeout
    )

    gross = sum(abs(q) for q in book.values())
    assert gross <= _GROSS_BOUND + 1e-9, (
        f"cap BREACHED under account lock: final book gross {gross:.4f} > bound "
        f"{_GROSS_BOUND:.4f}; book={book} fired={fired}"
    )
    # Not BOTH at full size — the whole point of serializing is that the loser is
    # clipped/silenced rather than firing full into the same headroom.
    both_full = (
        fired.get("AAPL") == pytest.approx(_FIRE_TARGET)
        and fired.get("MSFT") == pytest.approx(_FIRE_TARGET)
    )
    assert not both_full, (
        f"both symbols fired full ({fired}) — the account lock failed to serialize the "
        f"cross-symbol cap-read"
    )


@pytest.mark.timeout(120)
def test_caps_off_account_lock_on_harmless(tmp_path: Path) -> None:
    """Caps OFF + account lock ON: the lock SERIALIZES the two fires but there is no cap
    to enforce, so both append at full size. Proves the lock is a harmless no-op for the
    cap when caps are off — no crash, no deadlock, no silencing."""
    fired, _book = _run_two_symbol_race(
        tmp_path,
        account_lock="1",
        portfolio_caps="0",  # no cap seam
        slow_inside_critical_s=0.2,
        tick_lock_timeout_s="20",
    )
    assert fired.get("AAPL") == pytest.approx(_FIRE_TARGET)
    assert fired.get("MSFT") == pytest.approx(_FIRE_TARGET)


def test_account_lock_off_byte_identical(tmp_path, monkeypatch) -> None:
    """A single priced fire with HERMES_QUANT_ACCOUNT_LOCK unset produces a record
    field-for-field identical to the same fire with the flag explicitly =0 — proving the
    cr04 wiring is byte-identical on the default-OFF path."""
    from dataclasses import asdict

    from hermes_quant.react.paper import PaperReactor
    from hermes_quant.state import portfolio_state as ps_mod

    def _fire(account_lock_env: str | None) -> dict:
        # Fresh state per fire.
        db = tmp_path / f"state_{account_lock_env or 'unset'}.db"
        ps_mod.DEFAULT_STATE_DB = db
        with ps_mod._singleton_lock:
            ps_mod._singleton = None
        monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.1")
        monkeypatch.setenv("HERMES_QUANT_REFLECTION", "0")
        monkeypatch.setenv("HERMES_QUANT_TICK_LOCK", "1")
        monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)
        monkeypatch.delenv("HERMES_QUANT_ADMISSIBILITY", raising=False)
        if account_lock_env is None:
            monkeypatch.delenv("HERMES_QUANT_ACCOUNT_LOCK", raising=False)
        else:
            monkeypatch.setenv("HERMES_QUANT_ACCOUNT_LOCK", account_lock_env)
        ex = tmp_path / f"ex_{account_lock_env or 'unset'}.jsonl"
        rec = PaperReactor(executions_path=ex).execute(
            _proposal("AAPL", "prop_bi"), fill_size_pct=0.05, play_tag="autonomous"
        )
        d = asdict(rec)
        # asof_execution is wall-clock and differs between calls — normalize it out
        # (it is not part of the cr04 wiring; both paths stamp datetime.now()).
        d.pop("asof_execution", None)
        d.pop("asof_decision", None)
        return d

    unset = _fire(None)
    explicit_off = _fire("0")
    assert unset == explicit_off, (
        f"account-lock OFF path is NOT byte-identical:\n unset={unset}\n  off={explicit_off}"
    )
    # And it really fired (a position-moving record, not a silence).
    assert unset["fill_size_pct"] == pytest.approx(0.05)
    assert not (unset["reactor_metadata"] or {}).get("silenced")
