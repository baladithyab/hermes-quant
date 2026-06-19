"""cs19 — the ADR-0016 §D9 max_concurrent_positions HARD rail must fail CLOSED on a
read EXCEPTION, not fail open.

THE BUG (verified hermes_quant/autonomous.py):
    The D9 rail counts the open book at tick start (:514-525) inside a try; on ANY
    exception the bare `except` at :526-530 swallows it and LEAVES the rail state at
    the empty defaults (open_positions_at_tick_start=0, open_symbols_at_tick_start=set()).
    The enforcement at :650-652 then computes is_new_symbol=True for EVERY symbol (empty
    set) and projected_concurrent = 0 + fires_this_tick, so the first `max_concurrent`
    brand-new symbols pass the HARD rail unconditionally — exactly when reconstruction
    is failing (when we are MOST blind to the real book).

reconstruct_portfolio_state is ALREADY internally fail-soft: a missing bus
(portfolio/state.py:108-109) and an OSError (:117-118) return an EMPTY PortfolioState
WITHOUT raising. So the bare except catches only GENUINELY-UNEXPECTED errors — the
population where treating the book as readable-and-empty is unjustified and fail-closed
on the HARD rail is warranted.

THE FIX (cs19): on a read EXCEPTION, treat the book as AT-CAP for any NEW-looking
symbol (silence NEW opens this tick) via a `rail_read_failed` sentinel wired into the
D9 check. PRESERVE: a SUCCESSFUL empty read still admits (not an error); a SUCCESSFUL
populated read is byte-identical; existing-symbol management (a fire on a held symbol)
is never blocked by a read failure.
"""

from __future__ import annotations

from hermes_quant import autonomous as auto
from hermes_quant.portfolio.state import PortfolioState
from hermes_quant.watchlist import WatchlistEntry


# --------------------------------------------------------------------------- #
# Harness — mirrors tests/unit/test_stop_loss_backstop.py + test_safety_rails_live.py
# --------------------------------------------------------------------------- #
def _firing_advisor_result(symbol, kelly=0.10, conf=0.90):
    """An advisor_result shaped to pass the silence-bias gate and FIRE.

    A real stop_loss is set so the (opt-in) stopless backstop never engages and the
    only rail under test is the D9 concurrent-positions rail.
    """
    return {
        "aggregated_signal": {
            "asset": symbol, "direction": 1, "confidence": conf,
            "magnitude": 0.05, "timeframe": "1d", "n_components": 3,
            "metadata": {"id": f"sig-{symbol}"},
        },
        "risk_gate": {"pass": True, "kelly_fraction": kelly,
                      "reason": "test_fire", "recommended_action": "long"},
        "analyst_views": [
            {"analyst": "a", "direction": 1, "confidence": 0.8},
            {"analyst": "b", "direction": 1, "confidence": 0.7},
            {"analyst": "c", "direction": 1, "confidence": 0.75},
        ],
        "trader_proposal": {"action": "BUY", "size_fraction": kelly,
                            "stop_loss": 92.0, "entry_price": 100.0},
        "lessons": [],
        "decision_price": 100.0, "bar_ts": "2026-06-05T04:00:00Z",
        "as_of": "2026-06-05T04:00:00Z", "caveats": [],
    }


def _rails(**ov):
    base = {
        "max_per_tick_opens": 50, "max_concurrent_positions": 1,
        "kill_switch_pct": 0.10, "log_silences": False, "allow_live": False,
        "paper_zero_costs": False,
    }
    base.update(ov)
    return base


def _common_monkeypatch(monkeypatch, rails):
    monkeypatch.setattr(auto, "_read_pdr_mode", lambda: "autonomous")
    monkeypatch.setattr(auto, "_read_kill_switch", lambda: auto.KillSwitchState(
        tripped=False, tripped_at=None, cumulative_pnl_pct=0.0,
        threshold_pct=0.10, reason=None))
    monkeypatch.setattr(auto, "compute_cumulative_realized_pnl_pct", lambda *a, **k: 0.0)
    monkeypatch.setattr(auto, "_read_safety_rails", lambda: rails)


def _patch_reconstruct_raises(monkeypatch):
    """Make the D9 rail's reconstruct call raise a GENUINELY-UNEXPECTED error.

    The rail imports reconstruct_portfolio_state lazily INSIDE tick
    (autonomous.py:517 `from hermes_quant.portfolio.state import
    reconstruct_portfolio_state as _recon`), so we patch the source symbol on the
    state module. We raise RuntimeError (NOT OSError) precisely because the reader
    is already fail-soft on OSError/missing-path — this models the unexpected-fault
    population the bare except actually catches.
    """
    def _boom(*a, **k):
        raise RuntimeError("simulated unexpected reconstruct failure")

    monkeypatch.setattr(
        "hermes_quant.portfolio.state.reconstruct_portfolio_state", _boom
    )


