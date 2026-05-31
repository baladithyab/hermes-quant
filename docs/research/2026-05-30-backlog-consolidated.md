# Hermes-Quant — Consolidated Backlog (2026-05-30)

> Single deduplicated, categorized backlog for the deep-work execution loop.
> Mined from: roadmap (`docs/roadmap/PROJECT-ROADMAP-2026-05-27.md`), gap tracker
> (`~/wiki/projects/hermes-quant-architecture-and-gaps.md`), project log
> (`~/wiki/projects/hermes-quant.md`), all ADRs under `docs/adr/`, the codebase
> marker grep, and git history (`e4ecad5` … `75cd27e`, 40 commits) + `CHANGELOG.md`.
> Each row collapses duplicate appearances across sources into ONE item.

## Conventions

- **Priority** — P0 = correctness/safety/money-bug, explicit 🚨, or blocks multiple items.
  P1 = high-leverage capability gap the operator explicitly wants. P2 = polish / deferred-v0.3+ / exploratory.
- **Complexity** — S / M / L / XL (carried from source effort columns where given).
- **Status** — `open` (unbuilt), `gated` (built but behind a flag / awaiting an eval-gate or manual flip), `proposed` (ADR not yet implemented in code), `stale-cleanup` (code/comment drift to remove).
- **Note on ADR "Status: Proposed":** this repo ships ADRs but rarely flips their header to *Accepted* (only ADR-0076 is Accepted). So an ADR header of "Proposed" is NOT by itself a signal of pending work — every row below was cross-checked against git log + code. Genuinely-unbuilt ADRs are flagged `proposed`; shipped-but-header-stale ones are listed under "Already shipped".

---

## Backlog

