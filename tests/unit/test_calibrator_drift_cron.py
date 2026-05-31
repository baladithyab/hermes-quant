"""Tests for the calibrator-drift change-detecting no_agent watchdog (B11/M11).

The ops script (quant-calibrator-drift.py) used to print() the "collecting
pairs" lines, the full DRIFT RESULT JSON, and the auto_refit line on EVERY run,
spamming the operator every Monday with zero drift (no_agent silence-contract
violation, meta-review N10/M11). It is now a state-baseline change-detecting
watchdog (mirrors quant-catalyst-profitability.py): it emits stdout ONLY when
the alert state TRANSITIONS vs the persisted baseline.

We test the _emit() gate + main()'s silence/emit behavior with monkeypatched
run_drift_check / _collect_pairs / _load_universe (no network) and a tmp_path
baseline. The drift computation, the durable drift-log append, and the
auto-refit behavior are covered by tests/unit/test_calibrator_drift.py and are
NOT re-gated here — only the stdout emit is change-detected.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from hermes_quant.training.calibrator_drift import DriftResult


def _load_cron_module():
    """Import the ops script execv-safely (it re-execs the venv at import)."""
    repo = Path(__file__).resolve().parents[2]
    path = repo / "ops" / "scripts" / "quant-calibrator-drift.py"
    venv_py = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
    spec = importlib.util.spec_from_file_location("quant_calibrator_drift", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    saved = sys.executable
    try:
        sys.executable = str(venv_py)  # neutralize the script's execv guard
        spec.loader.exec_module(mod)
    finally:
        sys.executable = saved
    return mod


@pytest.fixture(scope="module")
def cron():
    return _load_cron_module()


def _result(*, should_alert: bool, drift: float = 0.0, n: int = 300) -> DriftResult:
    return DriftResult(
        drift=drift,
        predicted_mean=0.6,
        realized_mean=0.6 - drift,
        n_samples=n,
        threshold=0.05,
        should_alert=should_alert,
        refit_recommended=should_alert and n >= 200,
        reason="test",
    )


# ---------------------------------------------------------------------------
# _emit() change-detection gate (pure; no network)
# ---------------------------------------------------------------------------


def test_emit_first_run_clean_is_silent(cron, monkeypatch, tmp_path, capsys):
    """First-ever run that is in-tolerance -> empty stdout (no transition)."""
    monkeypatch.setattr(cron, "_BASELINE", tmp_path / "baseline.json")
    rc = cron._emit(_result(should_alert=False, drift=0.01), verbose=False, auto_refit=False)
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_emit_standing_clean_is_silent(cron, monkeypatch, tmp_path, capsys):
    """Was clean, still clean -> silent (the every-Monday-zero-drift case)."""
    baseline = tmp_path / "baseline.json"
    baseline.write_text('{"should_alert": false}')
    monkeypatch.setattr(cron, "_BASELINE", baseline)
    rc = cron._emit(_result(should_alert=False, drift=0.02), verbose=False, auto_refit=False)
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_emit_clean_to_drift_emits_headline_and_json(cron, monkeypatch, tmp_path, capsys):
    """clean -> drift transition: non-empty headline + full DRIFT RESULT JSON."""
    baseline = tmp_path / "baseline.json"
    baseline.write_text('{"should_alert": false}')
    monkeypatch.setattr(cron, "_BASELINE", baseline)
    rc = cron._emit(_result(should_alert=True, drift=0.4), verbose=False, auto_refit=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert out != ""
    assert "DRIFT RESULT" in out
    assert "ALERT: calibrator drift" in out
    assert '"should_alert": true' in out


def test_emit_standing_drift_is_silent(cron, monkeypatch, tmp_path, capsys):
    """Was alerting, still alerting -> silent (don't re-cry-wolf every run)."""
    baseline = tmp_path / "baseline.json"
    baseline.write_text('{"should_alert": true}')
    monkeypatch.setattr(cron, "_BASELINE", baseline)
    rc = cron._emit(_result(should_alert=True, drift=0.4), verbose=False, auto_refit=False)
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_emit_drift_to_clean_emits_once_then_silent(cron, monkeypatch, tmp_path, capsys):
    """drift -> clean: one transition note, then silent on the next clean run."""
    baseline = tmp_path / "baseline.json"
    baseline.write_text('{"should_alert": true}')
    monkeypatch.setattr(cron, "_BASELINE", baseline)

    # Transition back to clean -> one emit.
    rc = cron._emit(_result(should_alert=False, drift=0.01), verbose=False, auto_refit=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert out != ""
    assert "cleared" in out.lower()

    # Baseline now records clean; a subsequent clean run is silent.
    rc = cron._emit(_result(should_alert=False, drift=0.01), verbose=False, auto_refit=False)
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_emit_verbose_forces_full_picture_even_when_unchanged(cron, monkeypatch, tmp_path, capsys):
    """--verbose bypasses the gate: full DRIFT RESULT even on a standing-clean state."""
    baseline = tmp_path / "baseline.json"
    baseline.write_text('{"should_alert": false}')
    monkeypatch.setattr(cron, "_BASELINE", baseline)
    rc = cron._emit(_result(should_alert=False, drift=0.01), verbose=True, auto_refit=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRIFT RESULT" in out


def test_emit_updates_baseline(cron, monkeypatch, tmp_path):
    """Every run rewrites the baseline so a transition fires exactly once."""
    baseline = tmp_path / "baseline.json"
    monkeypatch.setattr(cron, "_BASELINE", baseline)
    cron._emit(_result(should_alert=True, drift=0.4), verbose=False, auto_refit=False)
    import json
    assert json.loads(baseline.read_text())["should_alert"] is True
    cron._emit(_result(should_alert=False, drift=0.01), verbose=False, auto_refit=False)
    assert json.loads(baseline.read_text())["should_alert"] is False


# ---------------------------------------------------------------------------
# main() end-to-end (no network — run_drift_check / collection stubbed)
# ---------------------------------------------------------------------------


def _stub_main_deps(cron, monkeypatch, *, result: DriftResult):
    monkeypatch.setattr(cron, "_source_alpaca_env", lambda: None)
    monkeypatch.setattr(cron, "_load_universe", lambda top: ["AAPL", "MSFT"])
    monkeypatch.setattr(cron, "_collect_pairs", lambda *a, **k: ([0.6], [True]))
    # Stub the module-level symbol that main() imports locally.
    monkeypatch.setattr(
        "hermes_quant.training.calibrator_drift.run_drift_check",
        lambda **kwargs: result,
    )


def test_main_silent_when_in_tolerance(cron, monkeypatch, tmp_path, capsys):
    """In-tolerance / unchanged since last run => empty stdout (the M11 fix)."""
    baseline = tmp_path / "baseline.json"
    baseline.write_text('{"should_alert": false}')
    monkeypatch.setattr(cron, "_BASELINE", baseline)
    _stub_main_deps(cron, monkeypatch, result=_result(should_alert=False, drift=0.01))
    monkeypatch.setattr(sys, "argv", ["prog"])
    rc = cron.main()
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_main_first_run_clean_is_silent(cron, monkeypatch, tmp_path, capsys):
    """No baseline yet + in-tolerance => still silent (no spam on a fresh cron)."""
    monkeypatch.setattr(cron, "_BASELINE", tmp_path / "baseline.json")
    _stub_main_deps(cron, monkeypatch, result=_result(should_alert=False, drift=0.01))
    monkeypatch.setattr(sys, "argv", ["prog"])
    rc = cron.main()
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_main_emits_on_drift_over_threshold(cron, monkeypatch, tmp_path, capsys):
    """Drift over threshold (clean->drift transition) => non-empty headline."""
    baseline = tmp_path / "baseline.json"
    baseline.write_text('{"should_alert": false}')
    monkeypatch.setattr(cron, "_BASELINE", baseline)
    _stub_main_deps(cron, monkeypatch, result=_result(should_alert=True, drift=0.4))
    monkeypatch.setattr(sys, "argv", ["prog"])
    rc = cron.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert out != ""
    assert "ALERT: calibrator drift" in out
