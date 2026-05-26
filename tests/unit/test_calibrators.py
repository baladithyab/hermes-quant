"""Unit tests for hermes_quant.calibrators."""

from __future__ import annotations

import numpy as np
import pytest

from hermes_quant.calibrators import (
    ColdStartCalibrator,
    IdentityCalibrator,
    IsotonicCalibrator,
)
from hermes_quant.protocol import CalibratorNotReady


class TestProtocolContract:
    def test_identity_satisfies_protocol(self):
        # Note: Calibrator is not @runtime_checkable in protocol.py for now;
        # we test behavioral interface instead
        c = IdentityCalibrator()
        assert hasattr(c, "calibrate")
        assert hasattr(c, "fit")
        assert hasattr(c, "status")
        assert isinstance(c.is_calibrated, bool)
        assert isinstance(c.n_samples, int)

    def test_cold_start_behavioral_contract(self):
        c = ColdStartCalibrator()
        assert hasattr(c, "calibrate")
        assert hasattr(c, "fit")
        assert hasattr(c, "status")


class TestIdentity:
    def test_passthrough(self):
        c = IdentityCalibrator()
        assert c.calibrate(0.7) == 0.7

    def test_clips_to_unit_interval(self):
        c = IdentityCalibrator()
        assert c.calibrate(1.5) == 1.0
        assert c.calibrate(-0.1) == 0.0


class TestColdStart:
    def test_beta_prior_default_alpha2_beta5(self):
        """ADR-0009 §P0-2 amendment 2026-05-26: cold-start uses Beta(2,5) prior.

        Formula: (raw + alpha) / (1 + alpha + beta) = (raw + 2) / 8.
        """
        c = ColdStartCalibrator()
        assert c.prior_alpha == 2.0
        assert c.prior_beta == 5.0
        assert c.calibrate(0.8) == pytest.approx(0.35)  # (0.8 + 2)/8 = 0.35
        assert c.calibrate(0.5) == pytest.approx(0.3125)  # (0.5 + 2)/8 = 0.3125

    def test_clamps_at_unit_interval(self):
        """Beta posterior never escapes [0.25, 0.375] for raw in [0, 1].

        Replaces the legacy clamp-to-zero test: the new formula does NOT
        zero out small raws (that was the deadlock bug) but its output is
        still bounded inside a sub-interval that respects silence-by-default.
        """
        c = ColdStartCalibrator()
        # raw=0.0 → 0.25 (was 0.0 under shrinkage formula); raw=-0.5 clipped to 0 → 0.25
        assert c.calibrate(0.1) == pytest.approx(0.2625)
        assert c.calibrate(0.0) == pytest.approx(0.25)
        assert c.calibrate(-0.5) == pytest.approx(0.25)
        # raw=1.5 clipped to 1.0 → 0.375; the cap stays below silence-bias min_conf=0.65
        assert c.calibrate(1.5) == pytest.approx(0.375)
        assert c.calibrate(2.0) == pytest.approx(0.375)

    def test_status_reports_not_calibrated(self):
        c = ColdStartCalibrator()
        s = c.status()
        assert s["is_calibrated"] is False
        assert s["prior_alpha"] == 2.0
        assert s["prior_beta"] == 5.0


class TestIsotonic:
    def test_unfitted_calibrate_raises(self):
        c = IsotonicCalibrator()
        with pytest.raises(CalibratorNotReady):
            c.calibrate(0.7)

    def test_below_min_samples_raises(self):
        c = IsotonicCalibrator()
        with pytest.raises(CalibratorNotReady):
            c.fit([0.5, 0.6, 0.7], [True, False, True])

    @pytest.mark.requires_network  # actually requires sklearn but we use this marker for "skippable on minimal install"
    def test_fitted_calibrate_returns_probabilities(self):
        """Requires sklearn. Uses our extras-aware test marker."""
        try:
            import sklearn  # noqa
        except ImportError:
            pytest.skip("sklearn not installed")

        c = IsotonicCalibrator()
        # Synthesize a calibrated dataset: when raw=0.6, P(correct)=0.55, etc.
        rng = np.random.default_rng(42)
        n = 500
        raw = rng.uniform(0, 1, n)
        # Generate direction_correct with probability matching raw (perfect calibration)
        correct = rng.uniform(0, 1, n) < raw
        c.fit(raw, correct)
        assert c.is_calibrated
        # At raw=0.5, calibrated should be near 0.5
        cal = c.calibrate(0.5)
        assert 0.3 < cal < 0.7

    def test_length_mismatch_raises(self):
        try:
            import sklearn  # noqa
        except ImportError:
            pytest.skip("sklearn not installed")
        c = IsotonicCalibrator()
        with pytest.raises(ValueError):
            c.fit([0.1, 0.2], [True, False, True])  # mismatch
