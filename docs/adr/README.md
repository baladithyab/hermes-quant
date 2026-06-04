# Architecture Decision Records

85 ADRs — **generated** index (regenerate with `python ops/scripts/quant-adr-index.py --write`; do not hand-maintain).

Status vocabulary: proposed | accepted | rejected | deprecated | superseded by ADR-NNNN. A compound status (e.g. "Part A accepted; Part B proposed") is the ADR's own — see the file.

| # | Title | Status | Date |
|---|---|---|---|
| [ADR-0001](ADR-0001-sidecar-architecture.md) | Sidecar architecture — daemon decoupled from gateway | Accepted (2026-05-12), implemented | 2026-05-12 |
| [ADR-0002](ADR-0002-analyst-protocol.md) | Analyst protocol contract | Accepted (2026-05-12), implemented | 2026-05-12 |
| [ADR-0003](ADR-0003-aggregator.md) | Aggregator design — Bayesian baseline + logistic stacking, RL deferred | Accepted (2026-05-12), implemented | 2026-05-12 |
| [ADR-0004](ADR-0004-risk-gate.md) | Risk gate — deterministic rules, silence-by-default | Accepted (2026-05-12), implemented | 2026-05-12 |
| [ADR-0005](ADR-0005-data-layer.md) | Data layer — yfinance bootstrap, ccxt for crypto, alpaca-py for equities | Accepted (2026-05-12), implemented | 2026-05-12 |
| [ADR-0006](ADR-0006-rl-aggregator-deferred.md) | RL aggregator deferred to v0.2 with concrete success criterion | Proposed (deferred to v0.2) | 2026-05-12 |
| [ADR-0007](ADR-0007-plugin-shape.md) | Plugin shape — Hermes plugin tools = read-only views; daemon owns the loop | Accepted (2026-05-12), implemented | 2026-05-12 |
| [ADR-0008](ADR-0008-freqtrade-integration.md) | Freqtrade integration via signal bus (sidecar consumer) | Accepted (2026-05-12), implemented | 2026-05-12 |
| [ADR-0009](ADR-0009-amendments-from-phase4-review.md) | Phase-4 cross-family review amendments to ADR-0001..0008 | proposed | 2026-05-12 |
| [ADR-0010](ADR-0010-settlement-journal.md) | Settlement journal (markdown sidecar) | Accepted | 2026-05-13 |
| [ADR-0011](ADR-0011-portfolio-reconstruction-sign-convention.md) | Portfolio reconstruction sign convention | Accepted (2026-05-13), target v0.1.2 implementation | ? |
| [ADR-0012](ADR-0012-llmanalyst-protocol-deferred.md) | LLMAnalyst protocol (deferred to v0.3.0) | Proposed (2026-05-13), deferred to v0.3.0 implementation | ? |
| [ADR-0013](ADR-0013-hermes-core-integration-stance.md) | Hermes-core integration stance + dual-surface architecture | Accepted (2026-05-13), implemented | ? |
| [ADR-0014](ADR-0014-chat-mode-advisor-surface.md) | Chat-mode advisor surface | Accepted (2026-05-13), implemented | ? |
| [ADR-0015](ADR-0015-hitl-propose-decide-react.md) | HITL propose-decide-react surface | Accepted (2026-05-13), implemented | ? |
| [ADR-0016](ADR-0016-autonomous-mode.md) | Autonomous mode (silence-bias gated paper-trading) | Accepted (2026-05-13), implemented | 2026-05-13 |
| [ADR-0017](ADR-0017-ccxt-provider.md) | CcxtProvider for crypto OHLCV bars | Accepted (2026-05-13), implemented | 2026-05-13 |
| [ADR-0018](ADR-0018-kronos-analyst.md) | KronosAnalyst — third voice, not the oracle | Accepted (2026-05-13), implemented | 2026-05-13 |
| [ADR-0019](ADR-0019-evaluation-module.md) | `evaluation/` module promotion (CV + lookahead + DSR) | Accepted (2026-05-13), implemented | 2026-05-13 |
| [ADR-0020](ADR-0020-backtest-harness.md) | Backtest harness — `hermes_quant.backtest` | Accepted (2026-05-13), implemented | 2026-05-13 (post v0.3.1) |
| [ADR-0021](ADR-0021-pdr-recipe-runtime.md) | Adopt PDR recipes as the Hermes-native runtime contract | accepted | 2026-05-14 |
| [ADR-0022](ADR-0022-hermes-semantic-perception.md) | Hermes semantic perception layer | Accepted | 2026-05-13 |
| [ADR-0023](ADR-0023-deliberative-committee-decision-layer.md) | Deliberative committee decision layer | Accepted | 2026-05-13 |
| [ADR-0024](ADR-0024-autonomous-semantic-perception.md) | Autonomous semantic perception artifacts | Accepted | 2026-05-14 |
| [ADR-0025](ADR-0025-user-editable-recipes-and-perception-status.md) | User-editable recipes and perception status | Accepted | 2026-05-14 |
| [ADR-0026](ADR-0026-retrospective-amendment-loop.md) | Retrospective amendment loop — deterministic postmortems + LLM weekly/monthly retro, proposal-only | Proposed | 2026-05-24 |
| [ADR-0027](ADR-0027-options-aware-risk-gate.md) | Options-aware risk gate — extends ADR-0004 with Greek limits, BPR, and assignment risk | Accepted (2026-05-24), implemented | 2026-05-24 |
| [ADR-0028](ADR-0028-options-data-layer.md) | Options data layer — `OptionContract`, `OptionChain`, provider abstraction, greek completion | Accepted (2026-05-24), implemented | 2026-05-24 |
| [ADR-0029](ADR-0029-multi-leg-paper-reactor.md) | Multi-Leg Paper Reactor | Accepted (2026-05-24), implemented | 2026-05-24 |
| [ADR-0030](ADR-0030-daily-picker-recipe-and-from-reel-pipeline.md) | Daily Picker Recipe + From-Reel Methodology Pipeline | Proposed | 2026-05-24 |
| [ADR-0031](ADR-0031-governance-plane-consolidation.md) | Governance Plane Consolidation | Accepted (2026-05-24), implemented | 2026-05-24 |
| [ADR-0032](ADR-0032-trading-flow-contract.md) | Trading Flow Contract | Proposed | 2026-05-24 |
| [ADR-0033](ADR-0033-evidence-store.md) | Evidence Store + Three-Timestamp Invariant | Accepted (2026-05-24), implemented | 2026-05-24 |
| [ADR-0034](ADR-0034-run-cards.md) | Run Cards | Accepted (2026-05-24), implemented | 2026-05-24 |
| [ADR-0035](ADR-0035-playbook-cadence-daily-weekly-quarterly.md) | Playbook Cadence — Daily / Weekly / Quarterly (NOT Intraday) | Accepted (2026-05-26), implemented | 2026-05-26 |
| [ADR-0036](ADR-0036-multi-timeframe-analyst-fan-out.md) | Multi-Timeframe Analyst Fan-Out | Accepted (2026-05-26), implemented | 2026-05-26 |
| [ADR-0037](ADR-0037-llm-backed-committee-turns.md) | LLM-Backed Committee Turns (Bull/Bear/Risk-Mgmt Debate) | Accepted (2026-05-26), implemented | 2026-05-26 |
| [ADR-0038](ADR-0038-tradingagents-pattern-backfill.md) | TradingAgents Pattern Backfill (P3 / P5 / P6 / P8 / P11 / P12) | proposed | 2026-05-26 |
| [ADR-0039](ADR-0039-robinhood-mcp-reactor.md) | Robinhood Agentic Trading MCP Reactor — additive equity execution rail | Proposed | 2026-05-27 |
| [ADR-0041](ADR-0041-signal-provenance-audit-trail.md) | Signal Provenance & Audit-Trail Observability | Accepted (2026-05-27), implemented | 2026-05-27 |
| [ADR-0042](ADR-0042-persistent-memory-reflection.md) | Persistent Memory & Deferred Reflection Layer | Accepted (2026-05-27), implemented | 2026-05-27 |
| [ADR-0043](ADR-0043-three-way-risk-committee.md) | Three-Way Risk Committee (Aggressive / Conservative / Neutral) | Accepted | 2026-05-27 |
| [ADR-0044](ADR-0044-trader-stage-and-structured-output.md) | Trader Stage & Structured Output (Wave 2) | Accepted | 2026-05-27 |
| [ADR-0045](ADR-0045-backtester-walk-forward-cost-model.md) | Walk-Forward Backtester with Explicit Cost Model | Accepted | 2026-05-27 |
| [ADR-0047](ADR-0047-regime-aware-bma-weights.md) | Regime-Aware BMA Weights (Wave 7) | Accepted | 2026-05-27 |
| [ADR-0048](ADR-0048-hypothesis-registry-and-run-cards.md) | Hypothesis Registry + Run Card Artifacts (Research Autopilot) | Accepted | 2026-05-27 |
| [ADR-0049](ADR-0049-shadow-account-counterfactual.md) | Shadow Account Counterfactual Backtest | Accepted | 2026-05-27 |
| [ADR-0050](ADR-0050-alpha-zoo-with-ast-purity-and-lookahead-gate.md) | Alpha Zoo with AST Purity Gate and Lookahead Sentinel | Accepted | 2026-05-27 |
| [ADR-0051](ADR-0051-lookahead-sentinel-v0.2.md) | Lookahead Sentinel v0.2: Closing MoA Review False-Negatives | Accepted | 2026-05-27 |
| [ADR-0052](ADR-0052-promotion-orchestrator-and-cron.md) | Promotion Orchestrator and Cron | Accepted | 2025-05-27 |
| [ADR-0053](ADR-0053-daily-brief-regime-and-research-surfacing.md) | Daily Brief: Regime + Research + Shadow Surfacing | Accepted | 2026-05-27 |
| [ADR-0054](ADR-0054-llm-caller-foundation-and-trader-v02.md) | LLM-Caller Foundation & TraderNode v0.2 | Accepted | 2026-05-27 |
| [ADR-0055](ADR-0055-factor-oracle-and-production-readiness-tiers.md) | FactorOracle and Production-Readiness Tiers | Accepted | 2026-05-27 |
| [ADR-0056](ADR-0056-risk-committee-v02-llm-wiring.md) | RiskCommittee v0.2 — LLM Wiring | Accepted | 2026-05-27 |
| [ADR-0057](ADR-0057-reflector-v02-llm-wiring.md) | Reflector v0.2 — LLM-Wired Structured Reflection | Accepted | 2026-05-27 |
| [ADR-0058](ADR-0058-hmm-regime-classifier-v0.2.md) | HMM Regime Classifier v0.2 | Accepted | 2026-05-27 |
| [ADR-0059](ADR-0059-unified-status-cli.md) | Unified `quant status` CLI for single-pane observability across event stores | Accepted | 2026-05-27 |
| [ADR-0060](ADR-0060-fallback-probe.md) | Fallback Probe for Silence-by-Default Verification | Accepted | 2026-05-27 |
| [ADR-0061](ADR-0061-daily-report.md) | Daily Markdown Report | Accepted | 2026-05-27 |
| [ADR-0062](ADR-0062-rollout-playbook.md) | Production Rollout Playbook for v0.2 LLM Surfaces | Accepted | 2026-05-27 |
| [ADR-0063](ADR-0063-regime-in-marketcontext-extras.md) | Regime in MarketContext.extras | Accepted (2026-05-27) | ? |
| [ADR-0064](ADR-0064-fundamentals-analyst.md) | FundamentalsAnalyst Integration | Accepted (2026-05-27) | ? |
| [ADR-0065](ADR-0065-bull-bear-adversarial-debate.md) | Bull/Bear Adversarial Debate Stage (with ResearchPlan + 5-tier PortfolioRating) | Accepted (2026-05-27) | ? |
| [ADR-0066](ADR-0066-research-debate-production-wiring.md) | Production Wiring for ResearchDebateStage (v0.6.2) | Accepted (2026-05-28), implemented | 2026-05-28 |
| [ADR-0067](ADR-0067-robinhood-mcp-usage-research-amendment.md) | Robinhood Agentic Trading MCP — usage-research amendment to ADR-0039 | Proposed | 2026-05-28 |
| [ADR-0068](ADR-0068-decision-time-vs-bar-time-honesty.md) | Decision-time vs bar-time honesty — `asof_decision` semantics | Accepted (2026-05-28), implemented | 2026-05-28 |
| [ADR-0069](ADR-0069-still-forming-bar-discipline.md) | Still-forming-bar discipline for daily timeframe mid-session | Accepted (2026-05-28), implemented | 2026-05-28 |
| [ADR-0070](ADR-0070-paper-execution-fidelity.md) | Paper-execution fidelity — slippage, queue delay, and fill realism | Accepted (2026-05-28), implemented | 2026-05-28 |
| [ADR-0071](ADR-0071-portfolio-aware-dynamic-kelly.md) | Portfolio-aware dynamic Kelly sizing and exposure caps | Accepted (2026-05-28), implemented | 2026-05-28 |
| [ADR-0072](ADR-0072-advisor-intraday-open-guard.md) | Advisor-layer intraday open-guard (cross-run per-symbol-per-day dedup) | Accepted | 2026-05-29 |
| [ADR-0073](ADR-0073-event-catalyst-awareness.md) | Event/catalyst awareness — universe onboarding, semantic analyst activation, intraday cadence | Accepted (2026-05-29), implemented | 2026-05-29 |
| [ADR-0074](ADR-0074-catalyst-sense-semantic-fusion.md) | Catalyst Sense — semantic-numerical fusion via parallel catalyst detection | Accepted (2026-05-29), implemented | 2026-05-29 |
| [ADR-0075](ADR-0075-catalyst-driven-universe-onboarding.md) | Catalyst-driven universe onboarding | Proposed | 2026-05-29 |
| [ADR-0076](ADR-0076-social-arbitrage-integration.md) | Social-arbitrage integration — consumer-trend entity class, sized fusion, and a profitability-verification loop | Accepted | 2026-05-30 |
| [ADR-0077](ADR-0077-pretrade-admissibility-shortability.md) | Pre-trade admissibility engine + ShortabilityOracle (paper→live fidelity foundation) | Accepted (2026-05-30), implemented | 2026-05-30 |
| [ADR-0078](ADR-0078-order-lifecycle-fills-idempotency.md) | Order-lifecycle state machine + fill realism + exactly-once idempotency | Proposed | 2026-05-30 |
| [ADR-0079](ADR-0079-perception-decision-reaction-architecture.md) | Unified Perception → Decision → Reaction architecture + signal-source unification | Accepted (2026-05-30), implemented | 2026-05-30 |
| [ADR-0080](ADR-0080-self-evolution-framework.md) | Self-evolution framework — the advisory plane, multi-rate retro tiers, and the held-out eval-gate contract | Proposed | 2026-05-30 |
| [ADR-0081](ADR-0081-belief-store-and-distillation-tiers.md) | Bounded decaying belief store with weekly/monthly distillation tiers (CVRF + FINMEM) | Accepted (2026-05-30), implemented | 2026-05-30 |
| [ADR-0082](ADR-0082-deterministic-structure-selection-layer.md) | Deterministic structure-selection layer + registry-open plays | Accepted (2026-05-31), implemented | 2026-05-31 |
| [ADR-0083](ADR-0083-defer-intraday-build-horizon-neutral-foundations.md) | Defer long-horizon intraday; build the horizon-neutral foundations first | Accepted (2026-05-31), implemented | 2026-05-31 |
| [ADR-0084](ADR-0084-scheduled-event-calendar-and-pre-event-guard.md) | Scheduled-event calendar as asof-honest perception + a default-OFF pre-event REJECT/abstain guard | Accepted (2026-05-31), implemented | 2026-05-31 |
| [ADR-0085](ADR-0085-ledger-authority-and-state-derivation.md) | executions.jsonl is the authoritative event log; state.db is a derived projection | Accepted (2026-06-01), implemented | 2026-06-01 |
| [ADR-0086](ADR-0086-ledger-share-quantity-dollar-accounting.md) | Migrate the paper ledger to share-quantity + dollar accounting with mark-to-market equity | proposed | 2026-06-02 |
| [ADR-0087](ADR-0087-centralize-portfolio-cap-at-reactor-seam.md) | Centralize the portfolio-cap clip at the PaperReactor.execute() seam | proposed | 2026-06-02 |
