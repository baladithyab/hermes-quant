# ADR-0037: LLM-Backed Committee Turns (Bull/Bear/Risk-Mgmt Debate)

**Status:** Accepted (2026-05-26), implemented
**Date:** 2026-05-26
**Wave:** Multi-agent deliberation (port TradingAgents bull/bear + risk-mgmt patterns)
**Related:** ADR-0023 (Deliberative Committee), ADR-0026 (Retrospective Amendment Loop), ADR-0035 (Cadence), ADR-0036 (Multi-Timeframe Fan-Out), TradingAgents reference research at `docs/research/reference-projects/2026-05-24-r1-tradingagents.md`
**Cost:** ~$0.02-0.10 per symbol per daily tick (gated, dry-run-first, opt-in)

---

## Context

ADR-0023 designed the deliberative-committee aggregator and shipped a
**deterministic skeleton** (`hermes_quant/aggregators/deliberative.py`) that:

- Honors the two-tier LLM split (`quick_thinking_llm` for analysts/debaters,
  `deep_thinking_llm` for managers — per TradingAgents R1 §2)
- Implements the deterministic turn cap (`bull_bear_count >= 2 *
  max_debate_rounds`) per TradingAgents R5 routing-level convergence
- Accepts inbound `CommitteeTurn` objects via `context.extras["committee_turns"]`
- Strips `messages`/`tool_calls`/`context_messages`/`prior_messages` from
  metadata (msg-clear pattern preventing prompt-injection)
- Falls back to baseline BMA when deliberation does not improve certainty

What's **not yet implemented**: the LLM caller that *produces* the bull/bear
turns. The seam is open (`context.extras["committee_turns"]`); ADR-0023 §
"Model-mixture contract" reserved the work as future scope.

The TradingAgents reference architecture (R1 §1b–1f) prescribes:

1. Bull researcher reads all analyst reports → emits prose case
2. Bear researcher mirror-image
3. Research Manager (deep-tier) judges → structured `ResearchPlan(rating,
   rationale, strategic_actions)`
4. Trader (deep-tier) → `TraderProposal(action, reasoning, sizing)`
5. Risk-mgmt triumvirate (aggressive/conservative/neutral) — round-robin
   3-way debate
6. Portfolio Manager (deep-tier) → final `PortfolioDecision`

R1 §3 + R1 §"What hermes-quant has that they don't" identified that we should
**preserve our calibrated `AnalystView` schema** (don't degrade to free-text
markdown) but **add the bull/bear/judge layer on top** of the existing
analyst tier.

## Decision

Adopt a **two-stage LLM committee** that runs **on top of** the existing BMA
aggregator, **not as a replacement**:

```
[stage 1: existing BMA]  AnalystView × N → AggregatedSignal (BMA)
                                ↓
[stage 2: NEW LLM committee]    BMA signal + AnalystView reports
                                  → Bull turn (LLM, quick-tier)
                                  → Bear turn (LLM, quick-tier)
                                  → Research Manager judge (LLM, deep-tier)
                                  → final AggregatedSignal (committee)
```

The deterministic-committee path stays as the **default**; the LLM path is
**opt-in** via:

- `HERMES_QUANT_DELIBERATIVE=1` — enable LLM bull/bear turns
- `HERMES_QUANT_DELIBERATIVE_RISK=1` — additionally enable risk-mgmt triumvirate
- `HERMES_QUANT_DELIBERATIVE_MODEL_QUICK=anthropic/claude-haiku-4.6` (default)
- `HERMES_QUANT_DELIBERATIVE_MODEL_DEEP=anthropic/claude-sonnet-4.6` (default)

### Bull / Bear turn structure

Each turn is one LLM call producing a `CommitteeTurn` with structured output:

```python
class BullBearTurn(BaseModel):
    role: Literal["bull_researcher", "bear_researcher"]
    stance: str             # one-line summary
    confidence: float       # 0-1, the LLM's self-assessed strength
    rationale: str          # ≤500 words narrative
    key_evidence: list[str] # references to analyst views, max 5
    counterarguments: str   # what the other side will say (sharpens thinking)
    metadata: dict          # tier='quick', model_id, prompt_hash
```

Two prompts (see `hermes_quant/aggregators/prompts/bull_bear.md`):

- **Bull prompt** receives the full BMA signal, all analyst views (already
  structured, not free-text), and the prior bear turn (if any). Asked to make
  the strongest possible case to enter the position.
- **Bear prompt** mirror image. Critically, asked to identify failure modes
  the bull case is glossing over.

Both prompts include a literal `silence-by-default` instruction: if the
analyst evidence is weak, say so explicitly with confidence < 0.5 — do not
manufacture conviction.

### Research Manager judge

Single deep-tier LLM call. Receives:

- All analyst views (structured)
- Both bull and bear turns
- The deterministic baseline BMA signal (so the LLM has a measurable to
  agree-with-or-overrule)

Produces:

