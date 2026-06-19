"""ar80 — the autonomous tick must charge the REACTOR'S REALIZED post-clip fill into the
in-memory running headroom, not the pre-reactor-clip REQUESTED size.

Found by the parallel find->fix workflow (wf_d7d2cc27). _react fires the routed reactor, which
(PaperReactor._portfolio_cap_clip, ADR-0087) independently re-reads the PERSISTED book and may apply a
SECOND, tighter clip — so the realized record.fill_size_pct can be SMALLER than the requested size.
The fire loop then mutated portfolio_state.positions[symbol] = effective_size (the REQUESTED size),
over-charging the in-memory running headroom and spuriously shrinking/silencing later picks in the same
tick. Fix: _react returns (pid, realized_fill_size_pct); the caller charges the realized size (falling
back conservatively to the requested size when realized is missing/non-finite/larger).

This test isolates the CHARGE logic by stubbing _react to return a realized size smaller than the
requested, then asserts the in-memory book reflects the realized (not requested) size — independent of
the real reactor's cap arithmetic (which a prior stale-base test got wrong against the current branch).
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from hermes_quant import autonomous as auto
from hermes_quant.watchlist import WatchlistEntry


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    qhome = tmp_path / "quant"
    qhome.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("hermes_quant.autonomous.QUANT_HOME", qhome)
    monkeypatch.setattr("hermes_quant.autonomous.KILL_SWITCH_PATH", qhome / "ks.json")
    cfg = tmp_path / "config.yaml"
    monkeypatch.setattr("hermes_quant.watchlist.get_config_path", lambda: cfg)
    monkeypatch.setattr(auto, "_read_pdr_mode", lambda: "autonomous")
    monkeypatch.setattr(auto, "_read_safety_rails", lambda: {
        "max_per_tick_opens": 5, "max_concurrent_positions": 10, "kill_switch_pct": 0.10,
        "log_silences": False, "allow_live": False, "paper_zero_costs": False,
        "require_stop_loss": False,
    })
    # The in-memory headroom charge only runs when the portfolio-cap gate is ON
    # (that's the path tick() builds portfolio_state for).
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")
    return qhome


def _advisor(kelly):
    return {
        "as_of": "2026-05-13T20:00:00Z", "decision_price": 100.0, "signal_id": "s",
        "aggregated_signal": {"confidence": 0.85, "direction": 1, "magnitude": 0.05},
        "risk_gate": {"pass": True, "kelly_fraction": kelly, "reason": "ok"},
        "analyst_views": [{"analyst": f"A{i}", "metadata": {"atr_relative": 0.05}} for i in range(2)],
        "lessons": [],
    }


def _fresh_state():
    # A real (empty) PortfolioState; the tick mutates its .positions dict in place.
    from hermes_quant.risk.portfolio_normalize import PortfolioState
    return PortfolioState(positions={})


def test_ar80_charges_realized_not_requested(isolate, monkeypatch):
    """_react returns a REALIZED size (0.02) smaller than the requested (0.20). The in-memory
    portfolio_state.positions must hold 0.02 (realized), not 0.20 (requested)."""
    ps = _fresh_state()
    monkeypatch.setattr(auto, "_react", lambda *a, **k: ("exec_NVDA", 0.02))
    import hermes_quant.portfolio.state as pstate
    monkeypatch.setattr(pstate, "reconstruct_portfolio_state", lambda *a, **k: ps, raising=False)

    result = auto.tick(
        dry_run=False,
        symbols=[WatchlistEntry("NVDA", "equity", "1d")],
        advisor_recommend=lambda **kw: _advisor(0.20),
    )
    fired = [d for d in result.decisions if d.execution_id]
    assert fired, f"NVDA did not fire: {[d.to_dict() for d in result.decisions]}"
    assert ps.positions.get("NVDA") == pytest.approx(0.02), (
        f"ar80: in-memory headroom charged {ps.positions.get('NVDA')} (requested 0.20), "
        "must charge the realized 0.02"
    )


def test_ar80_falls_back_to_requested_when_realized_missing(isolate, monkeypatch):
    """If _react returns realized=None (no usable fill_size_pct), the tick charges the
    REQUESTED size — conservative: never UNDER-charge headroom."""
    ps = _fresh_state()
    monkeypatch.setattr(auto, "_react", lambda *a, **k: ("exec_NVDA", None))
    import hermes_quant.portfolio.state as pstate
    monkeypatch.setattr(pstate, "reconstruct_portfolio_state", lambda *a, **k: ps, raising=False)

    result = auto.tick(
        dry_run=False, symbols=[WatchlistEntry("NVDA", "equity", "1d")],
        advisor_recommend=lambda **kw: _advisor(0.20),
    )
    assert [d for d in result.decisions if d.execution_id]
    assert ps.positions.get("NVDA") == pytest.approx(0.20)  # fell back to requested
