# Self-Evolving Quantitative Researcher — Definitive Capability-Gap Map

**Date:** 2026-05-30
**Author:** lead quant-systems architect (synthesis pass)
**Inputs synthesized (read in full):**
- `docs/research/2026-05-30-r-selfevolve-wiki-corpus.md` — operator's own ~/wiki knowledge (PRIMARY)
- `docs/research/2026-05-30-r-selfevolve-sota.md` — frontier-paper mechanism + safety layer
- `docs/research/2026-05-30-r-selfevolve-current-audit.md` — code-level loop trace (`file:line`)

**Thesis (one sentence).** hermes-quant has every *evidence producer* a self-evolving researcher needs, but exactly **one** policy-affecting feedback edge is closed in code — and that one is *starved* — so the system today is a **reflective rule-executor**, not a self-evolving researcher; the work is closing loops, not building components.

**Rails (reaffirmed, load-bearing for every wave below).** Deterministic risk gate is FINAL authority. LLM/committee = evidence, never authority. All new loop components default-OFF and eval-gated. No lookahead; no overfit-to-own-history. Self-evolution may NEVER mutate the hard risk limits or the discrete sizing ladder, and the kill-switch stays a separate process the agent runtime cannot signal.

---

## 1. The Capability Ladder

Five rungs, evaluated on four independent axes. The audit pins where hermes-quant sits **today** on each.

| Rung | Definition | reflect | critique | deliberate | evolve |
|---|---|---|---|---|---|
| **R0 Rule-executor** | Deterministic signal→gate→order; no memory of outcomes. | — | — | — | — |
| **R1 Reflective** | Scores each closed trade vs an *external* truth (alpha-vs-benchmark), writes a verbal lesson, can re-read it. | **● HERE** | | | |
| **R2 Self-critiquing** | Adversarial roles (bull/bear/judge, risk committee) argue *before* a decision; dissent shapes confidence. | | **◐ HERE** | **● HERE** | |
| **R3 Hypothesis-driven** | Forms falsifiable hypotheses, backtests them out-of-sample, promotes survivors to live influence on a cadence. | | | **◐** | **◐ HERE** |
| **R4 Self-evolving** | Periodically distills accumulated outcomes into a *bounded, decaying belief/weight delta*, proves it on held-out data the optimizer never saw, and ships it default-OFF behind an eval gate; meta-tier evolves *how* it learns. | | | | — |

`●` = present & wired; `◐` = built but gated-OFF / starved / display-only; `—` = absent.

### Where hermes-quant sits, per axis, with citations

**REFLECT — R1, but the loop is starved (one closed edge, no source-water).**
`Reflector.reflect_on_close()` computes `raw_return`, `alpha_return`, `holding_days`, `outcome_quality`, and the Oracle-Fallacy-critical `tau_observable` deterministically (`hermes_quant/memory/reflector.py:434-435`, guard re-asserted `reflector.py:680-682`), persists to `reflections.jsonl` (`reflector.py:59,505`). The read-back path is real and is the **one genuinely closed policy edge**: `get_past_context()` applies the `tau_observable < asof` Oracle guard FIRST (`hermes_quant/memory/retriever.py:300-303`), BM25-ranks, and `format_context_block()` (`retriever.py:452`) injects a `lessons_block` into the **portfolio_manager prompt only**, gated `HERMES_QUANT_MEMORY_INJECT=1` (`hermes_quant/aggregators/llm_committee.py:296`). **But it is starved at the source:** the trigger fires only under `HERMES_QUANT_REFLECTION=1` (`react/paper.py:242`, `react/multileg.py:256`), and it depends on a `pending` decision row that **production never writes** — `DecisionLog.record_decision()` (`hermes_quant/memory/decisions.py:102`) has *zero* non-test callers (verified repo-wide: only `tests/` and `_paper_reflection_hook.py` reference it). So even with both flags ON, the loop cannot fire. R1 mechanism = built and architecturally ahead of base Reflexion (alpha-vs-benchmark + Oracle guard + SHA-stable IDs, per SOTA §0/§1); R1 in practice = dark.

