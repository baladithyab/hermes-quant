"""Tests for V03-6: quant_doctor analyst confidence-drift surface."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_quant.tools import _compute_drift_surface, quant_doctor


@pytest.fixture
def signal_bus(tmp_path, monkeypatch):
    """Create an isolated tmp signal bus and return (writer, path).

    Pytest's import system can create duplicate `hermes_quant.tools`
    module dicts under certain orderings, so monkeypatching the module
    attribute is unreliable. Tests therefore pass `signal_bus_path=`
    explicitly to `_compute_drift_surface`. quant_doctor() reads the
    module-level path, so for the doctor-wiring tests we monkeypatch via
    the module reference (which still works because pytest's namespace
    lookup uses the LIVE module dict for module-attribute access from
    Python code, even when function `__globals__` are stale).
    """
    import hermes_quant.tools as t

    bus = tmp_path / "signals.jsonl"
    exec_bus = tmp_path / "executions.jsonl"
    state_db = tmp_path / "state.db"
    monkeypatch.setattr(t, "SIGNAL_BUS_PATH", bus)
    monkeypatch.setattr(t, "EXECUTION_BUS_PATH", exec_bus)
    monkeypatch.setattr(t, "STATE_DB_PATH", state_db)
    monkeypatch.setattr(t, "QUANT_HOME", tmp_path)

    def write_signals(records):
        with bus.open("w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    yield write_signals, bus


def _signal(views: list[tuple[str, float]], **extra) -> dict:
    """Build a signal-bus record with the given (analyst, confidence) tuples."""
    return {
        "asset": extra.get("asset", "BTC/USDT"),
        "asof": extra.get("asof", "2024-06-01T00:00:00Z"),
        "analyst_views": [
            {"analyst": name, "confidence": conf, "direction": 1, "magnitude": 0.5, "horizon": "1h"}
            for name, conf in views
        ],
    }


# ===========================================================================
# Drift computation
# ===========================================================================


def test_drift_empty_bus_returns_safe_default(signal_bus):
    write, bus = signal_bus
    write([])
    out = _compute_drift_surface(recent_n=50, signal_bus_path=bus)
    assert out["n_signals_total"] == 0
    assert out["per_analyst"] == {}
    assert out["any_flagged"] is False


def test_drift_stable_analyst_not_flagged(signal_bus):
    """An analyst whose confidence stays at ~0.7 throughout shouldn't flag."""
    write, bus = signal_bus
    records = [_signal([("steady", 0.7)]) for _ in range(100)]
    write(records)
    out = _compute_drift_surface(recent_n=20, threshold=0.15, signal_bus_path=bus)
    e = out["per_analyst"]["steady"]
    assert abs(e["delta"]) < 0.01
    assert not e["flagged"]
    assert not out["any_flagged"]


def test_drift_recent_shift_flags_analyst(signal_bus):
    """An analyst whose recent confidence dropped sharply should flag."""
    write, bus = signal_bus
    # 80 historical observations at 0.8, then 20 recent at 0.3
    records = [_signal([("regime_change", 0.8)]) for _ in range(80)] + [
        _signal([("regime_change", 0.3)]) for _ in range(20)
    ]
    write(records)
    out = _compute_drift_surface(recent_n=20, threshold=0.15, signal_bus_path=bus)
    e = out["per_analyst"]["regime_change"]
    assert e["flagged"]
    assert e["delta"] < -0.15
    assert "shifted" in e["reason"]
    assert out["any_flagged"]


def test_drift_recent_window_caps_at_total(signal_bus):
    """If recent_n > total, recent window equals all records and delta = 0."""
    write, bus = signal_bus
    records = [_signal([("a", 0.5)]) for _ in range(10)]
    write(records)
    out = _compute_drift_surface(recent_n=100, threshold=0.15, signal_bus_path=bus)
    e = out["per_analyst"]["a"]
    assert e["delta"] == 0.0
    assert not e["flagged"]


