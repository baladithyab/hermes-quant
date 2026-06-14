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

This is a TEST-ONLY disposition: the fix (an account-level lock that serializes ALL
symbols on an account through the cap-read) touches the LIVE fire path, so it is
DEFERRED / flag-gated to its own parity-tested increment. This test RED-documents the
bypass; it does NOT change the reactor.

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
) -> None:
    """Child process: fire ONE PaperReactor.execute for `symbol` with caps ON."""
    os.environ["HERMES_QUANT_HOME"] = quant_home
    os.environ["HERMES_QUANT_TICK_LOCK"] = "1"  # lock ON — proves it does NOT help here
    os.environ["HERMES_QUANT_TICK_LOCK_TIMEOUT_S"] = "5"
    os.environ["HERMES_QUANT_PAPER_SLIPPAGE_MODEL"] = "v0.1"
    os.environ["HERMES_QUANT_REFLECTION"] = "0"
    os.environ["HERMES_QUANT_PORTFOLIO_CAPS"] = "1"  # cap seam ARMED
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
