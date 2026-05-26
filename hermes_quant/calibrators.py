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
    """Bayesian Beta(alpha=2, beta=5) weak prior. Used until enough fitted samples exist.

    Per ADR-0002 + ADR-0009 §P0-2 (amended 2026-05-26):

    Original: confidence = max(0, raw - 0.20).
    Amended : confidence = (raw + alpha) / (1 + alpha + beta), alpha=2, beta=5.

    Why amended: the original `max(0, raw - 0.20)` shrinkage created a deadlock.
    Typical 2-of-4-agreement signals from ClassicalTAAnalyst produce raw≈0.20,
    which the original formula floored to exactly 0.0, silencing every symbol.
    Until 200 settled trades existed, no signal could clear the cost gate; but
    the system needed signals to clear the gate to ever get to 200 trades. The
    cold-start path was a permanent silencer, not a warm-start.

    The Beta(2,5) replacement is still skeptical (prior mean = 2/(2+5) ≈ 0.286),
    but treats `raw` as a single observation rather than punishing it. It maps:
        raw=0.0  → 0.250  (still below cost-gate threshold)
        raw=0.20 → 0.275  (clears 2-analyst silence-bias gate at min_conf=0.65? no, but readable)
        raw=0.50 → 0.313
        raw=1.0  → 0.375
    A 4-of-4 agreement (raw ≈ 1.0 * mean_sub_conf) at sub_conf=0.7 → raw=0.7 →
    calibrated=0.338, which is still below the 0.65 silence-bias threshold but
    no longer zero — meaning the cost gate can see it and the journal can
    accumulate samples toward fitting an IsotonicCalibrator.

    Pessimism is preserved: the highest possible cold-start confidence is 0.375
    (raw=1.0), so an untrained analyst alone cannot trigger autonomous fires
    (which require min_confidence=0.65 per ADR-0016 §D2). Once the calibrator
    accumulates N>=200 samples and switches to IsotonicCalibrator, the full
    confidence range becomes available.

    This unblocks the feedback loop: agreement signals pass through → executions
    accumulate → calibrator fits → real confidence emerges. ADR-0009 amendment
    docs/diagnostics/2026-05-26-no-conviction-bimodal-pattern.md.
    """

    name = "cold_start"
    n_samples = 0
    is_calibrated = False
    # Beta prior parameters (kept as instance/class attrs so tests and the
    # IsotonicCalibrator switchover can introspect them).
    prior_alpha = 2.0
    prior_beta = 5.0

    def calibrate(self, raw_score: float) -> float:
        # Beta posterior with single observation: (raw + alpha) / (1 + alpha + beta)
        raw = float(np.clip(raw_score, 0.0, 1.0))
        return float((raw + self.prior_alpha) / (1.0 + self.prior_alpha + self.prior_beta))

    def fit(self, raw_scores: Any, direction_correct: Any) -> None:
        # Cold-start ignores fit; switch to IsotonicCalibrator when ready.
        self.n_samples = len(raw_scores) if raw_scores is not None else 0

    def status(self) -> dict:
        return {
            "name": self.name,
            "is_calibrated": False,
            "n_samples": self.n_samples,
            "prior_alpha": self.prior_alpha,
            "prior_beta": self.prior_beta,
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
            raise CalibratorNotReady("sklearn not installed; install hermes-quant[stacking]") from e
        return IsotonicRegression

    def calibrate(self, raw_score: float) -> float:
        if not self.is_calibrated or self._model is None:
            raise CalibratorNotReady(f"isotonic calibrator not fitted (n_samples={self.n_samples})")
        # Clip output to [0, 1] just in case
        out = float(self._model.predict([raw_score])[0])
        return float(np.clip(out, 0.0, 1.0))

    def fit(self, raw_scores: Any, direction_correct: Any) -> None:
        IsotonicRegression = self._import_sklearn()
        x = np.asarray(raw_scores, dtype=float)
        y = np.asarray(direction_correct, dtype=float)  # bool → 0/1
        if len(x) != len(y):
            raise ValueError(f"length mismatch: raw_scores={len(x)} direction_correct={len(y)}")
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
