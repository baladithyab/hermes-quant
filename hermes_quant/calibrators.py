"""hermes_quant.calibrators — Confidence calibration (ADR-0009 §P0-2).

Three implementations of the Calibrator Protocol:

- IdentityCalibrator: passthrough; raw == calibrated. For testing only.
- ColdStartCalibrator: max(0, raw - 0.20). Used until N>=200 fitted samples.
- IsotonicCalibrator: sklearn's IsotonicRegression on (raw, direction_correct)
  pairs. Production. Lazy sklearn import.

Per ADR-0002 + ADR-0009 §P0-2: confidence is a CALIBRATED probability of
directional correctness in [0, 1]. Until a fitted calibrator exists with
N>=200 samples, use ColdStartCalibrator (which conservatively shrinks raw
confidence by 0.20).

The risk gate uses calibrated confidence in cost-gate + Kelly. Drift is
detected by comparing fitted calibrator's E[direction_correct | calibrated]
against the calibrated probability — surfaced in quant_doctor.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np

from hermes_quant.protocol import CalibratorNotReady

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# IdentityCalibrator (testing)
# ---------------------------------------------------------------------------

class IdentityCalibrator:
    """Passthrough — raw == calibrated. Tests only."""

    name = "identity"
    n_samples = 0
    is_calibrated = True

    def calibrate(self, raw_score: float) -> float:
        return float(np.clip(raw_score, 0.0, 1.0))

    def fit(self, raw_scores: Any, direction_correct: Any) -> None:
        # No-op
        self.n_samples = len(raw_scores) if raw_scores is not None else 0

    def status(self) -> dict:
        return {"name": self.name, "is_calibrated": True, "n_samples": self.n_samples}


# ---------------------------------------------------------------------------
# ColdStartCalibrator (used pre-fit per ADR-0002 + §P0-2)
# ---------------------------------------------------------------------------

class ColdStartCalibrator:
    """max(0, raw - 0.20). Conservative shrinkage until enough samples exist.

    Per ADR-0002 + ADR-0009 §P0-2:
        Until a fitted calibrator exists with N >= 200 samples, confidence =
        max(0, raw - 0.20).

    The 0.20 shrinkage is intentional over-pessimism: it prevents an
    over-confident untrained analyst from triggering large positions, while
    still allowing strong raw signals to clear the cost-gate threshold.
    """

    name = "cold_start"
    n_samples = 0
    is_calibrated = False
    shrinkage = 0.20

    def calibrate(self, raw_score: float) -> float:
        return float(max(0.0, raw_score - self.shrinkage))

    def fit(self, raw_scores: Any, direction_correct: Any) -> None:
        # Cold-start ignores fit; switch to IsotonicCalibrator when ready.
        self.n_samples = len(raw_scores) if raw_scores is not None else 0

    def status(self) -> dict:
        return {
            "name": self.name,
            "is_calibrated": False,
            "n_samples": self.n_samples,
            "shrinkage": self.shrinkage,
        }


# ---------------------------------------------------------------------------
# IsotonicCalibrator (production)
# ---------------------------------------------------------------------------

class IsotonicCalibrator:
    """Production calibrator. sklearn isotonic regression.

    fit(raw_scores, direction_correct) where:
      - raw_scores: array-like of pre-calibration raw scores in [0, 1]
      - direction_correct: array-like of bool (was the analyst's direction right?)

    calibrate(raw_score) returns the isotonic regression's predicted P(correct | raw).

    Fitting requires N >= n_min_samples (default 200). Below that, callers
    should use ColdStartCalibrator.

    sklearn is imported lazily so plugin install without [stacking] extra
    doesn't fail.
    """

    name = "isotonic"
    n_min_samples = 200

    def __init__(self):
        self._model: Any = None
        self.n_samples = 0
        self.is_calibrated = False

    def _import_sklearn(self):
        try:
            from sklearn.isotonic import IsotonicRegression
        except ImportError as e:
            raise CalibratorNotReady(
                "sklearn not installed; install hermes-quant[stacking]"
            ) from e
        return IsotonicRegression

    def calibrate(self, raw_score: float) -> float:
        if not self.is_calibrated or self._model is None:
            raise CalibratorNotReady(
                f"isotonic calibrator not fitted (n_samples={self.n_samples})"
            )
        # Clip output to [0, 1] just in case
        out = float(self._model.predict([raw_score])[0])
        return float(np.clip(out, 0.0, 1.0))

    def fit(self, raw_scores: Any, direction_correct: Any) -> None:
        IsotonicRegression = self._import_sklearn()
        x = np.asarray(raw_scores, dtype=float)
        y = np.asarray(direction_correct, dtype=float)  # bool → 0/1
        if len(x) != len(y):
            raise ValueError(
                f"length mismatch: raw_scores={len(x)} direction_correct={len(y)}"
            )
        if len(x) < self.n_min_samples:
            raise CalibratorNotReady(
                f"isotonic needs n_samples >= {self.n_min_samples}, got {len(x)}"
            )

        model = IsotonicRegression(
            out_of_bounds="clip",  # extrapolate to 0/1 outside training range
            y_min=0.0,
            y_max=1.0,
        )
        model.fit(x, y)
        self._model = model
        self.n_samples = len(x)
        self.is_calibrated = True

    def status(self) -> dict:
        return {
            "name": self.name,
            "is_calibrated": self.is_calibrated,
            "n_samples": self.n_samples,
            "n_min_samples": self.n_min_samples,
        }
