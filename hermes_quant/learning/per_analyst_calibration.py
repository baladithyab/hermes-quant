"""f254 — per-analyst Beta-shrinkage calibration.

The system's only *learning* calibrator is a single global IsotonicCalibrator
whose bootstrap fit merges every analyst's (raw, correct) pairs into one curve
("per-analyst calibrators are out of scope" — bootstrap_calibrator.py). A
skilled analyst's confidence is therefore shrunk toward the population average,
dragging it down; an unskilled analyst is flattered upward.

This module routes each analyst's raw confidence through a calibrator keyed by
that analyst's OWN learned Beta(alpha, beta) directional-accuracy posterior —
the same posteriors BMA already tracks in ``_stats`` and c96e now persists. It
uses exactly the ADR-0009 §P0-2 cold-start formula, but with the analyst's
learned prior in place of the fixed Beta(2, 5):

    calibrate(raw) = (raw + alpha) / (1 + alpha + beta)

Properties:
  - Skilled analyst (alpha >> beta): same raw maps HIGHER.
  - Unskilled analyst (beta >> alpha): same raw maps LOWER.
  - No history (alpha, beta == prior): maps near the prior mean — a safe,
    non-zero fallback that never crashes and never silently zeroes an analyst.
  - With the canonical (2, 5) prior and zero history it is byte-identical to the
    existing ColdStartCalibrator, so an unseen analyst is calibrated exactly as
    today.

Pure-Python, deterministic, offline. No sklearn.
"""

from __future__ import annotations

import numpy as np


def beta_shrinkage_calibrate(raw_score: float, *, alpha: float, beta: float) -> float:
    """Map a raw confidence to a calibrated probability using a Beta posterior.

    ``alpha`` / ``beta`` are the analyst's learned Beta directional-accuracy
    posterior parameters (alpha = prior_alpha + recency-weighted n_correct,
    beta = prior_beta + recency-weighted n_incorrect). The result is clipped to
    [0, 1] for numerical safety.
    """
    raw = float(np.clip(raw_score, 0.0, 1.0))
    denom = 1.0 + float(alpha) + float(beta)
    if denom <= 0.0:
        # Degenerate priors should never reach here, but never divide by zero on
        # the decision path — fall back to the neutral raw passthrough.
        return raw
    return float(np.clip((raw + float(alpha)) / denom, 0.0, 1.0))
