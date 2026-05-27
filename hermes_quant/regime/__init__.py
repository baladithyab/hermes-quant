"""hermes_quant.regime — Wave 7 regime detection and per-regime BMA weight priors.

Public API (rule-based v0.1):
  - StateVariables dataclass + compute_state_variables()
  - RegimeState enum (BULL | BEAR | VOLATILE | UNKNOWN)
  - RegimeDetector.classify()
  - RegimeWeightTable + DEFAULT_REGIME_WEIGHTS
  - apply_regime_weights(), load_regime_weights(), save_regime_weights()

Reference: Mantshimuli & Mwamba, "Hidden Markov Bayesian Model Averaging for
Financial Returns", Springer 2026.

v0.1 uses a deterministic rule-based classifier.
v0.2 will wire in the HMM from the paper (plumbing hook: RegimeDetector accepts
an optional hmm_classifier callable).
"""

from hermes_quant.regime.detector import RegimeDetector, RegimeState
from hermes_quant.regime.per_regime_weights import (
    DEFAULT_REGIME_WEIGHTS,
    RegimeWeightTable,
    apply_regime_weights,
    load_regime_weights,
    save_regime_weights,
)
from hermes_quant.regime.state_variables import StateVariables, compute_state_variables

__all__ = [
    # state variables
    "StateVariables",
    "compute_state_variables",
    # detector
    "RegimeState",
    "RegimeDetector",
    # per-regime weights
    "RegimeWeightTable",
    "DEFAULT_REGIME_WEIGHTS",
    "apply_regime_weights",
    "load_regime_weights",
    "save_regime_weights",
]
