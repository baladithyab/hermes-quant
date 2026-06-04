"""f254 — per-analyst Beta-shrinkage calibration.

Today the only *learning* calibrator is a single global one whose bootstrap fit
merges every analyst's samples into one isotonic curve, so a skilled analyst's
confidence is dragged toward the population average. The per-analyst calibrator
keys off the analyst's OWN learned Beta(alpha, beta) posterior, using exactly the
ADR-0009 cold-start formula but with the analyst's learned prior instead of the
fixed Beta(2, 5):

    calibrate(raw) = (raw + alpha) / (1 + alpha + beta)

So the same raw score maps to a HIGHER calibrated probability for a skilled
analyst and a LOWER one for an unskilled analyst. An analyst with no history
(alpha, beta == prior) maps near the prior mean — a safe, non-zero fallback.

Pure-Python, offline, deterministic.
"""

from __future__ import annotations

import pytest

from hermes_quant.learning.per_analyst_calibration import beta_shrinkage_calibrate


def test_skilled_analyst_maps_same_raw_higher_than_unskilled():
    raw = 0.5
    skilled = beta_shrinkage_calibrate(raw, alpha=15.0, beta=5.0)
    unskilled = beta_shrinkage_calibrate(raw, alpha=5.0, beta=15.0)
    assert skilled > unskilled
    # Explicit values from the ADR-0009 formula:
    assert skilled == pytest.approx((0.5 + 15.0) / (1.0 + 15.0 + 5.0))   # ≈ 0.738
    assert unskilled == pytest.approx((0.5 + 5.0) / (1.0 + 5.0 + 15.0))  # ≈ 0.262


def test_neutral_prior_maps_near_one_half():
    """An analyst with the symmetric prior (no skill signal yet) lands near 0.5
    — never zero, never a crash."""
    c = beta_shrinkage_calibrate(0.5, alpha=5.0, beta=5.0)
    assert c == pytest.approx((0.5 + 5.0) / (1.0 + 5.0 + 5.0))  # 5.5/11 = 0.5


def test_output_bounded_unit_interval():
    for raw in (-3.0, 0.0, 0.3, 1.0, 9.0):
        c = beta_shrinkage_calibrate(raw, alpha=15.0, beta=5.0)
        assert 0.0 <= c <= 1.0


def test_zero_history_falls_back_to_codebase_coldstart_when_prior_is_2_5():
    """With the canonical ADR-0009 cold-start prior (2, 5), the per-analyst
    formula reduces EXACTLY to the existing ColdStartCalibrator — so a never-seen
    analyst is calibrated identically to today (additive, no regression)."""
    from hermes_quant.calibrators import ColdStartCalibrator

    cold = ColdStartCalibrator()
    for raw in (0.0, 0.2, 0.5, 0.8, 1.0):
        assert beta_shrinkage_calibrate(raw, alpha=2.0, beta=5.0) == pytest.approx(
            cold.calibrate(raw)
        )


def test_deterministic():
    a = beta_shrinkage_calibrate(0.42, alpha=9.0, beta=3.0)
    b = beta_shrinkage_calibrate(0.42, alpha=9.0, beta=3.0)
    assert a == b
