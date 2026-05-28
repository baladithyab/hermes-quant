"""hermes_quant.agents.research_debate — Bull/Bear adversarial debate stage.

ADR-0065 (Wave v0.6.1, G1+G6+G13+G17): real adversarial back-and-forth between
Bull and Bear researchers, judged by a deep-tier ResearchManager that emits a
strongly-typed ResearchPlan with a 5-tier PortfolioRating recommendation.

Public API:
    PortfolioRating  (StrEnum, 5 tiers, signed_intensity ∈ {-2,-1,0,1,2})
    ResearchPlan     (Pydantic; recommendation + confidence + rationale)
    InvestDebateState (Pydantic; mutable state across alternation loop)
    run_research_debate(...)  — stage entry point
    DEFAULT_MAX_ROUNDS, MAX_ALLOWED_ROUNDS  (env-clamping constants)
    RESEARCH_DEBATE_FLAG_ENV_VAR, RESEARCH_ROUNDS_ENV_VAR

The stage is feature-flagged at v0.6.1 via ``HERMES_QUANT_RESEARCH_DEBATE=1``
(default OFF — bit-identical to v0.6.0 when off). Round count is capped at
``MAX_ALLOWED_ROUNDS=3`` and read from ``HERMES_QUANT_RESEARCH_DEBATE_ROUNDS``.
"""

from __future__ import annotations

from hermes_quant.agents.research_debate.schemas import (
    InvestDebateState,
    PortfolioRating,
    ResearchPlan,
)
from hermes_quant.agents.research_debate.stage import (
    CONVERSATIONAL_PREAMBLE,
    DEFAULT_MAX_ROUNDS,
    MAX_ALLOWED_ROUNDS,
    RESEARCH_DEBATE_AUDIT_KIND,
    RESEARCH_DEBATE_FLAG_ENV_VAR,
    RESEARCH_ROUNDS_ENV_VAR,
    _resolve_max_rounds,
    run_research_debate,
)

__all__ = [
    "CONVERSATIONAL_PREAMBLE",
    "DEFAULT_MAX_ROUNDS",
    "MAX_ALLOWED_ROUNDS",
    "RESEARCH_DEBATE_AUDIT_KIND",
    "RESEARCH_DEBATE_FLAG_ENV_VAR",
    "RESEARCH_ROUNDS_ENV_VAR",
    "InvestDebateState",
    "PortfolioRating",
    "ResearchPlan",
    "_resolve_max_rounds",
    "run_research_debate",
]
