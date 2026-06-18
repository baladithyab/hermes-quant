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


def test_per_position_stop_sweep_react_error_is_not_silent(monkeypatch):
    """A ValueError from _react inside the stop sweep must produce gate=PER_POSITION_STOP_ERROR,
    not silently omit the symbol (fail-open on a safety rail).

    RED (before fix): the except block only did `continue`; result.errors == 0 and no
    decision was recorded -> the error was invisible (fail-open on the stop rail).
    GREEN (after fix): result.errors == 1 and the decision carries gate=PER_POSITION_STOP_ERROR.

    Scenario: paper_zero_costs=True + HERMES_QUANT_DETERMINISTIC_EQUITY=1 causes _react to
    raise ValueError("paper_zero_costs is set but reactor is not paper").
    """
    import hermes_quant.autonomous as auto_mod
    import hermes_quant.perception as perc_mod
    import hermes_quant.risk.per_position_stop as pps_mod
    from hermes_quant.autonomous import TickResult, _run_per_position_stop_sweep
    from hermes_quant.risk.per_position_stop import StopDecision

    # Force _react to raise the exact ValueError the verifier confirmed.
    def _bad_react(*args, **kwargs):
        raise ValueError("paper_zero_costs is set but reactor is not paper")

    monkeypatch.setattr(auto_mod, "_react", _bad_react)

    # Force evaluate_stop to always say should_stop=True so the breach branch is reached.
    monkeypatch.setattr(
        pps_mod,
        "evaluate_stop",
        lambda **kw: StopDecision(symbol=kw.get("symbol", "UNKNOWN"), should_stop=True, loss_pct=-0.15, reason="test"),
    )

    # Provide a valid mark price so the sweep reaches the _react call.
    class _FakeFrame:
        last_close = 100.0

    monkeypatch.setattr(
        perc_mod, "build_perception_frame_live", lambda *a, **kw: _FakeFrame()
    )

    # Provide a valid entry price.
    monkeypatch.setattr(auto_mod, "_establishing_avg_entry_price", lambda sym: 110.0)

    result = TickResult(asof="2026-06-17T00:00:00Z", mode="autonomous", dry_run=False, watchlist_size=0)
    stopped = _run_per_position_stop_sweep(
        open_book={"ASTS": 0.05},
        stop_pct=0.08,
        paper_zero_costs=True,
        result=result,
    )

    # The position was NOT stopped (reactor raised, so no fill was executed) — correct.
    assert "ASTS" not in stopped, "a failed _react must not mark the symbol as stopped"

    # GREEN: result.errors must be 1 and the decision gate must be PER_POSITION_STOP_ERROR.
    # RED (before fix): result.errors == 0 and result.decisions == [] (silent fail-open).
    assert result.errors == 1, (
        f"expected 1 error for the _react failure, got {result.errors}; "
        "the stop sweep was silently fail-open before the fix"
    )
    assert len(result.decisions) == 1
    assert result.decisions[0].gate == "PER_POSITION_STOP_ERROR", (
        f"expected gate PER_POSITION_STOP_ERROR, got {result.decisions[0].gate}"
    )
    assert "stop_sweep_error" in (result.decisions[0].error or "")


# --------------------------------------------------------------------------- #
# AG-EQ-1 take-profit sweep (HERMES_QUANT_TAKE_PROFIT_SWEEP, default-OFF).
# --------------------------------------------------------------------------- #
def test_take_profit_fires_on_winner_when_flag_on(
    isolate_config, isolate_quant_home, monkeypatch
):
    _set_mode_autonomous(isolate_config)
    _seed_open_losing_position(isolate_quant_home)  # ASTS entry 118.17, +0.20
    monkeypatch.setenv("HERMES_QUANT_PER_POSITION_STOP", "1")
    monkeypatch.setenv("HERMES_QUANT_TAKE_PROFIT_SWEEP", "1")
    # Mark WAY above entry -> +27% gain >= 16% TP -> take profit.
    monkeypatch.setattr(
        "hermes_quant.perception.build_perception_frame_live",
        lambda *a, **k: _MarkFrame(150.0),
    )
    react_calls = []

    def fake_react(advisor_result, entry, fill_size_pct, **kwargs):
        react_calls.append((entry.symbol, fill_size_pct, advisor_result.get("reason")))
        return ("exec_tp_asts", fill_size_pct)

    monkeypatch.setattr("hermes_quant.autonomous._react", fake_react)
    result = auto.tick(dry_run=False, symbols=[], advisor_recommend=lambda **kw: None)

    assert react_calls, "TP must force-exit the winner via _react"
    sym, fill, reason = react_calls[0]
    assert sym == "ASTS"
    assert fill == pytest.approx(0.0, abs=1e-9)  # flat absolute target (no double-slippage)
    assert reason == "autonomous_per_position_take_profit"
    tp_decisions = [d for d in result.decisions if d.gate == "PER_POSITION_TAKE_PROFIT_FIRED"]
    assert len(tp_decisions) == 1
    assert tp_decisions[0].details["exit_kind"] == "take_profit"
    assert tp_decisions[0].details["gain_pct"] == pytest.approx(0.2693, abs=1e-2)


