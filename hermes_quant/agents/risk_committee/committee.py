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

v0.1 deterministic — no LLM call. v0.2 LLM wiring is feature-flagged via
HERMES_QUANT_RISK_COMMITTEE_LLM=1 (default OFF). Each persona's turn is
routed through LLMCaller with structured RiskCommitteeTurn output. Partial
fallback per persona: if one persona's LLM call fails, other personas can
still go through LLM. CV5 anti-amplify invariant is enforced outside the
LLM scope — the wrapper clamps silence_multiplier in [0.0, 1.0] regardless
of what any LLM returns.

See ADR-0056 for the full wiring decision record.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any, Callable, Literal, Optional, TYPE_CHECKING

from pydantic import BaseModel, Field

from hermes_quant.agents.risk_committee.personas import (
    AggressivePersona,
    ConservativePersona,
    NeutralPersona,
    RiskPersona,
    _PersonaDecision,
)
from hermes_quant.agents.trader import TraderProposal

if TYPE_CHECKING:
    from hermes_quant.agents.llm_caller import LLMCaller

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level audit helper (lazily resolved — never crashes at import time)
# ---------------------------------------------------------------------------


def _audit_append(kind: str, source: str, payload: dict) -> None:
    """Thin wrapper so tests can patch 'committee._audit_append' directly."""
    try:
        from hermes_quant.agents.llm_caller import _audit_append as _impl
        _impl(kind, source, payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("RiskCommittee: _audit_append failed (%s); continuing.", exc)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# TauricResearch should_continue_risk_analysis style: count < 3 * max_rounds.
DEFAULT_MAX_ROUNDS: int = 1
MAX_ALLOWED_ROUNDS: int = 3
ROUNDS_ENV_VAR: str = "HERMES_QUANT_RISK_ROUNDS"

# Feature flag for v0.2 LLM path (default OFF — same discipline as TraderNodeLLM).
_LLM_FLAG_ENV_VAR: str = "HERMES_QUANT_RISK_COMMITTEE_LLM"

# Each silence vote multiplies the silence_multiplier by this factor.
_SILENCE_FACTOR: float = 0.5

# Persona execution order within a round (TauricResearch convention).
_PERSONA_ORDER: tuple[str, ...] = ("aggressive", "conservative", "neutral")

# Audit-log event kind for risk committee LLM calls.
_RISK_COMMITTEE_AUDIT_KIND: str = "risk_committee_llm_call"


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
        llm_caller: Optional LLMCaller instance for v0.2 LLM-driven debate.
            v0.1 is deterministic and does NOT use this parameter.
            When provided (and HERMES_QUANT_RISK_COMMITTEE_LLM=1), each
            persona's turn is routed through the LLM; partial fallback to
            v0.1 occurs per-persona on any LLM failure (ADR-0056).

    Usage:
        committee = RiskCommittee()
        summary = committee.debate(trader_proposal, plan)
    """

    def __init__(
        self,
        personas: tuple[RiskPersona, RiskPersona, RiskPersona] | None = None,
        *,
        llm_caller: Optional["LLMCaller"] = None,
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
        self._llm_caller = llm_caller  # None → v0.1 deterministic only

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

        Routes to v0.2 LLM debate when:
          (a) self._llm_caller is not None
          (b) self._llm_caller.available() returns True
          (c) HERMES_QUANT_RISK_COMMITTEE_LLM=1

        Falls back to v0.1 deterministic when any condition is not met.

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

        # Route to v0.2 LLM path when feature-flagged and caller is ready.
        if self._should_use_llm():
            return self._debate_with_llm(
                trader_proposal,
                plan,
                max_rounds=rounds,
                proposal_id=pid,
            )

        return self._debate_deterministic(
            trader_proposal,
            plan,
            max_rounds=rounds,
            proposal_id=pid,
        )

    # ------------------------------------------------------------------
    # v0.1 deterministic debate
    # ------------------------------------------------------------------

    def _debate_deterministic(
        self,
        proposal: TraderProposal,
        plan: dict[str, Any],
        *,
        max_rounds: int,
        proposal_id: str,
    ) -> RiskDebateSummary:
        """Pure v0.1 deterministic round-robin debate."""
        turns: list[RiskCommitteeTurn] = []
        silence_multiplier: float = 1.0
        max_turns = 3 * max_rounds  # TauricResearch should_continue_risk_analysis
        terminated_reason = "max_rounds_reached"

        # Round-robin loop.
        try:
            count = 0
            while count < max_turns:
                persona_name = _PERSONA_ORDER[count % 3]
                persona = self._personas[persona_name]
                decision = self._invoke_persona(
                    persona=persona,
                    proposal=proposal,
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
            trader_proposal_id=proposal_id,
            turns=turns,
            silence_multiplier=silence_multiplier,
            final_recommendation=final_recommendation,
            n_rounds=n_rounds_completed,
            terminated_reason=terminated_reason,
        )

    # ------------------------------------------------------------------
    # v0.2 LLM debate
    # ------------------------------------------------------------------

    def _debate_with_llm(
        self,
        proposal: TraderProposal,
        plan: dict[str, Any],
        *,
        max_rounds: int,
        proposal_id: str,
    ) -> RiskDebateSummary:
        """v0.2 LLM-driven round-robin debate with per-persona partial fallback.

        ADR-0056: Each persona's turn is independently routed to the LLM.
        If a single persona's LLM call fails (returns None or raises),
        only that persona falls back to v0.1 deterministic for THIS turn;
        other personas continue on the LLM path.

        CV5 anti-amplify invariant is enforced outside the LLM scope:
        silence_multiplier is clamped in [0.0, 1.0] after every turn
        regardless of which path produced the turn.
        """
        turns: list[RiskCommitteeTurn] = []
        silence_multiplier: float = 1.0
        max_turns = 3 * max_rounds
        terminated_reason = "max_rounds_reached"

        proposal_json = json.dumps(proposal.model_dump(), default=str)
        plan_json = json.dumps(plan, default=str)

        try:
            count = 0
            while count < max_turns:
                persona_name = _PERSONA_ORDER[count % 3]
                persona = self._personas[persona_name]

                turn, turn_path = self._invoke_persona_llm(
                    persona=persona,
                    proposal=proposal,
                    plan=plan,
                    prior_turns=turns,
                    turn_index=count,
                    proposal_json=proposal_json,
                    plan_json=plan_json,
                )
                turns.append(turn)

                # CV5 anti-pattern guard — invariant lives HERE, outside LLM scope.
                # The LLM cannot raise silence_multiplier above 1.0 because this
                # structural enforcement is the only place it changes.
                if turn.risk_assessment == "silence":
                    silence_multiplier *= _SILENCE_FACTOR

                # Per-turn audit record (ADR-0056).
                _audit_append(
                    kind=_RISK_COMMITTEE_AUDIT_KIND,
                    source="hermes_quant.agents.risk_committee.committee",
                    payload={
                        "proposal_id": proposal_id,
                        "persona": persona_name,
                        "turn_index": count,
                        "path": turn_path,
                        "risk_assessment": turn.risk_assessment,
                        "confidence": turn.confidence,
                        "silence_multiplier_after": round(
                            max(0.0, min(1.0, silence_multiplier)), 6
                        ),
                    },
                )

                count += 1

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "RiskCommittee._debate_with_llm aborted (%s) after %d turns; "
                "returning partial summary.",
                exc,
                len(turns),
                exc_info=True,
            )
            terminated_reason = f"exception:{type(exc).__name__}"

        # CV5 invariant: final clamp — always enforced regardless of path.
        silence_multiplier = max(0.0, min(1.0, silence_multiplier))

        n_rounds_completed = len(turns) // 3 + (1 if len(turns) % 3 else 0)
        n_rounds_completed = min(n_rounds_completed, MAX_ALLOWED_ROUNDS)

        final_recommendation = self._compose_final_recommendation(
            silence_multiplier=silence_multiplier,
            turns=turns,
        )

        return RiskDebateSummary(
            trader_proposal_id=proposal_id,
            turns=turns,
            silence_multiplier=silence_multiplier,
            final_recommendation=final_recommendation,
            n_rounds=n_rounds_completed,
            terminated_reason=terminated_reason,
        )

    def _invoke_persona_llm(
        self,
        *,
        persona: RiskPersona,
        proposal: TraderProposal,
        plan: dict[str, Any],
        prior_turns: list[RiskCommitteeTurn],
        turn_index: int,
        proposal_json: str,
        plan_json: str,
    ) -> tuple[RiskCommitteeTurn, str]:
        """Attempt the LLM call for one persona turn.

        Returns (turn, path_label) where path_label is one of:
          * 'v02_llm_succeeded'      — LLM returned a valid RiskCommitteeTurn
          * 'v02_llm_fallback_to_v01' — LLM failed; fell back to persona.decide()

        Partial-fallback contract (ADR-0056): failure here only affects THIS
        persona's turn. The caller (_debate_with_llm) continues for remaining
        personas regardless of what happens here.
        """
        persona_name = persona.name
        template = getattr(persona, "LLM_PROMPT_TEMPLATE", "")

        prior_turns_json = json.dumps(
            [t.model_dump() for t in prior_turns], default=str
        )
        ticker = plan.get("ticker", plan.get("symbol", "UNKNOWN"))

        # Render the LLM prompt.
        try:
            system_prompt = template.format(
                ticker=ticker,
                turn_index=turn_index,
                proposal_json=proposal_json,
                plan_json=plan_json,
                prior_turns_json=prior_turns_json,
            )
        except (KeyError, IndexError, ValueError) as exc:
            logger.warning(
                "RiskCommittee: persona '%s' LLM_PROMPT_TEMPLATE render failed "
                "(%s); falling back to v0.1.",
                persona_name,
                exc,
            )
            return self._v01_turn(
                persona=persona,
                proposal=proposal,
                plan=plan,
                prior_turns=prior_turns,
                turn_index=turn_index,
                path="v02_llm_fallback_to_v01",
            )

        user_prompt = (
            f"Produce your RiskCommitteeTurn JSON for turn {turn_index}. "
            "Do not include any prose outside the JSON object."
        )

        # Attempt LLM call.
        try:
            obj, _raw = self._llm_caller.call(  # type: ignore[union-attr]
                system_prompt,
                user_prompt,
                schema=RiskCommitteeTurn,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "RiskCommittee: LLM call raised for persona '%s' turn %d (%s); "
                "falling back to v0.1 for this turn only.",
                persona_name,
                turn_index,
                exc,
            )
            return self._v01_turn(
                persona=persona,
                proposal=proposal,
                plan=plan,
                prior_turns=prior_turns,
                turn_index=turn_index,
                path="v02_llm_fallback_to_v01",
            )

        # Validate the returned object.
        if isinstance(obj, RiskCommitteeTurn):
            logger.debug(
                "RiskCommittee: v0.2 LLM succeeded for persona '%s' turn %d.",
                persona_name,
                turn_index,
            )
            # Enforce persona/turn_index fields to be canonical regardless of LLM.
            # Pydantic 'extra=forbid' means we rebuild to be safe.
            try:
                validated = RiskCommitteeTurn(
                    persona=persona_name,
                    turn_index=turn_index,
                    critique_text=obj.critique_text,
                    evidence_ids=obj.evidence_ids,
                    risk_assessment=obj.risk_assessment,
                    confidence=obj.confidence,
                )
                return validated, "v02_llm_succeeded"
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "RiskCommittee: LLM turn validation failed for persona '%s' "
                    "turn %d (%s); falling back to v0.1.",
                    persona_name,
                    turn_index,
                    exc,
                )

        else:
            logger.warning(
                "RiskCommittee: LLM returned %r for persona '%s' turn %d; "
                "falling back to v0.1 for this turn only.",
                type(obj).__name__,
                persona_name,
                turn_index,
            )

        return self._v01_turn(
            persona=persona,
            proposal=proposal,
            plan=plan,
            prior_turns=prior_turns,
            turn_index=turn_index,
            path="v02_llm_fallback_to_v01",
        )

    def _v01_turn(
        self,
        *,
        persona: RiskPersona,
        proposal: TraderProposal,
        plan: dict[str, Any],
        prior_turns: list[RiskCommitteeTurn],
        turn_index: int,
        path: str,
    ) -> tuple[RiskCommitteeTurn, str]:
        """Run one persona's v0.1 deterministic decision and return (turn, path)."""
        decision = self._invoke_persona(
            persona=persona,
            proposal=proposal,
            plan=plan,
            prior_turns=prior_turns,
        )
        turn = RiskCommitteeTurn(
            persona=persona.name,
            turn_index=turn_index,
            critique_text=decision.critique_text,
            evidence_ids=list(decision.evidence_ids),
            risk_assessment=decision.risk_assessment,
            confidence=decision.confidence,
        )
        return turn, path

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _should_use_llm(self) -> bool:
        """Return True iff all three conditions for v0.2 LLM path are met.

        Conditions (AND):
          (a) self._llm_caller is not None
          (b) self._llm_caller.available() returns True
          (c) env var HERMES_QUANT_RISK_COMMITTEE_LLM=1
        """
        if self._llm_caller is None:
            return False
        flag = os.environ.get(_LLM_FLAG_ENV_VAR, "0").strip()
        if flag != "1":
            return False
        if not self._llm_caller.available():
            logger.warning(
                "RiskCommittee: HERMES_QUANT_RISK_COMMITTEE_LLM=1 but "
                "LLMCaller.available() is False (no API key); "
                "falling back to v0.1 deterministic."
            )
            return False
        return True

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
        """v0.1: deterministic. v0.2 branches via _debate_with_llm."""
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