**CRITIQUE — R2 partial (◐): adversarial roles exist and shape the signal, but never the *reasoning*, and outcomes are never scored.**
`run_research_debate()` (`agents/research_debate/stage.py:133`) runs bull/bear/judge with deterministic `max_turns` routing (`stage.py:212-213`), and under `HERMES_QUANT_RESEARCH_DEBATE=1` (`llm_committee.py:977`) the turns feed the deliberative aggregator's confidence math (`aggregators/deliberative.py:200-261`) → deterministic gate. The 3-way risk committee is the same shape (`agents/trader_node.py:65`). This is the proven-correct shape (SOTA §4: separated adversarial roles beat single-prompt self-critique — RedDebate, Anthropic ICML'24 Best Paper). It is `◐` not `●` because (a) it only shapes the **current tick** — the `research_debate` audit row (`stage.py:345`) is write-only, nothing learns "bull won / bear won"; (b) there is **no Socratic devil's-advocate turn** that attacks the *reasoning* of the leading view (SOTA §4 names this as the one additive, evidence-backed upgrade); (c) the ADR-0002 `counterarguments` field is reserved but left UNFILLED (wiki §3.2).

**DELIBERATE — R2 (●): this is hermes-quant's strongest axis.**
Separated roles + deterministic, evidence-weighted aggregation (not majority vote — SOTA §4 confirms vote-counting is a conformity trap and `deliberative.py` correctly avoids it). The only deliberation gap is meta: nothing aggregates *across* deliberations into "which persona is calibrated."

**EVOLVE — R3 boundary (◐): all the pieces exist and individually work; nothing drives them and no output mutates a weight.**
`HypothesisRunner.run()` (`research/orchestrator.py:182`), `FactorOracle.evaluate()` → 4-tier verdict → `factor_verdicts.jsonl` (`factors/factor_oracle.py:121,16`), `PromotionOrchestrator.run()` → `promotion_decisions.jsonl` (ADR-0052, `eval/promotion_orchestrator.py:1`) all work in isolation. But: **(1) no driving cron** — the only quant crons are per-tick trading + the daily brief (audit §3); **(2) promotion is operator-action-only by explicit design** (`promotion_orchestrator.py:11,357-359`); **(3) verdicts/run-cards are read display-only** (`cli/status.py`, `reporting/daily_report.py`); **(4) a `premium` factor verdict raises no live weight** — `alpha_zoo.latest_verdict()` is a read-only bridge; **(5) the one auto-learning weight path is gated OFF at source** — `BMAAggregator.update()` evolves per-analyst Beta posteriors (`aggregators/bma.py`, Beta-binomial conjugacy) but `settlement_loop` tags every outcome `_calibration_quality="slippage_only"` and SKIPS `aggregator.update()` (`daemon/settlement_loop.py:40-42,170`), pending entry+exit fill joining; posteriors also have no on-disk persistence (per-process only). The **one** real evidence→policy verdict that exists is `catalyst/profitability.py` (per-relation-class PROFITABLE / UNPROFITABLE_CONSIDER_PRUNE / INSUFFICIENT_SAMPLE, `MIN_SAMPLE=20`/`MIN_HIT_RATE=0.6`, `profitability.py:32-61`) — it decides whether to raise the `brand_self` confidence haircut or prune. That is SkillOpt's gate instantiated once, for one relation class. It is the **seed** of R4, not R4.

**Net placement.** hermes-quant = **R1-reflective with a dark loop + R2-deliberative, standing at the R3 threshold with the engine built but no ignition.** It is NOT yet self-evolving (R4): there is no periodic distillation tier, no held-out gate over policy/factor tunables, and no closed evidence→weight edge except the seed `profitability.py` verdict.

---

## 2. The Open Loops — every evidence producer whose output nothing reads back to policy

From audit §4, ranked by **leverage** (how much closing it converts producers already running into live learning) × **safety** (distance from the risk gate). All are downstream of the deterministic gate; none touches a hard limit.

