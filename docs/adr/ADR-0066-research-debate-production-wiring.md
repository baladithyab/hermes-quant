# ADR-0066: Production Wiring for ResearchDebateStage (v0.6.2)

**Status:** Proposed
**Date:** 2026-05-28
**Target:** v0.6.2
**Supersedes / extends:** ADR-0065 §Implementation Plan §7 (deferred from v0.6.1)
**Cross-cuts:** ADR-0065 (Bull/Bear Adversarial Debate), ADR-0058 (PortfolioRating StrEnum), ADR-0037 (Failure-closed posture)

## Context

ADR-0065 shipped in v0.6.1 with a fully-functional `run_research_debate` stage runner that requires injected `run_one_turn=` and `run_judge=` callables. v0.6.1 ships with `NotImplementedError` if either is missing because the production helpers (`_run_one_turn_with_history`, `_run_research_manager_judge`) were referenced in design docs but never written. v0.6.1 dispatch site logs a warning when `HERMES_QUANT_RESEARCH_DEBATE=1` and falls through to legacy committee path.

v0.6.2 closes this gap: implements both helpers as thin adapters over the existing `_run_one_turn(role=...)` infrastructure in `llm_committee.py`, then wires them into `run_research_debate` defaults so production callers work without explicit injection.

## Decision

### `_run_one_turn_with_history`

Thin adapter over `_run_one_turn` that forwards the v0.6.1 conversational placeholders (`current_response`, `own_history`, `round_index`, `conversational_preamble`) into `_render_prompt`. The renderer already accepts these as optional kwargs since commit `14dacd0` (v0.6.1).

Signature must match what `stage.py:204-215` calls:
```python
def _run_one_turn_with_history(
    *,
    role: str,
    client: Any,
    config: DeliberativeConfig,
    market_context: MarketContext,
    analyst_views: list[AnalystView],
    baseline_signal: AggregatedSignal,
    current_response: str,
    own_history: str,
    round_index: int,
    conversational_preamble: str,
) -> CommitteeTurn | None:
```

Implementation: render via `_render_prompt(...)` with the conversational kwargs, call LLM via `_call_llm_json`, parse via `_parse_pydantic(BullBearTurn)`, return `CommitteeTurn` with metadata mirroring `_run_one_turn`'s bull/bear branch (lines 528-556). The only difference from `_run_one_turn` is `prior_turns=[]` (the debate is conversational; the legacy parallel-emit path's `prior_turns` are not relevant here).

### `_run_research_manager_judge`

The `research_manager` role already has parser + Pydantic plumbing in `_run_one_turn` (lines 558-585), but it currently uses the **legacy** `ResearchPlan` from `llm_committee.py` (which has the `overrules_baseline` field). For v0.6.2 the judge must return the **new** `ResearchPlan` from `agents/research_debate/schemas.py` (which uses `PortfolioRating` StrEnum and dropped `overrules_baseline`).

Signature:
```python
def _run_research_manager_judge(
    *,
    client: Any,
    config: DeliberativeConfig,
    market_context: MarketContext,
    analyst_views: list[AnalystView],
    baseline_signal: AggregatedSignal,
    bull_turns: list[BullBearTurn],
    bear_turns: list[BullBearTurn],
) -> ResearchPlan | None:
```

Implementation: render `research_manager` role via `_render_prompt`, call LLM, parse via `_parse_pydantic(NewResearchPlan)` (the schema with the `field_validator` from `50738f9`). Return the validated `ResearchPlan` directly (NOT a `CommitteeTurn`). Stage runner expects a raw `ResearchPlan` per `stage.py:300-311`.

The `research_manager.md` prompt may need to be updated to drop `overrules_baseline` from the JSON envelope it asks for, OR the new schema's `extra='forbid'` model_config will reject it; I add a `model_config = ConfigDict(extra='ignore')` on the *judge-input* path so the prompt template stays backward-compatible.

### Dispatch wiring

Replace the v0.6.1 log+fallthrough at `llm_committee.py:702-710` with:

