"""hermes_quant.agents.risk_committee.committee — RiskCommittee orchestrator.

ADR-0043 (Wave 3): Round-robin debate among Aggressive / Conservative /
Neutral risk personas operating on a TraderProposal.

Invariants:
  * silence_multiplier starts at 1.0 and can ONLY DECREASE.
  * "amplify" critiques are RECORDED in the trail but DO NOT raise the
    multiplier above 1.0 (CV5 anti-pattern guard, gap #1).
  * Each "silence" critique multiplies silence_multiplier by 0.5.
  * The deterministic risk gate (ADR-0004) is the final authority; this
    committee runs BEFORE the gate and only ever reduces size.

v0.1 deterministic — no LLM call. v0.2 LLM wiring is deferred behind the
``llm_caller`` parameter (same pattern as Wave 4 Reflector).
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from hermes_quant.agents.risk_committee.personas import (
    AggressivePersona,
    ConservativePersona,
    NeutralPersona,
    RiskPersona,
    _PersonaDecision,
)
from hermes_quant.agents.trader import TraderProposal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# TauricResearch should_continue_risk_analysis style: count < 3 * max_rounds.
DEFAULT_MAX_ROUNDS: int = 1
MAX_ALLOWED_ROUNDS: int = 3
ROUNDS_ENV_VAR: str = "HERMES_QUANT_RISK_ROUNDS"

# Each silence vote multiplies the silence_multiplier by this factor.
_SILENCE_FACTOR: float = 0.5

# Persona execution order within a round (TauricResearch convention).
_PERSONA_ORDER: tuple[str, ...] = ("aggressive", "conservative", "neutral")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class RiskCommitteeTurn(BaseModel):
    """A single persona's critique within a debate round."""

    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    persona: str = Field(..., min_length=1, max_length=64)
    turn_index: int = Field(..., ge=0)
    critique_text: str = Field(..., min_length=1, max_length=2048)
    evidence_ids: list[str] = Field(default_factory=list, max_length=32)
    risk_assessment: Literal["amplify", "silence", "neutral"]
    confidence: float = Field(..., ge=0.0, le=1.0)


class RiskDebateSummary(BaseModel):
    """Aggregate output of one risk-committee debate."""

    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    trader_proposal_id: str = Field(..., min_length=1, max_length=128)
    turns: list[RiskCommitteeTurn] = Field(default_factory=list, max_length=32)
    silence_multiplier: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description=(
            "Multiplier applied to TraderProposal.size_fraction. Starts at "
            "1.0; only ever DECREASES via silence votes. Never exceeds 1.0 "
            "(CV5 anti-pattern guard)."
        ),
    )
    final_recommendation: str = Field(..., min_length=1, max_length=1024)
    n_rounds: int = Field(..., ge=0, le=MAX_ALLOWED_ROUNDS)
    terminated_reason: str = Field(..., min_length=1, max_length=256)


# ---------------------------------------------------------------------------
# RiskCommittee orchestrator
# ---------------------------------------------------------------------------


