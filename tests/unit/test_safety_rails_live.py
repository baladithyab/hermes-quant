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


def test_cum_pnl_bad_nav_cold_start_is_zero(tmp_path, monkeypatch):
    # ar20: NAV unreadable with a realized loss BUT no last-known sidecar (cold start)
    # falls back to the 0.0 floor — we cannot fabricate a loss fraction with no NAV and
    # no prior value. (With a last-known present it carries forward instead — covered by
    # test_killswitch_rail_failopen_ar19_ar21.py::test_ar20_nav_none_with_loss_carries_*.)
    monkeypatch.setattr(auto, "QUANT_HOME", tmp_path, raising=False)
    monkeypatch.setattr(auto, "_LAST_KNOWN_CUM_PNL_PATH",
                        tmp_path / "no_last_known.json", raising=False)
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


# ---------------------------------------------------------------------------
# ar08 — the LIVE kill-switch threshold (kill_switch_pct) is operator-editable
# YAML; a NaN/inf/negative value must NOT silently disable the ADR-0016 §D9 rail.
# (`nan > 0` is False, so the pre-fix `_ks_threshold > 0` guard short-circuited
# and a -50% catastrophic loss did NOT trip — a fail-OPEN with no log/audit.)
# ---------------------------------------------------------------------------


def _rails_with_threshold(val):
    return lambda: {
        "max_per_tick_opens": 1,
        "max_concurrent_positions": 5,
        "kill_switch_pct": val,
        "log_silences": False,
        "allow_live": False,
    }


def test_nan_kill_switch_threshold_still_trips_on_catastrophic_loss(monkeypatch):
    """A NaN kill_switch_pct must fall back to the 0.10 floor and STILL trip on a -50% loss."""
    monkeypatch.setattr(auto, "_read_pdr_mode", lambda: "autonomous")
    monkeypatch.setattr(auto, "_read_kill_switch", lambda: auto.KillSwitchState(
        tripped=False, tripped_at=None, cumulative_pnl_pct=0.0, threshold_pct=0.10, reason=None))
    monkeypatch.setattr(auto, "_read_safety_rails", _rails_with_threshold(float("nan")))
    monkeypatch.setattr(auto, "compute_cumulative_realized_pnl_pct", lambda *a, **k: -0.50)
    res = auto.tick(dry_run=True, symbols=[])
    assert res.kill_switch_state.tripped is True, (
        "a NaN kill_switch_pct must fall back to the 0.10 floor and trip on a -50% loss, "
        "NOT silently disable the rail (nan > 0 is False)"
    )


def test_negative_kill_switch_threshold_still_trips(monkeypatch):
    monkeypatch.setattr(auto, "_read_pdr_mode", lambda: "autonomous")
    monkeypatch.setattr(auto, "_read_kill_switch", lambda: auto.KillSwitchState(
        tripped=False, tripped_at=None, cumulative_pnl_pct=0.0, threshold_pct=0.10, reason=None))
    monkeypatch.setattr(auto, "_read_safety_rails", _rails_with_threshold(-0.10))
    monkeypatch.setattr(auto, "compute_cumulative_realized_pnl_pct", lambda *a, **k: -0.50)
    res = auto.tick(dry_run=True, symbols=[])
    assert res.kill_switch_state.tripped is True, "a negative threshold must fall back to 0.10, not disable"


def test_inf_kill_switch_threshold_still_trips(monkeypatch):
    monkeypatch.setattr(auto, "_read_pdr_mode", lambda: "autonomous")
    monkeypatch.setattr(auto, "_read_kill_switch", lambda: auto.KillSwitchState(
        tripped=False, tripped_at=None, cumulative_pnl_pct=0.0, threshold_pct=0.10, reason=None))
    monkeypatch.setattr(auto, "_read_safety_rails", _rails_with_threshold(float("inf")))
    monkeypatch.setattr(auto, "compute_cumulative_realized_pnl_pct", lambda *a, **k: -0.50)
    res = auto.tick(dry_run=True, symbols=[])
    assert res.kill_switch_state.tripped is True, "an inf threshold must fall back to 0.10, not disable"


def test_finite_positive_threshold_is_byte_identical(monkeypatch):
    """The only legal configured shape (finite positive) is unchanged: a healthy -5% loss under a
    0.10 threshold does NOT trip (byte-identical to pre-fix)."""
    monkeypatch.setattr(auto, "_read_pdr_mode", lambda: "autonomous")
    monkeypatch.setattr(auto, "_read_kill_switch", lambda: auto.KillSwitchState(
        tripped=False, tripped_at=None, cumulative_pnl_pct=0.0, threshold_pct=0.10, reason=None))
    monkeypatch.setattr(auto, "_read_safety_rails", _rails_with_threshold(0.10))
    monkeypatch.setattr(auto, "compute_cumulative_realized_pnl_pct", lambda *a, **k: -0.05)
    res = auto.tick(dry_run=True, symbols=[])
    assert res.kill_switch_state.tripped is False, "a -5% loss under a 0.10 floor must NOT trip"
