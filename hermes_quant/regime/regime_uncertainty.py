"""hermes_quant.regime.regime_uncertainty — ADR-0096 Gate 4: regime uncertainty propagation.

The regime LABEL is a single high-leverage input (keys analyst weights + gate rules +
calibration). Hard-label classifiers like HMMs lag at regime turns. This module provides
a PURE soft-factor that widens toward SILENCE when the posterior is ambiguous (high
entropy), giving downstream committee weighting and gate rules a continuous uncertainty
signal rather than a hard switch.

Given the regime POSTERIOR (a probability distribution over N regime states — e.g. from
regime/hmm.py predict_proba() or regime/detector.py confidence estimates), compute:

    regime_factor ∈ [0, 1]

such that:
    - A confident (peaked) posterior → factor near 1.0  (full committee weight)
    - A uniform (max-entropy) posterior → factor near 0.0 (silence / suppress weight)
    - A degenerate / NaN posterior → factor = 0.0        (fail-CLOSED, silence)

The factor is 1 - H_normalized, where H_normalized is the Shannon entropy of the
posterior normalized by the maximum possible entropy (log(N) for N states).

Usage (additive / DEFAULT-OFF):
    A future weighting path will multiply per-analyst committee weights by
    regime_factor when HERMES_QUANT_REGIME_UNCERTAINTY=1. Nothing consumes this
    factor yet; this module is additive plumbing only.

    from hermes_quant.regime.regime_uncertainty import compute_regime_factor
    factor = compute_regime_factor(posterior)          # np.ndarray of shape (N,)

ADR reference: ADR-0096 Gate 4 "Regime Uncertainty Propagation".
"""

from __future__ import annotations

import logging
import math
from typing import Sequence

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Minimum number of regime states in a valid posterior.
_MIN_STATES: int = 1

#: Floor below which a probability mass is treated as zero for entropy purposes.
#: Prevents log(0) without distorting the distribution materially.
_PROB_FLOOR: float = 1e-15

#: When a degenerate or NaN posterior is detected, return this factor (SILENCE).
_DEGENERATE_FACTOR: float = 0.0

# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------


def compute_regime_factor(
    posterior: "np.ndarray | Sequence[float]",
) -> float:
    """Compute a regime confidence factor in [0, 1] from a regime posterior.

    The factor equals 1 - H_normalized, where H_normalized is the Shannon
    entropy of the posterior divided by log(N) (the maximum entropy for N
    states). A peaked posterior has low entropy → high factor. A uniform
    posterior has maximum entropy → factor near 0.

    Finite-guard invariant (ar08 / ar09 NaN-defeats-every-comparison family):
        - NaN/inf in any element → returns 0.0 (fail-CLOSED, silence).
        - All-zero posterior → returns 0.0 (degenerate → silence).
        - Negative values → returns 0.0 (not a valid probability vector → silence).
        - Single-state posterior with any finite non-negative value → returns 1.0
          (no ambiguity possible with one state).

    Args:
        posterior: 1-D array-like of N non-negative floats representing the
            probability distribution over N regime states. Need not be
            pre-normalized (will be normalized internally).

    Returns:
        float in [0.0, 1.0]:
            1.0 means fully confident (all mass on one state),
            0.0 means maximally ambiguous (uniform) or degenerate / NaN input.
    """
    # -- Convert to numpy array --
    try:
        arr = np.asarray(posterior, dtype=float)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "compute_regime_factor: could not convert posterior to array (%s); "
            "returning SILENCE factor=0.0",
            exc,
        )
        return _DEGENERATE_FACTOR

    # -- Shape guard: must be 1-D --
    arr = arr.flatten()
    n = int(arr.size)

    if n < _MIN_STATES:
        logger.warning(
            "compute_regime_factor: empty posterior (size=%d); returning SILENCE", n
        )
        return _DEGENERATE_FACTOR

    # -- Finite guard: NaN / inf anywhere → silence (fail-CLOSED) --
    if not np.all(np.isfinite(arr)):
        logger.warning(
            "compute_regime_factor: non-finite values in posterior; returning SILENCE"
        )
        return _DEGENERATE_FACTOR

    # -- Negativity guard: negative probability mass is degenerate → silence --
    if np.any(arr < 0.0):
        logger.warning(
            "compute_regime_factor: negative probability mass in posterior; "
            "returning SILENCE"
        )
        return _DEGENERATE_FACTOR

    # -- All-zero guard: cannot normalize → degenerate → silence --
    total = float(arr.sum())
    if total <= 0.0 or not math.isfinite(total):
        logger.warning(
            "compute_regime_factor: posterior sums to %r (zero or non-finite); "
            "returning SILENCE",
            total,
        )
        return _DEGENERATE_FACTOR

    # -- Normalize to a proper probability distribution --
    p = arr / total  # shape (n,)

    # -- Single-state: no ambiguity possible → fully confident --
    if n == 1:
        return 1.0

    # -- Shannon entropy: H = -sum(p_i * log(p_i)), with p_i floored to avoid log(0) --
    p_safe = np.maximum(p, _PROB_FLOOR)
    entropy = float(-np.sum(p_safe * np.log(p_safe)))

    # Clamp entropy to [0, log(n)] to guard against floating-point rounding that
    # could produce a tiny negative or a value infinitesimally above log(n).
    max_entropy = math.log(n)
    entropy = max(0.0, min(entropy, max_entropy))

    # -- Normalize entropy: H_normalized = H / H_max --
    h_normalized = entropy / max_entropy  # in [0, 1]

    # -- Regime factor: high entropy → low factor (silence) --
    factor = 1.0 - h_normalized

    # Final clamp for float rounding safety
    factor = max(0.0, min(1.0, factor))

    logger.debug(
        "compute_regime_factor: n_states=%d total_mass=%.6f entropy=%.6f "
        "max_entropy=%.6f h_norm=%.6f factor=%.6f",
        n,
        total,
        entropy,
        max_entropy,
        h_normalized,
        factor,
    )
    return factor


# ---------------------------------------------------------------------------
# Convenience: factor from a single-state hard label (degenerate one-hot)
# ---------------------------------------------------------------------------


def regime_factor_from_hard_label(n_states: int, state_index: int) -> float:
    """Compute regime_factor for a hard (one-hot) label posterior.

    This is a convenience wrapper for callers that have a hard Viterbi label
    (e.g. HMMClassifier.classify's integer state index) but not a soft
    posterior. A hard label has entropy = 0 → factor = 1.0.

    Args:
        n_states: total number of states in the model.
        state_index: index of the predicted state (0-indexed).

    Returns:
        1.0 if the inputs are valid (a hard label is never ambiguous);
        0.0 if the inputs are degenerate (invalid n_states or state_index).
    """
    if n_states < 1 or not (0 <= state_index < n_states):
        logger.warning(
            "regime_factor_from_hard_label: invalid n_states=%d, state_index=%d; "
            "returning SILENCE",
            n_states,
            state_index,
        )
        return _DEGENERATE_FACTOR

    one_hot = np.zeros(n_states, dtype=float)
    one_hot[state_index] = 1.0
    return compute_regime_factor(one_hot)
