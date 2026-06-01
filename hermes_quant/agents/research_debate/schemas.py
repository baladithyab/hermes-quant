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

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    "StructureIntent",
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


class StructureIntent(StrEnum):
    """Coarse, advisory multi-leg STRUCTURE intent the debate may PROPOSE (ADR-0082 Part B).

    This is the contract field only — *intent* granularity, NOT legs/Greeks/
    strikes. The bull/bear/judge can argue a qualitative structural stance
    (e.g. "thesis is range-bound → prefer premium capture"); the deterministic
    structure-selection table (``options/structure_select.py``, a SEPARATE
    seed) + the ``options_gate`` decide the actual ``StrategyKind`` and legs.
    The LLM NEVER picks legs and ``structure_intent`` is NEVER a money-path
    lever: it is advisory input to a downstream deterministic selector.

    ``StrEnum`` (not plain ``Enum``) for the same reason as ``PortfolioRating``:
    free, label-stable JSON serialisation across restarts (ADR-0058).

    Members mirror ADR-0082 §"Part B" exactly:
        * ``NONE``                → no structure preference → today's equity path.
          The silence-by-default member; absent/ambiguous deliberation defaults
          here so no option structure is ever implied by omission.
        * ``DEFINED_RISK_CREDIT`` → defined-risk credit stance (e.g. credit spread).
        * ``DEFINED_RISK_DEBIT``  → defined-risk debit stance (e.g. debit spread).
        * ``PREMIUM_CAPTURE``     → income/premium-selling stance (e.g. CC / CSP).
        * ``LONG_PREMIUM``        → long-volatility / long-premium stance.

    NOTHING in this seed consumes these members; they exist so the debate
    schema can CARRY the intent. Wiring to the selector/gate is Part B + a
    later seed. Out-of-table / non-defined-risk intents resolve to silence in
    that downstream layer, never here.
    """

    NONE = "none"
    DEFINED_RISK_CREDIT = "defined_risk_credit"
    DEFINED_RISK_DEBIT = "defined_risk_debit"
    PREMIUM_CAPTURE = "premium_capture"
    LONG_PREMIUM = "long_premium"


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

    @field_validator("recommendation", mode="before")
    @classmethod
    def _normalize_case(cls, v):  # noqa: ANN001
        # ADR-0065 v0.6.1-fix-C3: accept case-insensitive PortfolioRating strings on the wire.
        # Tauric-style LLM judges sometimes emit "Buy"/"buy"; PortfolioRating is upper-cased.
        if isinstance(v, str):
            return v.upper()
        return v

    recommendation: PortfolioRating
    confidence: float = Field(..., ge=0.0, le=1.0)
    rationale: str = Field(..., min_length=1, max_length=6000)
    strategic_actions: str = Field(..., min_length=1, max_length=4000)
    horizon_emphasis: Literal["1d", "1w", "1M"] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    # ADR-0080 W7 (default-OFF): the reserved ADR-0002/0003 counterarguments
    # field, left UNFILLED until W7. Populated from the standing Socratic
    # devil's-advocate turn (advisory plane only — never mutates direction,
    # magnitude, confidence, the gate, or any limit). Defaults to None so the
    # off-state is byte-identical (extra='forbid' requires it be declared).
    counterarguments: str | None = Field(default=None, max_length=4000)

    # ADR-0082 Part B (additive, advisory): the COARSE multi-leg structure intent
    # the deliberation may PROPOSE. Optional with default None so existing plans
    # parse and round-trip byte-identically (absence ≡ ``StructureIntent.NONE`` ≡
    # today's equity path; silence-by-default). ``extra='forbid'`` requires it be
    # declared. This seed adds ONLY the contract field: NOTHING consumes it yet.
    # The deterministic structure-selection table (separate seed) + the
    # options_gate decide the actual StrategyKind/legs — the LLM NEVER picks legs
    # and this is NEVER a money-path lever (it is advisory input to a downstream
    # deterministic selector, ADR-0082 D-1).
    structure_intent: StructureIntent | None = None


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

    # ADR-0080 W7 (default-OFF): standing Socratic devil's-advocate turn outcome
    # (the ADVISORY PLANE only — beliefs/telemetry, never direction/size/gate).
    # The red-team turn attacks the REASONING of the leading view AFTER the
    # judge forms it. ``red_team_turn`` is the BullBearTurn-shaped critique;
    # ``dissent_surfaced`` is a deterministic (NOT a vote) flag the operator/
    # daily-report can see; ``dissent_reason`` carries the strongest objection.
    # All three default to the off-state (None / False / "") so when the flag
    # is OFF the dumped state is byte-identical except these defaulted keys, and
    # the audit row only emits the red_team block when the turn actually ran.
    red_team_turn: BullBearTurn | None = None
    dissent_surfaced: bool = False
    dissent_reason: str = ""