```python
class ResearchPlan(BaseModel):
    recommendation: Literal["Buy", "Overweight", "Hold", "Underweight", "Sell"]  # 5-tier per R1
    confidence: float          # 0-1, calibrated against historical hit rate
    rationale: str             # ≤300 words
    overrules_baseline: bool   # True iff direction differs from BMA baseline
    strategic_actions: str     # ≤200 words: what to do if entered
    horizon_emphasis: str | None  # which horizon (1d/1w/1M) drove the decision
```

The prompt explicitly says "reserve Hold for genuinely-balanced cases; lean
to Underweight or Overweight when there's any tilt" (mirrors R1 §2 verbatim).

### Risk-management triumvirate (opt-in, second-stage)

Three additional LLM calls (aggressive / neutral / conservative) when
`HERMES_QUANT_DELIBERATIVE_RISK=1`. Each receives the bull, bear, judge, and
proposed entry/sizing. Each emits a `RiskTurn`:

```python
class RiskTurn(BaseModel):
    role: Literal["risk_aggressive", "risk_conservative", "risk_neutral"]
    stance: str
    proposed_size_multiplier: float   # 0.0-2.0; 1.0 = honor judge's size, 0 = veto
    confidence: float
    rationale: str
    risk_flags: list[str]             # e.g. ["earnings_in_5d", "vol_spike", "concentration_breach"]
```

The Portfolio Manager (deep-tier) merges the three risk turns:

```
final_size = judge_proposed_size × median(risk_size_multipliers)
```

Risk veto: if **any** risk turn has `proposed_size_multiplier == 0.0`, the
proposal is **silenced** (gate-rejected by the existing risk gate as
`risk_committee_veto`). This makes the LLM committee an *additional* safety
layer, never a way to amplify a marginal proposal.

### Two-tier LLM split (per ADR-0023 + R1 §"two-LLM tiers")

| Role | Tier | Default model |
|---|---|---|
| bull_researcher | quick | claude-haiku-4.6 |
| bear_researcher | quick | claude-haiku-4.6 |
| risk_aggressive | quick | claude-haiku-4.6 |
| risk_conservative | quick | claude-haiku-4.6 |
| risk_neutral | quick | claude-haiku-4.6 |
| **research_manager** | **deep** | **claude-sonnet-4.6** |
| **portfolio_manager** | **deep** | **claude-sonnet-4.6** |

The `_DEEP_REQUIRED_ROLES` set in `aggregators/deliberative.py` is already
hard-coded with these two; the deterministic skeleton rejects quick-tier
turns bound to deep-required roles (lines 346-352). LLM caller honors this
constraint by construction.

### Turn-cap respected

The existing deterministic logic (`bull_bear_count >= 2 * max_debate_rounds`,
default 1 round = 2 turns total) caps the LLM debate identically. Even with
`max_debate_rounds=3`, the cost is bounded:

- Default: 1 bull + 1 bear + 1 judge = 3 quick + 1 deep = ~$0.02/symbol
- `max_debate_rounds=2`: 2 bull + 2 bear + 1 judge = ~$0.04/symbol
- `max_debate_rounds=3` + risk-mgmt: 3 bull + 3 bear + 1 judge + 3 risk +
  1 portfolio mgr = ~$0.08-0.10/symbol

For a 30-symbol active watchlist, the daily LLM cost ceiling is
**~$0.60-3.00/day**, well within the cost ceiling ADR-0026 §"LLM cost
discipline" allowed for the retrospective layer.

### Caller wiring

A new module `hermes_quant/aggregators/llm_committee.py`:

```python
def run_llm_committee(
    *,
    market_context: MarketContext,
    analyst_views: list[AnalystView],
    baseline_signal: AggregatedSignal,  # the BMA output
    config: DeliberativeConfig,
) -> list[CommitteeTurn]:
    """Run bull/bear/judge (and risk-mgmt if enabled). Return turns to push
    into context.extras['committee_turns'] for the existing aggregator to
    consume."""
```

The deterministic aggregator's `_model_turns_from_context` (lines 315-360)
already validates inbound turns; the LLM caller does NOT have a privileged
path. It builds turns and pushes them into the same context-keyed list the
deterministic skeleton already consumes. **Same audit semantics, same
turn-cap, same msg-clear.**

### Failure-closed posture

Any LLM call that raises, times out, returns invalid structured output, or
fails Pydantic validation → that turn is **dropped**. Two consecutive drops
on the same role within one tick → **fall back to the deterministic
skeleton entirely** for that symbol. The aggregator's existing fallback to
baseline BMA when committee is degraded covers this.

### Prompt evidence

Each turn's `metadata` carries a SHA-256 `prompt_hash` of the rendered
prompt + system message. The hash is logged to the journal alongside the
analyst-view IDs the turn referenced. This is the audit trail required by
the convergent finding CV1 in the synthesis doc (evidence-IDs linkage)
applied to the committee layer.

## Consequences

### Positive

- **Closes the TradingAgents-pattern port** — bull/bear/risk-mgmt is the
  most-frequently-cited reference-project feature; landing it productionizes
  ADR-0023.
