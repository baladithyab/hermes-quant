"""Integration tests for the autonomous-mode tick orchestrator (ADR-0016).

Covers:
  - mode_mismatch when quant.pdr.mode != autonomous
  - dry-run safety (FIRE decisions don't actually React)
  - paper-only React happy path (FIRE + dry_run=False writes execution)
  - max_per_tick_opens cap
  - kill-switch trip prevents further fires
  - per-symbol error isolation
  - all four silence reasons surface in tick output
  - empty watchlist no-op
  - reset_kill_switch recovers
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from hermes_quant.autonomous import (
    reset_kill_switch,
    tick,
    trip_kill_switch,
)
from hermes_quant.watchlist import WatchlistEntry


@pytest.fixture
def isolate_quant_home(tmp_path, monkeypatch):
    """Redirect ~/.hermes/quant to a tmpdir so tests don't pollute real state."""
    qhome = tmp_path / "quant"
    qhome.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "hermes_quant.autonomous.QUANT_HOME",
        qhome,
    )
    monkeypatch.setattr(
        "hermes_quant.autonomous.KILL_SWITCH_PATH",
        qhome / "autonomous_kill_switch.json",
    )
    return qhome


@pytest.fixture
def isolate_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    monkeypatch.setattr(
        "hermes_quant.watchlist.get_config_path",
        lambda: cfg,
    )
    # autonomous module reads via watchlist's get_config_path, so this is enough
    return cfg


def _set_mode_autonomous(cfg_path: Path):
    import yaml

    cfg = {}
    if cfg_path.exists():
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg.setdefault("quant", {}).setdefault("pdr", {})["mode"] = "autonomous"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _make_advisor_result(
    *,
    confidence=0.85,
    direction=1,
    magnitude=0.05,
    n_voices=2,
    risk_pass=True,
    kelly=0.05,
    atr_rel=0.05,
    lessons=None,
):
    return {
        "as_of": "2026-05-13T20:00:00Z",
        "decision_price": 100.0,
        "signal_id": "sig_test",
        "aggregated_signal": {
            "confidence": confidence,
            "direction": direction,
            "magnitude": magnitude,
        },
        "risk_gate": {
            "pass": risk_pass,
            "kelly_fraction": kelly,
            "reason": "ok" if risk_pass else "vetoed",
            "gated_reason": None if risk_pass else "vetoed",
        },
        "analyst_views": [
            {"analyst": f"A{i}", "metadata": {"atr_relative": atr_rel}} for i in range(n_voices)
        ],
        "lessons": lessons or [],
    }


# ---------------------------------------------------------------------------
# Mode gate
# ---------------------------------------------------------------------------


def test_tick_returns_mode_mismatch_when_advise(isolate_config, isolate_quant_home):
    # Default mode is 'advise' (no config written -> _read_pdr_mode returns advise)
    result = tick(dry_run=True)
    assert result.mode == "advise"
    assert result.errors == 1
    assert result.watchlist_size == 0
    assert result.decisions == []


def test_tick_runs_when_mode_autonomous(isolate_config, isolate_quant_home):
    _set_mode_autonomous(isolate_config)
    result = tick(
        dry_run=True,
        symbols=[],  # empty watchlist
        advisor_recommend=lambda **kw: _make_advisor_result(),
    )
    assert result.mode == "autonomous"
    assert result.watchlist_size == 0


# ---------------------------------------------------------------------------
# Empty watchlist
# ---------------------------------------------------------------------------


def test_tick_empty_watchlist_is_noop(isolate_config, isolate_quant_home):
    _set_mode_autonomous(isolate_config)
    result = tick(
        dry_run=True,
        symbols=[],
        advisor_recommend=lambda **kw: _make_advisor_result(),
    )
    assert result.fires == 0
    assert result.silences == 0
    assert result.errors == 0
    assert result.decisions == []


# ---------------------------------------------------------------------------
# Dry-run safety
# ---------------------------------------------------------------------------


