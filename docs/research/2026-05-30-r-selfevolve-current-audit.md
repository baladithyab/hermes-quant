# Self-Evolution Loop Audit — Current State (2026-05-30)

**Scope.** Trace the hermes-quant self-improvement loop end to end by reading
code. Identify where the loop CLOSES (evidence → changed weight/prompt/strategy)
and where it is OPEN (evidence produced, never read back). All citations are
`file:line` against the working tree at audit time.

**Rails reaffirmed.** The deterministic risk gate is final authority; the
LLM/committee is evidence, never authority; all loop components are default-OFF
and eval-gated; no closure below touches the hard risk limits or the discrete
sizing ladder.

---

## 1. Reflection layer (Layer 1/2/3, ADR-0042)

### What a reflection produces
`Reflector.reflect_on_close()`
(`hermes_quant/memory/reflector.py:377-506`) computes `raw_return`,
`alpha_return`, `holding_days`, `outcome_quality` (1-5,
`reflector.py:172-190`), a heuristic-or-LLM `lesson_category`
(`reflector.py:193-204` stub; `reflector.py:512-694` v0.2 structured path), a
prose `reflection_text`, and the Oracle-Fallacy-critical `tau_observable`
(always deterministic, `reflector.py:229-240` / guard re-asserted
`reflector.py:679-682`).

### Where it is stored
Appended to `~/.hermes/quant/memory/reflections.jsonl`
(`reflector.py:59`, `_persist` `reflector.py:700-713`). A linking
`resolution` row is appended to `decisions.jsonl`
(`decisions.py:155-169`, called from
`_paper_reflection_hook.py:89`).

### Is it read back into a future decision? — YES, but the loop is starved
The read-back path EXISTS and is wired:
`get_past_context()` (`retriever.py:281`) loads reflections, applies the
Oracle-Fallacy guard `tau_observable < asof` FIRST
(`retriever.py:351-362`), BM25-ranks cross-ticker
(`retriever.py:375-391`), and `format_context_block()`
(`retriever.py:452`) renders the `lessons_block`. That block is injected into
the **portfolio_manager / research_manager** prompt ONLY, gated by
`HERMES_QUANT_MEMORY_INJECT=1` (`llm_committee.py:294-316`). Debaters are
deliberately kept clean.

**This is the one genuinely CLOSED reflection→policy edge** (reflection text
changes a future PM prompt). BUT it is starved at the source (see §4): the
producing trigger only fires under `HERMES_QUANT_REFLECTION=1`
(`react/paper.py:242`, `react/multileg.py:256`), and it depends on a pending
decision row that production never writes.

### Per-trade only — no weekly/monthly aggregation
The reflector is invoked per-close from the paper/multileg reactor
(`react/paper.py:242-247`, `react/multileg.py:692-694`). There is NO weekly
pattern-mining pass and NO monthly meta-retro over `reflections.jsonl`. The
retriever surfaces individual rows (k=5/3/2) plus simple aggregate stats
(`retriever.py:408-442`), but nothing distils recurring `lesson_category`
patterns into a durable policy artifact. `grep` for `weekly_retro|monthly|
meta_retro|pattern_mining` finds only the consumer-side stub (§3) and a
design-only module (§4) — no producer.

---

## 2. Internal deliberation

### Bull/bear research debate (ADR-0065)
`run_research_debate()` (`agents/research_debate/stage.py:133`):
round-robin bull/bear, `max_turns = 2 * rounds`
(`stage.py:212-213`); rounds default to 1, env-clamped via
`HERMES_QUANT_RESEARCH_DEBATE_ROUNDS` to `[1,3]`
(`stage.py:99-125`). Bail-out: two consecutive turn failures →
`terminated_reason="two_consecutive_failures"`, judge still runs on partial
state (`stage.py:245-254`). One `research_debate` audit row per stage
(`stage.py:345-379`).

**Does it shape the gate, or is it logged-only? — It SHAPES the signal.**
Dispatched only under `HERMES_QUANT_RESEARCH_DEBATE=1`
(`llm_committee.py:977`). Bull/bear turns + the judge's
`portfolio_manager` turn are converted to `CommitteeTurn`s
(`llm_committee.py:996-1061`) which feed the deliberative aggregator's
confidence math (`deliberative.py:200-261`), which then passes through the
deterministic gate. So the debate is NOT logged-only — but it shapes only the
CURRENT tick. Nothing persists "bull won / bear won" outcomes for later
learning; the audit row (`stage.py:345`) is write-only evidence.

