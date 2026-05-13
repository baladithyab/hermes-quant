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
    def test_shrinkage_default_020(self):
        c = ColdStartCalibrator()
        assert c.shrinkage == 0.20
        assert c.calibrate(0.8) == pytest.approx(0.6)
        assert c.calibrate(0.5) == pytest.approx(0.3)

    def test_clamps_at_zero(self):
        c = ColdStartCalibrator()
        assert c.calibrate(0.1) == 0.0
        assert c.calibrate(0.0) == 0.0
        assert c.calibrate(-0.5) == 0.0

    def test_status_reports_not_calibrated(self):
        c = ColdStartCalibrator()
        s = c.status()
        assert s["is_calibrated"] is False
        assert s["shrinkage"] == 0.20


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
