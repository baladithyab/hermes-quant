# R — Self-Evolve / Self-Improvement: ~/wiki Corpus Mining

**Date:** 2026-05-30
**Brief:** Mine the operator's `~/wiki` corpus + repo reference-project deep-dives for everything already documented about self-improvement, multi-rate learning, reflection→policy loops, internal deliberation, and anti-patterns for self-modifying trading systems. This is the operator's own accumulated knowledge — treated as the PRIMARY source.
**Scope discipline:** This report mines what the wiki ALREADY documents. It does NOT propose rebuilding the components named in the brief (reflector, decisions, retriever, research_debate, risk_committee, aggregators, hypothesis lifecycle, factor research, promotion governance, evidence store) — it identifies what is MISSING or UNDERPOWERED around them, and where the wiki already prescribes the answer.

---

## TL;DR (5 bullets)

1. **The operator has already built and shipped a full self-improving-MEMORY loop with a held-out validation gate** (`~/.hermes/scripts/skillopt.py`, 9-layer system, L8.5 SkillOpt+MemEvolve, `self-improvement-system.md`). hermes-quant's self-evolution gap is NOT "design a feedback loop from scratch" — it is "port the validation-gate + Pareto-config-evolution + rejected-edit-buffer pattern (already proven on the wiki) onto the trading policy/factor surface, where tunables are data not code." The wiki's #1 thesis: **"prove the change is better on data the optimizer didn't see, or it doesn't ship."**

2. **The 3 nested retro loops the vision names map cleanly onto the documented 4-tier multi-rate hierarchy** (`multi-rate-learning-systems.md` T0 fast / T1 medium / T2 slow / T3 meta, from HRM + Nested Learning). hermes-quant already has per-trade reflection (T0/T1) + a weekly strategy retro cron (T2, `quant-strategy-retro-weekly`, Sun 13:00 PT) wired; the MISSING tier is **T3 meta** — a monthly meta-retro that mines accumulated reflections/retros and proposes a *policy/config change* that clears a held-out gate. The wiki explicitly warns multi-rate is "not a free lunch": instrument rates as recommendations-only (telemetry) before gating on them.

3. **TauricResearch is the ONE reference repo with a concrete reflection→prompt-update loop** (`r1-tradingagents.md` §5): `TradingMemoryLog` stores each decision `[pending]`, computes realized α-vs-benchmark on the next run, generates a 2–4-sentence LLM reflection, and injects top-N same-ticker + cross-ticker lessons into the next Portfolio-Manager prompt. **Crucial nuance the wiki flags: this is prompt-conditioning, NOT a policy/weight update and NOT a deterministic settlement journal.** hermes-quant already ported the per-trade reflection; the gap is the *closed loop that turns those reflections into a config/policy delta under a gate* (the documented `profitability.py` verdict is the first instance of this — see §2.3).