### Risk committee (3-way)
Dispatched under `HERMES_QUANT_RISK_COMMITTEE_LLM=1`
(`agents/trader_node.py:65`, `backtest/strategy.py:164`,
gate flag referenced `observability/fallback_probe.py:403`). Same
shape — silence-biased confidence multiplier, never authority; logged but not
fed back across ticks.

**Both default OFF.** Deliberation is single-tick evidence; there is no loop
that turns "which persona was right last month" into a changed persona weight.

---

## 3. Hypothesis → evolution

### The pieces exist and individually work
- `HypothesisRunner.run()` (`research/orchestrator.py:182-363`): open→running,
  execute strategy, auto-eval success/falsification criteria
  (`orchestrator.py:94-155`), write a RunCard
  (`orchestrator.py:339-340`), transition hypothesis to validated/falsified
  (`orchestrator.py:343-353`). Lookahead contamination forces `falsified`
  (`orchestrator.py:313-318`).
- `FactorOracle.evaluate()` (`factors/factor_oracle.py:310-448`): IC panel →
  4-tier verdict (premium/standard/experimental/rejected,
  `factor_oracle.py:119-183`) → appended to `factor_verdicts.jsonl`
  (`factor_oracle.py:274-282`).
- `PromotionOrchestrator.run()` (`eval/promotion_orchestrator.py:373-456`):
  STOCKBENCH harness → gate → `PromotionRecord` appended to
  `promotion_decisions.jsonl` (`promotion_orchestrator.py:214-232`).

### Is there a working cycle: hypothesis → backtest → score → PROMOTE to live weight? — NO
1. **No driving cron.** `grep` of `ops/` finds no scheduled invocation of
   `HypothesisRunner`, `FactorOracle.evaluate_all`, or `PromotionOrchestrator`.
   The only quant crons are `quant-autonomous-tick.py` (per-tick trading) and
   `quant-daily-interim.py` (a brief). Neither runs research/factor/promotion.
2. **Promotion is decision-support only, by explicit design.** The orchestrator
   "does NOT modify hypothesis status" (`promotion_orchestrator.py:11-12,
   354-360`) — promotion to live is an operator action, not an automated loop.
3. **Verdicts/run-cards are read ONLY for display.** Consumers of
   `factor_verdicts.jsonl` and `run_cards.jsonl` are `cli/status.py`
   (`status.py:54`, `status.py:439-441`), `reporting/daily_report.py`
   (`daily_report.py:224-225`), and the daily brief's research section
   (`ops/scripts/quant-daily-interim.py:510-558`). None mutate a weight.
4. **`AlphaZoo` has no weight to mutate from a verdict.** `latest_verdict()` is
   a read-only bridge (`factors/alpha_zoo.py:465-487`); `grep weight` in
   `alpha_zoo.py` returns nothing. A `premium` verdict does not raise any
   factor's live influence.

### BMA posterior learning exists but is gated OFF at the source
`BMAAggregator.update(EpisodeOutcome)` evolves per-analyst Beta posteriors
(`aggregators/bma.py:638-656`), and `_weight_for` consumes them
(`bma.py:289-295`). The settlement loop builds the outcomes
(`daemon/settlement_loop.py:270`), but tags every one
`_calibration_quality="slippage_only"` and `dispatch_settlement` SKIPS
`aggregator.update()` for those (`settlement_loop.py:35-48`). So the one
auto-learning weight path is deliberately disabled pending entry+exit fill
joining (v0.1.2). Posteriors also have no on-disk persistence (only the
isotonic calibrator is loaded, `bma.py:200-242`), so any learning is per-process.

---

## 4. THE GAP — producers that write evidence nothing reads back to policy

| Producer (writes) | Artifact | Read back to a weight/prompt/strategy? |
|---|---|---|
| `reflector._persist` (`reflector.py:700`) | `reflections.jsonl` | Read by retriever→PM prompt (CLOSED edge) — but starved: `HERMES_QUANT_REFLECTION`+`_MEMORY_INJECT` both default OFF, and no pending decision row exists to reflect on (see below). |
| `DecisionLog.record_decision` (`decisions.py:102`) | `decisions.jsonl` | **NEVER CALLED in production.** Only `_paper_reflection_hook.py:41` and `decisions_render.py:136` reference the log, both readers. The reflection chain's required input is never produced → the reflection→memory loop cannot fire even when flags are ON. |
| `stage._audit_append` (`stage.py:345`) | audit-log `research_debate` rows | Write-only. Nothing mines debate outcomes into persona/role weights. |
| `_audit_reflector_call` (`reflector.py:310`) | audit-log `reflector_llm_call` rows | Write-only telemetry. |
| `FactorOracle._append_verdict` (`factor_oracle.py:274`) | `factor_verdicts.jsonl` | Display-only (`status.py`, `daily_report.py`). No live factor weight. |
| `HypothesisRunner` RunCard (`orchestrator.py:340`) | `run_cards.jsonl` | Display-only (brief research section). No auto-promotion. |
| `PromotionOrchestrator.log.record` (`promotion_orchestrator.py:225`) | `promotion_decisions.jsonl` | Operator-review only by design. |
| `propagation.log_propagations` (`catalyst/propagation.py:197`) | `propagation-log.jsonl` | Consumed only by `profitability.measure_profitability` (by relation class). Per-edge sign/weight learning is **DESIGN ONLY** — `catalyst/graph_mining.py:1` ("THIS MODULE IS A DESIGN SPECIFICATION, NOT A BUILD"; B10 open). |
| `governance/promotion.py` gate reads `weekly_retro_promotion_readiness` (`promotion.py:158, 235`) | — | **No producer exists.** Nothing ever writes this field to a `promotion_event`, so the gate's weekly-retro precondition is permanently False. The consumer was built; the producer was not. |

