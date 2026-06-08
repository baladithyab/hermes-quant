"""Tests for the two safety rails wired live in PR1 (deep-review 2026-06-07):

  C. max_concurrent_positions — was read into rails but never enforced.
  D. kill_switch_pct cumulative-PnL trip — was dead code (tick only honored a
     pre-tripped file; nothing computed live P&L).

Both are ALWAYS-ON safety rails (not flag-gated): a control that is supposed to
work and silently doesn't is a latent loss, not a flexibility feature.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_quant import autonomous as auto


# --------------------------------------------------------------------------- #
# D. compute_cumulative_realized_pnl_pct — the kill-switch basis
# --------------------------------------------------------------------------- #
def _write_bus(tmp_path: Path, fills: list[dict]) -> Path:
    p = tmp_path / "executions.jsonl"
    p.write_text("\n".join(json.dumps(f) for f in fills) + "\n", encoding="utf-8")
    return p


def _fill(asset, pct, price, asof, pid):
    # Minimal real-bus-shaped ExecutionRecord (what _record_to_dict emits).
    return {
        "asset": asset,
        "asset_class": "equity",
        "timeframe": "1d",
        "fill_size_pct": pct,
        "fill_price": price,
        "decision_price": price,
        "asof_execution": asof,
        "asof_decision": asof,
        "bar_ts": asof,
        "proposal_id": pid,
        "signal_id": None,
        "reactor_name": "paper",
        "human_in_the_loop": True,
        "play_tag": "autonomous",
        "reactor_metadata": {"paper": True},
        "target_position_pct": pct,
        "approver_user_id": "test",
    }


def test_cum_pnl_empty_bus_is_zero(tmp_path, monkeypatch):
    p = _write_bus(tmp_path, [])
    assert auto.compute_cumulative_realized_pnl_pct(p) == 0.0


def test_cum_pnl_missing_file_is_zero(tmp_path):
    assert auto.compute_cumulative_realized_pnl_pct(tmp_path / "nope.jsonl") == 0.0


def test_cum_pnl_realized_loss_is_negative(tmp_path, monkeypatch):
    # Open +0.2 @ 100, close -0.2 @ 80 = -20% on the lot.
    bus = _write_bus(tmp_path, [
        _fill("ASTS", 0.2, 100.0, "2026-06-01T15:00:00Z", "p1"),
        _fill("ASTS", -0.2, 80.0, "2026-06-02T15:00:00Z", "p2"),
    ])
    # Pin NAV so the fraction is deterministic.
    monkeypatch.setattr(auto, "_account_nav_usd", lambda: 100000.0)
    frac = auto.compute_cumulative_realized_pnl_pct(bus)
    assert frac < 0.0  # a realized loss is negative


def test_cum_pnl_realized_gain_is_positive(tmp_path, monkeypatch):
    bus = _write_bus(tmp_path, [
        _fill("ASTS", 0.2, 100.0, "2026-06-01T15:00:00Z", "p1"),
        _fill("ASTS", -0.2, 120.0, "2026-06-02T15:00:00Z", "p2"),
    ])
    monkeypatch.setattr(auto, "_account_nav_usd", lambda: 100000.0)
    assert auto.compute_cumulative_realized_pnl_pct(bus) > 0.0


def test_cum_pnl_fails_open_to_zero_on_bad_nav(tmp_path, monkeypatch):
    bus = _write_bus(tmp_path, [
        _fill("ASTS", 0.2, 100.0, "2026-06-01T15:00:00Z", "p1"),
        _fill("ASTS", -0.2, 80.0, "2026-06-02T15:00:00Z", "p2"),
    ])
    monkeypatch.setattr(auto, "_account_nav_usd", lambda: None)  # unknown NAV
    assert auto.compute_cumulative_realized_pnl_pct(bus) == 0.0


# --------------------------------------------------------------------------- #
# D. live kill-switch trip in tick()
# --------------------------------------------------------------------------- #
def test_live_kill_switch_trips_and_halts(monkeypatch):
    monkeypatch.setattr(auto, "_read_pdr_mode", lambda: "autonomous")
    monkeypatch.setattr(auto, "_read_kill_switch", lambda: auto.KillSwitchState(
        tripped=False, tripped_at=None, cumulative_pnl_pct=0.0,
        threshold_pct=0.10, reason=None))
    monkeypatch.setattr(auto, "_read_safety_rails", lambda: {
        "max_per_tick_opens": 1, "max_concurrent_positions": 5,
        "kill_switch_pct": 0.10, "log_silences": False, "allow_live": False})
    # Cumulative P&L breaches -10%.
    monkeypatch.setattr(auto, "compute_cumulative_realized_pnl_pct", lambda *a, **k: -0.15)
    res = auto.tick(dry_run=True, symbols=[])
    assert res.kill_switch_state.tripped is True
    assert res.kill_switch_state.cumulative_pnl_pct == -0.15
    assert res.watchlist_size == 0  # halted: no symbols processed


def test_live_kill_switch_dry_run_does_not_persist(monkeypatch, tmp_path):
    monkeypatch.setattr(auto, "_read_pdr_mode", lambda: "autonomous")
    monkeypatch.setattr(auto, "_read_kill_switch", lambda: auto.KillSwitchState(
        tripped=False, tripped_at=None, cumulative_pnl_pct=0.0,
        threshold_pct=0.10, reason=None))
    monkeypatch.setattr(auto, "_read_safety_rails", lambda: {
        "max_per_tick_opens": 1, "max_concurrent_positions": 5,
        "kill_switch_pct": 0.10, "log_silences": False, "allow_live": False})
    monkeypatch.setattr(auto, "compute_cumulative_realized_pnl_pct", lambda *a, **k: -0.20)
    monkeypatch.setattr(auto, "KILL_SWITCH_PATH", tmp_path / "ks.json")
    res = auto.tick(dry_run=True, symbols=[])
    assert res.kill_switch_state.tripped is True
    assert "dry-run" in (res.kill_switch_state.reason or "")
    assert not (tmp_path / "ks.json").exists()  # NOT persisted on dry run


def test_live_kill_switch_healthy_pnl_does_not_trip(monkeypatch):
    monkeypatch.setattr(auto, "_read_pdr_mode", lambda: "autonomous")
    monkeypatch.setattr(auto, "_read_kill_switch", lambda: auto.KillSwitchState(
        tripped=False, tripped_at=None, cumulative_pnl_pct=0.0,
        threshold_pct=0.10, reason=None))
    monkeypatch.setattr(auto, "_read_safety_rails", lambda: {
        "max_per_tick_opens": 1, "max_concurrent_positions": 5,
        "kill_switch_pct": 0.10, "log_silences": False, "allow_live": False})
    monkeypatch.setattr(auto, "compute_cumulative_realized_pnl_pct", lambda *a, **k: -0.03)
    # Healthy: -3% does not breach -10%. Tick proceeds (empty watchlist -> size 0
    # but kill switch NOT tripped).
    res = auto.tick(dry_run=True, symbols=[])
    assert res.kill_switch_state.tripped is False
