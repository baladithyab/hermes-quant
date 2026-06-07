"""Tests for the stop-loss backstop in the autonomous tick (PR2-B, defense-in-
depth for the June-4 ASTS stopless-loss; deep-review 2026-06-07).

The trader root-cause fix (agents/trader.py default_stop_pct) should mean a
proposal rarely reaches the tick stopless. This backstop is the LAST line: when
require_stop_loss is enabled and a FIRE still carries stop_loss=None above the
allowed band, size it down (default) or silence it. Opt-in (default-off).
"""
from __future__ import annotations

import pytest

from hermes_quant import autonomous as auto
from hermes_quant.watchlist import WatchlistEntry


def _firing_advisor_result(stop_loss, kelly=0.20, conf=0.90):
    """An advisor_result shaped to pass the silence-bias gate and FIRE."""
    return {
        "aggregated_signal": {
            "asset": "ASTS", "direction": 1, "confidence": conf,
            "magnitude": 0.05, "timeframe": "1d", "n_components": 3,
            "metadata": {"id": "sig1"},
        },
        "risk_gate": {"pass": True, "kelly_fraction": kelly,
                      "reason": "test_fire", "recommended_action": "long"},
        "analyst_views": [
            {"analyst": "a", "direction": 1, "confidence": 0.8},
            {"analyst": "b", "direction": 1, "confidence": 0.7},
            {"analyst": "c", "direction": 1, "confidence": 0.75},
        ],
        "trader_proposal": {"action": "BUY", "size_fraction": kelly,
                            "stop_loss": stop_loss, "entry_price": 100.0},
        "lessons": [],
        "decision_price": 100.0, "bar_ts": "2026-06-05T04:00:00Z",
        "as_of": "2026-06-05T04:00:00Z", "caveats": [],
    }


def _rails(**ov):
    base = {
        "max_per_tick_opens": 5, "max_concurrent_positions": 50,
        "kill_switch_pct": 0.10, "log_silences": False, "allow_live": False,
        "paper_zero_costs": False,
        "require_stop_loss": True, "stopless_max_size_pct": 0.05,
        "stopless_mode": "size_down",
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


def _run(monkeypatch, stop_loss, **rail_ov):
    rails = _rails(**rail_ov)
    _common_monkeypatch(monkeypatch, rails)
    return auto.tick(
        dry_run=True,
        symbols=[WatchlistEntry("ASTS", "equity", "1d")],
        advisor_recommend=lambda **kw: _firing_advisor_result(stop_loss),
    )


def test_stopless_large_fire_is_sized_down(monkeypatch):
    res = _run(monkeypatch, stop_loss=None, stopless_mode="size_down")
    fire = [d for d in res.decisions if d.action]
    assert fire, "expected a FIRE decision"
    # 0.20 kelly with no stop -> capped to stopless_max_size_pct=0.05
    assert fire[0].action["target_position_pct"] == pytest.approx(0.05)
    assert fire[0].action["stopless_backstop"]["kelly_before"] == pytest.approx(0.20)


def test_stopless_large_fire_is_silenced_when_configured(monkeypatch):
    res = _run(monkeypatch, stop_loss=None, stopless_mode="silence")
    silenced = [d for d in res.decisions if d.gate == "SILENCE_NO_STOP_LOSS"]
    assert silenced, "expected SILENCE_NO_STOP_LOSS"
    assert res.fires == 0


def test_stop_present_fires_at_full_size(monkeypatch):
    # A real stop -> backstop does NOT engage, full kelly preserved.
    res = _run(monkeypatch, stop_loss=92.0)
    fire = [d for d in res.decisions if d.action]
    assert fire
    assert fire[0].action["target_position_pct"] == pytest.approx(0.20)
    assert "stopless_backstop" not in fire[0].action


def test_stopless_small_fire_not_capped(monkeypatch):
    # Stopless but UNDER the band -> allowed (a small stopless position is OK).
    rails = _rails(stopless_max_size_pct=0.05)
    _common_monkeypatch(monkeypatch, rails)
    res = auto.tick(
        dry_run=True, symbols=[WatchlistEntry("ASTS", "equity", "1d")],
        advisor_recommend=lambda **kw: _firing_advisor_result(None, kelly=0.04),
    )
    fire = [d for d in res.decisions if d.action]
    assert fire
    assert fire[0].action["target_position_pct"] == pytest.approx(0.04)
    assert "stopless_backstop" not in fire[0].action


def test_backstop_off_by_default_is_byte_identical(monkeypatch):
    # require_stop_loss=False -> stopless large fire goes through untouched.
    res = _run(monkeypatch, stop_loss=None, require_stop_loss=False)
    fire = [d for d in res.decisions if d.action]
    assert fire
    assert fire[0].action["target_position_pct"] == pytest.approx(0.20)
    assert "stopless_backstop" not in fire[0].action
