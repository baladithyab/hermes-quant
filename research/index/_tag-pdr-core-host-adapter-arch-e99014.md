---
title: "Tag: pdr-core-host-adapter-arch-e99014"
id: "_tag-pdr-core-host-adapter-arch-e99014"
type: index
created: 2026-06-17T15:42:47.138940+00:00
updated: 2026-06-17T15:42:47.138940+00:00
---

# Tag: pdr-core-host-adapter-arch-e99014

**18** notes.

- [[deepfund-time-travel-is-cheating-leakage-free-live-eval]] — DeepFund Time Travel is Cheating leakage-free live eval — DeepFund 2025: leakage-free LIVE eval; DeepSeek-V3 AND Claude-3.7-Sonnet incur NET trading LOSSES; backtest = time travel cheating `review`
- [[dual-write-problem-and-event-sourcing-as-the-cure]] — Dual-write problem and event sourcing as the cure — Confluent 2024: dual-write = hermes bug class; cure = single append-only log + derive consumers; banking is naturally event-sourced `review`
- [[event-schema-versioning-upcasting-vs-copy-replace-never-mutate]] — Event schema versioning upcasting vs copy-replace never mutate — ES versioning: never mutate events; upcast at READ time (=version-discriminated carry-forward fold = ADR-0091-C); copy-replace only for clean log; keep streams short `review`
- [[hitl-approvalrequest-handler-approvaldecision-fail-closed]] — HITL ApprovalRequest handler ApprovalDecision fail-closed — AgentOS HITL: uniform ApprovalRequest->handler->ApprovalDecision; non-2xx=reject (fail-closed); auto-vs-human is a pluggable handler = SHELL decision (matches ADR-0092) `review`
- [[hexagonal-driven-ports-leaking-infrastructure]] — Hexagonal driven ports leaking infrastructure — Leaky port exposes HOW (SQL/HTTP/host types) not WHAT domain needs -> domain coupled to infra; the AnalystView leak test `review`
- [[profit-mirage-llm-financial-agent-information-leakage]] — Profit Mirage LLM financial-agent information leakage — Profit Mirage 2025: GPT-4o memorizes 85%+ of historical market QA; agents lose 55.68% Sharpe OOS; remedy=LLM as strategy generator not decision-maker `review`
- [[quantconnect-lean-interface-driven-brokerage-adapters]] — QuantConnect Lean interface-driven brokerage adapters — Lean: IBrokerage contract + canonical Order + IBrokerageModel per-venue rules + BrokerageTransactionHandler intermediary `review`
- [[rl-on-portfolio-value-fragility-offline-policy-not-trustworthy]] — RL-on-portfolio-value fragility offline policy not trustworthy — MetaTrader+Velay: RL portfolio offline policies overfit in-sample max, fail OOS due to non-stationarity; validates charter rejecting RL-on-portfolio-value `review`
- [[safe-rl-shielding-formal-monotonic-restriction-prior-art]] — Safe-RL shielding formal monotonic-restriction prior art — Shielding: runtime gate can only RESTRICT never expand action set; pre/post-shield; separation of concerns (safety state != agent state); proven no optimality cost; breaks when no safe action exists `review`
- [[shared-libraries-become-shared-shackles-adversarial]] — Shared libraries become shared shackles adversarial — Adversarial: shared libs couple/throttle teams; BUT concedes SECURITY protocols (stable/catastrophic/thin/coupling-is-a-feature) as the exception = exactly the money-gate profile `review`
- [[strangler-fig-gradual-replacement-transitional-architecture-seams]] — Strangler Fig gradual replacement transitional architecture seams — Strangler Fig: gradual replacement beats big-bang rewrite; insert SEAMS (ADR-0092 already has AnalystView); transitional architecture is worth it `review`
- [[tigerbeetle-debit-credit-immutability-in-ledger-invariants]] — TigerBeetle debit-credit immutability in-ledger invariants — TigerBeetle: double-entry, append-only immutable, reversals-as-new-entries, invariants enforced IN ledger, don't roll your own (Uber/Airbnb/Stripe) `review`
- [[tradingagents-llm-as-final-authority-anti-pattern]] — TradingAgents LLM-as-final-authority anti-pattern — TradingAgents: LLM Portfolio Manager is FINAL authority, risk team only debates - the charter's forbidden anti-pattern `review`
- [[vibe-trading-liveorderguardtool-fail-closed-deterministic-gate]] — Vibe-Trading LiveOrderGuardTool fail-closed deterministic gate — Vibe-Trading: fail-closed LiveOrderGuardTool hard-caps notional/exposure/leverage downstream of LLM; versioned Mandate; kill switch `review`
- [[ai-hedge-fund-deterministic-risk-gate-constrains-llm]] — ai-hedge-fund deterministic risk gate constrains LLM — ai-hedge-fund: DETERMINISTIC risk manager computes position limits; LLM picks action<=max within computed envelope `review`
- [[hummingbot-connector-standardization-what-early-connectors-got-wrong]] — hummingbot connector standardization what early connectors got wrong — hummingbot: early per-connector duplicated order-tracking/throttling -> divergence; fix=pull state machinery into shared base (mirrors hermes dual-ledger fix) `review`
- [[nautilus_trader-ports-and-adapters-venue-integration]] — nautilus_trader ports-and-adapters venue integration — nautilus_trader: ports-and-adapters, normalized domain model, RiskEngine-before-ExecutionEngine chokepoint, fixed-point money `review`
- [[vnpy-basegateway-eventengine-canonical-object-seam]] — vn.py BaseGateway EventEngine canonical-object seam — vn.py: BaseGateway + canonical BaseData + pub/sub; risk is a SUBSCRIBER not an inline chokepoint (cautionary contrast) `review`

---
*Auto-generated by hyperresearch. Do not edit manually.*
