"""Bull/Bear adversarial debate stage runner (ADR-0065).

Modeled directly on ``hermes_quant.agents.risk_committee.committee._debate_with_llm``
and ``_resolve_max_rounds``. Mirrored design points:

  * Round-robin alternation (turn ``2k`` is bull, turn ``2k+1`` is bear)
  * Termination at ``count >= 2 * max_rounds`` (mirrors risk committee's
    ``count < 3 * max_rounds`` shape, scaled to two roles instead of three).
  * Two-failure bail (``consecutive_failures >= 2`` → break early; judge
    still runs on partial state).
  * Per-stage audit row (NOT per-turn — for volume control). One ``GovernanceEvent``
    row of ``kind='research_debate'`` per stage invocation.
  * Env-clamped round count: ``HERMES_QUANT_RESEARCH_DEBATE_ROUNDS`` is read
    if no explicit ``max_rounds`` argument is passed; clamped to
    ``[1, MAX_ALLOWED_ROUNDS]``; default ``DEFAULT_MAX_ROUNDS=1`` preserves
    v0.6.0 behaviour (1 round = 2 turns = bull + bear).

The stage is feature-flagged at the dispatch site (``llm_committee.py``)
via ``HERMES_QUANT_RESEARCH_DEBATE=1``. When the flag is OFF (the v0.6.1
default) ``run_research_debate`` is never called; ``run_llm_committee`` falls
through to the legacy parallel-emit loop and emission is bit-identical to
v0.6.0 (pinned by T10).
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from hermes_quant.agents.research_debate.schemas import (
    BullBearTurn,
    InvestDebateState,
    PortfolioRating,
    ResearchPlan,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

DEFAULT_MAX_ROUNDS: int = 1
MAX_ALLOWED_ROUNDS: int = 3
RESEARCH_ROUNDS_ENV_VAR: str = "HERMES_QUANT_RESEARCH_DEBATE_ROUNDS"
RESEARCH_DEBATE_FLAG_ENV_VAR: str = "HERMES_QUANT_RESEARCH_DEBATE"
RESEARCH_DEBATE_AUDIT_KIND: str = "research_debate"

# Imported verbatim into prompts to keep the two adversarial subsystems'
# style identical (G17 uniformity).
CONVERSATIONAL_PREAMBLE: str = (
    "Output conversationally as if you are speaking without any special "
    "formatting"
)


# ---------------------------------------------------------------------------
# Audit helper (lazy, never crashes import)
# ---------------------------------------------------------------------------


def _audit_append(kind: str, source: str, payload: dict[str, Any]) -> None:
    """Thin wrapper so tests can patch ``stage._audit_append`` directly.

    Routes through ``hermes_quant.governance.audit_log.append`` (the strict
    path with VALID_KINDS gate). The kind ``'research_debate'`` is added
    to VALID_KINDS as part of this work.
    """
    try:
        from datetime import UTC, datetime
        from hermes_quant.governance.audit_log import (
            GovernanceEvent,
            append as _append,
        )
        evt = GovernanceEvent(
            kind=kind,  # type: ignore[arg-type]
            asof=datetime.now(UTC),
            source=source,
            payload=payload,
        )
        _append(evt)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ResearchDebateStage: _audit_append failed (%s); continuing.", exc
        )


# ---------------------------------------------------------------------------
# Env clamping
# ---------------------------------------------------------------------------


def _resolve_max_rounds(explicit: int | None) -> int:
    """Resolve the effective max_rounds value.

    Precedence: explicit argument → ``HERMES_QUANT_RESEARCH_DEBATE_ROUNDS``
    env var → ``DEFAULT_MAX_ROUNDS``. Clamped to ``[1, MAX_ALLOWED_ROUNDS]``.

    Mirror of ``RiskCommittee._resolve_max_rounds`` (committee.py:579).
    Non-int env values log a warning and fall back to the default.
    """
    if explicit is not None:
        n = int(explicit)
    else:
        raw = os.environ.get(RESEARCH_ROUNDS_ENV_VAR)
        if not raw:
            n = DEFAULT_MAX_ROUNDS
        else:
            try:
                n = int(raw)
            except (TypeError, ValueError):
                logger.warning(
                    "%s=%r is not an int; falling back to default %d",
                    RESEARCH_ROUNDS_ENV_VAR,
                    raw,
                    DEFAULT_MAX_ROUNDS,
                )
                n = DEFAULT_MAX_ROUNDS
    return max(1, min(MAX_ALLOWED_ROUNDS, n))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_research_debate(
    ctx: Any,
    baseline_signal: Any,
    *,
    analyst_views: list[Any] | None = None,
    config: Any = None,
    client: Any = None,
    max_rounds: int | None = None,
    proposal_id: str | None = None,
    run_one_turn: Any = None,
    run_judge: Any = None,
) -> InvestDebateState:
    """Run the bull/bear adversarial debate stage and return the final state.

    Stage placement: AFTER analysts/BMA baseline, BEFORE TraderNode.

    Args:
        ctx: ``MarketContext`` for the tick (asset, asof, asset_class, ...).
        baseline_signal: ``AggregatedSignal`` from the BMA baseline.
        analyst_views: list of calibrated ``AnalystView`` for the tick.
        config: ``DeliberativeConfig`` (model ids, max tokens, ...).
        client: pre-built OpenAI-compatible client (or test stub).
        max_rounds: optional explicit override. ``None`` reads from env.
        proposal_id: optional stable id; auto-generated if ``None``.
        run_one_turn: optional injection point for tests; defaults to
            ``llm_committee._run_one_turn_with_history`` (lazy import below).
        run_judge: optional injection point for tests; defaults to
            ``llm_committee._run_research_manager_judge`` (lazy import below).

    Returns:
        Final ``InvestDebateState`` with ``judge_decision`` populated
        (or ``None`` if the judge itself failed validation).

    Failure-closed contract:
        * Two consecutive turn failures → ``terminated_reason
          == "two_consecutive_failures"`` and judge still runs on partial state.
        * Any uncaught exception → caught at outer try, ``terminated_reason``
          records the exception type, judge still runs on partial state.
        * Judge failure → ``state.judge_decision is None``; audit row is still
          emitted with ``final_recommendation=None``.
    """
    rounds = _resolve_max_rounds(max_rounds)
    pid = proposal_id or f"rdp-{uuid.uuid4().hex[:12]}"
    state = InvestDebateState()

    # Lazy import to avoid cycles: schemas.py → llm_committee → schemas (BullBearTurn).
    if run_one_turn is None or run_judge is None:
        from hermes_quant.aggregators.llm_committee import (
            _run_one_turn_with_history,
            _run_research_manager_judge,
        )
        if run_one_turn is None:
            run_one_turn = _run_one_turn_with_history
        if run_judge is None:
            run_judge = _run_research_manager_judge

    consecutive_failures = 0
    asset_str = getattr(ctx, "asset", None) or "unknown"
    asof_str = str(getattr(ctx, "asof", "")) if ctx is not None else ""

    try:
        max_turns = 2 * rounds
        while state.count < max_turns:
            is_bull = (state.count % 2 == 0)
            role = "bull_researcher" if is_bull else "bear_researcher"
            own_history_raw = state.bull_history if is_bull else state.bear_history
            own_history = own_history_raw or "(no prior turns by you yet)"
            current_response = (
                state.current_response
                or "(no prior turn — open the debate)"
            )
            round_index = (state.count // 2) + 1

            try:
                turn = run_one_turn(
                    role=role,
                    client=client,
                    config=config,
                    market_context=ctx,
                    analyst_views=analyst_views or [],
                    baseline_signal=baseline_signal,
                    current_response=current_response,
                    own_history=own_history,
                    round_index=round_index,
                    conversational_preamble=CONVERSATIONAL_PREAMBLE,
                )
            except Exception:  # noqa: BLE001 — failure-closed at the per-turn level
                logger.exception(
                    "ResearchDebateStage: turn %d (role=%s) raised; counting as failure",
                    state.count,
                    role,
                )
                turn = None

            if turn is None:
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    state.terminated_reason = "two_consecutive_failures"
                    break
                # Advance the count even on failure so the loop can retry the
                # OPPOSITE role next iter (avoids infinite-loop on the same
                # failing role). Mirrors risk-committee semantics.
                state.count += 1
                continue

            consecutive_failures = 0

            # ``turn`` is expected to be a CommitteeTurn whose metadata carries
            # the structured BullBearTurn payload (per llm_committee._run_one_turn).
            structured: BullBearTurn
            try:
                structured = BullBearTurn.model_validate(
                    turn.metadata["structured"]
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "ResearchDebateStage: turn %d (role=%s) lacked valid 'structured' "
                    "metadata; counting as failure",
                    state.count,
                    role,
                )
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    state.terminated_reason = "two_consecutive_failures"
                    break
                state.count += 1
                continue

            speech = structured.rationale
            if is_bull:
                state.bull_turns.append(structured)
                state.bull_history = (
                    state.bull_history + f"\n[Bull r{round_index}] {speech}"
                )
            else:
                state.bear_turns.append(structured)
                state.bear_history = (
                    state.bear_history + f"\n[Bear r{round_index}] {speech}"
                )
            state.history = state.history + f"\n[{role} t{state.count}] {speech}"
            prefix = "Bull: " if is_bull else "Bear: "
            state.current_response = prefix + speech
            state.count += 1

    except Exception as exc:  # noqa: BLE001 — failure-closed at the top
        logger.warning(
            "ResearchDebateStage aborted (%s) after %d turns; "
            "running judge on partial state.",
            exc,
            state.count,
            exc_info=True,
        )
        state.terminated_reason = f"exception:{type(exc).__name__}"

    # Always attempt the judge, even on partial state. The deep-tier judge has
    # its own try/except + Pydantic validator inside _run_research_manager_judge.
    try:
        judge_plan = run_judge(
            client=client,
            config=config,
            market_context=ctx,
            analyst_views=analyst_views or [],
            baseline_signal=baseline_signal,
            bull_turns=state.bull_turns,
            bear_turns=state.bear_turns,
        )
    except Exception:  # noqa: BLE001
        logger.exception("ResearchDebateStage: judge invocation raised; using None")
        judge_plan = None

    if judge_plan is not None and not isinstance(judge_plan, ResearchPlan):
        # The committee module returns a CommitteeTurn-shaped object today; if a
        # caller injects a different judge we coerce it to ResearchPlan when
        # possible, else discard.
        try:
            judge_plan = ResearchPlan.model_validate(judge_plan)
        except Exception:  # noqa: BLE001
            logger.warning(
                "ResearchDebateStage: judge output is not a valid ResearchPlan; "
                "dropping (state.judge_decision=None)."
            )
            judge_plan = None

    state.judge_decision = judge_plan

    # Single audit row per stage invocation.
    final_rec_value: str | None = None
    if state.judge_decision is not None:
        rec = state.judge_decision.recommendation
        # PortfolioRating is a StrEnum, but be defensive.
        final_rec_value = (
            rec.value if isinstance(rec, PortfolioRating) else str(rec)
        )

    _audit_append(
        kind=RESEARCH_DEBATE_AUDIT_KIND,
        source="hermes_quant.agents.research_debate.stage",
        payload={
            "proposal_id": pid,
            "asset": asset_str,
            "asof": asof_str,
            "rounds_configured": rounds,
            "bull_count": len(state.bull_turns),
            "bear_count": len(state.bear_turns),
            "terminated_reason": state.terminated_reason,
            "final_recommendation": final_rec_value,
            "research_plan": (
                state.judge_decision.model_dump(mode="json")
                if state.judge_decision is not None
                else None
            ),
            "bull_turns": [t.model_dump(mode="json") for t in state.bull_turns],
            "bear_turns": [t.model_dump(mode="json") for t in state.bear_turns],
        },
    )

    return state