| ID | Title | Category | Priority | Complexity | Depends-on | Source (file:line) | Status |
|---|---|---|---|---|---|---|---|
| **B01** | **ADR-0029 multi-leg options reactor** — covered_call / CSP / wheel / spreads cannot fire; PaperReactor is equity-only. Playbook ranks but cannot execute options. 22-of-25 universe signals are SHORT and all gate-rejected (`short_signal_deferred`). Largest gap vs operator vision. 6-PR plan exists. | capability | **P0** | XL | B02, B03 | wiki/hermes-quant.md:93 (🚨); docs/adr/ADR-0029-multi-leg-paper-reactor.md:3; roadmap:178; `~/.hermes/plans/2026-05-28_multi-leg-options-implementation.md` | proposed |
| **B02** | **ADR-0028 options data layer** — OCC-21 format/parse, `OptionLeg`/`NetGreeks` dataclasses, Alpaca options-chain snapshot reader. Prerequisite foundation for B01. | capability | P0 | M | — | docs/adr/ADR-0028-options-data-layer.md:3; plan Phase A (Tasks A1–A4) | proposed |
| **B03** | **ADR-0027 options-aware risk gate** — net-greeks + defined-risk/collateral-secured checks; wired into MultiLegProposal construction before HITL. Blocks B01 proposals. Optlib kernel ready, "just needs the gate wired." | capability | P0 | M | B02 | docs/adr/ADR-0027-options-aware-risk-gate.md:3; wiki/hermes-quant.md:92 (open work #5); plan Phase B | proposed |
| **B04** | **Direction-vs-play-bias mismatch in autonomous-tick** — `quant-autonomous-tick.py:run_tick` fires advisor SHORT signals through bullish-bias plays (CSP/covered_call). Live evidence: AXP SHORT routed via `csp`. Fix: direction-compatibility filter before propagating; flag as `gate=DIRECTION_BIAS_MISMATCH`. | correctness | **P0** | S | — | wiki/hermes-quant.md:98 (🚨) | open |
| **B05** | **ADR-0075 catalyst-driven universe onboarding** — temporarily admit out-of-universe symbols when a fresh high-conf/high-sev packet targets them (tradeability gate, scoped horizon, ≤3 cap, `admitted_via=catalyst` tag, behind `HERMES_QUANT_CATALYST_ONBOARDING=1`). 4/5 consumer targets (CELH/CROX/DIIBF/NWL) + LUNR/LCID/SPR are perceived-but-unactable. No code yet (`grep CATALYST_ONBOARDING` = 0 hits). | capability | P1 | M | — | docs/adr/ADR-0075-catalyst-driven-universe-onboarding.md:1; wiki/hermes-quant.md:302,307 | proposed |
| **B06** | **Catalyst profitability cron schedule** — `profitability.py` + `quant-catalyst-profitability.py` exist but are NOT wired into a firing cron (only referenced in skill docs). Needed to clear `MIN_SAMPLE=20` brand_self propagations at ≥0.60 hit-rate before the consumer-trend confidence haircut can be raised. | capability | P1 | S | — | wiki/hermes-quant.md:307; hermes_quant/catalyst/profitability.py:35; ops/scripts/quant-catalyst-profitability.py | gated |
| **B07** | **Raise consumer-trend confidence haircut** — `CONSUMER_TREND_CONFIDENCE_HAIRCUT=0.5` is a deliberate weak-PEER setting; raise toward 1.0 (or prune) only after the B06 profitability loop confirms edge on live returns. | capability | P1 | S | B06 | wiki/hermes-quant.md:305; hermes_quant/catalyst/synthesize.py (`CONSUMER_TREND_CONFIDENCE_HAIRCUT`); docs/adr/ADR-0076 | gated |
| **B08** | **Wire dedicated social producers (Reddit + Google Trends) into ingester** — `hermes_quant/catalyst/social.py` producers exist but only GN consumer queries are in the deployed ingester. Reddit 403s datacenter IPs → needs script-OAuth app or residential egress. | capability | P1 | M | — | wiki/hermes-quant.md:226,307; hermes_quant/catalyst/social.py; deployed `quant-catalyst-ingest.py:43` | gated |
| **B09** | **Larger labeled social-arb eval set at higher threshold** — Phase-0 passed at knife-edge 3/5 = exactly 0.60 (TPR/NWL were false positives). Need a larger set clearing a HIGHER bar before consumer-trend edges earn live weight. | capability | P1 | M | — | wiki/hermes-quant.md:197-201,226; ops/scripts/quant-catalyst-socialarb-eval.py | gated |
| **B10** | **Learned-graph mining job** — durable propagation-log corpus accumulates at `propagation-log.jsonl`; the actual job that joins it against forward returns to learn corrected edge signs is unbuilt. The "moat." | capability | P1 | L | — | wiki/hermes-quant.md:222,226,290; hermes_quant/catalyst/propagation.py (`log_propagations`) | open |
| **B11** | **Calibrator drift detection** — auto-refit weekly via the bootstrap CLI; alert when raw→calibrated drift > 5%. Still open. | correctness | P1 | M | — | wiki/hermes-quant.md:90 (open work #3) | open |
| **B12** | **Flip ADR-0070 paper-slippage + ADR-0071 portfolio-caps to default ON** — both shipped default-OFF (`HERMES_QUANT_PAPER_SLIPPAGE_MODEL=v0.2`, `HERMES_QUANT_PORTFOLIO_CAPS=1`). Plan: enable on cron wrappers after operator audits a side-by-side tick log; promote after one clean day. 880%-gross book + 0-bps assumption currently baked into reflectors/calibrators. | correctness | P1 | S | — | wiki/hermes-quant.md:50; CHANGELOG/ADR-0070, ADR-0071 | gated |
| **B13** | **`play_tag` plumbing on executions.jsonl** — retro can't distinguish advisor vs playbook vs autonomous-tick fires (all read `advisor`). Naturally carried by multi-leg fills, so deferred to B01. | observability | P2 | S | B01 | wiki/hermes-quant.md:97 (open work #10) | open |
| **B14** | **Codex review LOW/MED follow-ups** — (a) journal-then-state ordering crash window [LOW]; (b) `_SNAPSHOT_CACHE` thread-safety under cron concurrency [MED]; (c) wheel composite eviction divergence [LOW]. | correctness | P2 | S | — | wiki/hermes-quant.md:101 (open work #14) | open |
| **B15** | **Re-commit 2-week ADR freeze** — audit recommendation broken on day 2 (ADR-0067). Re-commit through end of June. | process | P2 | S | — | wiki/hermes-quant.md:102 (open work #15) | open |
| **B16** | **G3 — markdown render layer over decisions.jsonl** | observability | P2 | S | — | gaps.md:89; roadmap:64; roadmap Wave 4 | open |
| **B17** | **G7 — Pydantic `PortfolioDecision` schema + executive summary / PortfolioManager node** (gap matrix marks ❌ "no PortfolioManager node"; note `PortfolioDecision` schema partially exists from ADR-0037, verify scope) | capability | P2 | M | — | gaps.md:93 | open |
| **B18** | **G10 — per-schema `render_X(schema_obj)->str` markdown helpers** (currently ad-hoc formatting) | polish | P2 | S | — | gaps.md:94; roadmap:181 | open |
| **B19** | **G11 — `bind_structured()` + `invoke_structured_or_freetext()` provider-aware helper** (currently ad-hoc fallback) | polish | P2 | S–M | — | gaps.md:95; roadmap:40; roadmap Wave 2 | open |
| **B20** | **G12 — insider-transactions tool (Form 4 / SEC EDGAR or yfinance.insider_transactions)** | capability | P2 | S–M | — | gaps.md:96 | open |
| **B21** | **G15 — same-ticker rich vs cross-ticker lean retriever split in `get_past_context`** (BM25 ranks but doesn't split) | capability | P2 | S | — | gaps.md:98 | open |
| **B22** | **C6 — multi-source data fallback chain (yfinance → ccxt → alpha_vantage with retry)** (providers exist, no auto-fallback) | resilience | P2 | M | — | gaps.md:113 | open |
| **B23** | **HKUDS G3 — task-routing decision tree in system prompt** | capability | P2 | S | — | gaps.md:114 | open |
| **B24** | **A7 — 5-section structured System Prompt template + frozen persistent memory** (partial; 5-layer compression done) | capability | P2 | S | — | gaps.md:115 | open |
| **B25** | **B2 — Hypothesis Registry richer schema (`monitoring` status, `run_cards[]` linkage)** | capability | P2 | S | — | gaps.md:116 | open |
| **B26** | **D1 — broker trade-journal parser (CSV → behavior diagnostics)** | capability | P2 | M | — | gaps.md:108; roadmap Wave 8:112 | open |
| **B27** | **D2 — auto-extract shadow rules (3–5 if-then) from profitable trades** (currently hand-coded ShadowRule) | capability | P2 | M | B26 | gaps.md:109 | open |
| **B28** | **D3 — Delta-PnL attribution buckets (missed/noise/early/late/over)** (shadow report has per-rule, not bucket) | observability | P2 | M | B26 | gaps.md:110 | open |
| **B29** | **B1 — Finance Research Goal Ledger (SQLite, status machine, criteria, audit trail)** | capability | P2 | M | — | gaps.md:107 | open |
| **B30** | **B3 — research-only RiskTier keyword guard at goal creation** | safety | P2 | S | B29 | gaps.md:124 | open |
| **B31** | **F2 — signal-engine source AST validation** | safety | P2 | M | — | gaps.md:123 | open |
| **B32** | **C3 — validation suite (MC + Bootstrap CI + Walk-Forward → `validation.json`)** (aspired per ADR-0006, not plumbed) | rigor | P2 | M | — | gaps.md:111; roadmap Wave 6:93 | open |
| **B33** | **Backtester `--dry-run` StubLLM mode + CostTracker** | rigor | P2 | L | — | roadmap Wave 6:91 | open |
| **B34** | **Point-in-time data layer — historical fetchers filter by reporting-lag-adjusted as_of** | rigor | P2 | L | — | roadmap Wave 6:92 | open |
| **B35** | **Transaction-cost model — half-spread + market-impact per asset class (backtest)** | rigor | P2 | M | — | roadmap Wave 6:94 | open |
| **B36** | **Survivorship-bias guard — universe = listed-at-asof, not currently-listed** | rigor | P2 | M | — | roadmap Wave 6:95 | open |
| **B37** | **STOCKBENCH-style smoke harness — 5 tickers over 60-day post-cutoff window vs buy-and-hold** | rigor | P2 | M | B34 | roadmap Wave 6:96 | open |
| **B38** | **IC deduplication gate at factor ingestion (ICmax ≥ 0.99 → discard)** | rigor | P2 | S | — | roadmap Wave 6:97 | open |
| **B39** | **G5 — LangGraph `ToolNode` loops with `should_continue_<analyst>` gating** (analysts non-agentic; deferred for HITL-surprise risk) | capability | P2 | L | — | gaps.md:91; gaps.md:147 (defer until G1) | open |
| **B40** | **A1 — SwarmRuntime + DAG execution (topological scheduler with `input_from`)** + A5 AgentLoop | capability | P2 | L | — | gaps.md:112,200 | open |
| **B41** | **R4 — production rollout favors deterministic path; LLM stages flagged but heuristic fallbacks heavier.** Decide a path to make LLM stages production-default. | strategy | P2 | M | — | gaps.md:134 | open |
| **B42** | **R6 — HMM unsupervised label-mapping; downstream must not anchor on label strings** (defensive guard) | correctness | P2 | M | — | gaps.md:136 | open |
| **B43** | **R5 — load testing — full-universe pipeline behavior untested** | rigor | P2 | L | — | gaps.md:135 (🔴 skip-tier) | open |
| **B44** | **Wave 7 regime depth — Markov-switching regime as conditioning input to BMA weights + per-regime priors + RV-percentile/yield-curve-slope state vars** (base regime shipped v0.6.0; this is the deeper conditioning) | capability | P2 | L | — | roadmap Wave 7:103-105 | open |
| **B45** | **Wave 8 experimental — Hypothesis Registry + Run Cards artifacts; AlphaBench FFO MCP factor oracle; 452-factor Alpha Zoo w/ AST purity + lookahead sentinel; RL post-training (explicitly skip per user direction)** | exploratory | P2 | L/XL | — | roadmap Wave 8:111-115; gaps.md:146 (RL: do-not-build) | open |
| **B46** | **data_grounding v0.2 — replace 2-layer trim with HKUDS full 5-layer compression pipeline** | polish | P2 | M | — | hermes_quant/grounding/data_grounding.py:19,313 (TODO) | open |
| **B47** | **`proposals.py` JSONL index reconstruction (`_reconcile_index`) not implemented** | resilience | P2 | S | — | hermes_quant/proposals.py:363 | open |
| **B48** | **`governance/promotion.py` remove `react.live` fallback once live reactor lands** (TODO; tied to B01) | cleanup | P2 | S | B01 | hermes_quant/governance/promotion.py:11 | open |
| **B49** | **STALE-COMMENT cleanup: `research_debate/stage.py:175-184`** — comment says production turn/judge wiring "not yet implemented" and helpers "not defined," but `_run_one_turn_with_history`/`_run_research_manager_judge` exist (`llm_committee.py:659,779`) and the dispatch site (`llm_committee.py:983-991`) already injects them. Remove stale comment; consider defaulting the injection points instead of raising NotImplementedError. | cleanup | P2 | S | — | hermes_quant/agents/research_debate/stage.py:175 | stale-cleanup |
| **B50** | **bma.py — cross-correlation in Beta update not yet exploited (StackingAggregator, v0.1.3)** | capability | P2 | M | — | hermes_quant/aggregators/bma.py:30,644 | open |
| **B51** | **Proper one-way deploy-sync for `quant-daily-interim.py`** — immediate drift closed (Issue #23) by vendoring the 904-line deployed copy, but a real deployed↔repo sync mechanism is still worth doing. | process | P2 | S | — | wiki/hermes-quant.md:180,120 | open |

---

## Already shipped (do NOT re-do)

Items that the docs (roadmap / gap matrix / older ADR headers) imply are pending but git history + code confirm are DONE:

| Item | Evidence it shipped |
|---|---|
| **Waves 1–6 of the 2026-05-27 roadmap** (observability/state, TraderProposal contract, 3-way risk committee + Trader stage, memory/reflection, data grounding, much of backtesting discipline) | Pipeline in gaps.md:52-57 shows full chain live: `risk-committee(3-way LLM) → trader(LLM-structured) → gate(provenance) → react(state.db) → reflector`. Commits `75cd27e`…`53c47ad`. |
| **TauricResearch G1, G2, G4, G6, G13, G17** (Bull/Bear adversarial debate, two-debate independence, FundamentalsAnalyst, ResearchPlan Pydantic, 5-tier PortfolioRating enum, conversational debate prompts) | gaps.md:87-99 all marked ✅ SHIPPED v0.6.1/v0.6.2; commits `525c102` (PR #14), `081982e` (PR #15). |
| **ADR-0066 ResearchDebateStage production wiring** | git `081982e` "v0.6.2: ResearchDebateStage Production Wiring"; dispatch live at `llm_committee.py:977-991` with both helpers injected. (The stage.py NotImplementedError *comment* is stale — see B49.) |
| **R1 — regime in `MarketContext.extras`** | gaps.md:130 ✅ SHIPPED v0.6.0 PR #13; commit `05213bd`. |
| **R2b — pre-existing test failures / baseline cleanup; R3 — CI** | git `13b03af` "v0.6.3: Baseline Cleanup + CI Bootstrap (close R2b)"; gaps.md:36 "CI is now live." |
| **ADR-0070 paper-slippage model; ADR-0071 portfolio-aware Kelly** (code shipped) | git `fd688e3`, `9227464`; wiki:47-48. (Only the *default-ON flip* remains — B12.) |
| **ADR-0068 decision-time honesty; ADR-0069 still-forming-bar drop** | git `91ecff0`; wiki:45-46. |
| **ADR-0072 advisor intraday open-guard** | git `b8318ba`. |
| **ADR-0074 Catalyst Sense Phase 1 + GO-LIVE** (ingester, classify, propagation graph 8 sectors, synthesize, eval gate, advisor wiring, decision-time fix) | git `afd390f`,`60e87a3`,`e17c079`,`53c47ad`,`2b917fb`,`e4ecad5`; `HERMES_QUANT_SEMANTIC_ENABLED=1` set in `.env:437`. |
| **ADR-0076 social-arbitrage integration** (consumer-trend edges live in `_BUILTIN_GRAPH`, confidence haircut, profitability module, social producers built) | Only ADR with header **Status: Accepted**; wiki:265-309. Live wiring of producers + raising the haircut still gated (B07/B08). |
| **Kronos GPU path; magnitude clip** | git via `cab7541`/`180c621`; wiki:64,78,91 (open work #4 ✅). |
| **Open-work items #1, #2, #4, #7, #8, #9, #12, #13** (sticky onboarding, market-hours tick, Kronos GPU, strategy-retro cron, regime-gated play activation, reflection wiring, cron formatting sweep, daily portfolio snapshot) | wiki/hermes-quant.md:88-100 all struck-through / ✅. |
| **R1 advisor regime merge, FundamentalsAnalyst cron, pytest-socket (F6)** | gaps.md:156-174, v0.6.0/v0.6.1. |
| **Issue #23 (deployed↔repo drift for quant-daily-interim)** — immediate drift CLOSED | wiki:180. (Only the *generic* deploy-sync mechanism remains — B51.) |

### Markers that are NOT actionable backlog (scaffold/by-design)

- `cli/__init__.py`, `cli/halts.py`, `tools.py` "NOT YET IMPLEMENTED in v0.1.0 scaffold" / "daemon not yet implemented" — these are old v0.1 CLI-scaffold strings; the daemon/cron path superseded them. Not real backlog unless the CLI `start`/`status` surface is revived.
- `daemon/tick_loop.py:20`, `signal_bus.py:223`, `slippage.py:101` "v0.2 may add …" — speculative future-notes, not committed work.
- ChromaDB, multi-vendor routing, Pine Script export, AI-Trader social features, RL post-training, ToolNode-for-all-analysts — explicit **do-not-build** (gaps.md:140-147, roadmap:117-126).

---

## Count summary (open + gated + proposed = actionable)

- **P0: 4** (B01, B02, B03, B04)
- **P1: 8** (B05, B06, B07, B08, B09, B10, B11, B12)
- **P2: 39** (B13–B51)

**Total actionable: 51.**

**Top P0:** B01 — ADR-0029 multi-leg options reactor (the single biggest gap vs operator vision; covered-call/CSP/wheel cannot fire; gated behind B02+B03).
**Top P1:** B05 — ADR-0075 catalyst-driven universe onboarding (perceive-but-can't-act gap; the catalyst feature's signal is dead-on-arrival for the exact high-beta small-caps it targets).

---

## Reconciliation pass — 2026-05-30 (post deep-work-loop + concurrent meta-review)

This backlog was mined at `e7abf50`, BEFORE the session's execution + ADR-0079 capstone +
the concurrent meta-review. The meta-review (`docs/reviews/2026-05-30-concurrent-meta-review.md`)
graded `backlog_complete = FALSE`. This section reconciles status drift and enrolls the net-new
items so the backlog can safely drive the next loop. (Source for every M-item: the meta-review.)

### Status corrections (items whose state drifted)

| Item | Was | Now | Evidence |
|---|---|---|---|
| B02 / B03 options data-layer + gate | proposed | **built, default-OFF (gated)** | `6b1c05d` |
| B04 direction-vs-play-bias gate | open | **shipped default-OFF** (but NOT live — deploy drift M01) | `73d7ed4` |
| B06 catalyst profitability cron | gated | **built as change-detecting watchdog; NOT deployed/registered** (M10) | `2e84f28` |
| B11 calibrator drift | open | **built; NOT deployed; violates silence contract** (M11) | `4f27d19` |
| B16 / B18 render layer | open | **built-but-UNWIRED** (orphaned modules, M18) | `4f27d19` |
| B12 portfolio-caps + slippage | gated | **silently LIVE in deployed armed wrappers** (M09) | deploy (outside git) |
| B47 proposals `_reconcile_index` | open | **shipped + hardened fail-loud** | `0aebd88` |
| ADR-0077 admissibility | (absent) | **shipped default-OFF — NEW gated item** | `ee75811` |

### Net-new items enrolled (from the meta-review — full detail in the review doc)

**P0 (must precede live enablement):**
- **N1 (M01/M02)** deploy-sync + anti-drift — ✅ TOOL+TEST+RUNBOOK SHIPPED this round (`6fdb2ed`); operator reconciliation/redeploy still pending.
- **N2 (plugin-enable)** `hermes plugins enable hermes-quant` — manifest fixed `0bde804`; operator config.yaml edit pending.
- **N3 (M03/B01)** build + wire the ADR-0029 multi-leg reactor — THE single biggest vision unlock; foundation (B02/B03) now shipped.
- **N4 (M04)** B04 ships dormant even after redeploy — armed wrapper must also set `HERMES_QUANT_DIRECTION_BIAS_GATE=1`.

**P1 (blocks a specific feature's safe enablement):**
- **N5 (M05)** admissibility gates ONLY the autonomous-tick seam — daily-interim brief / HITL `quant_approve` / PaperReactor are NOT admissibility-aware → an inadmissible short still fires on paper when the flag flips.
- **N6 (M06)** live admissibility seam runs POST-gate, contradicting ADR-0077 D77.4 + the corrected ADR-0079 ADMIT-before-GATE ordering.
- **N7 (M07)** account context (`account_equity`/`available_bp`) not plumbed → live oracle fail-closed-silences EVERY short; AND the 38-short premise is unasserted by any eval.
- **N8 (M08)** the "fix #11 before flipping ADMISSIBILITY" ordering lived only in a review artifact — now in FEATURE-ENABLEMENT.md.
- **N9 (M10)** the 2 new crons are neither deployed nor registered → B06/B07/B11 feedback loops dead-on-arrival.
- **N10 (M11)** calibrator-drift cron prints every run → violates the no_agent silence contract → operator spam.
- **N11 (M12)** ADR-0075 onboarding eval-gate axis is named-but-unbuilt; only the flag separates the live scanning seam from firing.
- **N12 (M13)** **PerceptionFrame + PDR-1..4 are 100% aspirational and absent from this backlog** — enroll PDR-1 (PerceptionFrame, first; also fixes M06 ordering + M17 tool-path decoupling), PDR-2 (TrendVelocity), PDR-3 (ConvergenceValidator), PDR-4 (SaturationScore + M24 property tests), in dependency order, each behind its eval gate.
- **N13 (M16)** social-arb / AMZN-OOS evals read/write `/tmp` + live yfinance → promote the labeled set to a versioned `tests/fixtures` before B07/B09 can be trusted.

**P2 (correctness/coverage to close before the relevant flip):**
- **N14 (M14)** weekly-pattern-mining retro + monthly meta-retro crons (the self-improving loop) don't exist.
- **N15 (M17)** the `quant_autonomous_tick` TOOL path never injects semantic packets even with the flag ON (only the cron wrapper does) — PDR-1 closes this.
- **N16 (M18)** wire `render_decisions_md` / schema renderers into a cron/tool/CLI (currently orphaned).
- **N17 (M19)** test the profitability verdict at the `MIN_HIT_RATE=0.60` boundary + `MARGINAL_HOLD` band (the exact B07 decision line).
- **N18 (M20)** the autonomous-tick wiring regression test reconstructs the wrapper inline — load the real shipped script instead.
- **N19 (M21)** extend the no-lookahead release-blocker gate to the wiring/onboarding PRODUCING path (a PDR-1 precondition).
- **N20 (M22)** run the live-broker integration tests (Alpaca admissibility, options chain) with recorded output as a mandatory pre-flip gate.

**P3 (record-only, attached to the named future gate):**
- **N21 (M24)** the saturation "can-only-subtract" + "never-touches-non-semantic-views" property tests are the named eval gate for `HERMES_QUANT_SATURATION` (PDR-4).

### The enablement critical path (the load-bearing sequence — see DEPLOY-SYNC.md + FEATURE-ENABLEMENT.md)

0. deploy + anti-drift reconcile (N1) → 1. fix #11 hardening + plumb account context (N7) →
2. run live-broker tests (N20) → 3. flip PORTFOLIO_CAPS+SLIPPAGE (reconcile M09) →
4. redeploy + flip DIRECTION_BIAS_GATE (N4) → 5. fix brief/reactor admissibility seam (N5) +
ordering (N6), then flip ADMISSIBILITY → 6. build+wire B01 multi-leg reactor (N3) →
7. deploy+register the 2 crons after fixing calibrator silence (N9/N10) →
8. build ADR-0075 eval axis (N11), flip CATALYST_ONBOARDING →
9. fix tool-path decoupling (N15) + extend no-lookahead gate (N19), flip SEMANTIC_ENABLED on all paths →
10. land PDR-1 PerceptionFrame (N12), then PDR-2→3→4.

**Verdict:** backlog now reconciled. Open items are sequenced behind explicit gates; none are
silently dropped. The next loop's headline is **N3 (multi-leg reactor)** once N1 (deploy) lands.