| # | Producer (writes) | Artifact | Why it's open | Leverage |
|---|---|---|---|---|
| **O1** | `DecisionLog.record_decision` (`decisions.py:102`) | `decisions.jsonl` `pending` row | **Never called in production.** The reflection chain's required INPUT is never produced → the already-closed reflection→PM-prompt edge cannot fire even with flags ON. | **HIGHEST** — one missing WRITE unblocks the only closed edge. ~30 lines. |
| **O2** | `reflector._persist` (`reflector.py:505`) | `reflections.jsonl` | Read by retriever→PM prompt (the closed edge) but starved by O1 + two default-OFF gates; **no weekly/monthly distillation** over the corpus. | **HIGH** — the literal M14 gap; FINCON CVRF target. |
| **O3** | `governance/promotion.py` consumer | `weekly_retro_promotion_readiness` | Gate consumer built (`promotion.py:158,235`) but **no producer ever writes the field** → precondition permanently False. | **HIGH** — a dangling gate that silently blocks promotion forever. |
| **O4** | `FactorOracle._append_verdict` (`factor_oracle.py:16`) | `factor_verdicts.jsonl` | Display-only; no live factor weight responds to a `premium`/`rejected` verdict. | **MEDIUM-HIGH** — closes the factor half of evolve. |
| **O5** | `propagation.log_propagations` (`catalyst/propagation.py:197`) | `propagation-log.jsonl` | Consumed only by `profitability.py` (relation-class verdict). **Per-edge sign/weight learning is DESIGN-ONLY** — `catalyst/graph_mining.py:1` ("NOT A BUILD"; B10 open). | **MEDIUM** — corpus already accreting; config-as-data so safe. |
| **O6** | `BMAAggregator.update` path | per-analyst Beta posteriors | Built + consumed by `_weight_for` (`bma.py:289`) but `settlement_loop` SKIPS it via `slippage_only` (`settlement_loop.py:40-42`); no persistence. | **MEDIUM** — blocked on entry+exit fill joining (v0.1.2); unblock is upstream. |
| **O7** | `stage._audit_append` (`stage.py:345`) | `research_debate` audit rows | Write-only. Nothing mines "which stance/persona was right" into a persona weight. | **MEDIUM** — feeds the monthly meta-retro. |
| **O8** | `HypothesisRunner` RunCard (`orchestrator.py:340`) | `run_cards.jsonl` | Display-only; no auto-promotion, no driving cron. | **MEDIUM** — needs the cron, not new code. |
| **O9** | `PromotionOrchestrator.log.record` (`promotion_orchestrator.py:225`) | `promotion_decisions.jsonl` | Operator-review-only by design (correct rail — keep it). | LOW (by design). |

**Leverage ordering for sequencing:** O1 ≫ O2 ≈ O3 > O4 > O5 ≈ O6 > O7 ≈ O8 ≫ O9.

---

## 3. Adoptable Mechanisms (SOTA + wiki), filtered to those that PRESERVE the rails

Each is rails-compatible by construction: writes to *beliefs/hypotheses/candidate-weights*, never to limits; external-truth scored; default-OFF; eval-gated.

