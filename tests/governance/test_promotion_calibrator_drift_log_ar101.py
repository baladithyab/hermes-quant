"""ar101 — promotion gate must derive calibrator_drift_max from the drift-log.jsonl
(the only real producer), not a never-emitted promotion_event payload field.

Sibling of ar100. `_collect_metrics` read `calibrator_drift` out of a `promotion_event`
payload, but the ONLY producer of a drift magnitude — `training.calibrator_drift.
append_drift_log` — writes its own `~/.hermes/quant/calibrators/drift-log.jsonl` plane,
never a governance promotion_event. So `calibrator_drift_max` stayed at its 0.0 default
and the `evaluate()` block `calibrator_drift_max > max_calibrator_drift` was VACUOUS — a
strategy with a drifted calibrator was NOT blocked from paper->live promotion (fail-OPEN).

FIX (ar101): read the drift-log directly within the 30d window (best-effort, 0.0 on any
failure; byte-identical when absent). This file exercises `_collect_metrics` /
`_max_calibrator_drift_in_window` directly with an injected drift-log path so it needs no
live audit log.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from hermes_quant.governance import promotion


@pytest.fixture
def asof() -> datetime:
    return datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC)


def _write_drift_log(path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_ar101_real_fn_reads_injected_log(tmp_path, monkeypatch, asof):
    """The REAL _max_calibrator_drift_in_window reads DRIFT_LOG_PATH — repoint it and
    confirm it surfaces the max in-window drift + ignores out-of-window rows."""
    log = tmp_path / "drift-log.jsonl"
    _write_drift_log(log, [
        {"schema_version": 1, "asof": (asof - timedelta(days=3)).isoformat(), "drift": 0.20},
        {"schema_version": 1, "asof": (asof - timedelta(days=45)).isoformat(), "drift": 0.99},  # out of 30d window
        {"schema_version": 1, "asof": (asof - timedelta(days=10)).isoformat(), "drift": -0.15},  # abs -> 0.15
    ])
    monkeypatch.setattr("hermes_quant.training.calibrator_drift.DRIFT_LOG_PATH", log)
    got = promotion._max_calibrator_drift_in_window(asof - timedelta(days=30), asof)
    assert got == pytest.approx(0.20), f"max in-window abs drift = 0.20 (got {got}); 0.99 is out of window"


def test_ar101_absent_log_is_zero_byte_identical(tmp_path, monkeypatch, asof):
    """Non-vacuity / byte-identity: a missing drift-log yields 0.0 — exactly the prior
    behavior, so a deployment without the log promotes exactly as before."""
    log = tmp_path / "does-not-exist.jsonl"
    monkeypatch.setattr("hermes_quant.training.calibrator_drift.DRIFT_LOG_PATH", log)
    got = promotion._max_calibrator_drift_in_window(asof - timedelta(days=30), asof)
    assert got == 0.0


def test_ar101_corrupt_row_tolerated(tmp_path, monkeypatch, asof):
    """A torn/corrupt trailing line must not crash the gate (best-effort read)."""
    log = tmp_path / "drift-log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        json.dumps({"schema_version": 1, "asof": (asof - timedelta(days=2)).isoformat(), "drift": 0.07})
        + "\n{ this is a torn line\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("hermes_quant.training.calibrator_drift.DRIFT_LOG_PATH", log)
    got = promotion._max_calibrator_drift_in_window(asof - timedelta(days=30), asof)
    assert got == pytest.approx(0.07), "the good row must still be read past a torn trailing line"
