# ADR-0080: Self-evolution framework — the advisory plane, multi-rate retro tiers, and the held-out eval-gate contract

**Status:** Proposed
**Date:** 2026-05-30
**Wave:** Capstone (organizing framework; ratifies the self-evolution architecture, builds nothing here)
**Supersedes:** nothing
**Cites:** [ADR-0079](ADR-0079-perception-decision-reaction-architecture.md) (the PDR organizing architecture — this ADR is its self-evolution sibling: PDR is the *forward* path Perception→Decision→Reaction; this is the *backward* path outcome→reflection→advisory-belief→eval-gate→PDR), [ADR-0042](ADR-0042-persistent-memory-reflection.md) (reflection layer — the `tau_observable` Oracle-Fallacy guard the belief store inherits), [ADR-0048](ADR-0048-hypothesis-registry-and-run-cards.md) (hypothesis registry + Run-Cards — the candidate-hypothesis lifecycle T3 feeds), [ADR-0050](ADR-0050-alpha-zoo-with-ast-purity-and-lookahead-gate.md) (Alpha Zoo + AST-purity + lookahead sentinel — the factor surface the candidate-weight proposer reads), [ADR-0055](ADR-0055-factor-oracle-and-production-readiness-tiers.md) (FactorOracle 4-tier verdict — the producer the factor proposer consumes), [ADR-0052](ADR-0052-promotion-orchestrator-and-cron.md) (PromotionOrchestrator + cron — operator-action-only promotion, the outer gate this loop never bypasses), [ADR-0004](ADR-0004-risk-gate.md) (deterministic risk gate, silence-by-default — the FINAL authority, immutable by this loop), [ADR-0002](ADR-0002-analyst-protocol.md) (analyst protocol — beliefs enter only as prompt context, never change the contract), [ADR-0006](ADR-0006-rl-aggregator-deferred.md) (RL aggregator deferred — reaffirms "no weight-fine-tuning"; Hermes orchestrates frontier models), [ADR-0010](ADR-0010-settlement-journal.md) (deterministic settlement journal — the external-truth source, no LLM in the path)
**Grounded in:** `docs/research/2026-05-30-selfevolve-capability-map.md` (the definitive capability-gap map this ADR ratifies); its three input docs `docs/research/2026-05-30-r-selfevolve-{wiki-corpus,sota,current-audit}.md` (operator's own ~/wiki knowledge; frontier-paper mechanism+safety; code-level `file:line` loop trace).

> **This ADR ratifies an organizing framework; it does not build it.** W1 (the dark-loop ignition — `record_decision()` at the live decision site so a `pending` row exists, lighting the one already-closed reflection→PM-prompt edge) is **already shipped** (commit `08326e1`). Waves W2–W7 are scoped as *future waves* in Rollout §, each default-OFF and eval-gated. With every self-evolution flag OFF, the system is bit-for-bit today's R1-reflective (now lit) + R2-deliberative pipeline: reflection text → PM prompt → BMA → deterministic gate → silence-bias → paper. The deterministic risk gate, the hard risk limits, the discrete sizing ladder `{0, ±0.05, ±0.10, ±0.15, ±0.20}`, and the kill-switch sit **outside** this loop and are **immutable** by it.

---

## Context and Problem Statement

hermes-quant has reached an unusual place on the capability ladder: it possesses **every evidence producer** a self-evolving quantitative researcher needs — a per-trade reflector that scores realized **alpha-vs-benchmark** against an external truth (ADR-0042), separated adversarial roles (bull/bear/judge + a 3-way risk committee, ADR-0065), deterministic evidence-weighted aggregation (BMA, never majority vote), a hypothesis registry with reproducible Run-Cards (ADR-0048), a FactorOracle 4-tier verdict (ADR-0055), a promotion orchestrator with a held-out gate (ADR-0052), and an accreting catalyst-propagation corpus — yet, as the capability map establishes at `file:line`, **exactly one policy-affecting feedback edge is closed in code, and W1 just lit it.** Every other producer is display-only, operator-review-only, design-only, or gated-OFF. The map's one-sentence thesis: hermes-quant is a **reflective rule-executor at the R3 threshold**, not yet a self-evolving researcher (R4); the work ahead is **closing loops, not building components**.

The operator's north-star is R4: a system that periodically distills accumulated outcomes into a *bounded, decaying belief/weight delta*, proves it on held-out data the optimizer never saw, and ships it default-OFF behind an eval gate — **and a meta-tier that evolves *how* it learns.** The hazard is named and empirically grounded: the research surfaces a now-named failure area — *Misevolution* (ICLR 2026) shows memory/tool/workflow self-evolution measurably **degrades safety alignment after memory accumulation**, even on frontier-class models; *reward-hacking* becomes recursive once the policy can see its evaluator; *model-collapse* accelerates when a model conditions on its own un-validated output as truth; and the operator's own measured instances (MT3 "pure train-score winner = overfit garbage"; AMZN-weight "the 30% peak IS overfit — use a RANGE") prove that selecting on the in-sample peak ships overfit garbage. TradingGroup's ablation is the decisive datum: removing the hard risk module produced the *largest* return on one ticker but **−14.4% on TSLA and losses on 4/5 datasets** — empirical proof that self-evolution must sit *behind*, never *replace*, the deterministic risk gate.

The problem this ADR solves: **define an architecture in which the system can climb from R1 to R4 without ever becoming a system that can talk itself out of its own risk limits.** The capability map's answer — which this ADR ratifies — is a strict separation between an **advisory plane** (the only thing self-evolution may write) and an **outer standard-of-truth** (the deterministic OOS backtest + promotion gate + risk gate, immutable by the loop), tiered across four update rates, every tier governed by one universal eval-gate contract.

---

## Decision Drivers

The safety frame from the capability map §5. Every wave inherits these; they are non-negotiable.

- **D-1 The advisory plane is the only writable surface.** Self-evolution writes ONLY to *beliefs / hypotheses / candidate-weights / candidate-edges* — a strictly advisory plane that feeds the inner judge. It feeds context and proposals; it never writes a live limit, a live size, or live code.
- **D-2 The outer standard-of-truth is immutable by the loop.** The deterministic risk gate (ADR-0004), the hard risk limits (max loss, position caps, exposure), the discrete sizing ladder `{0, ±0.05, ±0.10, ±0.15, ±0.20}`, and the kill-switch sit OUTSIDE the loop and are immutable by it. The kill-switch is a separate process the agent runtime cannot signal (the inverse of moon-dev `risk_agent.py:319`, the canonical worst pattern — an LLM disabling a loss limit via free-text substring match).
- **D-3 External-truth evaluator ONLY.** Reward = realized alpha-vs-benchmark computed deterministically from market data (ADR-0010 settlement journal, no LLM in the path). Never an LLM self-score; never the agent's own narrative re-ingested as truth. The agent cannot author the signal that grades it (reward-hacking is recursive once it can).
- **D-4 The held-out gate is necessary, NOT sufficient.** A candidate must clear a walk-forward / time-advanced held-out window the optimizer never saw — and passing is *necessary not sufficient*, so operator/eval-gated promotion stays (ADR-0052). Eval-gaming is recursive; the held-out gate catches "fixes" that feel right but regress (the operator's own SkillOpt save: a mechanically-correct fix regressed recall@5 0.478→0.406).
- **D-5 Select on robustness/plateau, NEVER the in-sample peak.** Jitter-stable plateau + OOS; "use a RANGE, not the decimal-optimized point" (MT3 + AMZN-weight evidence). **Checkpoint-fallback:** never ship an evolved variant that does not *strictly* beat prior-best on held-out — revert to the prior checkpoint if not.
- **D-6 Bounded, decaying, Oracle-provenance-tagged belief store.** Cap active beliefs (Reflexion Ω / FINCON small-set); FINMEM-style access-counter + half-life so stale-regime lessons fade deterministically (non-LLM promotion/expiry — no self-grading); every belief tagged as the agent's OWN prior output (carry the ADR-0042 `tau_observable` guard through distillation) and never re-ingested as ground truth.
- **D-7 Propose-only; deterministic aggregation; surface dissent.** Every new component PROPOSES; only the existing operator/eval-gated promotion machinery applies. Aggregate by evidence weighting, never majority vote (the conformity trap). Disagreement after N turns → FLAT, not a forced winner.
- **D-8 Default-OFF + eval-gated for every component.** Every wave ships behind a `HERMES_QUANT_*` flag, default OFF, byte-identical off-state, promoted only after it clears its own eval gate and an operator audit.

---

## Considered Options

- **Option A — The advisory-plane + multi-rate-tier framework** (CHOSEN)
- **Option B — A single monolithic "evolve everything" optimizer**
- **Option C — RL / weight-fine-tuning of an in-house policy model**

### Option A — Advisory plane + multi-rate tiers + a universal eval-gate contract (CHOSEN)

Ratify the capability map's architecture. Define two strictly separated planes and four update-rate tiers, governed by one eval-gate contract:

- **The ADVISORY PLANE** — the only surface self-evolution writes: *beliefs* (distilled verbal lessons, bounded/decaying/Oracle-tagged), *hypotheses* (falsifiable, novelty-gated), *candidate-weights* (factor/BMA weight diffs, silence-only within a cap), *candidate-edges* (catalyst-graph FLIP/DOWNWEIGHT/PRUNE proposals). It feeds the *inner* judge (the committee/LLM as cheap evidence).
- **The OUTER STANDARD-OF-TRUTH** — the deterministic OOS backtest + the promotion gate (ADR-0052) + the risk gate (ADR-0004). It is the only path to live policy, sits outside the self-evolving loop, and is immutable by it. (QuantAgent / FunSearch inner-cheap-judge / outer-real-eval two-loop made explicit.)
- **The multi-rate tiers** (HRM + Nested Learning frame): **T0 tick** (market ingest, signal compute, BMA vote); **T1 per-trade** (per-close reflection scored on external alpha — W1, shipped); **T2 weekly** (FINCON CVRF pattern-mining retro → bounded belief deltas — W2); **T3 monthly-meta** (the MISSING tier: distill weekly digests + debate/promotion records into candidate hypotheses + persona-calibration telemetry — W3). Telemetry-first, off-by-default (Eidolon Stage-1 template).
- **The universal eval-gate contract** every wave's flag must pass to flip: (1) external-truth scored (D-3); (2) clears a held-out window the optimizer never saw (D-4); (3) selected on robustness/plateau with checkpoint-fallback (D-5); (4) belief/proposal budget capped, Oracle-provenance preserved (D-6); (5) propose-only — operator/eval-gated promotion applies it (D-7).
- The six waves (W1–W7, W6 absorbing the cron) are the rollout, each closing one named open loop from the capability map's leverage ordering.

**Pros / cons** — see Pros and Cons of the Options below.

### Option B — A single monolithic "evolve everything" optimizer

One loop that jointly optimizes prompts, factor weights, catalyst edges, hypothesis priors, *and* (in the limit) risk parameters against one combined objective, applied automatically on a clear-gate.

### Option C — RL / weight-fine-tuning of an in-house policy model

Train (or fine-tune via RL/post-training) a model whose weights *are* the trading policy, updated from realized returns — the FLAG-Trader hierarchical-RL direction.

---

## Decision Outcome

Chosen option: **Option A — the advisory-plane + multi-rate-tier framework**, because it is the only option that lets hermes-quant climb R1→R4 while keeping the deterministic gate, hard limits, sizing ladder, and kill-switch *structurally* outside and immutable by the loop — the inner-advisory / outer-deterministic separation is exactly what prevents the system from ever talking itself out of its own risk limits.

### D80.1 The two planes and the immutability invariant

| Plane | What self-evolution may do | Authority over live policy |
|---|---|---|
| **ADVISORY PLANE** (beliefs / hypotheses / candidate-weights / candidate-edges) | WRITE freely — distill, propose, decay, tag. Feeds the *inner* judge (committee/LLM = evidence). | **none** — proposals only |
| **OUTER STANDARD-OF-TRUTH** (deterministic OOS backtest + promotion gate ADR-0052 + risk gate ADR-0004 + sizing ladder + kill-switch) | **READ-ONLY to the loop.** The loop cannot mutate it. | **the sole authority** — and only the operator/eval-gate flips a candidate live |

**The defining invariant:** the only path from "evolved idea" to "live policy" runs *through* the outer standard-of-truth, which the loop can never modify. Authority is concentrated, exactly as ADR-0079 concentrates it at the gate in the forward (PDR) direction; this ADR concentrates it at the same gate in the backward (learning) direction. The hard risk limits, the discrete sizing ladder, and the kill-switch are **never** in the advisory plane — they are the things the loop most plausibly "learns" it should relax, and that is precisely why they are walled off (TradingGroup −14.4% ablation; moon-dev `risk_agent.py:319` inverse).

### D80.2 The multi-rate tiers (T0 / T1 / T2 / T3)

| Tier | Cadence | Object | Writes to advisory plane? | Wave | Status |
|---|---|---|---|---|---|
| **T0** | every tick | market ingest, signal compute, BMA vote, deterministic gate | no (forward path) | — | live |
| **T1** | per-trade | per-close reflection scored on external alpha; `pending`→resolved chain; lessons → PM prompt | yes — beliefs (one reflection) | **W1** | **shipped (`08326e1`)** |
| **T2** | weekly | FINCON CVRF: split winners/losers by realized **alpha**, distill ≤N belief-deltas, decay/promote via FINMEM access-counter; writes `weekly_retro_promotion_readiness` (closes the dangling gate field, O3) | yes — beliefs (distilled, capped) | **W2** | future, default-OFF |
| **T3** | monthly-meta | aggregate weekly digests + debate/risk-committee audit rows + promotion records → candidate hypotheses + persona-calibration **telemetry** (recommendations-only); novelty/dedup-gated; the MISSING tier | yes — hypotheses + candidate-personas (telemetry only until ≥M months of agreement) | **W3** | future, default-OFF |

T3 evolves *how* the system learns (the meta-tier) — but only by *proposing* candidate hypotheses and emitting persona-calibration telemetry; it never auto-promotes. Multi-rate is "not a free lunch": each tier is instrumented as **recommendations-only** before it gates anything, the off-state is byte-identical, and each potential firing logs "what would have changed."

### D80.3 The universal eval-gate contract (every wave's flag must pass ALL five to flip)

1. **External-truth** — scored on realized alpha-vs-benchmark from market data (ADR-0010), never an LLM self-score.
2. **Held-out** — clears a walk-forward / time-advanced window the optimizer never saw; passing is *necessary not sufficient* → operator/eval-gated promotion stays (ADR-0052).
3. **Robustness-not-peak** — jitter-stable plateau + OOS; **checkpoint-fallback**: never ship a variant that does not strictly beat prior-best on held-out.
4. **Bounded + provenance** — belief/proposal budget capped; FINMEM half-life decay; every belief Oracle-tagged as the agent's own prior output (ADR-0042 `tau_observable` carried through distillation).
5. **Propose-only** — the component PROPOSES; only the existing operator/eval-gated promotion machinery applies; deterministic aggregation (no majority vote); disagreement after N turns → FLAT.

### D80.4 The FINMEM-style bounded/decaying/Oracle-tagged belief store

Beliefs are not an unbounded log. The store is **bounded** (cap active beliefs — Reflexion Ω / FINCON small-set), **decaying** (FINMEM access-counter + tiered half-life: a belief pivotal to a *profitable* trade gains importance and a slower-decaying tier; un-retrieved beliefs decay below threshold and purge), and **deterministic** in its promotion/expiry (a non-LLM rule — no self-grading of what to keep). Every belief carries the Oracle-Fallacy provenance tag — it is the agent's OWN prior output, never re-ingested as ground truth — which directly counters the Misevolution "memory-accumulation → safety degradation" vector and model-collapse.

### D80.5 The propose-only invariant and selective propagation

Every component is a *proposer*. Beliefs are selectively propagated to *only the relevant committee role's prompt* (FINCON selective propagation — avoids the echo-chamber), never broadcast to all. Candidate-weights are silence-only within a cap (premium↑ capped, rejected→0; never amplify above the cap). Candidate-edges are silence-only `confidence_multiplier`s; the seed YAML is never auto-edited. Nothing a proposer emits reaches live policy except through D80.1's outer gate.

### D80.6 The six waves are the rollout (capability-map §4)

| Wave | Closes (open loop) | Advisory-plane object | Flag | Status |
|---|---|---|---|---|
| **W1** — ignite the dark edge: production decision recorder | O1 (reflection→retriever→PM-prompt) | belief (per-trade) | `HERMES_QUANT_REFLECTION` (+`_MEMORY_INJECT`) | **shipped (`08326e1`)** |
| **W2** — weekly pattern-mining retro (per-trade→weekly, FINCON CVRF) | O2 + O3 (writes `weekly_retro_promotion_readiness`) | belief (distilled, capped, decaying) | `HERMES_QUANT_WEEKLY_RETRO` | future, default-OFF |
| **W3** — monthly meta-retro (weekly→monthly; the MISSING T3 tier) | O7 + O8 (debate/promotion → candidate hypotheses) | hypothesis + persona telemetry | `HERMES_QUANT_MONTHLY_META_RETRO` | future, default-OFF |
| **W4** — factor-verdict → BMA-weight proposer (silence-only) | O4 (+ unblocks O6 on fill-joining) | candidate-weight | `HERMES_QUANT_FACTOR_WEIGHT_PROPOSER` | future, default-OFF |
| **W5** — B10 learned-graph miner (catalyst edges) | O5 | candidate-edge | `HERMES_QUANT_GRAPH_MINING` | future, default-OFF |
| **W6** — hypothesis→backtest→promote driving cron (inner/outer rail explicit) | O8/O9 wiring | hypothesis → Run-Card (operator promotes) | `HERMES_QUANT_RESEARCH_LOOP` | future, default-OFF |
| **W7** — self-critique / red-team deliberation upgrade (Socratic devil's-advocate) | the CRITIQUE-axis gap (R2 ◐→●) | belief (persona calibration inputs) | `HERMES_QUANT_REDTEAM_TURN` | future, default-OFF |

**Dependency graph:** W1 → {W2 → W3 → W6}; W1 → W4; W1 → W5; W1 → W7. W4/W5/W7 are parallelizable once W1 lands; W3 is the convergence point (consumes W2; feeds W6).

### Consequences

- **Positive**: The operator gets the R4 north-star — periodic distillation into a bounded, decaying, eval-gated belief/weight delta with a meta-tier — *without* the loop ever being able to touch a risk limit, a size, or the kill-switch. The inner-advisory / outer-deterministic separation is the single structural property that makes self-evolution safe in money-software.
- **Positive**: Every one of the capability map's nine open loops (O1–O9) is given a wave and a place; the system stops being an evidence-producer factory whose feedback edge never closes (the M14 finding), and instead closes loops in leverage order.
- **Positive**: The framework is additive and reversible. With every self-evolution flag OFF, the system is byte-identical to today's R1-lit + R2 pipeline; W1 (shipped) already proves the off→on transition is gated and bounded.
- **Positive**: Reuses every existing surface (reflector, retriever, decisions, debate, hypothesis registry, FactorOracle, promotion orchestrator, propagation corpus) — the work is wiring, not rebuilding — so the eval surface grows on top of components already proven in isolation.
- **Negative (REQUIRED)**: **Eval-gaming is recursive.** A held-out gate is necessary but not sufficient: optimization pressure accumulates against *any* fixed benchmark, and eval scores say nothing about behavior when the deployment distribution diverges from the eval window. The mitigation (walk-forward / time-advanced windows + mandatory human-in-the-loop promotion that never automates) is a *permanent operational cost* — HITL never goes away, which caps how "autonomous" R4 can ever be in this system.
- **Negative**: **Belief-store drift / Misevolution is the loop's own #1 documented degradation vector.** The very memory/reflection tier this framework builds is empirically shown (ICLR 2026) to degrade safety alignment as memory accumulates. The bounded/decaying/Oracle-tagged store and the external-truth-only rule bound the risk but do not eliminate it; a mis-tuned half-life or budget cap could let stale-regime beliefs persist and quietly bias prompts, and detecting that drift requires its own monitoring the framework does not yet specify.
- **Negative**: **Cron cost and silent-staleness.** Four nested retro tiers add scheduled compute (LLM-distillation passes, walk-forward backtests) and operational surface; a slow-tier component (T2/T3) that breaks fires rarely enough to stay broken silently — mitigated only by the "log what would have changed on every potential firing" discipline, which is itself code that must be maintained.
- **Negative**: **The held-out gate is necessary-not-sufficient, so the system can never fully close its own loop.** Selecting on robustness-not-peak + checkpoint-fallback bounds the *direction* of overfit error but not its cost; a robustness-selected variant can still underperform out-of-regime. The framework deliberately keeps the operator in the promotion path forever, which means "self-evolving" here always means "proposes; a human still ships."
- **Neutral**: RL / weight-fine-tuning and Tier-3 generated-code architecture evolution are explicitly out of scope (Hermes orchestrates frontier models; code-evolution stays DEFERRED until a sandbox + a 30-day-clean-dogfood trigger exist). FINMEM's self-adaptive risk-character (flip conservative on cumulative loss) is admitted only as *advisory* telemetry, never auto-applied to limits.
- **Neutral**: The advisory plane is a new central concept that future signals must map onto (belief / hypothesis / candidate-weight / candidate-edge); like any organizing taxonomy it can ossify, though its four categories are deliberately broad.

## Pros and Cons of the Options

### Option A — Advisory plane + multi-rate tiers + universal eval-gate contract (CHOSEN)

- Good, because it makes the inner-advisory / outer-deterministic separation a *structural* property: the risk gate, hard limits, sizing ladder, and kill-switch are not "protected by policy" but literally outside the writable plane — the loop cannot reach them.
- Good, because it reuses every existing producer and the existing operator/eval-gated promotion machinery; the work is closing loops, not building components (capability-map thesis).
- Good, because it is additive, reversible, and default-OFF per wave; W1 (shipped) already demonstrates the gated off→on transition.
- Good, because the multi-rate framing supplies the one genuinely missing tier (T3 monthly-meta) telemetry-first, with a documented byte-identical off-state.
- Bad, because it is a multi-wave commitment (seven waves), not a one-shot fix; under-prioritized, it risks being ratified-but-not-realized — a framework doc without the loops closed.
- Bad, because four nested tiers add cron cost and slow-tier silent-staleness risk that must be actively monitored.
- Bad, because it keeps a permanent human-in-the-loop promotion cost (eval-gaming is recursive), capping how autonomous the system can become.

### Option B — A single monolithic "evolve everything" optimizer

- Good, because one objective and one loop is conceptually simpler and could, in principle, find cross-surface optima a tiered/walled approach misses.
- Good, because there is a single place to instrument and a single gate to clear.
- Bad, because a combined objective that *can* touch risk parameters is exactly the TradingGroup −14.4% / moon-dev `risk_agent.py:319` failure mode — the optimizer's fastest path to "more return" is to relax the risk module, and a monolith has no structural wall preventing it.
- Bad, because joint optimization over prompts + weights + edges + risk maximally invites reward-hacking and the Correlation-Red-Sea collapse (all surfaces converge to one over-fit objective), and auto-applying on clear-gate removes the necessary HITL.
- Bad, because it discards the proven inner-cheap-judge / outer-standard-of-truth separation (QuantAgent / FunSearch) that the safety frame depends on.
- **Rejected** on rails grounds: it has no structural immutability boundary, so it can talk itself out of its own risk limits.

### Option C — RL / weight-fine-tuning of an in-house policy model

- Good, because a policy whose weights *are* the strategy is the most expressive form of learning and matches the most-cited RL-trading direction (FLAG-Trader).
- Good, because it could, in theory, internalize patterns no prompt-level belief can express.
- Bad, because **Hermes orchestrates frontier models — building/fine-tuning an in-house policy model is explicitly on the wiki "do NOT build" list** (ADR-0006 already deferred the RL aggregator on the same grounds).
- Bad, because a learned-weight policy is opaque, hard to audit per-decision, and cannot carry the per-belief Oracle-provenance tag the safety frame requires; reward-hacking in a weight-space policy is far harder to detect than in a bounded verbal belief store.
- Bad, because it conditions the policy on the agent's own trade history (model-collapse / feedback-loop poisoning) and needs a training/serving stack the single-operator paper system does not have.
- **Rejected** per the wiki and ADR-0006: the system's leverage is orchestration of frontier models, not gradient-trained in-house weights.

## More Information

- **Relationship to ADR-0079 (sibling capstone):** ADR-0079 ratifies the *forward* path (Perception → Decision → Reaction) and concentrates authority at the gate. ADR-0080 ratifies the *backward* path (outcome → reflection → advisory belief → eval-gate → back into PDR) and concentrates authority at the *same* gate. Together they are the two halves of one R4 system: PDR is how it acts; self-evolution is how it learns to act better — both immutable at the deterministic gate.
- **The seed already in code:** `catalyst/profitability.py` (per-relation-class PROFITABLE / UNPROFITABLE_CONSIDER_PRUNE / INSUFFICIENT_SAMPLE; `MIN_SAMPLE=20` / `MIN_HIT_RATE=0.6`) is SkillOpt's held-out gate instantiated once, for one relation class. W4/W5 generalize this seed across the factor and catalyst-edge surfaces.
- **Explicitly NOT adopted (rail violations, per capability-map §3):** RL post-training / weight-fine-tuning; FINMEM's self-adaptive risk-character auto-applied to limits (advisory-only); any moon-dev pattern (LLM-overridable loss limit via substring match); Tier-3 generated-code architecture evolution (DEFERRED until a sandbox + 30-day-clean-dogfood trigger).
- **Verification of the off-state (every future wave):** with its flag OFF, the wave is byte-identical to today; the universal eval-gate contract (D80.3) is the checklist every flag must pass to flip; promotion to live is always a separate, explicit operator action (ADR-0052), never bundled with the build.
- **Source of truth:** `docs/research/2026-05-30-selfevolve-capability-map.md` (and its three input docs) is the detailed design this ADR ratifies. The capability ladder (R0–R4), the nine open loops (O1–O9), the adoptable-mechanism table, and the safety frame §5 are all grounded there.