| Mechanism | Source | What it gives hermes-quant | Rail compliance |
|---|---|---|---|
| **Conceptual Verbal Reinforcement (CVRF)** | FINCON, NeurIPS 2024 (arXiv:2407.06567); SOTA §3a | Compare *sustained winners vs losers* across episodes → distill a **small** investment-belief delta → **selectively propagate to the one relevant role's prompt**. This IS the per-trade→weekly→monthly cadence the vision names. | Output = verbal beliefs only. Never touches limits. Selective propagation avoids echo-chamber. |
| **Held-out validation gate + rejected-edit buffer + textual learning-rate** | SkillOpt (arXiv:2605.23904) / MemEvolve (arXiv:2512.18746); wiki §1.1 — **operator already SHIPPED this on the wiki memory system, incl. a documented real save** (a "fix" that regressed recall@5 0.478→0.406 was caught) | The exact eval-gated-rollout the rails demand, already proven by the operator. Bounded per-cycle change; Pareto over (Sharpe, drawdown, turnover); buffer so losing configs aren't re-proposed. | "Prove it on data the optimizer never saw, or it doesn't ship." Config-write-not-code. |
| **Inner cheap-judge / outer real-eval two-loop** | QuantAgent (arXiv:2402.03755) / FunSearch (Nature 2023); SOTA §3c | Names the rail explicitly: committee/LLM judge = inner cheap loop (evidence); **deterministic OOS backtest + promotion gate = outer "standard of truth"** — only the outer can mutate live policy. | Reinforces the gate as sole authority. |
| **Hypothesis critique: implementation-vs-approach failure tag + novelty/dedup gate + checkpoint-fallback** | RD-Agent (microsoft/RD-Agent); SOTA §3b | Reflector rubric tags failures retry-vs-abandon; novelty gate forbids re-running near-duplicate hypotheses (extend `ic_dedup.py` concept); never ship an evolved variant unless it strictly beats prior-best on held-out. | Anti-overfit, anti-Correlation-Red-Sea. |
| **Access-counter promotion + tiered decay (deterministic, non-LLM)** | FINMEM (arXiv:2311.13743); SOTA §3d | A *deterministic* rule for which reflections become durable beliefs and which fade (half-life by tier; +importance on profitable-trade pivotal events; purge below threshold). Safer than letting an LLM decide what to keep. | Non-LLM promotion = no self-grading; bounds belief store (anti-Misevolution). |
| **Multi-rate 4-tier (T0 tick / T1 trade / T2 weekly / T3 monthly-meta), telemetry-first** | HRM + Nested Learning; wiki §1.3; Eidolon Stage-1 template (`tick_scheduler.py`, `enabled=False` byte-identical) | Formal frame for the 3 nested retros. T3 monthly-meta is the MISSING tier. Add it telemetry-first / off-by-default; log "what would have changed." | Off-state byte-identical; recommendations-only before gating. |
| **Socratic devil's-advocate / red-team turn + surface dissent** | IUI'24 devil's-advocate; RedDebate (arXiv:2506.11083); FREE-MAD; SOTA §4 | A standing critic that attacks the *reasoning* of the leading view (distinct from the bear, who argues a *position*); surface dissent to operator instead of collapsing to consensus. | Evidence-only; aggregation stays deterministic. |
| **Oracle-Fallacy provenance tag, carried through distillation** | wiki §4.1; SOTA §5 (model-collapse, Misevolution) | Every distilled belief tagged as the agent's OWN prior output, never re-ingested as ground truth. hermes-quant already does this at the reflection layer (`tau_observable`); preserve it through the new tiers. | Blocks self-citation-as-truth. |
| **Select on robustness/plateau, never the in-sample peak** | operator's own measured instances — MT3 "pure train-score winner = overfit garbage"; AMZN-weight "the 30% peak IS overfit, use a RANGE 15–30%"; wiki §4.2 | The selection rule for any config the evolver proposes: jitter-stable plateau + OOS, never the decimal-optimized peak. | Hard anti-reward-hack constraint. |

**Explicitly NOT adopted (rail violations):** RL post-training / weight-fine-tuning (Hermes orchestrates frontier models — wiki §2.3 "do NOT build"); FINMEM's self-adaptive risk-character that flips conservative on cumulative loss (that's *policy mutation of risk posture* — advisory-only, never auto-applied); any moon-dev pattern (`risk_agent.py:319` LLM-overridable loss limit via substring match — the canonical worst pattern, wiki §4.3); Tier-3 generated-code architecture evolution (stays DEFERRED until a sandbox exists, 30-day-clean-dogfood trigger, wiki §1.1).

---

## 4. Prioritized, Dependency-Ordered Wave Plan

Sequenced by **leverage × safety**. Each wave: a default-OFF flag, an eval gate that must pass to flip it, and the named loop it closes. Waves W1–W3 are the M14 core; W4–W7 extend the same propose-only pattern outward.