4. **The wiki documents a precise anti-pattern taxonomy for self-modifying trading systems** (`llm-trading-sota-and-codebases-research-bundle.md` "6 failure modes" + `r6-moon-dev-cautionary.md`): **Oracle Fallacy** (past decisions in memory smell like ground truth — must be tagged as the agent's own output), reward-hacking / overfit-to-own-history (the MT3 note: "pure train-score winner = overfit garbage"; the AMZN-weight OOS note: "the 30% peak IS overfit"), **LLM modifying its own risk limits** (`risk_agent.py:319` — the single worst pattern), and **string-grep control flow on stochastic output.** These directly justify the RAILS: deterministic gate is final, self-evolution NEVER mutates hard risk limits or the discrete sizing ladder.

5. **The single highest-leverage missing primitive is the same one the wiki already named for memory: a held-out eval harness + scoring function that BOTH the SkillOpt validation gate and MemEvolve tournament reduce to** (`self-improving-memory-skillopt-memevolve.md`). For trading this is the eval-gated rollout the RAILS demand; the wiki's `learned-graph mining` (B10), `profitability.py` per-class verdict, and the deferred "Tier 3 architecture evolution" (needs a sandbox; 30-day-dogfood trigger) are the concrete next moves — all gated, all default-OFF, all proving edge on accumulated LIVE returns before earning weight.

---

## Q1 — Self-improvement / self-evolution mechanisms the wiki ALREADY describes

### 1.1 SkillOpt + MemEvolve (the canonical validation-gate loop)
**Source:** `~/wiki/concepts/self-improving-memory-skillopt-memevolve.md` (ADR), `~/wiki/concepts/self-improvement-system.md` §3 L8.5.

The operator built and SHIPPED (2026-05-28) a 3-tier self-improvement loop on the wiki memory system. It is the template hermes-quant should mirror:

| Tier | Object optimized | Mechanism | Status |
|---|---|---|---|
| **Tier 1 — SkillOpt** | the skill/text doc | rollout → reflect → bounded edit → **held-out validation gate** → accept/reject; **rejected-edit buffer**; **textual learning-rate** (bounded edit budget/cycle) | SHIPPED |
| **Tier 2 — MemEvolve** | the config genotype | perturb tunables → score on (recall↑, MRR↑, latency↓) → **Pareto-rank** → non-dominated winner versioned. Safe because tunables are DATA (`evolution_state` rows), not code → "config-write not code-change" | SHIPPED (dry-run default, `--apply` writes winner) |
| **Tier 3 — full architecture evolution** | generated Python (encode/store/retrieve/manage) | tournament selection over generated code | **DEFERRED — needs a sandbox; trigger = Tier 1+2 clean ≥30 days** |

Papers: SkillOpt (arXiv 2605.23904, MS, MIT) — "train the procedure not the weights"; MemEvolve (arXiv 2512.18746, OPPO+NUS, ICML'26) — "evolve how you learn." **The unifying primitive both share, and the wiki names as the single highest-leverage move: a held-out eval set + a scoring function** ("prove the change is better on data the optimizer didn't see").

The wiki documents this loop's **first real save** (`self-improvement-system.md` §6, 2026-05-29): a mechanically-correct AND→OR retrieval fix that *felt* like a sure ship was caught REGRESSING the held-out metric (recall@5 0.478→0.406). "Without the eval harness, a −7pt recall regression that felt like a fix would have shipped." This is the proof-point for why hermes-quant self-evolution must be eval-gated.

**Mapping table the ADR already provides (paper mechanism → implementation):** validation gate → recall@k/MRR delta on held-out; bounded edit budget → textual learning rate; rejected-edit buffer → DB table; epoch-wise consolidation → weekly roll-up; multi-objective selection → Pareto; candidate generation → N config genotypes. *Every one of these maps onto a trading analogue* (held-out OOS Sharpe/hit-rate delta; bounded per-cycle config change; rejected-strategy buffer; weekly→monthly consolidation; Pareto over (Sharpe, drawdown, turnover-cost); N factor/weight genotypes).

### 1.2 The wiki self-improve cron (the gap-FILLER, not yet optimizer)
**Source:** `self-improvement-system.md` §L8; `_inbox/2026-05-28-wiki-self-improve-cron-cadence-tier-delivery.md`.

Every-30-min cron: scan → research ONE gap → write to `_inbox/` only (never canonical). State machine pending→in_progress→researched|promoted|expired|skipped with cooldowns. **Cadence-tier auto-adapts to gap-arrival rate** (quiet ≤1/2h → 1 research; steady 2-5 → 2; busy >5 → 3), daily budget (48 runs / 12 research). The explicit lesson: this is a gap-FILLER, **not yet an optimizer — it has no feedback loop proving a change helped.** That is exactly the gap SkillOpt closed for memory and that hermes-quant's policy loop still has open (meta-review M14: "evidence producers grew, but the feedback loop that turns evidence → improved policy did not").

### 1.3 Multi-rate learning = the formal frame for the 3 nested retro loops
**Source:** `~/wiki/concepts/multi-rate-learning-systems.md`; `_inbox/2026-05-28-eidolon-multi-rate-pdr-loops-stage1-shipped.md`.

The canonical 4-tier hierarchy (HRM + Nested Learning, NeurIPS 2025): "update frequency is part of the architecture." The wiki's **Application 2 is hermes-quant verbatim**:

| Tier | Cadence | hermes-quant mapping (per the wiki) | Vision's retro loop |
|---|---|---|---|
| **T0 fast** | every tick | market ingest, signal compute, BMA vote | (per-tick) |
| **T1 medium** | every trade | order placement, halt checks, HITL queue | **per-trade reflection** |
| **T2 slow** | daily roll / weekly | EOD reconciliation, P&L attribution, risk recalibration | **weekly pattern-mining retro** |
| **T3 meta** | step/batch/epoch | parameter retune, model retrain, regime-shift detection | **monthly meta-retro** ← MISSING |

The wiki explicitly notes hermes-quant "already has a soft multi-rate structure… as separate processes/cron jobs, a perfectly valid implementation," and that **naming the structure + giving each tier config + telemetry "would give a clean lens for spotting cross-tier bugs (e.g., a T2 reconciliation that should have triggered a T3 retune but didn't because the trigger was implicit)."** That implicit-trigger gap IS the M14 broken feedback loop.

Eidolon (the operator's sibling project) already SHIPPED Stage 1 of multi-rate as a directive: `tick_scheduler.py` with `RateTier` enum, `MultiRateTickScheduler.recommend()`, **defaults `enabled=False` to preserve byte-identity**, emits `TierDecision` telemetry per tick. This is the documented template for how to add a rate tier safely (telemetry-first, off-by-default) — directly applicable to adding hermes-quant's T3 monthly meta-retro.

**Pitfalls the wiki names for multi-rate:** (1) not a free lunch — instrument as recommendations-only before gating; (2) the "off" state must be byte-identical to the prior single-rate system; (3) slow-tier work masks bugs (a rarely-firing component stays broken silently — log "what would have changed" on each potential firing); (4) external rate ≠ internal rate hierarchy; (5) HRM/HOPE domain wins are NOT transferable claims — borrow the update-frequency *principle*, don't commit to a 6-month re-architecture on one paper.

### 1.4 What hermes-quant has ALREADY wired toward this (so as not to rebuild)
**Source:** `_inbox/2026-05-28-hermes-quant-regime-gates-and-strategy-retro-shipped.md`; `projects/hermes-quant.md`.

- **T1 reflection LIVE** on all 6 firing surfaces (`HERMES_QUANT_REFLECTION=1` on 3 advisor crons + 3 armed tick wrappers); per-trade reflections write to `~/.hermes/quant/memory/reflections.jsonl`.
- **T2 weekly retro LIVE** — `quant-strategy-retro-weekly` cron (Sun 13:00 PT) reads 7d of `executions.jsonl` + `reflections.jsonl` + `state.db.positions`, marks-to-market, aggregates P&L by layer/direction/symbol, silence-by-default. (Known limitation: `play_tag` not plumbed → all layers read as `advisor`; deferred to ADR-0029.)
- **The first true evidence→policy verdict loop EXISTS** — `hermes_quant/catalyst/profitability.py` + `quant-catalyst-profitability.py` (2026-05-30): joins the propagation log against realized yfinance forward returns, per relation-class verdict PROFITABLE / UNPROFITABLE_CONSIDER_PRUNE / INSUFFICIENT_SAMPLE (MIN_SAMPLE=20, MIN_HIT_RATE=0.6). The `brand_self` verdict *decides whether to raise the confidence haircut toward 1.0 or prune.* This is SkillOpt's gate, instantiated for trading. **It is the seed of the monthly meta-retro — the gap is generalizing it from one relation-class to the whole policy/factor surface and putting it on a T3 cadence.**

---

## Q2 — Reference repos/papers with a REFLECTION → POLICY-UPDATE loop, concretely

### 2.1 TauricResearch/TradingAgents — the canonical (and only fully concrete) reflection loop
**Source:** `docs/research/reference-projects/2026-05-24-r1-tradingagents.md` §5; `r5-codex-tradingagents-graph.md`; SOTA bundle pattern #7.

`agents/utils/memory.py:TradingMemoryLog` — append-only markdown. Each decision written as `[YYYY-MM-DD | TICKER | RATING | pending]`. After realized returns, the entry is **atomically rewritten** (temp file + `os.replace`) into `[… | +X.X% | +α.α% | Nd]` with an LLM-generated `REFLECTION:` block (what went right/wrong). `get_past_context()` injects up to N same-ticker entries + N cross-ticker reflections into the **next** Portfolio-Manager prompt.

**How it concretely closes the loop:** decision → pending → α-vs-benchmark computed on next run → 2–4-sentence reflection → top-N same-ticker + top-N cross-ticker lessons re-injected as prompt context. The SOTA bundle lists this as an "adopt medium-term" pattern (#7) and flags hermes-quant's specific gaps vs it (gap #8: "no alpha-vs-benchmark calc in reflection (just raw P&L)").

**The critical wiki-flagged limitation (why it is NOT enough on its own):** r1 §4 — "Useful for prompt-conditioning, but it is **not** a deterministic settlement journal — there is no per-trade ground truth check that hard-stops a misbehaving strategy." It's a prompt-context cache, not a policy/weight update. hermes-quant's posture is stronger: ADR-0010 settlement journal is deterministic with NO LLM in the decision path. **So the reflection→policy loop hermes-quant wants is TauricResearch's reflection feeding a GATED config/policy delta (SkillOpt-style), not just the next prompt.**

### 2.2 HKUDS/Vibe-Trading — Shadow Account = the counterfactual policy-extraction loop
**Source:** `_inbox/vibe-trading.md`; `r3-vibe-trading.md` §3; SOTA bundle #12.

The Shadow Account is the most distinctive reflection→policy mechanism: `analyze_trade_journal` (behavioral diagnostics: disposition effect, overtrading, chasing, anchoring) → `extract_shadow_strategy` (distills profitable roundtrips into 3–5 if-then `ShadowRule`s via KMeans + depth-3 decision-tree path extraction) → `run_shadow_backtest` (delta-PnL attribution buckets: `missed_signals / noise_trades / early_exit / late_exit / overtrading`) → `render_shadow_report` → `scan_shadow_signals` (scans today's market for symbols matching the extracted profile). The SOTA bundle: "this is what hermes-quant's evolving watchlist *should* become." hermes-quant has a hand-coded `ShadowRule` (gap D2/D3: not auto-extracted, attribution is per-rule not bucketed) and a **PMCC shadow tracker** (`hermes_quant/shadow/pmcc.py`, 2026-05-30) — but the *auto-extraction* + *delta-PnL bucket attribution* policy-mining loop is the documented gap (v0.7 "Shadow Account real").

Also documented: **Hypothesis Registry** (status machine exploring→testing→validated/rejected/monitoring) + **Run Cards** (reproducibility manifest with config_hash/strategy_hash/artifact SHA-256) — the lifecycle scaffolding a self-evolving researcher needs to version its own experiments. hermes-quant already has `research/hypothesis.py` + `research/run_card.py`.

### 2.3 The papers the wiki names for the policy-update mechanism
**Source:** SOTA bundle "5 critical 2026 papers"; `multi-rate-learning-systems.md`.

- **Agentic Trading survey (arXiv 2605.19337)** — 77-study audit; only 2/19 primary papers have valid train/test splits, ZERO reach R3 reproducibility. *Defines the field's methods debt* → anchors hermes-quant's burn-in / eval-gate discipline.
- **STOCKBENCH (ICLR 2026)** — first contamination-free benchmark; most LLMs fail to beat buy-and-hold → named the "north-star benchmark" to grade self-evolution against.
- **Mantshimuli & Mwamba (Springer 2026)** — regime-aware portfolio optimization w/ LLM signals (Sharpe +0.373) → closest analog to the BMA design + regime-gating already shipped.
- **FLAG-Trader (2026)** — hierarchical RL + LLM, validating "prompt-only is insufficient" → confirms the policy update must eventually be more than prompt-conditioning, but RL post-training is explicitly on the "do NOT build" list (Hermes orchestrates frontier models).
- **SkillOpt / MemEvolve / HRM / Nested Learning** — the actual mechanism papers (see Q1).

---

## Q3 — Internal deliberation / self-critique patterns documented BEYOND what hermes-quant ported

hermes-quant already ported: bull/bear/judge (`research_debate/`), 3-way risk committee, deliberative + llm_committee aggregators, trader node. The wiki documents these ADDITIONAL deliberation patterns that are NOT yet fully exploited:

1. **Disagreement as a first-class signal, not a forced winner** (`r1-tradingagents.md` §7 anti-pattern 3). TradingAgents debate is a pure turn-cap loop with NO convergence detection. The wiki's prescription is STRONGER than what was ported: compute embedding-cosine between successive debate responses; **persistent disagreement after N turns should produce a FLAT signal, not force a winner** — "the absence of convergence is itself a signal," aligning with the silence-by-default risk posture. (hermes-quant capped the loops deterministically but the "disagreement → flat" semantics is the under-exploited piece.)

2. **Bull/bear as a disagreement AMPLIFIER pre-aggregation** (`r1` §6 pattern 2). Run the adversarial pass on the top-2 highest-confidence conflicting `AnalystView`s and fill the `counterarguments` field (which the ADR-0002 schema already reserves but leaves UNFILLED). Self-critique writes structured evidence, not just prose.

3. **Two-LLM tier deliberation** (`r1` §6 pattern 1; `reference-scatter` CV4): `quick_thinking_llm` for analysts/debaters, `deep_thinking_llm` for the judge/synthesis roles. Cost-discipline primitive for running deliberation at scale. (SOTA bundle #14: "dual-speed LLM routing"; hermes-quant currently single-tier.)

4. **Deliberation limits in ROUTING, not prompts** (`r5-codex-tradingagents-graph.md` §6 lesson 2): debate termination is deterministic count-based routing, independent of model compliance — never trust the model to self-terminate. (Ported, but worth re-stating as the rail for any new self-deliberation loop.)

5. **`current_clear` context-hygiene node** (`reference-scatter` pattern 4; SOTA #15): purge tool-call messages between deliberation steps to prevent context bloat / cross-contamination of the critique.

6. **5-layer context compression** (`vibe-trading.md`; SOTA #9): microcompact → LLM structured summary → iterative update — the mechanism that lets a long self-deliberation session not overflow, prerequisite for a monthly meta-retro that reads a month of reflections.

7. **Cross-MODEL adversarial review as a meta-deliberation pattern** (`_inbox/2026-05-26-cross-model-adversarial-review-pattern.md`, applied repeatedly across the operator's projects; trend-arbitrage `pillars/02-council.md` "never two from the same family" rule). The operator's OWN proven self-critique pattern is to fan a decision out to reviewers from *distinct model families* (GPT + Gemini + Opus + DeepSeek + Grok) and synthesize — independent cross-model signal. This is the highest-trust self-critique layer documented and is used as the final-verify gate on hermes-quant waves themselves.

---

## Q4 — Documented anti-patterns for self-modifying trading systems

### 4.1 The 6 failure modes (SOTA bundle — `llm-trading-sota-and-codebases-research-bundle.md`)
1. **Contaminated backtesting** (LLM parametric look-ahead) — closed by Data Grounding Block + cache-layer date cutoffs (NOT agent-layer); FutureSim's `available_at` invariant (`r4-futuresim-evidence-store.md`).
2. **Oracle Fallacy in memory** — *past decisions reflected in memory smell like ground truth; they MUST be tagged as the agent's own past output.* This is the central self-evolution trap: a self-evolving researcher reading its own reflections can mistake its prior guesses for facts. (The architecture-gap tracker explicitly cites "Oracle-Fallacy" as a reason hermes-quant's BM25+JSONL memory is "architecturally ahead" of TauricResearch's ChromaDB.)
3. **Pattern hallucination with high-confidence justification** — LLM invents a chart pattern + confident prose. (Mitigation: force tool calls before synthesis; Citation HARD RULE.)
4. **Correlation Red Sea** — multi-agent ensembles converge to highly-correlated signals → no diversification benefit. Direct risk for self-evolution: optimizing toward one objective collapses analyst diversity.
5. **Regime-shift brittleness** — strategies tuned in low-vol fail in high-vol. (hermes-quant counters with regime-gated play activation + regime-aware BMA, both shipped.)
6. **Adversarial prompt injection via market feeds** — news items carry injection payloads. (See 4.2.)

Plus the 3 TauricResearch anti-patterns: **empty-memory hallucination** (guard `if past_context`), **fabricated sentiment** (no synthesis-only agents), **look-ahead leakage** (cutoff at cache layer).

### 4.2 Reward-hacking / overfit-to-own-history — the operator's OWN measured instances
The wiki contains hard, recent evidence (not theory) of the overfit trap on the operator's own models:
- **MT3 note** (`_inbox/2026-05-29-mt3-tactical-trading-model-digitized.md`): "**Pure train-score winner = overfit garbage** (train 0.473, OOS −0.073; jitter ±1 collapsed median → 'spike not plateau' = curve-fit). **Blended-objective winner generalizes.** Lesson: select on blended/robust objective, NEVER pure in-sample." Plus the honesty rail: "50% CAGR is NOT reachable… searched 2,500 configs, zero hit ≥50%" and "Renaissance Medallion does ~40% and is a once-in-history anomaly." Self-evolution must select on **robustness/plateau (jitter-stable), never the in-sample peak.**
- **AMZN-weight OOS note** (`projects/hermes-quant.md`, 2026-05-30): "**the 30% peak IS overfit (direction robust, point is not).**" IS-first-half optimal 15%, OOS-second-half 70% — the Sharpe-maximizing weight is window-specific. Verdict: **use a RANGE (15–30%), not the point.** "Don't optimize to the decimal." The literal refutation of treating a backtested peak as a tradeable target.
- **The validation-gate save** (§1.1) — a self-improvement that *felt* correct regressed the held-out metric. Proof that the gate must be the authority, not the optimizer's intuition.

### 4.3 The worst single pattern — LLM mutating its own risk limits (moon-dev cautionary)
**Source:** `docs/research/reference-projects/2026-05-24-r6-moon-dev-cautionary.md` (verdict: "treat as anti-pattern reference, adopt NOTHING").

`risk_agent.py:319` — `self.override_active = "OVERRIDE" in response_text.upper()` lets an LLM bypass `MAX_LOSS_PERCENT` for 15 minutes. This collapses 3 failure modes into one line: (a) the risk limit is overridable at all; (b) overridable by an LLM (no human); (c) the override is a substring match on free-text. "A single prompt-injection in a market-data feed (a token name or headline containing 'OVERRIDE') could disable the loss limit for 15 minutes during a flash crash." **This is the precise reason the RAILS state self-evolution must NEVER mutate hard risk limits or the discrete sizing ladder.** Mapped inverse: hermes-quant's kill-switch is a separate process the agent runtime cannot signal; the deterministic gate is downstream of (never overridable by) any LLM/committee.

Companion anti-patterns from moon-dev (all inverse-encoded in hermes-quant's posture): LLM → order directly (no HITL); free-text → `lines[0].strip()` → action (string-grep control flow); in-memory DataFrames reset per cycle (no replayable audit); live-only test environment (no L0→L4 fidelity ladder); `nice_funcs` (money tools) importable by every agent (no capability isolation). AI-Trader adds **1:1 blind copy-trading** and **agent-submitted pricing** (`price: 51000` from the LLM payload — the daemon must dictate fill price locally).

### 4.4 The methods-debt / reproducibility anti-pattern (the macro frame)
The Agentic Trading survey's finding (only 2/19 papers reproducible, ZERO at R3) is itself the cautionary frame: a self-evolving system that can't reproduce its own past experiments will harden unverified guesses into "fact" — the same junk-accretion failure the memory system's bi-temporal facts + held-out gate were built to prevent (`self-improvement-system.md` §1). Hence: Run Cards (config_hash/strategy_hash), append-only immutable evidence store, `available_at` invariant, and the eval gate are not optional polish — they are the load-bearing defense for any self-modification.

---

## Synthesis — what's MISSING/UNDERPOWERED around the existing components (for the architect)

The wiki's accumulated knowledge points at four concrete, gated, default-OFF moves — none of which is a rebuild:

1. **Port SkillOpt's validation gate + rejected-edit buffer + textual-learning-rate onto the policy/factor surface.** The pattern is proven on the wiki memory system (incl. a documented real save). hermes-quant's `profitability.py` per-class verdict is the first instance; generalize it to a held-out OOS Sharpe/hit-rate gate over factor weights / play parameters, with a rejected-strategy buffer so losing configs aren't re-proposed. NEVER touches hard risk limits or the discrete sizing ladder.

2. **Add the T3 monthly meta-retro tier** (the multi-rate frame's missing tier). Telemetry-first / off-by-default (Eidolon Stage-1 template): mine the month of `reflections.jsonl` + weekly retros + propagation-log + decisions log, emit a *proposed* config/policy delta, and route it through the held-out gate before anything ships. Wire the implicit cross-tier trigger the wiki warns about (a T2 retro that should fire a T3 retune).

3. **Build the learned-graph mining job (B10)** — the durable propagation-log corpus already accumulates (`~/.hermes/quant/catalyst/propagation-log.jsonl`, join-able against forward returns to learn corrected signs). This is MemEvolve-style config-evolution on the catalyst graph: tunables (edge signs/weights) are data, so it's a gated config-write, not code-gen.

4. **Tag everything as the agent's own output (Oracle-Fallacy guard) and select on robustness not peaks.** Any reflection/retro the self-evolver reads must be provenance-tagged as its own prior guess; any config it selects must pass jitter/plateau robustness + OOS, never the in-sample peak (MT3 + AMZN-weight lessons). Deferred Tier-3 "architecture code evolution" stays deferred until a sandbox exists (30-day-dogfood trigger), per the wiki's own staging.

---

## Sources cited (all read for this report)
- `~/wiki/concepts/self-improvement-system.md`
- `~/wiki/concepts/self-improving-memory-skillopt-memevolve.md`
- `~/wiki/concepts/multi-rate-learning-systems.md`
- `~/wiki/_inbox/2026-05-27-llm-trading-sota-and-codebases-research-bundle.md`
- `~/wiki/_inbox/2026-05-24-hermes-quant-reference-scatter.md`
- `~/wiki/_inbox/2026-05-26-hermes-quant-wave-d-tradingagents-backfill.md`
- `~/wiki/_inbox/2026-05-29-mt3-tactical-trading-model-digitized.md` + `~/wiki/_inbox/2026-05-30-mt3-final-locked-config-code1-2.3x.md`
- `~/wiki/_inbox/vibe-trading.md`
- `~/wiki/projects/composer-replication-framework.md`
- `~/wiki/projects/trend-arbitrage-engine.md`
- `~/wiki/_inbox/2026-05-28-wiki-self-improve-cron-cadence-tier-delivery.md`
- `~/wiki/_inbox/2026-05-28-eidolon-multi-rate-pdr-loops-stage1-shipped.md`
- `~/wiki/_inbox/2026-05-28-hermes-quant-regime-gates-and-strategy-retro-shipped.md`
- `~/wiki/projects/hermes-quant.md` + `~/wiki/projects/hermes-quant-architecture-and-gaps.md`
- `docs/research/reference-projects/2026-05-24-r1-tradingagents.md`
- `docs/research/reference-projects/2026-05-24-r2-ai-trader.md`
- `docs/research/reference-projects/2026-05-24-r3-vibe-trading.md`
- `docs/research/reference-projects/2026-05-24-r4-futuresim-evidence-store.md`
- `docs/research/reference-projects/2026-05-24-r5-codex-tradingagents-graph.md`
- `docs/research/reference-projects/2026-05-24-r6-moon-dev-cautionary.md`