def test_dry_run_does_not_react_even_on_fire(
    isolate_config,
    isolate_quant_home,
):
    """Even when the gate FIREs, dry_run=True must NOT call PaperReactor."""
    _set_mode_autonomous(isolate_config)

    react_calls = []

    def fake_advisor(**kw):
        return _make_advisor_result()  # gate-passing

    with mock.patch(
        "hermes_quant.autonomous._react",
        side_effect=lambda *a, **k: react_calls.append((a, k)) or "exec_xxx",
    ):
        result = tick(
            dry_run=True,
            symbols=[WatchlistEntry("AAPL", "equity", "1d")],
            advisor_recommend=fake_advisor,
        )

    assert result.fires == 1
    assert react_calls == []  # KEY: no React in dry-run
    assert result.decisions[0].execution_id is None  # unset in dry-run


def test_no_dry_run_calls_react_on_fire(
    isolate_config,
    isolate_quant_home,
):
    _set_mode_autonomous(isolate_config)

    react_calls = []

    def fake_react(advisor_result, entry, kelly):
        react_calls.append((entry.symbol, kelly))
        return f"exec_{entry.symbol}"

    with mock.patch(
        "hermes_quant.autonomous._react",
        side_effect=fake_react,
    ):
        result = tick(
            dry_run=False,
            symbols=[WatchlistEntry("AAPL", "equity", "1d")],
            advisor_recommend=lambda **kw: _make_advisor_result(),
        )

    assert result.fires == 1
    assert react_calls == [("AAPL", 0.05)]
    assert result.decisions[0].execution_id == "exec_AAPL"


# ---------------------------------------------------------------------------
# max_per_tick_opens cap
# ---------------------------------------------------------------------------


def test_max_per_tick_opens_caps_fires(
    isolate_config,
    isolate_quant_home,
):
    """First FIRE goes through; subsequent FIREs become SILENCE_PER_TICK_CAP."""
    _set_mode_autonomous(isolate_config)
    # Default max_per_tick_opens=1

    result = tick(
        dry_run=True,
        symbols=[
            WatchlistEntry("AAPL", "equity", "1d"),
            WatchlistEntry("MSFT", "equity", "1d"),
            WatchlistEntry("GOOG", "equity", "1d"),
        ],
        advisor_recommend=lambda **kw: _make_advisor_result(),
    )

    # All three signals are gate-passing, but cap=1 so only 1 fires
    assert result.fires == 1
    assert result.silences == 2
    capped = [d for d in result.decisions if d.gate == "SILENCE_PER_TICK_CAP"]
    assert len(capped) == 2
    assert all(d.details.get("would_have_fired") for d in capped)


# ---------------------------------------------------------------------------
# Kill-switch
# ---------------------------------------------------------------------------


def test_kill_switch_trip_halts_tick(isolate_config, isolate_quant_home):
    _set_mode_autonomous(isolate_config)
    trip_kill_switch(
        cumulative_pnl_pct=-0.15,
        threshold_pct=0.10,
        reason="manual_test_trip",
    )
    result = tick(
        dry_run=True,
        symbols=[WatchlistEntry("AAPL", "equity", "1d")],
        advisor_recommend=lambda **kw: _make_advisor_result(),
    )
    assert result.kill_switch_state.tripped is True
    assert result.fires == 0
    assert result.decisions == []  # tick aborts before evaluating symbols


def test_reset_kill_switch_resumes_normal_operation(
    isolate_config,
    isolate_quant_home,
):
    _set_mode_autonomous(isolate_config)
    trip_kill_switch(cumulative_pnl_pct=-0.15, threshold_pct=0.10, reason="test")
    cleared = reset_kill_switch()
    assert cleared is True

    result = tick(
        dry_run=True,
        symbols=[WatchlistEntry("AAPL", "equity", "1d")],
        advisor_recommend=lambda **kw: _make_advisor_result(),
    )
    assert result.kill_switch_state.tripped is False
    assert result.fires == 1


def test_reset_kill_switch_when_not_tripped_returns_false(
    isolate_config,
    isolate_quant_home,
):
    assert reset_kill_switch() is False


# ---------------------------------------------------------------------------
# Per-symbol error isolation
# ---------------------------------------------------------------------------


def test_advisor_failure_for_one_symbol_does_not_break_tick(
    isolate_config,
    isolate_quant_home,
):
    _set_mode_autonomous(isolate_config)

    def fake_advisor(*, symbol, **_kw):
        if symbol == "MSFT":
            raise RuntimeError("rate limit hit")
        return _make_advisor_result()

    result = tick(
        dry_run=True,
        symbols=[
            WatchlistEntry("AAPL", "equity", "1d"),
            WatchlistEntry("MSFT", "equity", "1d"),
            WatchlistEntry("GOOG", "equity", "1d"),
        ],
        advisor_recommend=fake_advisor,
    )

    assert result.errors == 1
    assert result.fires + result.silences == 2
    msft = [d for d in result.decisions if d.symbol == "MSFT"][0]
    assert msft.gate == "ERROR"
    assert "rate limit" in (msft.error or "")


