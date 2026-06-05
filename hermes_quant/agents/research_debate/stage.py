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
from dataclasses import replace
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
_BUDGETED_DEBATE_DEFAULT_MAX_TOKENS: int = 800

# ADR-0080 W7 (default-OFF): the standing Socratic devil's-advocate turn flag.
# Mirror of the dispatch flag idiom (llm_committee.py). Default OFF; flag-unset
# is bit-for-bit identical to today (no red-team turn runs, byte-stable audit).
REDTEAM_FLAG_ENV_VAR: str = "HERMES_QUANT_REDTEAM_TURN"


def _redteam_enabled() -> bool:
    """Mirror of the dispatch flag idiom (``llm_committee.py``). Default OFF."""
    return os.environ.get(REDTEAM_FLAG_ENV_VAR, "0") == "1"


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
        )
        from hermes_quant.governance.audit_log import (
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
# Debate-level budget envelope (B41-e)
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Cheap deterministic token estimate matching LLMCaller budget posture."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _budget_tick_id(ctx: Any) -> str:
    asset = getattr(ctx, "asset", None) or "unknown"
    asof = getattr(ctx, "asof", None)
    return f"{asset}:{asof}"


def _budget_model_for_role(role: str, config: Any) -> str:
    deep_model = getattr(config, "deep_model", "anthropic/claude-sonnet-4.6")
    quick_model = getattr(config, "quick_model", "anthropic/claude-haiku-4.5")
    return deep_model if role == "research_manager" else quick_model


def _budget_requested_max_tokens(config: Any) -> int:
    raw = getattr(config, "max_tokens_per_turn", _BUDGETED_DEBATE_DEFAULT_MAX_TOKENS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _BUDGETED_DEBATE_DEFAULT_MAX_TOKENS
    return value if value > 0 else _BUDGETED_DEBATE_DEFAULT_MAX_TOKENS


def _budget_prompt_tokens(
    *,
    role: str,
    ctx: Any,
    baseline_signal: Any,
    analyst_views: list[Any],
    state: InvestDebateState,
) -> int:
    """Conservative deterministic prompt-size estimate for reservation checks.

    The actual LLM adapter owns prompt rendering. The stage-level budget gate
    therefore reserves against a stable context summary plus accumulated debate
    history, erring upward with a fixed overhead so an empty prompt estimate does
    not become a free call.
    """
    context = {
        "role": role,
        "asset": getattr(ctx, "asset", None),
        "asof": str(getattr(ctx, "asof", "")),
        "baseline": {
            "direction": getattr(baseline_signal, "direction", None),
            "magnitude": getattr(baseline_signal, "magnitude", None),
            "confidence": getattr(baseline_signal, "confidence", None),
            "horizon": getattr(baseline_signal, "horizon", None),
        },
        "analyst_count": len(analyst_views),
        "history": state.history,
        "current_response": state.current_response,
        "bull_history": state.bull_history,
        "bear_history": state.bear_history,
    }
    return max(64, _estimate_tokens(str(context)) + 64)


def _config_with_max_tokens(config: Any, max_tokens: int) -> Any:
    if config is None or getattr(config, "max_tokens_per_turn", None) == max_tokens:
        return config
    try:
        return replace(config, max_tokens_per_turn=int(max_tokens))
    except Exception:  # noqa: BLE001
        logger.warning(
            "ResearchDebateStage: could not clamp max_tokens_per_turn on config; "
            "continuing with original config"
        )
        return config


def _resolve_budget_guard(budget_guard: Any | None) -> Any | None:
    if budget_guard is not None:
        return budget_guard
    try:
        from hermes_quant.agents.llm_budget import LLMBudgetGuard
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ResearchDebateStage: could not import LLMBudgetGuard (%s); "
            "budget guard remains OFF",
            exc,
        )
        return None
    try:
        return LLMBudgetGuard.from_env()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ResearchDebateStage: LLMBudgetGuard.from_env raised (%s); "
            "failing closed for the debate",
            exc,
        )
        raise