def test_take_profit_does_not_fire_when_flag_off(
    isolate_config, isolate_quant_home, monkeypatch
):
    _set_mode_autonomous(isolate_config)
    _seed_open_losing_position(isolate_quant_home)
    monkeypatch.setenv("HERMES_QUANT_PER_POSITION_STOP", "1")
    monkeypatch.delenv("HERMES_QUANT_TAKE_PROFIT_SWEEP", raising=False)  # OFF
    # A big winner — would TP if the flag were on; must NOT with it off.
    monkeypatch.setattr(
        "hermes_quant.perception.build_perception_frame_live",
        lambda *a, **k: _MarkFrame(150.0),
    )
    react_calls = []
    monkeypatch.setattr(
        "hermes_quant.autonomous._react",
        lambda *a, **k: react_calls.append(a) or ("x", 0.0),
    )
    result = auto.tick(dry_run=False, symbols=[], advisor_recommend=lambda **kw: None)
    assert react_calls == [], "TP flag OFF: a winner must NOT be force-exited"
    assert not any("TAKE_PROFIT" in d.gate for d in result.decisions)


def test_stop_takes_precedence_over_take_profit(
    isolate_config, isolate_quant_home, monkeypatch
):
    """A losing position triggers the STOP, never TP, even with both flags on."""
    _set_mode_autonomous(isolate_config)
    _seed_open_losing_position(isolate_quant_home)  # entry 118.17
    monkeypatch.setenv("HERMES_QUANT_PER_POSITION_STOP", "1")
    monkeypatch.setenv("HERMES_QUANT_TAKE_PROFIT_SWEEP", "1")
    monkeypatch.setattr(
        "hermes_quant.perception.build_perception_frame_live",
        lambda *a, **k: _MarkFrame(93.44),  # -20.9% loss
    )
    monkeypatch.setattr("hermes_quant.autonomous._react", lambda *a, **k: ("exec_stop", 0.0))
    result = auto.tick(dry_run=False, symbols=[], advisor_recommend=lambda **kw: None)
    assert any(d.gate == "PER_POSITION_STOP_FIRED" for d in result.decisions)
    assert not any("TAKE_PROFIT" in d.gate for d in result.decisions)


# --------------------------------------------------------------------------- #
# AG-EQ-3 watch-registry state tracking (HERMES_QUANT_WATCH_REGISTRY, default-OFF).
# --------------------------------------------------------------------------- #
def test_watch_registry_records_and_ratchets_peak_when_on(
    isolate_config, isolate_quant_home, monkeypatch
):
    """When the flag is ON, the sweep records the open play + ratchets its peak gain
    across the tick — pure state, no exit-behavior change."""
    _set_mode_autonomous(isolate_config)
    _seed_open_losing_position(isolate_quant_home)  # ASTS entry 118.17, +0.20 held
    monkeypatch.setenv("HERMES_QUANT_PER_POSITION_STOP", "1")
    monkeypatch.setenv("HERMES_QUANT_WATCH_REGISTRY", "1")
    monkeypatch.delenv("HERMES_QUANT_TAKE_PROFIT_SWEEP", raising=False)
    # Mark ABOVE entry -> a winner (+~10%); no stop fires, so the play stays open and
    # the registry should record it + set its peak.
    monkeypatch.setattr(
        "hermes_quant.perception.build_perception_frame_live",
        lambda *a, **k: _MarkFrame(130.0),  # ~+10% from 118.17
    )
    monkeypatch.setattr("hermes_quant.autonomous._react", lambda *a, **k: ("x", 0.0))
    auto.tick(dry_run=False, symbols=[], advisor_recommend=lambda **kw: None)

    from hermes_quant.risk.watch_registry import WatchRegistry

    reg = WatchRegistry(
        db_path=isolate_quant_home / "watch_registry.db",
        mirror_path=isolate_quant_home / "watch_registry.json",
    )
    pos = reg.get("ASTS")
    assert pos is not None, "an open play must be recorded in the registry when the flag is ON"
    assert pos.entry_price == pytest.approx(118.17, abs=0.5)
    assert pos.peak_gain_pct > 0.05, f"peak gain must be ratcheted (~+10%); got {pos.peak_gain_pct}"