- **Two-tier LLM split saves cost** — quick model for high-volume bull/bear,
  deep model only for the judge. ~10× cost difference between
  haiku-4.6 and sonnet-4.6 means most of the spend is on the few decision
  points where it matters.
- **Failure-closed by construction** — LLM unreliability degrades gracefully
  to the existing deterministic skeleton; no new failure modes vs ADR-0023.
- **Adds a *real* risk gate** — the risk-mgmt triumvirate's 0.0-multiplier
  veto is independent of the deterministic risk gate (ADR-0004) and catches
  things the rule-based gate doesn't (e.g. "earnings 4 days out, gate said
  fine, but bear-mgmt sees the concentration risk").
- **Compatible with multi-timeframe (ADR-0036)** — the bull/bear prompts
  receive analyst views tagged by horizon, and judges can emit
  `horizon_emphasis` to record which horizon swung the decision.

### Negative

- **LLM API dependency for a money-software path.** Mitigated by:
  failure-closed fallback to deterministic; opt-in env var (default-off);
  cost cap; the deterministic risk gate (ADR-0004) is **still** the final
  authority before any order routes — committee can only silence, never
  amplify.
- **Latency budget.** Each LLM call adds 1-3 s. With default
  bull+bear+judge = 3 calls = ~5-9 s additional latency per symbol. For a
  30-symbol watchlist, ~3-5 minutes added to the daily tick. Still inside
  pre-open window. With risk-mgmt triumvirate enabled, doubles to
  ~10-15 minutes — must run earlier in the cron schedule.
- **LLM hallucination at the bull/bear stage.** Mitigated by structured
  output (Pydantic validation rejects garbage), key_evidence-must-cite-IDs
  (forces grounding), counterarguments-required-field (forces engagement
  with the other side), and the deep-tier judge (catches drift).
- **Prompt drift over LLM versions** — when haiku-4.6 → haiku-5 lands, bull
  prompts may behave differently. Mitigation: pin model versions in the env
  var; prompt_hash in audit log lets us replay historical prompts.

### Neutral

- The 5-tier rating scale (Buy/Overweight/Hold/Underweight/Sell) is a port
  from TradingAgents; we preserve our existing 2-tier `Direction` enum on
  `AnalystView` and accept the 5-tier only on the judge's output. Avoids a
  schema migration on every analyst.

## Out of scope

- **LLM-driven analysts.** The bull/bear/judge layer sits *on top of*
  structured `AnalystView` outputs; analysts themselves stay quantitative
  (or semantic-LLM-already-structured per ADR-0022). We do NOT migrate any
  existing analyst to free-text-LLM.
- **Auto-tuning of `max_debate_rounds`.** Fixed default of 1; user-tunable
  via config. Auto-tuning belongs to the retrospective-amendment loop
  (ADR-0026).
- **Multi-symbol cross-talk.** Each symbol's committee is independent. We do
  NOT pass other symbols' bull/bear turns into a given symbol's prompts —
  that would amplify correlated calls and is an anti-pattern from R6
  (moon-dev cross-symbol contagion).

## Implementation notes

- `hermes_quant/aggregators/llm_committee.py` is the new module.
- `hermes_quant/aggregators/prompts/bull_bear.md`,
  `prompts/research_manager.md`, `prompts/risk_*.md`,
  `prompts/portfolio_manager.md` — versioned prompt templates loaded at
  module import.
- `DeliberativeConfig` in `aggregators/deliberative.py` gains:
  `enable_llm_turns: bool = False`, `enable_risk_mgmt: bool = False`,
  `quick_model: str`, `deep_model: str`, `max_tokens_per_turn: int = 800`,
  `prompt_hash_in_journal: bool = True`.
- Tests:
  - `tests/unit/test_llm_committee_caller.py` — mock LLM, assert structured
    output validation, turn-cap respect, failure-closed fallback
  - `tests/unit/test_llm_committee_prompts.py` — golden-file tests for
    rendered prompts (so prompt drift surfaces in PR review)
  - `tests/integration/test_llm_committee_smoke.py` — live LLM call,
    `@pytest.mark.skipif("HERMES_QUANT_LIVE_LLM" not in os.environ)`
- Activation procedure (mirrors ADR-0035 §"Activation procedure"):
  1. `HERMES_QUANT_DELIBERATIVE=1` → bull/bear turns produced, judge runs,
     turns logged to journal, but committee output is *parallel-tracked*
     (not yet promoted to the gate)
  2. After ≥1 week of clean shadow runs, set
     `HERMES_QUANT_DELIBERATIVE_PROMOTE=1` to make the committee output
     the gating signal
  3. Add `HERMES_QUANT_DELIBERATIVE_RISK=1` for the risk-mgmt triumvirate
     once judge promotion has been stable for ≥1 week

## Decision summary

We commit to **LLM-backed bull/bear/research-manager turns** (and an opt-in
risk-mgmt triumvirate) layered **on top of** the existing BMA aggregator,
gated by env var, with two-tier LLM split, structured-output validation,
failure-closed fallback to the deterministic skeleton, and a shadow-mode
activation path. The TradingAgents reference is now ported in posture, not
just in deterministic skeleton.