**Net:** Exactly ONE policy-affecting edge is closed in code (reflection text →
PM prompt via memory injection), and it is starved because (a) two default-OFF
gates and (b) `record_decision()` has no production caller. Every other producer
is display-only, operator-review-only, design-only, or gated-OFF. This is the
M14 finding confirmed at `file:line`: evidence producers grew; the
evidence→policy feedback loop did not.

---

## 5. Smallest set of new components to close per-trade → weekly → monthly

None of these touch the deterministic risk limits or the discrete sizing ladder;
all are default-OFF, eval-gated, and additive (evidence, not authority).

**C1 — Production decision recorder (unblocks the existing closed edge).**
Call `DecisionLog.record_decision()` (`decisions.py:102`) at the live decision
site (the advisor/reactor path that today only logs to the bus) so a `pending`
row exists. This is the single missing WRITE that makes the already-built
reflection→retriever→PM-prompt loop actually fire. ~30 lines + flag reuse
(`HERMES_QUANT_REFLECTION`). Highest leverage, lowest risk.

**C2 — Weekly pattern-mining retro (per-trade → weekly).**
New `hermes_quant/memory/weekly_retro.py` + cron `quant-weekly-retro`. Reads
`reflections.jsonl`, groups by `lesson_category` (`reflector.py:75-83`) and
ticker/sector, computes recurring-loss patterns and a hit-rate-weighted
"lessons digest." Output: (a) a compact digest artifact the retriever can
prepend to `lessons_block` (extend `format_context_block`,
`retriever.py:452`), and (b) a `promotion_event` row that finally WRITES
`weekly_retro_promotion_readiness` (closes the dangling consumer at
`promotion.py:158`). Evidence-only; never changes a hard limit.

**C3 — Factor-verdict → BMA-weight proposer (closes the factor loop, silence-only).**
New `hermes_quant/factors/weight_proposer.py` driven by a weekly cron that runs
`FactorOracle.evaluate_all` (`factor_oracle.py:450`) and emits a CANDIDATE
weight diff (premium↑ within a cap, rejected→silence-toward-0). Mirror the
`graph_mining.py` honesty rails: PROPOSE only, operator/eval-gate to apply,
silence-only (never amplify above a cap). Pairs with lifting the
`settlement_loop` `slippage_only` gate (`settlement_loop.py:35-48`) once
entry+exit joining lands so BMA posteriors can persist and learn.

**C4 — Monthly meta-retro (weekly → monthly).**
New `quant-monthly-meta-retro` cron that aggregates the weekly digests (C2) +
debate/risk-committee audit rows (`stage.py:345`) + promotion records into a
monthly self-critique: which lesson categories repeat, which personas/debate
stances were right, candidate hypotheses to feed `HypothesisRunner`
(`orchestrator.py:182`). Output is a report + candidate hypotheses; promotion to
live stays operator/eval-gated.

**C5 — Build B10 learned-graph miner from its own spec.**
Implement `mine_graph()` exactly as designed in `catalyst/graph_mining.py:1`
(propose per-edge FLIP/DOWNWEIGHT/PRUNE; never auto-mutate the seed YAML;
silence-only `confidence_multiplier`). Corpus already accumulates
(`propagation.py:197`).

**Closure shape:** C1 lights the existing per-trade edge; C2 adds the weekly
aggregation the vision names and writes the one dangling gate field; C4 adds the
monthly meta-retro; C3+C5 extend the same propose-only, eval-gated pattern to
factors and the catalyst graph. The deterministic gate, hard limits, and sizing
ladder are untouched throughout — every new component is a proposer feeding the
existing operator/eval-gated promotion machinery.
