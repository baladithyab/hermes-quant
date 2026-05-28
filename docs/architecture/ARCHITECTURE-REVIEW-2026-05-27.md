# Architecture Review — hermes-quant

**Date:** 2026-05-27
**Branch:** `main` @ commit `e2f9935` (PR #11)
**Scope:** Full system review covering 11 PRs of work (Wave 1 → v0.5)

---

## 1. By the numbers

| Metric | Value |
|---|---|
| Production Python LoC | **42,318** across 35 packages |
| Test LoC | **39,450** (≈0.93:1 test:prod ratio) |
| Tests collected | **2,021** (excl. 25 pre-existing alpaca-import errors) |
| ADRs | **60** decisions of record |
| PRs merged this loop | **8** (#4 → #11) |
| Append-only event stores | **6** (audit_log, decisions, run_cards, hypotheses, promotion_decisions, factor_verdicts) |
| Cron jobs | **9** quant + 1 watchdog |
| LLM-wired stages | **3** (RiskCommittee, Reflector, Trader) — feature-flagged |

---

## 2. The pipeline (request → execution → reflection)

```
                 ┌─────────────────────────────────────────────────────────┐
                 │                  SCHEDULED PRODUCERS                    │
                 │                                                         │
   03:15 PT ──── │  universe-scan ─→ candidate set                         │
   03:30 PT ──── │  watchlist-evolve ─→ play-fit.json (eligibility scores) │
                 │                                                         │
                 └─────────────────────────────────────────────────────────┘
                                          │
                                          ▼
                 ┌─────────────────────────────────────────────────────────┐
                 │           ANALYSTS (parallel fan-out)                   │
                 │                                                         │
                 │  Mansa  Kamau  Asha  Imani  Tiwa  Nia  Sankofa  Anansi  │
                 │  ──────────────────────────────────────────────────     │
                 │  Each emits: AnalystSignal{action, confidence, evidence}│
                 │                                                         │
                 └─────────────────────────────────────────────────────────┘
                                          │
                                          ▼
                 ┌─────────────────────────────────────────────────────────┐
                 │         BMA AGGREGATOR (regime-aware, Wave 7)           │
                 │                                                         │
                 │  • Bayesian Model Averaging                             │
                 │  • Markov-switching regime weights (HMM v0.2)           │
                 │  • require_ensemble=True, n_distinct_analysts≥2         │
                 │  • Emits: AggregateSignal + degeneracy_metrics          │
                 │                                                         │
                 └─────────────────────────────────────────────────────────┘
                                          │
                                          ▼
                 ┌─────────────────────────────────────────────────────────┐
                 │       RISK COMMITTEE v0.2 (TauricResearch)              │
                 │                                                         │
                 │  Aggressive  Conservative  Neutral  (LLM round-robin)   │
                 │  • CV5 invariant: 5 rounds, 0.5× position size          │
                 │  • LLM_PROMPT_TEMPLATE per persona                      │
                 │  • Partial-fallback: heuristic if LLM degrades          │
                 │                                                         │
                 └─────────────────────────────────────────────────────────┘
                                          │
                                          ▼
                 ┌─────────────────────────────────────────────────────────┐
                 │     TRADER NODE v0.2 (LLM-structured, ADR-0054)         │
                 │                                                         │
                 │  Emits: TraderProposal{                                 │
                 │    ticker, side, entry_price, stop_loss,                │
                 │    target_price, time_horizon, size_fraction,           │
                 │    signal_provenance, regime, citations[]               │
                 │  }                                                      │
                 │                                                         │
                 └─────────────────────────────────────────────────────────┘
                                          │
                                          ▼
                 ┌─────────────────────────────────────────────────────────┐
                 │          GATE (governance.gate, Wave 1)                 │
                 │                                                         │
                 │  • Cost-aware (CostTracker, Wave 6)                     │
                 │  • signal_provenance plumbing (ADR-0041)                │
                 │  • is_bma_degenerate() guard                            │
                 │  • is_n1_collapse() structural guard                    │
                 │  • Emits: gate_event → audit_log.jsonl                  │
                 │                                                         │
                 └─────────────────────────────────────────────────────────┘
                                          │
                                          ▼
                 ┌─────────────────────────────────────────────────────────┐
                 │          PROPOSAL (24h TTL, HITL contract)              │
                 │                                                         │
                 │  proposals.db  ←  approve <PROPOSAL_ID>                 │
                 │                                                         │
                 └─────────────────────────────────────────────────────────┘
                                          │ (after approval)
                                          ▼
                 ┌─────────────────────────────────────────────────────────┐
                 │         PAPER REACTOR + STATE.DB (Wave 1c)              │
                 │                                                         │
                 │  • PaperReactor.execute → executions.jsonl              │
                 │  • PortfolioState.apply_execution → positions+cash      │
                 │  • Idempotent via processed_fills(proposal_id, asof)    │
                 │  • BEGIN IMMEDIATE write-lock                           │
                 │  • DELETE-on-flat (semantic clarity)                    │
                 │  • chmod 0o600 on state.db                              │
                 │                                                         │
                 └─────────────────────────────────────────────────────────┘
                                          │
                                          ▼
                 ┌─────────────────────────────────────────────────────────┐
                 │          REFLECTOR v0.2 (Mai0313, ADR-0057)             │
                 │                                                         │
                 │  • Post-trade reflection with rubric                    │
                 │  • LLM-structured ReflectionLLMOutput                   │
                 │  • Self-grade-refusal robustness (_normalize_model_id)  │
                 │  • BM25 cross-ticker memory retrieval                   │
                 │  • Oracle Fallacy guard (canonical regression)          │
                 │  • Alpha-vs-benchmark in reflection                     │
                 │                                                         │
                 └─────────────────────────────────────────────────────────┘
```

---

## 3. The 9 quant crons (orchestration layer)

| Time (PT) | Cron | Role | Hardening |
|---|---|---|---|
| 03:15 | `quant-universe-scan-daily` | Discovers candidate tickers | — |
| **03:30** | **`quant-watchlist-evolve-daily`** | Evolves play-fit scores | **v0.5: abort guard + budget warn (PR #11)** |
| **05:00** | **`quant-halts-watchdog-daily`** | Monitors orphaned halts | **v0.5: NEW (PR #11)** |
| 05:30 | `quant-daily-premarket-interim` | Generates premarket proposals | v0.4 surfaces |
| 06:00 | `quant-playbook-tick-daily` | Daily playbook execution | — |
| 07:00–13:00 | `quant-hourly-market-tick` (×7) | Intraday refresh | — |
| 12:30 | `quant-daily-eod-interim` | EOD proposal generation | v0.4 surfaces |
| Mon 06:30 | `quant-playbook-weekly` | Weekly rebalance | — |
| Q1 06:30 | `quant-playbook-quarterly` | Quarterly rebalance | — |

---

## 4. Six append-only event stores (audit infrastructure)

| File | Role | Producers |
|---|---|---|
| `audit_log.jsonl` | Canonical decision log | gate, react, state |
| `decisions.jsonl` | Per-cycle decision record | trader, risk_committee |
| `run_cards.jsonl` | Hypothesis-test artifacts | promotion_orchestrator |
| `hypotheses.jsonl` | Hypothesis registry | research_autopilot |
| `promotion_decisions.jsonl` | Promotion approve/reject | promotion_cron |
| `factor_verdicts.jsonl` | Factor production-readiness | factor_oracle |

All stores are **append-only**, **JSONL-format**, **silence-by-default** (per ADR-0031), and **observable via CLI** (`hermes_quant.governance.audit_log_query`).

---

## 5. The 35 packages (organized by stage)

### Producers (data → signal)
- `data/` (1,483 LoC) — yfinance, ccxt, alpaca providers
- `universe/` — candidate ticker discovery
- `playbook/` (1,529) — strategy templates
- `factors/` (2,820) — AlphaZoo + IC panel + FactorOracle
- `grounding/` (661) — Data Grounding Block + Citation HARD RULE

### Decision (signal → proposal)
- `analysts/` (2,007) — 8 analyst personas
- `aggregators/` (1,924) — BMA + regime-aware weights
- `regime/` (1,474) — HMM regime classifier (v0.2)
- `agents/` (2,670) — Risk committee + Trader node + LLM-caller
- `memory/` (1,594) — Reflector + BM25 retrieval
- `evidence/` (815) — Citation tracking
- `research/` (1,119) — Research autopilot

### Execution (proposal → state)
- `risk/` (713) — Gate + cost model
- `react/` — Paper reactor (positions + cash)
- `state/` (846) — PortfolioState + idempotency
- `journal/` (891) — settlement + audit
- `options/` (804) — Multi-leg paper reactor
- `shadow/` (1,159) — Shadow account counterfactual

### Governance (audit + observability)
- `governance/` (1,410) — Audit log + degeneracy guards + signal_provenance
- `observability/` (781) — Status CLI + daily report + fallback probe
- `eval/` (1,092) — Promotion orchestrator
- `evaluation/` (459) — Lookahead sentinel v0.2
- `runs/` — Run cards
- `reporting/` (832) — Daily reports
- `backtest/` (2,407) — Walk-forward + cost-tracker

### Infrastructure
- `cli/` (1,003) — halts, status, kill-switch
- `daemon/` (2,990) — long-running orchestrator
- `consumers/` — event consumers
- `schemas/` (469) — Pydantic types
- `config/` — config layer
- `skills/` — skill packs
- `training/` — RL training (deferred)
- `utils/` — helpers

---

## 6. The 60 ADRs (organized by wave)

### Wave 0: Foundation (ADR-0001 → ADR-0040)
Sidecar architecture, analyst protocol, BMA aggregator, risk gate, data layer, plugin shape, settlement journal, options-aware risk gate, governance plane consolidation, evidence store, run cards, RobinhoodMCP reactor.

### Wave 1: Observability + State (ADR-0041 → ADR-0042)
- 0041: signal_provenance audit trail
- 0042: persistent memory + reflection design

### Waves 2-6: Full PRD pipeline (ADR-0043 → ADR-0050)
- 0043: 3-way risk committee (TauricResearch)
- 0044: Trader stage + structured output
- 0045: Backtester + walk-forward + cost model
- 0047: Regime-aware BMA weights
- 0048: Hypothesis registry + run cards
- 0049: Shadow account counterfactual
- 0050: Alpha Zoo + AST-purity + lookahead gate

### v0.2 Hardening (ADR-0051 → ADR-0054)
- 0051: Lookahead sentinel v0.2 (6 new SuspicionKinds)
- 0052: Promotion orchestrator + cron
- 0053: Daily brief regime/research/shadow surfacing
- 0054: LLM-Caller foundation + TraderNode v0.2

### v0.3 LLM Wiring (ADR-0055 → ADR-0058)
- 0055: FactorOracle + production-readiness tiers
- 0056: RiskCommittee v0.2 LLM wiring
- 0057: Reflector v0.2 LLM wiring
- 0058: HMM regime classifier v0.2

### v0.4 Observability + Rollout (ADR-0059 → ADR-0062)
- 0059: Unified status CLI
- 0060: Fallback probe
- 0061: Daily report
- 0062: Rollout playbook

---

## 7. The HITL contract (single source of truth)

```
PROPOSAL CREATED → 24h TTL → Discord notification with PROPOSAL_ID
                                ↓
                  user types: approve <PROPOSAL_ID>
                                ↓
                  (NOT: approve <TICKER> — id only)
                                ↓
                  PaperReactor.execute → state.db update
                                ↓
                  Reflector v0.2 runs post-trade
                                ↓
                  Reflection appended to memory
```

**Halt lift contract:**
```
python -m hermes_quant.cli.halts resume <profile> <market> <symbol>
```

The watchdog cron (NEW in PR #11) reports stale halts daily at 5 AM PT, **silence-by-default** when nothing is wrong.

---

## 8. Self-verification harness (PR #10)

`scripts/v0.4-verify-end-to-end.sh` — **16 checks, runnable on demand.**

Each check traces a specific MoA cross-model review finding to a live assertion in production code. Suitable for CI integration. Currently **16/16 PASS** on unified main.

The harness covers:
- Status CLI invocation correctness
- Fallback probe exit-code trichotomy (0/1/2)
- Daily report file mode (0o600)
- Kill-switch CLI functional path
- ROLLOUT.md command correctness
- Reflector self-grade-refusal robustness
- BMA HMM tuple destructure
- And 9 more.

---

## 9. The 8 PRs that built this

| PR | Theme | LoC delta | Tests added |
|---|---|---|---|
| #4 | Wave 1 — observability + state.db | +2,455 | +82 |
| #5 | Waves 2-6 — full PRD pipeline | massive | many |
| #6 | Waves 7-8 — regime + research autopilot | +large | +4 e2e |
| #7 | v0.2 hardening — sentinel/promotion/trader | large | +many |
| #8 | v0.3 LLM wiring — FactorOracle/RC/Reflector/HMM | +6,300 | +many |
| #9 | v0.4 observability + rollout | +5,400 | +108 |
| #10 | Verification harness + probe CLI fix | +modest | +18 (16 e2e + 2 regression) |
| #11 | v0.5 — abort guard + halts watchdog | +689/-146 | +13 |

**Total: 8 PRs, ~2,021 tests, 60 ADRs, 35 packages, ~42K production LoC.**

---

## 10. Provenance: external sources distilled

| Project | Patterns extracted |
|---|---|
| **TauricResearch/TradingAgents** | 3-way risk committee, trader intermediary stage, current_clear node, separate round counts |
| **HKUDS Vibe-Trading** (8.7K⭐ MIT) | Data Grounding Block, Citation HARD RULE, 5-layer context compression, 452-factor Alpha Zoo, AST purity gate, Hypothesis Registry, Shadow Account |
| **virattt/ai-hedge-fund** | Trader protocol, structured output |
| **Mai0313** | TraderProposal schema, post-trade reflection, BM25 retrieval, alpha-vs-benchmark |
| **Mantshimuli/Mwamba** (Springer 2026) | Markov-switching regime-aware BMA weights |

Plus internal lessons:
- **2026-05-26 incident** — uniform `confidence=1.00` + `n_distinct_analysts=1` = BMA n=1 collapse → fixed in `8345f67` (`require_ensemble=True`)
- **2026-05-27 silent eviction** — yfinance Yahoo cookie-jail = mass play eviction → fixed in PR #11 (abort guard)

---

## 11. What hasn't been built (deferred)

- **RL post-training** (`hermes_quant/training/`) — skipped per user direction (Hermes orchestrates frontier models, no need for 7B-base RL)
- **HMM supervised label-mapping** — current implementation has unsupervised label-mapping limitation; deferred to v0.4+
- **STOCKBENCH-style post-cutoff smoke tests** — Wave 6 partial; backtester ships, post-cutoff harness pending
- **452-factor Alpha Zoo full population** — FactorOracle scaffolding ships, factor population deferred

---

## 12. The honest critique

**Strengths:**
1. Audit-log-first architecture: every decision leaves a record, every record has provenance
2. Silence-by-default observability: watchdogs only speak when something is wrong
3. Idempotency at every persistence boundary (state.db, processed_fills, BEGIN IMMEDIATE)
4. Multi-model self-review (MoA) on every substantial diff
5. ADR-driven changes; 60 decisions of record means future you can reconstruct *why*
6. Test:prod ratio of ~0.93:1 — load-bearing code is well-tested
7. HITL contract is single-source: id-only approval prevents ticker-ambiguity bugs

**Weaknesses:**
1. **25 pre-existing alpaca-import test errors** in `tests/unit/wave_d/` and `tests/unit/test_universe_scanner.py` — should be either fixed or quarantined
2. **No CI** — verification harness exists but isn't auto-enforced on PRs
3. **HMM unsupervised label-mapping** — regime states have arbitrary labels; downstream consumers must not anchor on label string
4. **LLM stages feature-flagged but heuristic-fallback paths are heavier** — production rollout still favors deterministic path when env vars unset
5. **No load testing** — pipeline tested on small inputs; behavior at full universe scale TBD

**Risks for tomorrow:**
1. **yfinance reliability** — abort guard prevents corruption but doesn't prevent missed runs. If Yahoo rate-limits us at 03:30 PT, watchlist won't refresh; existing plays will stale-out via `>25h universe` guard
2. **Cron clock drift** — assumed PT timezone; if Hermes scheduler uses UTC, premarket cron may fire at wrong wall-clock time
3. **Proposals 24h TTL** — if Discord notification fails, proposals expire silently. Should add `expiring_soon_alert` watchdog for proposals approaching TTL with no decision

---

**Generated:** 2026-05-27 by deep-work-loop architecture review
**Source:** `/mnt/e/CS/github/hermes-quant` @ `e2f9935`