def test_drift_analyst_silent_in_recent_window_flagged(signal_bus):
    """An analyst that emitted historically but is silent in the recent
    window should flag with reason='no_recent_views'."""
    write, bus = signal_bus
    records = (
        [_signal([("vanishing", 0.7), ("loud", 0.5)]) for _ in range(30)]
        + [_signal([("loud", 0.5)]) for _ in range(50)]  # vanishing absent
    )
    write(records)
    out = _compute_drift_surface(recent_n=30, threshold=0.15, signal_bus_path=bus)
    vanishing = out["per_analyst"]["vanishing"]
    assert vanishing["flagged"]
    assert vanishing["reason"] == "no_recent_views"
    assert vanishing["n_recent"] == 0
    assert vanishing["n_lifetime"] == 30


def test_drift_threshold_respected(signal_bus):
    """Lower threshold catches smaller drifts."""
    write, bus = signal_bus
    # 0.10 shift — not flagged at threshold=0.15, flagged at threshold=0.05
    records = [_signal([("borderline", 0.7)]) for _ in range(80)] + [
        _signal([("borderline", 0.6)]) for _ in range(20)
    ]
    write(records)

    loose = _compute_drift_surface(recent_n=20, threshold=0.15, signal_bus_path=bus)
    assert not loose["per_analyst"]["borderline"]["flagged"]

    tight = _compute_drift_surface(recent_n=20, threshold=0.05, signal_bus_path=bus)
    assert tight["per_analyst"]["borderline"]["flagged"]


def test_drift_skips_malformed_views(signal_bus):
    """Records with missing analyst name or confidence are skipped, not crashed."""
    write, bus = signal_bus
    records = [
        _signal([("good", 0.5)]),
        {"analyst_views": [{"analyst": None, "confidence": 0.5}]},
        {"analyst_views": [{"analyst": "noconf"}]},
        {"analyst_views": [{"analyst": "badtype", "confidence": "not-a-number"}]},
        _signal([("good", 0.6)]),
    ]
    write(records)
    out = _compute_drift_surface(recent_n=10, signal_bus_path=bus)
    assert "good" in out["per_analyst"]
    assert "noconf" not in out["per_analyst"]
    assert "badtype" not in out["per_analyst"]


# ===========================================================================
# Wiring: quant_doctor surfaces the drift block
# ===========================================================================


def test_quant_doctor_includes_drift_block(signal_bus):
    write, bus = signal_bus
    records = [_signal([("steady", 0.7)]) for _ in range(50)]
    write(records)
    raw = quant_doctor({})
    out = json.loads(raw)
    assert out["success"]
    assert out["drift"] is not None
    assert "per_analyst" in out["drift"]
    assert "steady" in out["drift"]["per_analyst"]


def test_quant_doctor_drift_can_be_disabled(signal_bus):
    write, bus = signal_bus
    records = [_signal([("steady", 0.7)]) for _ in range(20)]
    write(records)
    out = json.loads(quant_doctor({"drift": False}))
    assert out["drift"] is None


def test_quant_doctor_no_signal_bus_skips_drift(tmp_path, monkeypatch):
    """No signal bus on disk → drift block is None, no crash."""
    monkeypatch.setattr(
        "hermes_quant.tools.SIGNAL_BUS_PATH",
        tmp_path / "missing.jsonl",
    )
    monkeypatch.setattr(
        "hermes_quant.tools.EXECUTION_BUS_PATH",
        tmp_path / "missing-exec.jsonl",
    )
    monkeypatch.setattr(
        "hermes_quant.tools.STATE_DB_PATH",
        tmp_path / "missing-state.db",
    )
    monkeypatch.setattr("hermes_quant.tools.QUANT_HOME", tmp_path)
    out = json.loads(quant_doctor({}))
    assert out["success"]
    assert out["drift"] is None


def test_quant_doctor_drift_threshold_passthrough(signal_bus):
    """drift_threshold arg is honored end-to-end."""
    write, bus = signal_bus
    records = [_signal([("borderline", 0.7)]) for _ in range(80)] + [
        _signal([("borderline", 0.6)]) for _ in range(20)
    ]
    write(records)
    out_loose = json.loads(quant_doctor({"drift_threshold": 0.15, "drift_recent_n": 20}))
    assert not out_loose["drift"]["per_analyst"]["borderline"]["flagged"]

    out_tight = json.loads(quant_doctor({"drift_threshold": 0.05, "drift_recent_n": 20}))
    assert out_tight["drift"]["per_analyst"]["borderline"]["flagged"]
