"""hermes_quant.research — Hypothesis Registry + Run Card artifacts (Wave 8a / ADR-0048).

Public API
----------
Hypothesis           : Pydantic model for a falsifiable hypothesis.
HypothesisRegistry   : Append-only JSONL registry of hypotheses + status transitions.
RunCard              : Pydantic model for a post-run evidence artifact.
RunCardLog           : Append-only JSONL log of run cards.
HypothesisRunner     : Orchestrator: open→running→evaluate→write RunCard→validate/falsify.
AppendOnlyViolation  : Raised on any attempt to mutate the registries.

The research-autopilot pattern prevents post-hoc rationalisation:
  1. Register a HYPOTHESIS with concrete falsifiable criteria *before* running.
  2. After running, emit a RUN CARD that records evidence + verdict.
  3. Both stores are append-only — no cherry-picking, no silent loss erasure.

See ADR-0048 for full design rationale.
"""

from hermes_quant.research.hypothesis import (
    AppendOnlyViolation,
    Hypothesis,
    HypothesisRegistry,
)
from hermes_quant.research.run_card import RunCard, RunCardLog
from hermes_quant.research.orchestrator import HypothesisRunner

__all__ = [
    "AppendOnlyViolation",
    "Hypothesis",
    "HypothesisRegistry",
    "RunCard",
    "RunCardLog",
    "HypothesisRunner",
]