class RiskCommittee:
    """3-way risk debate (Aggressive / Conservative / Neutral).

    Args:
        personas: Optional explicit (Aggressive, Conservative, Neutral) tuple.
            Defaults to fresh instances of each.
        llm_caller: Optional callable for v0.2 LLM-driven debate. v0.1 is
            deterministic and does NOT use this parameter — it is reserved
            so that the public API does not change when LLM wiring lands.
            Signature: (system_prompt, user_prompt) -> str (the persona's
            critique text).

    Usage:
        committee = RiskCommittee()
        summary = committee.debate(trader_proposal, plan)
    """

    def __init__(
        self,
        personas: tuple[RiskPersona, RiskPersona, RiskPersona] | None = None,
        *,
        llm_caller: Callable[[str, str], str] | None = None,
    ) -> None:
        if personas is None:
            personas = (
                AggressivePersona(),
                ConservativePersona(),
                NeutralPersona(),
            )
        if len(personas) != 3:
            raise ValueError("RiskCommittee requires exactly 3 personas.")
        self._personas: dict[str, RiskPersona] = {p.name: p for p in personas}
        # Validate the trio matches the canonical persona names.
        missing = set(_PERSONA_ORDER) - set(self._personas)
        if missing:
            raise ValueError(
                f"RiskCommittee personas missing canonical names: {missing!r}"
            )
        self._llm_caller = llm_caller  # reserved for v0.2

    # ------------------------------------------------------------------

    def debate(
        self,
        trader_proposal: TraderProposal,
        plan: dict[str, Any],
        *,
        max_rounds: int | None = None,
        proposal_id: str | None = None,
    ) -> RiskDebateSummary:
        """Run the round-robin debate and return a RiskDebateSummary.

        Args:
            trader_proposal: The TraderProposal under critique.
            plan: The research plan dict (recommendation, confidence,
                rationale, ...). Used by personas for context.
            max_rounds: Optional override. Defaults to env-var
                HERMES_QUANT_RISK_ROUNDS (default 1, max 3).
            proposal_id: Optional stable identifier. If None, a uuid4 is
                generated so the summary has a non-empty id.
        """
        rounds = self._resolve_max_rounds(max_rounds)
        pid = proposal_id or f"prop-{uuid.uuid4().hex[:12]}"

        turns: list[RiskCommitteeTurn] = []
        silence_multiplier: float = 1.0
        max_turns = 3 * rounds  # TauricResearch should_continue_risk_analysis
        terminated_reason = "max_rounds_reached"

        # Round-robin loop.
        try:
            count = 0
            while count < max_turns:
                persona_name = _PERSONA_ORDER[count % 3]
                persona = self._personas[persona_name]
                decision = self._invoke_persona(
                    persona=persona,
                    proposal=trader_proposal,
                    plan=plan,
                    prior_turns=turns,
                )
                turn = RiskCommitteeTurn(
                    persona=persona_name,
                    turn_index=count,
                    critique_text=decision.critique_text,
                    evidence_ids=list(decision.evidence_ids),
                    risk_assessment=decision.risk_assessment,
                    confidence=decision.confidence,
                )
                turns.append(turn)

                # CV5 anti-pattern guard: only "silence" mutates the
                # multiplier; "amplify" and "neutral" are audit-only.
                if decision.risk_assessment == "silence":
                    silence_multiplier *= _SILENCE_FACTOR

                count += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "RiskCommittee.debate aborted (%s) after %d turns; "
                "returning partial summary.",
                exc,
                len(turns),
                exc_info=True,
            )
            terminated_reason = f"exception:{type(exc).__name__}"

        # CV5 invariant: silence_multiplier must be in [0.0, 1.0].
        silence_multiplier = max(0.0, min(1.0, silence_multiplier))

        n_rounds_completed = len(turns) // 3 + (1 if len(turns) % 3 else 0)
        n_rounds_completed = min(n_rounds_completed, MAX_ALLOWED_ROUNDS)

        final_recommendation = self._compose_final_recommendation(
            silence_multiplier=silence_multiplier,
            turns=turns,
        )

        return RiskDebateSummary(
            trader_proposal_id=pid,
            turns=turns,
            silence_multiplier=silence_multiplier,
            final_recommendation=final_recommendation,
            n_rounds=n_rounds_completed,
            terminated_reason=terminated_reason,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_max_rounds(explicit: int | None) -> int:
        """Resolve effective max_rounds from explicit arg or env var."""
        if explicit is not None:
            n = int(explicit)
        else:
            raw = os.environ.get(ROUNDS_ENV_VAR)
            if raw is None or raw == "":
                n = DEFAULT_MAX_ROUNDS
            else:
                try:
                    n = int(raw)
                except (TypeError, ValueError):
                    logger.warning(
                        "%s=%r is not an int; falling back to default %d",
                        ROUNDS_ENV_VAR,
                        raw,
                        DEFAULT_MAX_ROUNDS,
                    )
                    n = DEFAULT_MAX_ROUNDS
        return max(1, min(MAX_ALLOWED_ROUNDS, n))

    def _invoke_persona(
        self,
        *,
        persona: RiskPersona,
        proposal: TraderProposal,
        plan: dict[str, Any],
        prior_turns: list[RiskCommitteeTurn],
    ) -> _PersonaDecision:
        """v0.1: deterministic. v0.2 will branch on self._llm_caller."""
        # NOTE: self._llm_caller is reserved for v0.2; v0.1 always uses the
        # deterministic decision rule. We call persona.decide() directly so
        # that test doubles can subclass + override that single method.
        return persona.decide(proposal, plan, prior_turns)

    @staticmethod
    def _compose_final_recommendation(
        *,
        silence_multiplier: float,
        turns: list[RiskCommitteeTurn],
    ) -> str:
        """Compose a human-readable final recommendation."""
        n_silence = sum(1 for t in turns if t.risk_assessment == "silence")
        n_amplify = sum(1 for t in turns if t.risk_assessment == "amplify")

        if silence_multiplier <= 0.0:
            return (
                "Risk committee SILENCED the trade entirely "
                f"(silence_multiplier={silence_multiplier:.2f}, "
                f"silence_votes={n_silence}). "
                "Action overridden to HOLD."
            )
        if silence_multiplier < 1.0:
            return (
                f"Risk committee reduced size to {silence_multiplier:.2f}× "
                f"the trader's proposed sizing ({n_silence} silence "
                f"vote(s), {n_amplify} amplify vote(s) — amplify votes "
                "audit-only per ADR-0043)."
            )
        return (
            f"Risk committee approved at full size "
            f"({n_amplify} amplify vote(s) recorded for audit but never "
            "raise the multiplier above 1.0 per ADR-0043)."
        )
