"""hermes_quant.evaluation — Walk-forward CV, lookahead enforcement, DSR.

Per ADR-0019 + AGENTS.md target tree. Three modules:

- cv.PurgedWalkForward — train/val/test splits with embargo (López de Prado)
- lookahead.shuffle_timestamps_test — CI gate that fails if an analyst
  performs better than chance on shuffled timestamps
- dsr.deflated_sharpe — Bailey & López de Prado 2014 false-discovery hedge

v0.3 ships these as scaffolding; v0.4 RL training is the primary consumer
of cv.py and dsr.py. lookahead.py is active today — `tests/test_no_lookahead.py`
imports `shuffle_timestamps_test` from here.
"""

from .cv import PurgedWalkForward, WalkForwardSplit
from .dsr import deflated_sharpe
from .lookahead import LookaheadTestResult, shuffle_timestamps_test
from .validation import (
    BootstrapCI,
    PermutationResult,
    ValidationReport,
    validate_returns,
)

__all__ = [
    "PurgedWalkForward",
    "WalkForwardSplit",
    "LookaheadTestResult",
    "shuffle_timestamps_test",
    "deflated_sharpe",
    "validate_returns",
    "ValidationReport",
    "BootstrapCI",
    "PermutationResult",
]