```python
if os.environ.get("HERMES_QUANT_RESEARCH_DEBATE", "0") == "1":
    from hermes_quant.agents.research_debate.stage import run_research_debate
    try:
        state = run_research_debate(
            ctx=market_context,
            baseline_signal=baseline_signal,
            analyst_views=analyst_views,
            config=config,
            client=client,
            run_one_turn=_run_one_turn_with_history,
            run_judge=_run_research_manager_judge,
        )
        # Translate state.bull_turns + state.bear_turns into CommitteeTurn list
        # for the deterministic aggregator's tier-split filter. Each BullBearTurn
        # becomes a CommitteeTurn with role=bull_researcher/bear_researcher,
        # tier=quick.
        for bt in state.bull_turns:
            turns.append(_committee_turn_from_bull_bear(bt, role="bull_researcher", config=config))
        for bt in state.bear_turns:
            turns.append(_committee_turn_from_bull_bear(bt, role="bear_researcher", config=config))
        if state.judge_decision is not None:
            turns.append(_committee_turn_from_research_plan(state.judge_decision, config=config))
        return turns
    except Exception:
        logger.exception(
            "ResearchDebateStage failed; falling back to legacy committee for this tick"
        )
        # fall through
```

Two new private helpers `_committee_turn_from_bull_bear` and `_committee_turn_from_research_plan` produce the `CommitteeTurn` envelopes the deterministic aggregator already accepts.

### Backward compatibility

- Flag default OFF stays. `HERMES_QUANT_RESEARCH_DEBATE=0` (or unset) → bit-identical to v0.6.1 legacy path.
- Flag ON in v0.6.2 → real dispatch with real LLM helpers; falls back to legacy on any uncaught exception.
- After v0.6.2 ships AND one observation week of green CI on staging tick stream, flip default ON in v0.6.3.

## Test plan

**v0.6.2 unit tests** (added to `tests/agents/research_debate/test_stage.py`):

1. `test_run_one_turn_with_history_renders_conversational_prompt` — mock `_call_llm_json` to return a stub bull JSON; assert the rendered system_text contains the `current_response`, `own_history`, `round_index`, `conversational_preamble` values.
2. `test_run_one_turn_with_history_returns_None_on_llm_failure` — mock `_call_llm_json` to return None; assert the helper returns None (failure-closed).
3. `test_run_one_turn_with_history_returns_None_on_role_mismatch` — mock LLM to return `role=bear_researcher` when asked for `bull_researcher`; assert None (mirrors `_run_one_turn` line 532-538).
4. `test_run_research_manager_judge_happy_path` — mock LLM to return valid ResearchPlan JSON; assert returns ResearchPlan with `recommendation=PortfolioRating.OVERWEIGHT`.
5. `test_run_research_manager_judge_case_insensitive` — mock LLM to return `"recommendation": "Buy"` (Title-case); assert returns ResearchPlan with `recommendation=PortfolioRating.BUY` (depends on `field_validator`).
6. `test_run_research_manager_judge_returns_None_on_parse_failure` — mock LLM to return malformed JSON; assert None.

**v0.6.2 integration tests:**

7. `test_run_llm_committee_dispatches_to_research_debate_when_flag_on` — set `HERMES_QUANT_RESEARCH_DEBATE=1`, monkeypatch `_call_llm_json` to return canned stubs; assert returned `turns` list contains bull_researcher + bear_researcher + portfolio_manager(judge) entries with the expected metadata shape.
8. `test_run_llm_committee_falls_through_when_flag_off` — flag unset; assert legacy parallel-emit path runs (existing T10 covers this).
9. `test_run_llm_committee_falls_through_on_research_debate_exception` — flag ON but inject a stub that raises; assert we fall through to legacy emit.

## Risks

- **Prompt-hash drift**: bull_researcher/bear_researcher turns rendered via `_run_one_turn_with_history` will have different `prompt_hash` than legacy turns (because `current_response` etc. flow through). This is by-design per ADR-0065 T6/T7.
- **research_manager.md JSON envelope**: if the prompt asks for `overrules_baseline`, the new schema's `extra='forbid'` rejects it. Mitigation: relax to `extra='ignore'` for v0.6.2; tighten in v0.6.3 after prompt rewrite.
- **Audit volume**: legacy path emits one `committee_turn` event per turn; new path now emits BOTH per-turn `committee_turn` events (from the deterministic aggregator's intake) AND one stage-level `research_debate` event. Acceptable — gives operator a one-row stage summary plus per-turn forensics.

## Migration

- v0.6.2 ships flag default OFF. Existing flow bit-identical when flag off.
- v0.6.3 flips flag default ON after observation week.
- Prompt updates to drop `overrules_baseline` from `research_manager.md` JSON envelope land in v0.6.3 separately.
