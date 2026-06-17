"""GATE-0 anchor helper: refuse-when-disarmed + armed round-trip (ADR-0099 GATE-0)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "scripts" / "aegis-gate0-start.py"
_REQUIRED = [
    "HERMES_QUANT_DURABLE_DRAWDOWN_BASELINE",
    "HERMES_QUANT_PER_POSITION_STOP",
    "HERMES_QUANT_DELTA_NORMALIZER",
    "HERMES_QUANT_ACCOUNT_LOCK",
]


def _load():
    spec = importlib.util.spec_from_file_location("aegis_gate0_start", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["aegis_gate0_start"] = mod
    spec.loader.exec_module(mod)
    return mod


H = _load()


def _arm(monkeypatch):
    for f in _REQUIRED:
        monkeypatch.setenv(f, "1")


def test_refuses_when_disarmed(tmp_path, monkeypatch):
    for f in _REQUIRED:
        monkeypatch.delenv(f, raising=False)
    rc = H.main(["--home", str(tmp_path)])
    assert rc == 2  # refuse
    assert not (tmp_path / "quant" / "clean_window_start.json").exists()  # nothing written


def test_force_stamps_even_if_disarmed(tmp_path, monkeypatch):
    for f in _REQUIRED:
        monkeypatch.delenv(f, raising=False)
    rc = H.main(["--home", str(tmp_path), "--force"])
    assert rc == 0
    assert (tmp_path / "quant" / "clean_window_start.json").exists()


def test_armed_writes_anchor_and_roundtrips(tmp_path, monkeypatch):
    _arm(monkeypatch)
    rc = H.main(["--home", str(tmp_path)])
    assert rc == 0
    from hermes_quant.eval.clean_window import read_clean_window_start

    t0 = read_clean_window_start(home=tmp_path)
    assert t0 is not None  # the canonical reader round-trips what we wrote


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    _arm(monkeypatch)
    rc = H.main(["--home", str(tmp_path), "--dry-run"])
    assert rc == 0
    assert not (tmp_path / "quant" / "clean_window_start.json").exists()
