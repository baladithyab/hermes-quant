# ADR-0065 — Bull/Bear Adversarial Debate Stage (with ResearchPlan + 5-tier PortfolioRating)

**Status:** Accepted (2026-05-27)
**Wave:** v0.6.1
**Full design:** [docs/design/v0.6.1-bull-bear-debate.md](../design/v0.6.1-bull-bear-debate.md) (662 lines)

## Context

Per TauricResearch gaps G1+G6+G13+G17:
- **G1**: Bull/Bear is currently parallel emission with a counter (in `aggregators/llm_committee.py:689-704`), NOT real adversarial back-and-forth. Each turn does NOT see the previous opponent's argument.
- **G6**: `ResearchPlan` shape lives only in prompt text, not as a Pydantic class with `with_structured_output()`.
- **G13**: 5-tier `PortfolioRating` enum (Buy/Overweight/Hold/Underweight/Sell) referenced in `research_manager.md` prompt text but not enforced as a Python type.
- **G17**: Current `bull_bear.md` prompt is structured-JSON-only with `key_evidence` lists — opposite of conversational engagement.

These collectively block parity with TauricResearch's reported calibration improvements from adversarial debate.

## Decision

Introduce a separate **research debate stage** that runs BEFORE Trader, AFTER BMA baseline:

```
Analysts → BMA(baseline) → ResearchDebate(N rounds) → ResearchPlan
                                ↓                          ↓
                                                       Trader → RiskCommittee
```

**State machine:**
```
[start] → bull_turn(round=1) → bear_turn(round=1, sees bull_turn_1)
       → bull_turn(round=2, sees bear_turn_1) → bear_turn(round=2, sees bull_turn_2)
       → ... up to max_research_debate_rounds=3
       → research_manager judges all turns → ResearchPlan
       → [end of stage]
```

**Pydantic schemas:**
```python
class PortfolioRating(StrEnum):
    BUY = "BUY"
    OVERWEIGHT = "OVERWEIGHT"
    HOLD = "HOLD"
    UNDERWEIGHT = "UNDERWEIGHT"
    SELL = "SELL"

    @property
    def signed_intensity(self) -> int:  # -2..+2 for risk-gate consumption
        return {"SELL": -2, "UNDERWEIGHT": -1, "HOLD": 0, "OVERWEIGHT": 1, "BUY": 2}[self.value]

class ResearchPlan(BaseModel):
    recommendation: PortfolioRating
    rationale: str
    strategic_actions: str
    # NOTE: overrules_baseline DROPPED — risk gate is the only direction-vs-baseline authority

class InvestDebateState(BaseModel):
    bull_history: list[BullBearTurn]
    bear_history: list[BullBearTurn]
    history: list[BullBearTurn]            # interleaved
    current_response: str                   # opponent's last argument verbatim
    count: int                              # total turns
    terminated_reason: Literal["completed", "max_rounds_reached", "two_failures_bail"]
```

**Termination:** `count >= 2 * max_rounds` (mirrors risk committee). `DEFAULT_MAX_ROUNDS=1`, `MAX_ALLOWED_ROUNDS=3`. Env: `HERMES_QUANT_RESEARCH_DEBATE_ROUNDS`.

**Backward compat:** Default `max_research_debate_rounds=1` (= 2 turns = bull + bear, no second round) preserves current behavior. Feature-flagged at v0.6.1 (`HERMES_QUANT_RESEARCH_DEBATE=0` default).

## Consequences

**Positive:**
- Closes the single largest delta vs TauricResearch's reported wins
- Each turn explicitly engages the opponent's last argument → genuinely adversarial
- Pydantic discipline: every research-stage output is hash-stable typed
- 5-tier rating gives risk gate finer signal than 3-state TraderAction
- New `EventKind = 'research_debate'` makes the stage observable in audit log
- Two-failure bail (mirroring LLM committee) prevents infinite loops on degraded models

**Negative:**
- Adds N additional LLM calls per tick (3 rounds = 6 LLM calls vs current 2). Mitigation: feature-flag, capped, fallback to baseline on failure.
- Latency: 3-round debate ≈ 10-15s additional wall time. Acceptable for HITL paper trader.
- Audit log volume: one stage row per debate (NOT per turn — bounded-volume tradeoff)
- Prompt-template change for `bull_bear.md` is breaking (the JSON envelope is preserved so `BullBearTurn` Pydantic continues to validate, but the conversational instruction is new)

## Implementation Plan