### W1 — Ignite the dark edge: production decision recorder
- **Closes:** O1 → the already-built reflection→retriever→PM-prompt edge (the one closed edge in code, currently starved).
- **Build:** call `DecisionLog.record_decision()` (`decisions.py:102`) at the live advisor/reactor decision site so a `pending` row exists; reuse `HERMES_QUANT_REFLECTION` flag. ~30 lines (audit §5 C1).
- **Flag:** `HERMES_QUANT_REFLECTION=1` (+ `HERMES_QUANT_MEMORY_INJECT=1` for inject).
- **Eval gate to flip:** in shadow, ≥N closed trades produce a `pending`→resolved chain with a non-empty `lessons_block`, and a deterministic regression test confirms the `tau_observable < asof` Oracle guard still excludes future reflections. No A/B alpha claim required — this is plumbing, scored on *loop liveness*, not edge.
- **Leverage/safety:** HIGHEST / HIGHEST. Pure plumbing, downstream of gate, no new risk surface. **Do first — everything else feeds on a live decision log.**

### W2 — Weekly pattern-mining retro (per-trade → weekly) [FINCON CVRF, lower half]
- **Closes:** O2 + O3 (writes the dangling `weekly_retro_promotion_readiness` field, closing the permanently-False gate at `promotion.py:158,235`).
- **Build:** `hermes_quant/memory/weekly_retro.py` + `quant-weekly-retro` cron (audit §5 C2). Read `reflections.jsonl`, split winners/losers by realized **alpha** (not raw P&L — closes SOTA tauric gap #8), group by `lesson_category` + ticker/sector, distill ≤N belief-deltas (CVRF). Output: (a) a compact digest the retriever prepends to `lessons_block` via `format_context_block_split` (`retriever.py:488`); (b) a `promotion_event` writing `weekly_retro_promotion_readiness`. FINMEM access-counter + half-life on each belief (deterministic promote/expire).
- **Flag:** `HERMES_QUANT_WEEKLY_RETRO=1`, default-OFF.
- **Eval gate to flip:** the SkillOpt gate — on a held-out OOS window the optimizer never saw, the digest-injected PM prompt must NOT regress hit-rate/alpha vs the no-digest baseline (necessary, not sufficient); belief count stays under the budget cap; every belief carries Oracle provenance + half-life. Select on plateau, not peak.
- **Leverage/safety:** HIGH / HIGH. Beliefs-only; never a limit. **The literal M14 edge.** Depends on W1 (needs a live decision/reflection corpus).

### W3 — Monthly meta-retro (weekly → monthly) [FINCON over-episode + RD-Agent Trace; the MISSING T3 tier]
- **Closes:** O7 + O8 (mines debate/risk-committee audit rows + promotion records + weekly digests into candidate hypotheses).
- **Build:** `quant-monthly-meta-retro` cron (audit §5 C4). Aggregate the W2 digests + `research_debate` audit rows (`stage.py:345`) + promotion records → monthly self-critique: which lesson-categories repeat, which personas/stances were calibrated (feeds a *proposed* persona-weight telemetry, recommendations-only per multi-rate pitfall #1), candidate hypotheses for `HypothesisRunner`. Telemetry-first (Eidolon Stage-1 template, byte-identical off-state). RD-Agent rubric: tag failures implementation-vs-approach; novelty-gate candidate hypotheses.
- **Flag:** `HERMES_QUANT_MONTHLY_META_RETRO=1`, default-OFF.
- **Eval gate to flip:** report-and-candidate-hypotheses only; promotion to live stays operator/eval-gated (W6). Gate = the meta-retro reproduces (Run-Card config_hash) and its candidate hypotheses pass the novelty/dedup check; persona-weight deltas are emitted as telemetry only until ≥M months of agreement with realized calibration.
- **Leverage/safety:** HIGH / HIGH. Depends on W2 (consumes weekly digests).

### W4 — Factor-verdict → BMA-weight proposer (silence-only) [SkillOpt config-evolution]
- **Closes:** O4 (+ unblocks O6 when fill-joining lands).
- **Build:** `hermes_quant/factors/weight_proposer.py` + weekly cron running `FactorOracle.evaluate_all` (audit §5 C3). Emit a CANDIDATE weight diff: premium↑ *within a cap*, rejected→silence-toward-0. Mirror `graph_mining.py` honesty rails: PROPOSE only, eval-gate to apply, **silence-only** (never amplify above the cap). Pairs with lifting the `settlement_loop` `slippage_only` gate (`settlement_loop.py:40-42`) once entry+exit joining (v0.1.2) lands so BMA Beta posteriors can persist and learn.
- **Flag:** `HERMES_QUANT_FACTOR_WEIGHT_PROPOSER=1`, default-OFF.
- **Eval gate to flip:** the proposed weight set must beat prior-best on held-out OOS DSR/walk-forward (checkpoint-fallback: revert if not strictly better); robustness/plateau selection; rejected configs go to a rejected-strategy buffer.
- **Leverage/safety:** MEDIUM-HIGH / HIGH. Independent of W2/W3 (parallelizable after W1).

### W5 — B10 learned-graph miner [MemEvolve config-evolution on the catalyst graph]
- **Closes:** O5.
- **Build:** implement `mine_graph()` exactly as the spec dictates (`catalyst/graph_mining.py:1`): join `propagation-log.jsonl` against forward returns; propose per-edge FLIP/DOWNWEIGHT/PRUNE; **never auto-mutate the seed YAML**; silence-only `confidence_multiplier` (audit §5 C5). Corpus already accreting (`propagation.py:197`); generalizes the proven `profitability.py` verdict from one relation class to all edges.
- **Flag:** `HERMES_QUANT_GRAPH_MINING=1`, default-OFF.
- **Eval gate to flip:** edge proposals scored on held-out forward returns (`MIN_SAMPLE`/`MIN_HIT_RATE` as in `profitability.py`); apply as silence-only multiplier, operator/eval-gated; seed YAML edits stay manual.
- **Leverage/safety:** MEDIUM / HIGH. Gated on corpus volume; parallelizable after W1.

### W6 — Hypothesis→backtest→promote driving cron [QuantAgent inner/outer + RD-Agent Trace]
- **Closes:** O8/O9 wiring (the engine exists; this adds ignition + the inner/outer rail made explicit).
- **Build:** a `quant-research-loop` cron that drives `HypothesisRunner.run()` → `FactorOracle.evaluate_all` → `PromotionOrchestrator.run()` on a cadence, fed by W3's candidate hypotheses. Document the rail in-code: committee = inner cheap judge; deterministic OOS backtest + promotion gate = outer standard-of-truth; **promotion to live stays the operator action** (`promotion_orchestrator.py:357-359`) — the cron only *produces* `PromotionRecord`s.
- **Flag:** `HERMES_QUANT_RESEARCH_LOOP=1`, default-OFF.
- **Eval gate to flip:** end-to-end produces reproducible Run-Cards (config_hash/strategy_hash); lookahead sentinel clean (`orchestrator.py:313-318` forces falsified on contamination); zero auto-promotion to live without operator sign-off.
- **Leverage/safety:** MEDIUM / HIGH. Depends on W3 (consumes candidate hypotheses) + benefits from W4/W5.

### W7 — Self-critique / red-team deliberation upgrade [RedDebate + IUI'24 devil's-advocate]
- **Closes:** the CRITIQUE-axis gap (R2 ◐ → ●).
- **Build:** a standing Socratic devil's-advocate turn in `research_debate/stage.py` that attacks the *reasoning* of the leading view (distinct from the bear); fill the reserved ADR-0002 `counterarguments` field; surface dissent to the operator rather than collapsing to consensus; persist debate outcomes so W3 can mine persona calibration. Keep rounds capped (cost discipline, SOTA §4: round n=1 gave +0.006 F1 for double cost).
- **Flag:** `HERMES_QUANT_REDTEAM_TURN=1`, default-OFF.
- **Eval gate to flip:** in shadow, the red-team turn measurably changes the dissent-surfaced rate without inflating false-flat rate; aggregation stays deterministic (no vote-counting).
- **Leverage/safety:** MEDIUM / HIGH. Independent (parallelizable after W1); enriches W3's inputs.

**Dependency graph:** W1 → {W2 → W3 → W6}; W1 → W4; W1 → W5; W1 → W7. W4/W5/W7 parallelizable once W1 lands. W3 is the convergence point (consumes W2; feeds W6).

---

## 5. The Safety Frame — how self-evolution stays inside the money-software rails

Synthesizing the operator's anti-pattern taxonomy (wiki §4) + the SOTA safety layer (SOTA §5, "Misevolution" ICLR 2026, reward-hacking taxonomy, TradingGroup's −14.4% risk-removal ablation).

### The two lists

**MAY tune (all downstream of the gate, all eval-gated, all default-OFF):**
- **BMA per-analyst weights** — via persisted Beta posteriors, only after the `slippage_only` gate lifts and only on held-out OOS improvement (W4).
- **Committee/role prompts** — via selectively-propagated, bounded, decaying *verbal beliefs* (CVRF; W2/W3). Beliefs are context, never code.
- **Hypothesis priors / candidate generation** — novelty-gated, implementation-vs-approach-tagged, checkpoint-fallback (W3/W6).
- **Factor inclusion / influence** — silence-only weight proposals within a cap; premium↑ capped, rejected→0 (W4).
- **Catalyst edge signs/weights** — silence-only `confidence_multiplier`; FLIP/DOWNWEIGHT/PRUNE *proposals*, never seed-YAML auto-edits (W5).

**MAY NEVER touch (outside the loop, immutable by it):**
- **Hard risk-gate limits** (max loss, position caps, exposure). The moon-dev `risk_agent.py:319` pattern — an LLM disabling a loss limit via free-text substring match — is the canonical worst case. TradingGroup's ablation proves removing the hard risk module yields the *largest* return on one ticker but losses on 4/5 datasets.
- **The discrete sizing ladder.** No continuous re-optimization of position sizes; the ladder is a fixed, deterministic schedule.
- **The kill-switch.** A separate process the agent runtime cannot signal (wiki §4.3 inverse).

### The five enforcement primitives (every wave inherits these)

1. **External-truth evaluator only.** Reward = realized alpha-vs-benchmark from market data, never an LLM self-score, never the agent's own narrative (FINCON, QuantAgent outer-loop, reward-hacking taxonomy). The agent cannot author the signal that grades it.
2. **Held-out gate is necessary AND the optimizer never sees it.** Walk-forward / time-advanced window; passing is *necessary not sufficient* → keep human-in-the-loop promotion (eval-gaming is recursive). The operator's own SkillOpt save proves the gate catches "fixes" that feel right but regress.
3. **Select on robustness, never the peak.** Jitter-stable plateau + OOS; "use a RANGE, not the decimal-optimized point" (MT3 + AMZN-weight evidence). Checkpoint-fallback: never ship an evolved variant that doesn't *strictly* beat prior-best on held-out.
4. **Bounded, decaying, provenance-tagged belief store.** Cap active beliefs (Reflexion Ω / FINCON small-set); FINMEM access-counter + half-life so stale-regime lessons fade; every belief tagged as the agent's OWN prior output (Oracle-Fallacy guard, already at `reflector.py` `tau_observable`) and never re-ingested as ground truth — directly counters "Misevolution" memory-accumulation safety degradation + model-collapse.
5. **Propose-only, deterministic-aggregation, surface-dissent.** Every new component PROPOSES; only the existing operator/eval-gated promotion machinery applies. Aggregate by evidence weighting, never majority vote (conformity trap). Disagreement after N turns → FLAT, not a forced winner.

**The invariant that ties it together:** self-evolution writes to *beliefs / hypotheses / candidate-weights / candidate-edges* — a strictly **advisory plane** that feeds the inner judge. The **outer standard-of-truth** (deterministic OOS backtest + promotion gate + risk gate) is the only path to live policy, sits outside the self-evolving loop, and is immutable by it. That separation — inner advisory plane, outer deterministic authority — is what lets hermes-quant climb from R1 to R4 without ever becoming a system that can talk itself out of its own risk limits.
