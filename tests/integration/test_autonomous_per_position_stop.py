"""Integration: the per-position unrealized-loss stop sweep inside the autonomous tick.

The 2026-06-04 ASTS -20.9% loss ran unimpeded because no rail watches a single OPEN
position's unrealized loss. This wires the pure stop monitor into tick() behind the
default-OFF flag HERMES_QUANT_PER_POSITION_STOP and force-exits a breaching position via
the existing _react() chokepoint.

RED-proof: with the flag ON, a position seeded at entry $118.17 marked at $93.44
(-20.9%) MUST be force-exited (fill_size_pct = 0.0 — the ADR-0091 Option E absolute
flat target, NOT the negative delta -held). With the flag OFF the tick is byte-identical
(no stop fire, _react never called for a stop).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from hermes_quant import autonomous as auto
from hermes_quant.watchlist import WatchlistEntry


@pytest.fixture
def isolate_quant_home(tmp_path, monkeypatch):
    qhome = tmp_path / "quant"
    qhome.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("hermes_quant.autonomous.QUANT_HOME", qhome)
    monkeypatch.setattr(
        "hermes_quant.autonomous.KILL_SWITCH_PATH", qhome / "autonomous_kill_switch.json"
    )
    return qhome


@pytest.fixture
def isolate_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    monkeypatch.setattr("hermes_quant.watchlist.get_config_path", lambda: cfg)
    return cfg


def _set_mode_autonomous(cfg_path: Path):
    import yaml

    cfg = {}
    if cfg_path.exists():
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg.setdefault("quant", {}).setdefault("pdr", {})["mode"] = "autonomous"
    cfg["quant"].setdefault("autonomous", {})["max_per_tick_opens"] = 1
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _seed_open_losing_position(qhome: Path):
    """One open ASTS long: entry $118.17, 0.20 NAV-fraction (the real 2026-06-04 trade)."""
    bus = qhome / "executions.jsonl"
    rec = {
        "proposal_id": "prop_seed_ASTS",
        "asset": "ASTS",
        "asset_class": "equity",
        "reactor_name": "paper",
        "account_id": "paper-default",
        "fill_size_pct": 0.20,
        "target_position_pct": 0.20,
        "fill_price": 118.17,
        "decision_price": 118.17,
        "asof_execution": "2026-06-04T15:35:36Z",
        "asof_decision": "2026-06-04T15:35:36+00:00",
        "bar_ts": "2026-06-03T04:00:00+00:00",
        "play_tag": "autonomous",
        "signal_id": "sig_asts",
    }
    bus.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    return bus


class _MarkFrame:
    """Minimal PerceptionFrame stand-in carrying just last_close."""

    def __init__(self, last_close):
        self.last_close = last_close


def test_stop_fires_on_open_losing_position_when_flag_on(
    isolate_config, isolate_quant_home, monkeypatch
):
    _set_mode_autonomous(isolate_config)
    _seed_open_losing_position(isolate_quant_home)
    monkeypatch.setenv("HERMES_QUANT_PER_POSITION_STOP", "1")

    # Mark ASTS at $93.44 = -20.9% from the $118.17 entry -> breaches the 8% stop.
    monkeypatch.setattr(
        "hermes_quant.perception.build_perception_frame_live",
        lambda *a, **k: _MarkFrame(93.44),
    )

    react_calls = []

    def fake_react(advisor_result, entry, fill_size_pct, **kwargs):
        react_calls.append((entry.symbol, fill_size_pct, advisor_result.get("reason")))
        return ("exec_stop_asts", fill_size_pct)

    monkeypatch.setattr("hermes_quant.autonomous._react", fake_react)

    # Empty watchlist so the ONLY action this tick is the stop sweep.
    result = auto.tick(dry_run=False, symbols=[], advisor_recommend=lambda **kw: None)

    # The stop must have force-exited ASTS via _react with fill_size_pct=0.0 (the
    # ADR-0091 Option E absolute flat target, NOT the negative delta -held).
    # Using 0.0 means apply_slippage sees trade_delta = 0.0 - current = -held (correct
    # 1x impact), whereas -held would give trade_delta = -held - held = -2*held (2x impact).
    assert react_calls, "stop sweep must call _react to force-exit the losing position"
    sym, fill, reason = react_calls[0]
    assert sym == "ASTS"
    assert fill == pytest.approx(0.0, abs=1e-9), (
        "stop-loss must pass 0.0 (flat absolute target) not -held (delta) to _react; "
        "passing -held doubles apply_slippage impact: trade_delta = -held - held = -2*held"
    )
    assert reason == "autonomous_per_position_stop"

    # A PER_POSITION_STOP_FIRED decision is surfaced with the loss detail.
    stop_decisions = [d for d in result.decisions if d.gate == "PER_POSITION_STOP_FIRED"]
    assert len(stop_decisions) == 1
    assert stop_decisions[0].details["unrealized_loss_pct"] == pytest.approx(0.2092, abs=1e-3)
    assert stop_decisions[0].execution_id == "exec_stop_asts"


def test_stop_does_not_fire_when_flag_off_byte_identical(
    isolate_config, isolate_quant_home, monkeypatch
):
    _set_mode_autonomous(isolate_config)
    _seed_open_losing_position(isolate_quant_home)
    # Flag explicitly OFF (the production default).
    monkeypatch.delenv("HERMES_QUANT_PER_POSITION_STOP", raising=False)

    frame_calls = []
    monkeypatch.setattr(
        "hermes_quant.perception.build_perception_frame_live",
        lambda *a, **k: frame_calls.append(1) or _MarkFrame(93.44),
    )
    react_calls = []
    monkeypatch.setattr(
        "hermes_quant.autonomous._react",
        lambda *a, **k: react_calls.append(a) or ("x", 0.0),
    )

    result = auto.tick(dry_run=False, symbols=[], advisor_recommend=lambda **kw: None)

    # No stop sweep ran at all: no _react call, no stop decision.
    assert react_calls == [], "flag OFF: the stop sweep must never call _react"
    assert not any(d.gate == "PER_POSITION_STOP_FIRED" for d in result.decisions)


def test_winning_position_not_stopped_when_flag_on(
    isolate_config, isolate_quant_home, monkeypatch
):
    _set_mode_autonomous(isolate_config)
    _seed_open_losing_position(isolate_quant_home)  # entry 118.17
    monkeypatch.setenv("HERMES_QUANT_PER_POSITION_STOP", "1")
    # Mark ABOVE entry -> a winner; must NOT stop.
    monkeypatch.setattr(
        "hermes_quant.perception.build_perception_frame_live",
        lambda *a, **k: _MarkFrame(140.0),
    )
    react_calls = []
    monkeypatch.setattr(
        "hermes_quant.autonomous._react",
        lambda *a, **k: react_calls.append(a) or ("x", 0.0),
    )
    result = auto.tick(dry_run=False, symbols=[], advisor_recommend=lambda **kw: None)
    assert react_calls == [], "a winning position must not be force-exited"
    assert not any(d.gate == "PER_POSITION_STOP_FIRED" for d in result.decisions)


def test_no_mark_holds_when_flag_on(isolate_config, isolate_quant_home, monkeypatch):
    """A None perception frame (data fetch failed) must HOLD — never fabricate an exit."""
    _set_mode_autonomous(isolate_config)
    _seed_open_losing_position(isolate_quant_home)
    monkeypatch.setenv("HERMES_QUANT_PER_POSITION_STOP", "1")
    monkeypatch.setattr(
        "hermes_quant.perception.build_perception_frame_live", lambda *a, **k: None
    )
    react_calls = []
    monkeypatch.setattr(
        "hermes_quant.autonomous._react",
        lambda *a, **k: react_calls.append(a) or ("x", 0.0),
    )
    result = auto.tick(dry_run=False, symbols=[], advisor_recommend=lambda **kw: None)
    assert react_calls == [], "no mark -> HOLD, never force-exit on a missing price"
    assert not any(d.gate == "PER_POSITION_STOP_FIRED" for d in result.decisions)


def test_per_position_stop_slippage_uses_flat_target(
    isolate_config, isolate_quant_home, monkeypatch
):
    """Stop-loss exit must pass 0.0 (flat post-fill absolute target) not -held (delta) to _react.

    RED before fix: the call was ``_react(..., -float(held), ...)`` i.e. target_pct=-0.20 for a
    20% long.  apply_slippage then computes trade_delta = target_pct - current_position_pct =
    -0.20 - 0.20 = -0.40, doubling the market-impact term (20 bps vs the correct 10 bps).

    GREEN after fix: ``_react(..., 0.0, ...)`` i.e. target_pct=0.0 (flat absolute target per
    ADR-0091 Option E).  trade_delta = 0.0 - 0.20 = -0.20 → correct 10 bps impact.

    This test captures the target_pct that reaches apply_slippage by patching it in the
    paper reactor module (the reactor the stop sweep reaches when all routing flags are OFF).
    """
    _set_mode_autonomous(isolate_config)
    _seed_open_losing_position(isolate_quant_home)
    monkeypatch.setenv("HERMES_QUANT_PER_POSITION_STOP", "1")
    # Ensure routing flags are OFF so PaperReactor is selected (byte-identical default).
    monkeypatch.delenv("HERMES_QUANT_DETERMINISTIC_EQUITY", raising=False)
    monkeypatch.delenv("HERMES_QUANT_ALPACA_PAPER", raising=False)

    monkeypatch.setattr(
        "hermes_quant.perception.build_perception_frame_live",
        lambda *a, **k: _MarkFrame(93.44),
    )

    captured_slippage_kwargs: list[dict] = []

    from hermes_quant.react import slippage_model as _sm
    _original_apply_slippage = _sm.apply_slippage

    def capturing_apply_slippage(**kwargs):
        captured_slippage_kwargs.append(dict(kwargs))
        return _original_apply_slippage(**kwargs)

    # apply_slippage is imported inside the execute() body via a local import, so patch
    # it at the source module (hermes_quant.react.slippage_model) where the name lives.
    monkeypatch.setattr("hermes_quant.react.slippage_model.apply_slippage", capturing_apply_slippage)

    auto.tick(dry_run=False, symbols=[], advisor_recommend=lambda **kw: None)

    assert captured_slippage_kwargs, (
        "apply_slippage must be called by the stop-loss reactor path"
    )
    target = captured_slippage_kwargs[0]["target_pct"]
    assert target == pytest.approx(0.0, abs=1e-9), (
        f"stop-loss must pass target_pct=0.0 (flat absolute target, ADR-0091 Option E) "
        f"not target_pct=-held ({target!r}); passing -held doubles slippage impact: "
        f"trade_delta = -held - held = -2*held instead of 0.0 - held = -held"
    )
