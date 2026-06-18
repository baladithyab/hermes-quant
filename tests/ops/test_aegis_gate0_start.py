"""GATE-0 anchor helper: refuse-when-disarmed + armed round-trip (ADR-0099 GATE-0)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "scripts" / "aegis-gate0-start.py"
_REQUIRED = [
    "HERMES_QUANT_DURABLE_DRAWDOWN_BASELINE",
    "HERMES_QUANT_PER_POSITION_STOP",
    "HERMES_QUANT_TAKE_PROFIT_SWEEP",
    "HERMES_QUANT_DELTA_NORMALIZER",
    "HERMES_QUANT_ACCOUNT_LOCK",
    # d83b: the ADR-0097 slippage-haircut rail is now REQUIRED (was only
    # recommended) so GATE-0 cannot stamp t0 with the haircut rail disarmed.
    "HERMES_QUANT_SLIPPAGE_HAIRCUT",
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


# --------------------------------------------------------------------------- #
# d83b — the ADR-0097 slippage-haircut rail must be REQUIRED, not recommended.
# --------------------------------------------------------------------------- #
def test_slippage_haircut_is_required_not_recommended():
    """The haircut rail moved into _REQUIRED_ARMED so GATE-0 refuses without it.
    RED before d83b: it lived in _RECOMMENDED (warn-only, non-blocking)."""
    assert "HERMES_QUANT_SLIPPAGE_HAIRCUT" in H._REQUIRED_ARMED, (
        "SLIPPAGE_HAIRCUT must be a REQUIRED-armed rail (the run-card claims "
        "live-realistic evidence; a disarmed haircut makes that claim dishonest)"
    )
    assert "HERMES_QUANT_SLIPPAGE_HAIRCUT" not in H._RECOMMENDED, (
        "SLIPPAGE_HAIRCUT must no longer be merely recommended"
    )


def test_refuses_when_only_haircut_disarmed(tmp_path, monkeypatch):
    """All the OTHER rails armed, but the haircut rail disarmed -> GATE-0 REFUSES.
    RED before d83b: with the haircut only recommended, this returned 0 (stamped)."""
    for f in _REQUIRED:
        monkeypatch.setenv(f, "1")
    monkeypatch.delenv("HERMES_QUANT_SLIPPAGE_HAIRCUT", raising=False)  # the only disarmed rail
    rc = H.main(["--home", str(tmp_path)])
    assert rc == 2, "GATE-0 must refuse when the required haircut rail is disarmed"
    assert not (tmp_path / "quant" / "clean_window_start.json").exists()


def test_force_overrides_disarmed_haircut(tmp_path, monkeypatch):
    """--force still stamps (records the disarmed window honestly)."""
    for f in _REQUIRED:
        monkeypatch.setenv(f, "1")
    monkeypatch.delenv("HERMES_QUANT_SLIPPAGE_HAIRCUT", raising=False)
    rc = H.main(["--home", str(tmp_path), "--force"])
    assert rc == 0
    assert (tmp_path / "quant" / "clean_window_start.json").exists()


def test_take_profit_sweep_is_required_not_recommended():
    """2ab1: A clean AG-EQ-1 evidence window must prove SL and TP together.

    RED after the verify-pass refutation: TAKE_PROFIT_SWEEP was only warn-only, so
    Gate-0 could stamp a "clean" window without the live take-profit exit rail.
    """
    assert "HERMES_QUANT_TAKE_PROFIT_SWEEP" in H._REQUIRED_ARMED
    assert "HERMES_QUANT_TAKE_PROFIT_SWEEP" not in H._RECOMMENDED


def test_refuses_when_only_take_profit_disarmed(tmp_path, monkeypatch):
    for f in _REQUIRED:
        monkeypatch.setenv(f, "1")
    monkeypatch.delenv("HERMES_QUANT_TAKE_PROFIT_SWEEP", raising=False)
    rc = H.main(["--home", str(tmp_path)])
    assert rc == 2, "GATE-0 must refuse when the required TP rail is disarmed"
    assert not (tmp_path / "quant" / "clean_window_start.json").exists()
