"""tests/regime/test_detector.py — Wave 7 regime detector tests."""
from __future__ import annotations

import pandas as pd
import pytest

from hermes_quant.regime.detector import (
    BEAR_TREND_MAX,
    BEAR_VOL_MAX,
    BULL_TREND_MIN,
    BULL_VOL_MAX,
    VOLATILE_VOL_MIN,
    RegimeDetector,
    RegimeState,
)
from hermes_quant.regime.state_variables import StateVariables


# ---------------------------------------------------------------------------
# Helper: build StateVariables with explicit values
# ---------------------------------------------------------------------------


def _sv(
    vol_pct: float,
    trend: float | None = 0.0,
    *,
    slope: float | None = None,
    vol_60d: float = 0.15,
) -> StateVariables:
    return StateVariables(
        realized_vol_60d=vol_60d,
        realized_vol_percentile=vol_pct,
        yield_curve_slope=slope,
        trend_strength=trend,
        as_of=pd.Timestamp("2026-01-02", tz="UTC"),
    )


# ---------------------------------------------------------------------------
# Instantiation
# ---------------------------------------------------------------------------


def test_detector_default_init():
    det = RegimeDetector()
    assert det.hmm_classifier is None


def test_detector_status():
    det = RegimeDetector()
    s = det.status()
    assert s["version"] == "0.1"
    assert s["classifier"] == "rule_based"
    assert s["hmm_classifier_wired"] is False


# ---------------------------------------------------------------------------
# VOLATILE rule
# ---------------------------------------------------------------------------


def test_volatile_fires_on_high_vol():
    det = RegimeDetector()
    state, reason = det.classify(_sv(vol_pct=0.75, trend=0.6))
    assert state == RegimeState.VOLATILE
    assert "VOLATILE" in reason


def test_volatile_fires_at_exact_boundary_above():
    det = RegimeDetector()
    # > 0.70 → VOLATILE
    state, _ = det.classify(_sv(vol_pct=VOLATILE_VOL_MIN + 1e-9, trend=0.6))
    assert state == RegimeState.VOLATILE


def test_volatile_overrides_strong_bull_trend():
    """High vol should always win — VOLATILE overrides BULL condition."""
    det = RegimeDetector()
    state, _ = det.classify(_sv(vol_pct=0.80, trend=2.0))
    assert state == RegimeState.VOLATILE


# ---------------------------------------------------------------------------
# BULL rule
# ---------------------------------------------------------------------------


def test_bull_fires():
    det = RegimeDetector()
    state, reason = det.classify(_sv(vol_pct=0.40, trend=0.8))
    assert state == RegimeState.BULL
    assert "BULL" in reason


def test_bull_at_exact_thresholds():
    det = RegimeDetector()
    # trend == BULL_TREND_MIN (0.5) AND vol_pct == BULL_VOL_MAX (0.6) → BULL
    state, _ = det.classify(_sv(vol_pct=BULL_VOL_MAX, trend=BULL_TREND_MIN))
    assert state == RegimeState.BULL


# ---------------------------------------------------------------------------
# BEAR rule
# ---------------------------------------------------------------------------


def test_bear_fires():
    det = RegimeDetector()
    state, reason = det.classify(_sv(vol_pct=0.50, trend=-0.8))
    assert state == RegimeState.BEAR
    assert "BEAR" in reason


def test_bear_at_exact_thresholds():
    det = RegimeDetector()
    # trend == BEAR_TREND_MAX (-0.5) AND vol_pct == BEAR_VOL_MAX (0.7) → BEAR
    state, _ = det.classify(_sv(vol_pct=BEAR_VOL_MAX, trend=BEAR_TREND_MAX))
    assert state == RegimeState.BEAR


def test_bear_suppressed_when_vol_too_high():
    """When vol_pct > BEAR_VOL_MAX but <= VOLATILE threshold, BEAR should NOT fire
    (there's no matching rule → UNKNOWN)."""
    det = RegimeDetector()
    # vol_pct = 0.71 > BEAR_VOL_MAX=0.7 BUT < VOLATILE=0.7 (actually 0.71 > 0.70 = VOLATILE)
    # so VOLATILE wins at 0.71
    state, _ = det.classify(_sv(vol_pct=0.71, trend=-0.8))
    assert state == RegimeState.VOLATILE


# ---------------------------------------------------------------------------
# UNKNOWN cases
# ---------------------------------------------------------------------------


def test_unknown_on_moderate_conditions():
    det = RegimeDetector()
    # trend=0.0 (no trend) + vol_pct=0.50 (moderate) → UNKNOWN
    state, reason = det.classify(_sv(vol_pct=0.50, trend=0.0))
    assert state == RegimeState.UNKNOWN
    assert "UNKNOWN" in reason


def test_unknown_when_trend_is_none():
    det = RegimeDetector()
    state, reason = det.classify(_sv(vol_pct=0.50, trend=None))
    assert state == RegimeState.UNKNOWN
    assert "trend_strength is None" in reason


def test_unknown_on_boundary_neither_bull_nor_bear():
    det = RegimeDetector()
    # trend=0.3 < BULL_TREND_MIN=0.5 and vol_pct=0.5 → no rule → UNKNOWN
    state, _ = det.classify(_sv(vol_pct=0.50, trend=0.3))
    assert state == RegimeState.UNKNOWN


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_classifier_is_deterministic():
    det = RegimeDetector()
    sv = _sv(vol_pct=0.45, trend=0.7)
    results = {det.classify(sv)[0] for _ in range(10)}
    assert len(results) == 1, "Classifier must be deterministic"


# ---------------------------------------------------------------------------
# HMM hook (v0.2 plumbing)
# ---------------------------------------------------------------------------


def test_hmm_classifier_hook_overrides_rules():
    """When hmm_classifier returns BEAR it should override the rule-based BULL result."""
    def always_bear(_sv):
        return RegimeState.BEAR

    det = RegimeDetector(hmm_classifier=always_bear)
    state, reason = det.classify(_sv(vol_pct=0.40, trend=0.8))
    assert state == RegimeState.BEAR
    assert reason == "hmm_classifier"


def test_hmm_classifier_exception_falls_back_to_rules():
    def bad_hmm(_sv):
        raise RuntimeError("HMM broken")

    det = RegimeDetector(hmm_classifier=bad_hmm)
    # Should fall back to rule-based and return BULL (trend=0.8, vol_pct=0.40)
    state, _ = det.classify(_sv(vol_pct=0.40, trend=0.8))
    assert state == RegimeState.BULL