def test_watch_registry_inert_when_flag_off(
    isolate_config, isolate_quant_home, monkeypatch
):
    """Flag OFF: no registry db is created (byte-identical state tracking)."""
    _set_mode_autonomous(isolate_config)
    _seed_open_losing_position(isolate_quant_home)
    monkeypatch.setenv("HERMES_QUANT_PER_POSITION_STOP", "1")
    monkeypatch.delenv("HERMES_QUANT_WATCH_REGISTRY", raising=False)  # OFF
    monkeypatch.setattr(
        "hermes_quant.perception.build_perception_frame_live",
        lambda *a, **k: _MarkFrame(130.0),
    )
    monkeypatch.setattr("hermes_quant.autonomous._react", lambda *a, **k: ("x", 0.0))
    auto.tick(dry_run=False, symbols=[], advisor_recommend=lambda **kw: None)
    assert not (isolate_quant_home / "watch_registry.db").exists(), (
        "flag OFF: the registry must never be created (byte-identical)"
    )


# --------------------------------------------------------------------------- #
# tp1/tp2 tranche/trailing PARTIAL exit wired into the sweep (HERMES_QUANT_TP_TRANCHE).
# --------------------------------------------------------------------------- #
def _prime_registry(qhome, symbol, entry_price, stop_pct, tranches_taken=0, peak=0.0):
    from hermes_quant.risk.watch_registry import WatchRegistry
    reg = WatchRegistry(db_path=qhome / "watch_registry.db", mirror_path=qhome / "watch_registry.json")
    reg.record_open(symbol, entry_price=entry_price, stop_pct=stop_pct)
    if peak:
        reg.update_peak(symbol, peak)
    for _ in range(tranches_taken):
        reg.mark_tranche(symbol)
    return reg


def test_tranche1_partial_exit_keeps_position_open(isolate_config, isolate_quant_home, monkeypatch):
    """A +1R winner with 0 tranches taken does a PARTIAL exit (one 0.05 rung), the position
    stays OPEN (NOT in stopped), and tranches_taken increments."""
    _set_mode_autonomous(isolate_config)
    _seed_open_losing_position(isolate_quant_home)  # ASTS +0.20 held, entry 118.17
    monkeypatch.setenv("HERMES_QUANT_PER_POSITION_STOP", "1")
    monkeypatch.setenv("HERMES_QUANT_WATCH_REGISTRY", "1")
    monkeypatch.setenv("HERMES_QUANT_TP_TRANCHE", "1")
    _prime_registry(isolate_quant_home, "ASTS", 118.17, 0.08)
    # Mark comfortably past +1R (8% gain): 118.17 * 1.085 ~= 128.5 -> +8.7% >= +1R.
    monkeypatch.setattr("hermes_quant.perception.build_perception_frame_live",
                        lambda *a, **k: _MarkFrame(128.5))
    react_calls = []
    def fake_react(ar, e, fill, **k):
        react_calls.append((e.symbol, fill, ar.get("reason")))
        return ("exec_tr", fill)
    monkeypatch.setattr("hermes_quant.autonomous._react", fake_react)
    result = auto.tick(dry_run=False, symbols=[], advisor_recommend=lambda **kw: None)
    # PARTIAL: target = 0.20 - 0.05 = 0.15 (NOT 0.0 flat).
    assert react_calls, "tranche must fire a partial _react"
    sym, fill, reason = react_calls[0]
    assert fill == pytest.approx(0.15, abs=1e-9), f"tranche-1 must reduce 0.20->0.15 (one rung), got {fill}"
    assert "tranche" in reason
    # The play is NOT fully closed -> registry still has it, tranches_taken incremented.
    from hermes_quant.risk.watch_registry import WatchRegistry
    reg = WatchRegistry(db_path=isolate_quant_home / "watch_registry.db",
                        mirror_path=isolate_quant_home / "watch_registry.json")
    pos = reg.get("ASTS")
    assert pos is not None and pos.tranches_taken == 1
    # And it is NOT in the watchlist-loop exemption (stays managed) — surfaced as TRANCHE_FIRED.
    assert any(d.gate == "PER_POSITION_TRANCHE_FIRED" for d in result.decisions)