# --------------------------------------------------------------------------- #
# RED -> GREEN: a read EXCEPTION must fail CLOSED (silence NEW symbols)
# --------------------------------------------------------------------------- #
def test_read_exception_fails_closed_silences_new_symbol(monkeypatch):
    """On a reconstruct EXCEPTION the HARD D9 rail must NOT admit a new symbol.

    max_concurrent_positions=1. With a read failure the rail cannot see the real
    book; the conservative direction is to treat it as AT-CAP and SILENCE the new
    symbol. RED on current source (the new symbol FIRES because count starts at 0
    / empty set). GREEN after the fix (SILENCE_CONCURRENT_CAP).
    """
    _common_monkeypatch(monkeypatch, _rails(max_concurrent_positions=1))
    _patch_reconstruct_raises(monkeypatch)

    res = auto.tick(
        dry_run=True,
        symbols=[WatchlistEntry("ZZZA", "equity", "1d")],
        advisor_recommend=lambda **kw: _firing_advisor_result(kw.get("symbol", "ZZZA")),
    )

    fires = [d for d in res.decisions if d.action]
    silences = [d for d in res.decisions if d.gate == "SILENCE_CONCURRENT_CAP"]
    assert not fires, (
        "fail-CLOSED: a NEW symbol must NOT fire when the D9 rail's book read "
        f"raised (we are blind to the real book). got fires={[d.symbol for d in fires]}"
    )
    assert silences, (
        "expected SILENCE_CONCURRENT_CAP on the read-failure (fail-closed) path"
    )
    assert res.fires == 0


def test_read_exception_silences_all_new_symbols_not_just_first(monkeypatch):
    """Two NEW symbols, read failure -> BOTH silenced (book is unknown == at cap)."""
    _common_monkeypatch(monkeypatch, _rails(max_concurrent_positions=1, max_per_tick_opens=50))
    _patch_reconstruct_raises(monkeypatch)

    res = auto.tick(
        dry_run=True,
        symbols=[
            WatchlistEntry("ZZZA", "equity", "1d"),
            WatchlistEntry("ZZZB", "equity", "1d"),
        ],
        advisor_recommend=lambda **kw: _firing_advisor_result(kw.get("symbol", "ZZZA")),
    )
    assert res.fires == 0, "fail-closed: NO new symbol may open on a rail read failure"
    silenced = {d.symbol for d in res.decisions if d.gate == "SILENCE_CONCURRENT_CAP"}
    assert silenced == {"ZZZA", "ZZZB"}


# --------------------------------------------------------------------------- #
# PRESERVE: a SUCCESSFUL empty read still ADMITS (not an error)
# --------------------------------------------------------------------------- #
def test_successful_empty_read_still_admits(monkeypatch):
    """A SUCCESSFUL read returning an EMPTY book (no open positions) is NOT an error
    and must still admit a new symbol. Only the EXCEPTION path fails closed."""
    _common_monkeypatch(monkeypatch, _rails(max_concurrent_positions=1))
    monkeypatch.setattr(
        "hermes_quant.portfolio.state.reconstruct_portfolio_state",
        lambda *a, **k: PortfolioState(positions={}),
    )

    res = auto.tick(
        dry_run=True,
        symbols=[WatchlistEntry("ZZZA", "equity", "1d")],
        advisor_recommend=lambda **kw: _firing_advisor_result(kw.get("symbol", "ZZZA")),
    )
    fires = [d for d in res.decisions if d.action]
    assert fires and fires[0].symbol == "ZZZA", (
        "a SUCCESSFUL empty read (genuinely no positions) must still admit a new "
        "symbol — fail-closed applies ONLY to the read-EXCEPTION path"
    )
    assert res.fires == 1


# --------------------------------------------------------------------------- #
# PRESERVE: existing-symbol management is NOT blocked, and a populated successful
# read is byte-identical to today.
# --------------------------------------------------------------------------- #
def test_successful_populated_read_byte_identical_held_symbol_adjusts(monkeypatch):
    """A SUCCESSFUL read showing the book already AT cap with one HELD symbol:
    the held symbol may still FIRE (adjustment, not a new slot), while a genuinely
    NEW symbol is silenced by the normal cap. This is the existing behavior and the
    fix must not change it (rail_read_failed stays False on a successful read)."""
    _common_monkeypatch(monkeypatch, _rails(max_concurrent_positions=1))
    monkeypatch.setattr(
        "hermes_quant.portfolio.state.reconstruct_portfolio_state",
        lambda *a, **k: PortfolioState(positions={"HELD": 0.20}),
    )

    res = auto.tick(
        dry_run=True,
        symbols=[
            WatchlistEntry("HELD", "equity", "1d"),  # already held -> adjustment, exempt
            WatchlistEntry("ZZZNEW", "equity", "1d"),  # new -> cap-bound (book at 1/1)
        ],
        advisor_recommend=lambda **kw: _firing_advisor_result(kw.get("symbol", "HELD")),
    )
    fired = {d.symbol for d in res.decisions if d.action}
    silenced_cap = {d.symbol for d in res.decisions if d.gate == "SILENCE_CONCURRENT_CAP"}
    assert "HELD" in fired, (
        "existing-symbol management (a fire on a held symbol) must NOT be blocked "
        "by the concurrent-positions rail"
    )
    assert silenced_cap == {"ZZZNEW"}, (
        "a genuinely NEW symbol must be cap-silenced when the (successfully-read) "
        "book is already at max_concurrent_positions"
    )