def _budget_prepare_call(
    *,
    budget_guard: Any | None,
    role: str,
    ctx: Any,
    baseline_signal: Any,
    analyst_views: list[Any],
    state: InvestDebateState,
    config: Any,
    decision_id: str,
    tick_id: str,
) -> tuple[bool, Any, str | None]:
    """Reserve one LLM call against the shared debate decision bucket.

    ``budget_guard=None`` is the byte-identical OFF path. When a guard is
    present, every bull/bear/judge/red-team call uses the same ``decision_id``.
    A successful check is immediately recorded as a worst-case reservation so
    later turns see the cumulative debate spend and cannot pass independently.
    """
    if budget_guard is None:
        return True, config, None

    model_id = _budget_model_for_role(role, config)
    requested_max_tokens = _budget_requested_max_tokens(config)
    prompt_tokens = _budget_prompt_tokens(
        role=role,
        ctx=ctx,
        baseline_signal=baseline_signal,
        analyst_views=analyst_views,
        state=state,
    )
    try:
        check = budget_guard.check(
            model_id=model_id,
            prompt_tokens=prompt_tokens,
            max_tokens=requested_max_tokens,
            decision_id=decision_id,
            tick_id=tick_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ResearchDebateStage: budget_guard.check raised (%s); failing closed",
            exc,
        )
        return False, config, "guard_error"

    if not check.allowed:
        return False, config, check.reason or "exhausted"

    allowed_max_tokens = int(check.allowed_max_tokens)
    if allowed_max_tokens <= 0:
        return False, config, "no_max_tokens"

    call_config = _config_with_max_tokens(config, allowed_max_tokens)
    if getattr(call_config, "max_tokens_per_turn", None) != allowed_max_tokens:
        return False, config, "max_tokens_clamp_failed"

    before = budget_guard.snapshot(decision_id=decision_id, tick_id=tick_id)
    before_calls = int(before.get("decision_calls", 0))
    budget_guard.record(
        model_id=model_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=allowed_max_tokens,
        decision_id=decision_id,
        tick_id=tick_id,
    )
    after = budget_guard.snapshot(decision_id=decision_id, tick_id=tick_id)
    if after.get("corrupt") or int(after.get("decision_calls", 0)) <= before_calls:
        logger.warning(
            "ResearchDebateStage: budget reservation did not persist; "
            "failing closed before spending another call"
        )
        return False, config, "reservation_failed"

    return True, call_config, None


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
    run_red_team: Any = None,
    budget_guard: Any | None = None,
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
        budget_guard: optional ``LLMBudgetGuard``. ``None`` resolves through
            ``LLMBudgetGuard.from_env()``; with env unset that returns ``None``
            and this function follows the byte-identical historical path.

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
    budget_tick_id = _budget_tick_id(ctx)
    try:
        budget_guard = _resolve_budget_guard(budget_guard)
    except Exception:  # noqa: BLE001
        state.terminated_reason = "budget_exhausted:guard_error"
        budget_guard = "fail_closed"

    # ADR-0066 (v0.6.2): production turn/judge wiring is live. The default
    # injection points are the helpers in ``llm_committee``:
    # ``_run_one_turn_with_history`` (the bull/bear turn adapter) and
    # ``_run_research_manager_judge`` (the ResearchManager judge adapter). The
    # dispatch site (llm_committee.run_llm_committee, under
    # HERMES_QUANT_RESEARCH_DEBATE=1) passes both explicitly; here we default
    # them via lazy import so direct callers get the production wiring for free
    # while tests can still inject stubs. The import is lazy to avoid a circular
    # dependency (``llm_committee`` lazily imports this module at its dispatch
    # site). The ``NotImplementedError`` below now fires only if those helpers
    # cannot be imported at all — a genuine guard, not a "not yet built" stub.
    if run_one_turn is None or run_judge is None:
        try:
            from hermes_quant.aggregators.llm_committee import (
                _run_one_turn_with_history,
                _run_research_manager_judge,
            )
        except Exception as exc:  # noqa: BLE001
            raise NotImplementedError(
                "run_research_debate: could not import the production turn/judge "
                "helpers (_run_one_turn_with_history, _run_research_manager_judge) "
                "from hermes_quant.aggregators.llm_committee. Pass run_one_turn=, "
                "run_judge= kwargs explicitly to use custom wiring."
            ) from exc
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

            budget_ok, call_config, budget_reason = _budget_prepare_call(
                budget_guard=budget_guard,
                role=role,
                ctx=ctx,
                baseline_signal=baseline_signal,
                analyst_views=analyst_views or [],
                state=state,
                config=config,
                decision_id=pid,
                tick_id=budget_tick_id,
            )
            if not budget_ok:
                state.terminated_reason = f"budget_exhausted:{budget_reason}"
                break

            try:
                turn = run_one_turn(
                    role=role,
                    client=client,
                    config=call_config,
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

    # Attempt the judge on partial state unless the budget envelope has already
    # failed closed. The deep-tier judge has its own try/except + Pydantic
    # validator inside _run_research_manager_judge.
    judge_plan = None
    if state.terminated_reason.startswith("budget_exhausted:"):
        budget_ok, judge_config, budget_reason = False, config, "exhausted"
    else:
        budget_ok, judge_config, budget_reason = _budget_prepare_call(
            budget_guard=budget_guard,
            role="research_manager",
            ctx=ctx,
            baseline_signal=baseline_signal,
            analyst_views=analyst_views or [],
            state=state,
            config=config,
            decision_id=pid,
            tick_id=budget_tick_id,
        )
    if budget_ok:
        try:
            judge_plan = run_judge(
                client=client,
                config=judge_config,
                market_context=ctx,
                analyst_views=analyst_views or [],
                baseline_signal=baseline_signal,
                bull_turns=state.bull_turns,
                bear_turns=state.bear_turns,
            )
        except Exception:  # noqa: BLE001
            logger.exception("ResearchDebateStage: judge invocation raised; using None")
            judge_plan = None
    else:
        state.terminated_reason = f"budget_exhausted:{budget_reason}"

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

    # ------------------------------------------------------------------
    # W7 (ADR-0080): standing Socratic devil's-advocate turn. Default-OFF.
    # Attacks the REASONING of the leading view; surfaces dissent; fills the
    # reserved ADR-0002 counterarguments field. Propose-only — it never
    # changes direction/magnitude/confidence. Off-state byte-identical.
    # ------------------------------------------------------------------
    if _redteam_enabled() and state.judge_decision is not None:
        if run_red_team is None:
            try:
                from hermes_quant.aggregators.llm_committee import (
                    _run_red_team_turn,
                )
                run_red_team = _run_red_team_turn
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "W7: could not import _run_red_team_turn (%s); skipping", exc
                )
                run_red_team = None
        if run_red_team is not None:
            budget_ok, redteam_config, budget_reason = _budget_prepare_call(
                budget_guard=budget_guard,
                role="devils_advocate",
                ctx=ctx,
                baseline_signal=baseline_signal,
                analyst_views=analyst_views or [],
                state=state,
                config=config,
                decision_id=pid,
                tick_id=budget_tick_id,
            )
            if budget_ok:
                try:
                    rt = run_red_team(
                        client=client,
                        config=redteam_config,
                        market_context=ctx,
                        analyst_views=analyst_views or [],
                        baseline_signal=baseline_signal,
                        leading_view=state.judge_decision,
                    )
                except Exception:  # noqa: BLE001 — failure-closed; no dissent on failure
                    logger.exception("W7: red-team turn raised; treating as no-dissent")
                    rt = None
            else:
                state.terminated_reason = f"budget_exhausted:{budget_reason}"
                rt = None
            if rt is not None:
                state.red_team_turn = rt
                # Deterministic dissent rule (NOT a vote): a critique with
                # confidence >= threshold surfaces dissent to the operator.
                from hermes_quant.aggregators.llm_committee import (
                    RED_TEAM_DISSENT_THRESHOLD,
                )
                state.dissent_surfaced = rt.confidence >= RED_TEAM_DISSENT_THRESHOLD
                state.dissent_reason = (rt.counterarguments or rt.stance or "")[:4000]
                # Fill the reserved ADR-0002 plan-level counterarguments field.
                if state.judge_decision.counterarguments is None:
                    state.judge_decision.counterarguments = state.dissent_reason

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
            "bull_turns_summary": [
                {
                    "stance": t.stance,
                    "confidence": t.confidence,
                    "rationale_chars": len(t.rationale or ""),
                }
                for t in state.bull_turns
            ],
            "bear_turns_summary": [
                {
                    "stance": t.stance,
                    "confidence": t.confidence,
                    "rationale_chars": len(t.rationale or ""),
                }
                for t in state.bear_turns
            ],
            # ADR-0080 W7 (default-OFF): the W3-mineable red-team sub-block (O7).
            # When the flag is OFF (state.red_team_turn is None) this carries only
            # the byte-stable off-state record {"ran": False, "dissent_surfaced":
            # False}. Reuses the existing ``research_debate`` audit kind (no
            # VALID_KINDS migration).
            "red_team": (
                {
                    "ran": state.red_team_turn is not None,
                    "dissent_surfaced": state.dissent_surfaced,
                    "dissent_reason": state.dissent_reason,
                    "confidence": (
                        state.red_team_turn.confidence
                        if state.red_team_turn is not None
                        else None
                    ),
                    "rationale_chars": (
                        len(state.red_team_turn.rationale or "")
                        if state.red_team_turn is not None
                        else 0
                    ),
                    "prompt_hash": (
                        (state.red_team_turn.metadata or {}).get("prompt_hash")
                        if state.red_team_turn is not None
                        else None
                    ),
                }
                if state.red_team_turn is not None
                else {"ran": False, "dissent_surfaced": False}
            ),
        },
    )

    return state