1. **Schemas:** create `hermes_quant/agents/research_debate/schemas.py` with `PortfolioRating`, `ResearchPlan`, `InvestDebateState`, updated `BullBearTurn` (preserves backward compat)
2. **Stage runner:** create `hermes_quant/agents/research_debate/stage.py` with `run_research_debate(ctx, baseline_signal, max_rounds, ...) -> tuple[ResearchPlan, InvestDebateState]`
3. **Alternation loop:** modeled directly on `RiskCommittee._debate_with_llm` and `_resolve_max_rounds` — see full design §5 for pseudo-code
4. **Prompt rewrite:** update `aggregators/prompts/bull_bear.md` with conversational preamble + `{current_response}`, `{own_history}`, `{round_index}`, `{conversational_preamble}` placeholders. JSON envelope preserved.
5. **TraderProposal extension:** add optional `research_plan_recommendation: PortfolioRating | None` and `research_plan_id: str | None` for traceability
6. **Audit:** add `'research_debate'` to `governance/audit_log.py:VALID_KINDS`; emit one event per stage with InvestDebateState dump
7. **Wire-up:** modify `aggregators/llm_committee.py` to call `run_research_debate` after BMA baseline (gated on `HERMES_QUANT_RESEARCH_DEBATE=1`); pipe `ResearchPlan` into TraderNode

## Test Plan

14 unit tests + 2 integration tests:
- `test_one_round_happy_path` (bull → bear → judge, default backward-compat behavior)
- `test_three_round_full_debate` (max rounds reached, count == 6)
- `test_two_consecutive_failures_bail` (LLM drops twice → terminated_reason == "two_failures_bail")
- `test_research_plan_pydantic_strict_validation` (rejects malformed JSON)
- `test_overrules_baseline_field_rejected` (Pydantic rejects extra field)
- `test_opponent_text_changes_prompt_hash` (bear turn 1 prompt hash != bear turn 2 prompt hash)
- `test_label_stability_under_portfolio_rating_enum` (StrEnum guarantees serialization stability)
- `test_audit_log_emission_research_debate_event` (one row per stage)
- `test_backward_compat_legacy_flag_off` (with `HERMES_QUANT_RESEARCH_DEBATE=0` flow is unchanged)
- `test_env_var_clamping_to_max_3_rounds`
- `test_default_max_rounds_is_1`
- `test_signed_intensity_property` (PortfolioRating.SELL.signed_intensity == -2)
- `test_trader_consumes_research_plan_recommendation`
- `test_research_plan_serialization_round_trip`
- (integration) `test_e2e_research_debate_in_pipeline`
- (integration) `test_e2e_audit_chain_research_to_trader_to_risk`

## Migration

- v0.6.1: Ship schemas + stage runner + audit kind + TraderProposal extension + `HERMES_QUANT_RESEARCH_DEBATE` flag (default OFF). When the flag is ON in v0.6.1 the dispatch site at `aggregators/llm_committee.py` logs a warning and falls through to the legacy bull/bear committee for-loop — production turn/judge wiring (`_run_one_turn_with_history`, `_run_research_manager_judge`) lands in v0.6.2. Tests inject explicit `run_one_turn=`/`run_judge=` kwargs to validate the stage end-to-end. Bit-identicality of `prompt_hash` across v0.6.0 → v0.6.1 is **NOT** guaranteed because `aggregators/prompts/bull_bear.md` was rewritten unconditionally to add the `{current_response}`/`{own_history}`/`{round_index}`/`{conversational_preamble}` placeholders required by the conversational debate. Audit consumers comparing `prompt_hash` across versions must regenerate fixtures. T10 pins post-rewrite hashes as a forward drift sentinel. The bit-identical guarantee is on the `CommitteeTurn` schema and the for-loop control flow when the flag is OFF, not on `prompt_hash`.
- v0.6.2: Implement `_run_one_turn_with_history` and `_run_research_manager_judge` helpers in `aggregators/llm_committee.py`. Wire them into `run_research_debate`'s default-runner path. Flip `HERMES_QUANT_RESEARCH_DEBATE` default to ON after one observation week of clean staging-tick CI. The PortfolioRating field validator at `agents/research_debate/schemas.py` accepts mixed-case input from upstream prompts; this stays in place permanently as a defensive measure.
- v0.6.2: Flip flag default ON after a week of observation.
- v0.6.x: Tune `max_research_debate_rounds` based on observed value (likely 2 — diminishing returns past round 2).

## Alternatives Considered

- **Skip the new stage; just rewrite bull_bear.md to be conversational**: rejected. The "current_response" feedback loop is the actual mechanism that produces calibration improvement; conversational tone alone doesn't help.
- **Use TypedDict instead of Pydantic for InvestDebateState**: rejected. Audit log uses `.model_dump()`; Pydantic is required.
- **Run research debate AFTER risk committee (between trader proposal and final approval)**: rejected. TauricResearch order is "research → trade → risk"; their reported wins assume this ordering.
- **Cap at MAX_ALLOWED_ROUNDS=5**: rejected. 3 matches risk committee cap, keeps envelope tight.

## Related

- ADR-0037 (LLM-backed committee) — the parent stage this work refines
- ADR-0043 (3-way risk committee) — the round-robin pattern this work mirrors
- ADR-0044 (Trader stage + structured output) — the consumer of ResearchPlan
- ADR-0058 (HMM regime classifier) — label-stability invariant ResearchPlan must respect
- TauricResearch G1, G6, G13, G17 (gap analysis) — origin of this work
