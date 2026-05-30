"""tests/unit/test_calibrator_drift.py — B11 calibrator drift detection.

No network — uses IdentityCalibrator + synthetic pairs. The refit path passes
pairs= directly and monkeypatches bootstrap_calibrator so no Alpaca is hit.
"""

from __future__ import annotations

import json

from hermes_quant.calibrators import IdentityCalibrator
from hermes_quant.training import calibrator_drift as cd


def _correct_seq(n: int, frac_true: float) -> list[bool]:
    n_true = round(n * frac_true)
    return [True] * n_true + [False] * (n - n_true)


def test_no_samples_no_alert():
    result = cd.compute_drift(IdentityCalibrator(), [], [])
    assert result.drift == 0.0
    assert result.should_alert is False
    assert result.reason == "no_samples"


def test_well_calibrated_no_alert():
    n = 100
    raw = [0.6] * n
    correct = _correct_seq(n, 0.60)
    result = cd.compute_drift(IdentityCalibrator(), raw, correct)
    assert abs(result.predicted_mean - 0.6) < 1e-6
    assert abs(result.realized_mean - 0.6) < 1e-6
    assert result.drift < 0.05
    assert result.should_alert is False


def test_drift_above_threshold_alerts():
    n = 100
    raw = [0.9] * n
    correct = _correct_seq(n, 0.50)
    result = cd.compute_drift(IdentityCalibrator(), raw, correct)
    assert result.drift > 0.05
    assert abs(result.drift - 0.4) < 1e-6
    assert result.should_alert is True


def test_refit_recommended_requires_min_samples():
    n = 50  # < 200
    raw = [0.9] * n
    correct = _correct_seq(n, 0.50)
    result = cd.compute_drift(
        IdentityCalibrator(), raw, correct, min_refit_samples=200
    )
    assert result.should_alert is True
    assert result.refit_recommended is False


def test_custom_threshold_honored():
    n = 100
    raw = [0.9] * n
    correct = _correct_seq(n, 0.50)
    result = cd.compute_drift(IdentityCalibrator(), raw, correct, threshold=0.5)
    assert result.should_alert is False


def test_append_drift_log_writes_one_row(tmp_path):
    n = 10
    result = cd.compute_drift(IdentityCalibrator(), [0.6] * n, _correct_seq(n, 0.6))
    log_path = tmp_path / "drift-log.jsonl"
    cd.append_drift_log(result, path=log_path)
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert "schema_version" in row
    assert "drift" in row
    assert "should_alert" in row
    assert "asof" in row


def test_run_drift_check_no_refit_when_flag_off(tmp_path, monkeypatch):
    # Build a live pickle path (so _load_calibrator succeeds) and a sentinel
    # bootstrap_calibrator that fails if called.
    import pickle

    cal_path = tmp_path / "isotonic.pkl"
    with open(cal_path, "wb") as fh:
        pickle.dump(IdentityCalibrator(), fh)
    mtime_before = cal_path.stat().st_mtime

    def _boom(**kwargs):
        raise AssertionError("bootstrap_calibrator must NOT be called when auto_refit=False")

    monkeypatch.setattr(
        "hermes_quant.training.bootstrap_calibrator.bootstrap_calibrator", _boom
    )

    n = 300
    raw = [0.9] * n
    correct = _correct_seq(n, 0.50)
    result = cd.run_drift_check(
        calibrator_path=cal_path,
        pairs=(raw, correct),
        auto_refit=False,
        drift_log_path=tmp_path / "drift-log.jsonl",
    )
    assert result.should_alert is True
    assert result.refit_recommended is True
    # live pickle untouched
    assert cal_path.stat().st_mtime == mtime_before


def test_run_drift_check_refit_when_recommended(tmp_path, monkeypatch):
    import pickle

    cal_path = tmp_path / "isotonic.pkl"
    with open(cal_path, "wb") as fh:
        pickle.dump(IdentityCalibrator(), fh)

    calls = []

    def _fake_bootstrap(**kwargs):
        calls.append(kwargs)
        return {"fitted": True, "n_samples": kwargs.get("min_samples", 200)}

    monkeypatch.setattr(
        "hermes_quant.training.bootstrap_calibrator.bootstrap_calibrator",
        _fake_bootstrap,
    )

    n = 300
    raw = [0.9] * n
    correct = _correct_seq(n, 0.50)
    log_path = tmp_path / "drift-log.jsonl"
    result = cd.run_drift_check(
        calibrator_path=cal_path,
        pairs=(raw, correct),
        auto_refit=True,
        refit_kwargs={"symbols": ["AAPL"]},
        drift_log_path=log_path,
    )
    assert result.refit_recommended is True
    assert len(calls) == 1
    row = json.loads(log_path.read_text().strip().splitlines()[-1])
    assert row["refit"] is True


def test_compute_drift_pure_deterministic():
    n = 80
    raw = [0.7] * n
    correct = _correct_seq(n, 0.55)
    r1 = cd.compute_drift(IdentityCalibrator(), raw, correct)
    r2 = cd.compute_drift(IdentityCalibrator(), raw, correct)
    assert r1 == r2
