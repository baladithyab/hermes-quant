"""hermes_quant.agents.risk_committee — 3-way risk debate (ADR-0043).

Wave 3: Aggressive / Conservative / Neutral personas debate a TraderProposal
in a round-robin loop. v0.1 deterministic rules; v0.2 LLM wiring deferred
behind ``llm_caller`` plumbing.

The committee can only SILENCE (multiplier ≤ 1.0), never AMPLIFY (CV5
anti-pattern guard). The deterministic risk gate (ADR-0004) remains the
final authority — committee runs BEFORE the gate and only ever reduces
size.
"""

from __future__ import annotations

from hermes_quant.agents.risk_committee.committee import (
    RiskCommittee,
    RiskCommitteeTurn,
    RiskDebateSummary,
)
from hermes_quant.agents.risk_committee.personas import (
    AggressivePersona,
    ConservativePersona,
    NeutralPersona,
    RiskPersona,
)

__all__ = [
    "AggressivePersona",
    "ConservativePersona",
    "NeutralPersona",
    "RiskCommittee",
    "RiskCommitteeTurn",
    "RiskDebateSummary",
    "RiskPersona",
]