# ---------------------------------------------------------------------------
# All silence reasons appear in output
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario,expected_gate",
    [
        ("low_confidence", "SILENCE_LOW_CONFIDENCE"),
        ("low_urgency", "SILENCE_LOW_URGENCY"),
        ("insufficient_voices", "SILENCE_INSUFFICIENT_VOICES"),
        ("gated_by_advisor", "SILENCE_GATED_BY_ADVISOR"),
    ],
)
def test_all_silence_reasons_surface(
    isolate_config,
    isolate_quant_home,
    scenario,
    expected_gate,
):
    _set_mode_autonomous(isolate_config)

    def advisor_for_scenario(**kw):
        if scenario == "low_confidence":
            return _make_advisor_result(confidence=0.4)
        if scenario == "low_urgency":
            return _make_advisor_result(magnitude=0.001, atr_rel=0.10)
        if scenario == "insufficient_voices":
            return _make_advisor_result(n_voices=1)
        if scenario == "gated_by_advisor":
            return _make_advisor_result(risk_pass=False)
        raise AssertionError("unhandled scenario")

    result = tick(
        dry_run=True,
        symbols=[WatchlistEntry("AAPL", "equity", "1d")],
        advisor_recommend=advisor_for_scenario,
    )
    assert result.fires == 0
    assert result.silences == 1
    assert result.decisions[0].gate == expected_gate


# ---------------------------------------------------------------------------
# Tick output shape (operator-readable per ADR-0016 §D8)
# ---------------------------------------------------------------------------


def test_tick_output_to_dict_shape(isolate_config, isolate_quant_home):
    _set_mode_autonomous(isolate_config)
    result = tick(
        dry_run=True,
        symbols=[WatchlistEntry("AAPL", "equity", "1d")],
        advisor_recommend=lambda **kw: _make_advisor_result(),
    )

    out = result.to_dict()
    assert "asof" in out
    assert "mode" in out
    assert "dry_run" in out
    assert "watchlist_size" in out
    assert "decisions" in out
    assert "fires" in out
    assert "silences" in out
    assert "errors" in out
    assert "kill_switch" in out

    # Must be JSON-serializable
    serialized = json.dumps(out, default=str)
    assert serialized  # didn't raise

    # Each decision has required fields
    for d in out["decisions"]:
        assert "symbol" in d
        assert "gate" in d
        assert "details" in d


def test_fire_decision_includes_action_and_execution_id(
    isolate_config,
    isolate_quant_home,
):
    _set_mode_autonomous(isolate_config)
    with mock.patch(
        "hermes_quant.autonomous._react",
        return_value="exec_AAPL_001",
    ):
        result = tick(
            dry_run=False,
            symbols=[WatchlistEntry("AAPL", "equity", "1d")],
            advisor_recommend=lambda **kw: _make_advisor_result(),
        )

    fire = result.decisions[0]
    assert fire.gate == "FIRE"
    assert fire.action is not None
    assert fire.action["target_position_pct"] == 0.05
    assert fire.action["direction"] == 1
    assert fire.execution_id == "exec_AAPL_001"


# ---------------------------------------------------------------------------
# React error isolation
# ---------------------------------------------------------------------------


def test_react_failure_marks_decision_error_but_continues(
    isolate_config,
    isolate_quant_home,
):
    _set_mode_autonomous(isolate_config)

    def fake_react(advisor_result, entry, kelly):
        if entry.symbol == "AAPL":
            raise RuntimeError("paper bus full")
        return f"exec_{entry.symbol}"

    with mock.patch(
        "hermes_quant.autonomous._react",
        side_effect=fake_react,
    ):
        result = tick(
            dry_run=False,
            symbols=[
                WatchlistEntry("AAPL", "equity", "1d"),
            ],
            advisor_recommend=lambda **kw: _make_advisor_result(),
        )

    assert result.errors == 1
    assert result.decisions[0].gate == "ERROR"
    assert "paper bus full" in (result.decisions[0].error or "")
