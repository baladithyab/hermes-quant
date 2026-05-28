"""Pydantic schemas for the Bull/Bear adversarial debate stage (ADR-0065).

This module defines:
  * ``PortfolioRating`` — 5-tier StrEnum rating produced by the ResearchManager
    judge. ``StrEnum`` (not plain ``Enum``) so JSON serialisation preserves the
    label across runtime per ADR-0058 label-stability invariant.
  * ``ResearchPlan`` — the typed output contract the stage emits and the Trader
    consumes. ``overrules_baseline`` is INTENTIONALLY DROPPED relative to the
    inline ``ResearchPlan`` in ``llm_committee.py`` — the deterministic risk
    gate (ADR-0004) is the only authority for direction-vs-baseline disputes;
    surfacing the bool at this layer encouraged the LLM to lawyer it.
  * ``InvestDebateState`` — Pydantic ``BaseModel`` (NOT ``TypedDict``) state
    object mutated across the alternation loop. Pydantic is required because
    ``governance/audit_log.py`` consumers expect ``.model_dump()`` and we do
    one ``model_dump()`` at the end of the stage to populate the audit row.

Backward-compat: this is a NEW module at v0.6.1. It does not import from
``llm_committee.py`` to avoid a circular dependency (committee re-imports
``ResearchPlan`` from here when wiring the stage; see ADR-0065 §Implementation).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Re-export BullBearTurn from the existing module so callers of this stage have
# one symbol-set for all debate-related types. Importing inside a TYPE_CHECKING
# guard would still force runtime resolution at field-validation time, so we
# do a plain runtime import. This module is imported AFTER ``llm_committee``'s
# top-level work is done because the wiring lives in ``stage.py`` (which imports
# more), not here.
from hermes_quant.aggregators.llm_committee import BullBearTurn

__all__ = [
    "BullBearTurn",
    "InvestDebateState",
    "PortfolioRating",
    "ResearchPlan",
]


class PortfolioRating(StrEnum):
    """5-tier portfolio rating produced by the ResearchManager judge.

    StrEnum (Python 3.11+) gives free JSON serialisation:
        json.dumps({"rec": PortfolioRating.BUY}) → '{"rec": "BUY"}'
    and round-trips identically across restarts (label-stability per ADR-0058).

    Order is intentional: the index of each member reflects directional
    intensity, mapped to ``signed_intensity`` for risk-gate consumption.
    """

    SELL = "SELL"
    UNDERWEIGHT = "UNDERWEIGHT"
    HOLD = "HOLD"
    OVERWEIGHT = "OVERWEIGHT"
    BUY = "BUY"

    @property
    def signed_intensity(self) -> int:
        """Map to deterministic signed intensity in [-2, +2].

        * BUY        → +2  (aggressive long)
        * OVERWEIGHT → +1  (long lean)
        * HOLD       →  0  (neutral)
        * UNDERWEIGHT → -1 (short lean)
        * SELL       → -2  (aggressive short)
        """
        return {
            "SELL": -2,
            "UNDERWEIGHT": -1,
            "HOLD": 0,
            "OVERWEIGHT": 1,
            "BUY": 2,
        }[self.value]


class ResearchPlan(BaseModel):
    """Output contract of the ResearchDebateStage (ADR-0065).

    Replaces the inline 5-tier ``Literal`` recommendation in
    ``llm_committee.ResearchPlan``. The prior ``overrules_baseline: bool``
    field is DROPPED — the deterministic risk gate (ADR-0004) is the only
    authority for direction-vs-baseline disputes.

    Strict schema: ``extra='forbid'`` rejects unknown fields, including the
    legacy ``overrules_baseline`` bool. This is a deliberate breaking change
    pinned by ``test_research_plan_dropped_overrules_baseline``.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    recommendation: PortfolioRating
    confidence: float = Field(..., ge=0.0, le=1.0)
    rationale: str = Field(..., min_length=1, max_length=6000)
    strategic_actions: str = Field(..., min_length=1, max_length=4000)
    horizon_emphasis: Literal["1d", "1w", "1M"] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class InvestDebateState(BaseModel):
    """Mirrors TauricResearch's InvestDebateState as a Pydantic model.

    Mutated in-place across turns by ``run_research_debate``. After the stage
    completes the model is ``.model_dump()``ed into the audit log. Pydantic
    (rather than ``TypedDict``) is mandatory because the audit row reader
    expects a dict that round-trips back through ``model_validate``.

    Hermes-only fields (not in TauricResearch):
        * ``bull_turns`` / ``bear_turns`` — per-turn structured records so the
          journal does not lose the per-side confidence floats.
        * ``terminated_reason`` — termination tag for the audit row.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    bull_history: str = ""
    bear_history: str = ""
    history: str = ""
    current_response: str = ""
    judge_decision: ResearchPlan | None = None
    count: int = Field(default=0, ge=0)

    # Hermes-only — structured per-turn records.
    bull_turns: list[BullBearTurn] = Field(default_factory=list)
    bear_turns: list[BullBearTurn] = Field(default_factory=list)

    # Termination metadata. Free-form on top of the Literal so we can record
    # ``"exception:<TypeName>"`` for unexpected aborts without blowing the
    # whole stage up.
    terminated_reason: str = "max_rounds_reached"