def test_tranche_inert_when_flag_off(isolate_config, isolate_quant_home, monkeypatch):
    """TP_TRANCHE off -> the full all-at-once TP path runs (flatten to 0.0), not a partial."""
    _set_mode_autonomous(isolate_config)
    _seed_open_losing_position(isolate_quant_home)
    monkeypatch.setenv("HERMES_QUANT_PER_POSITION_STOP", "1")
    monkeypatch.setenv("HERMES_QUANT_WATCH_REGISTRY", "1")
    monkeypatch.setenv("HERMES_QUANT_TAKE_PROFIT_SWEEP", "1")
    monkeypatch.delenv("HERMES_QUANT_TP_TRANCHE", raising=False)  # OFF
    _prime_registry(isolate_quant_home, "ASTS", 118.17, 0.08)
    monkeypatch.setattr("hermes_quant.perception.build_perception_frame_live",
                        lambda *a, **k: _MarkFrame(150.0))  # big winner -> full TP
    react_calls = []
    monkeypatch.setattr("hermes_quant.autonomous._react",
                        lambda ar, e, fill, **k: react_calls.append(fill) or ("x", fill))
    auto.tick(dry_run=False, symbols=[], advisor_recommend=lambda **kw: None)
    assert react_calls and react_calls[0] == pytest.approx(0.0, abs=1e-9), (
        "tranche OFF: TP must FULL-flatten to 0.0, not a partial"
    )


# --------------------------------------------------------------------------- #
# wave3-wiring-review fixes: tranche no-fill / raise must NOT re-fire or bypass TP; NaN mark.
# --------------------------------------------------------------------------- #
def test_tranche_nofill_falls_through_to_full_tp_backstop(isolate_config, isolate_quant_home, monkeypatch):
    """DEFECT-1: when the tranche _react NO-FILLS, the full-TP backstop must still run this
    tick (return False, don't `continue`) — otherwise a position past +2R never exits and
    tranche-1 re-fires every tick. Here the position is a big winner (past +2R AND past the
    16% full-TP); the tranche reactor no-fills; the full-TP must then full-flatten it."""
    _set_mode_autonomous(isolate_config)
    _seed_open_losing_position(isolate_quant_home)
    monkeypatch.setenv("HERMES_QUANT_PER_POSITION_STOP", "1")
    monkeypatch.setenv("HERMES_QUANT_WATCH_REGISTRY", "1")
    monkeypatch.setenv("HERMES_QUANT_TP_TRANCHE", "1")
    monkeypatch.setenv("HERMES_QUANT_TAKE_PROFIT_SWEEP", "1")  # the backstop
    _prime_registry(isolate_quant_home, "ASTS", 118.17, 0.08)
    monkeypatch.setattr("hermes_quant.perception.build_perception_frame_live",
                        lambda *a, **k: _MarkFrame(160.0))  # ~+35% -> past +2R and +TP
    calls = []
    def fake_react(ar, e, fill, **k):
        calls.append((fill, ar.get("reason")))
        # tranche attempt NO-FILLS (None); the full-TP backstop fill succeeds.
        if "tranche" in ar.get("reason", ""):
            return None
        return ("exec_tp", fill)
    monkeypatch.setattr("hermes_quant.autonomous._react", fake_react)
    result = auto.tick(dry_run=False, symbols=[], advisor_recommend=lambda **kw: None)
    # The full-TP backstop must have FLATTENED (0.0) after the tranche no-fill.
    reasons = [r for _, r in calls]
    assert any("tranche" in r for r in reasons), "tranche attempt should have been made"
    assert any(r == "autonomous_per_position_take_profit" for r in reasons), (
        "DEFECT-1: full-TP backstop must run after a tranche no-fill (not be bypassed by continue)"
    )
    assert any(f == pytest.approx(0.0, abs=1e-9) for f, _ in calls), "TP backstop must full-flatten"


def test_nan_mark_in_tranche_path_holds_not_errors(isolate_config, isolate_quant_home, monkeypatch):
    """DEFECT-2: a NaN mark (passes the `is None` guard) must HOLD cleanly, NOT raise a
    TypeError mis-labeled as PER_POSITION_STOP_ERROR / spurious result.errors++."""
    _set_mode_autonomous(isolate_config)
    _seed_open_losing_position(isolate_quant_home)
    monkeypatch.setenv("HERMES_QUANT_PER_POSITION_STOP", "1")
    monkeypatch.setenv("HERMES_QUANT_WATCH_REGISTRY", "1")
    monkeypatch.setenv("HERMES_QUANT_TP_TRANCHE", "1")
    _prime_registry(isolate_quant_home, "ASTS", 118.17, 0.08)
    monkeypatch.setattr("hermes_quant.perception.build_perception_frame_live",
                        lambda *a, **k: _MarkFrame(float("nan")))  # NaN mark
    calls = []
    monkeypatch.setattr("hermes_quant.autonomous._react",
                        lambda ar, e, fill, **k: calls.append(fill) or ("x", fill))
    result = auto.tick(dry_run=False, symbols=[], advisor_recommend=lambda **kw: None)
    assert calls == [], "a NaN mark must HOLD (no exit fired)"
    assert result.errors == 0, "a NaN mark is a clean HOLD, must NOT increment result.errors"
    assert not any("ERROR" in d.gate for d in result.decisions), "no spurious *_ERROR gate on a NaN-mark HOLD"
