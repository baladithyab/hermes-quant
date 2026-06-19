# Orchestrator notes — pdr-core-host-adapter-arch-e99014

## Emerging thesis (after width sweep, pre-depth)
The evidence is converging hard toward CONFIRM-with-sharpening of ADR-0092 Option D, not challenge. Key signals:

### Sub-Q1 (core/adapter seam) — CONFIRM
- All 4 mature trading systems (nautilus, lean, vn.py, hummingbot) factor venue adapters from a shared core via the SAME shape: an abstract adapter contract + a CANONICAL normalized domain model (order/trade/position/account) that adapters translate into. This IS ports-and-adapters. ADR-0092's AnalystView-as-seam is the same move, one layer up (perception not execution).
- nautilus: RiskEngine BEFORE ExecutionEngine on every path = gate-as-chokepoint prior art. fixed-point money type.
- lean: canonical Order + IBrokerageModel.CanSubmitOrder (per-venue rules live in adapter) + BrokerageTransactionHandler intermediary. Clean "what the adapter owns vs core."
- hummingbot: THE cautionary tale — early per-connector duplicated order-tracking/throttling -> divergence + lost orders; fix = pull state machinery UP into shared base (ConnectorBase/ExchangePyBase/ClientOrderTracker). This is LITERALLY hermes' dual-ledger/cap-bypass disease and ADR-0092's prescribed cure.
- vn.py: CAUTION — risk is an EVENT SUBSCRIBER, not an inline chokepoint. Weaker. Validates ADR-0092's "ONE execute() chokepoint, one gate node on every path" over a pub/sub risk listener.
- Leak failure mode (optivem 2026): "driven ports leaking infrastructure" — port at wrong abstraction level couples domain to infra. ACL pattern (Azure/MS) is the named remedy. -> ADR-0092 must specify AnalystView at the DOMAIN abstraction (host-blind), not leak host/MCP/subagent concepts.

### Sub-Q2 (money-state) — CONFIRM single-fold append-only, with named upgrades
- TigerBeetle: purpose-built financial ledger; double-entry debit/credit schema; single-writer; safety beyond general DB (detects/repairs disk corruption + misdirected writes). Strong prior art for "money deserves a purpose-built single-writer ledger."
- Dual-write problem (Confluent/AWS/Auth0): the canonical name for hermes' exact bug class. Remedy = single source of truth + transactional outbox / CDC, NOT writing to two stores. ADR-0092's single Ledger.portfolio() fold = the "one store" answer; the dual-ledger bug IS the dual-write problem.
- Event-sourcing single fold + reconstruct vs materialized: ADR-0092 reconstruct-from-log is mainstream; materialized read-models are an optimization, must be rebuildable from the log (CQRS). The ADR-0091 lesson (don't make a producer read derived state to write the log) = avoid write-time coupling into immutable log = correct.

### Sub-Q3 (gate polarity) — CONFIRM, and it IS a recognized pattern
- Vibe-Trading LiveOrderGuardTool: fail-closed, versioned Mandate, HardCaps (notional/exposure/leverage/trade-count), kill switch, pause-for-reauth+BreachEvent. EXACT analog of ADR-0092 charter. 2025 system. CONFIRM.
- ai-hedge-fund: deterministic risk manager computes envelope; LLM picks action<=max. Polarity matches. BUT enforcement partly prompt-level ("<=max") -> ADR-0092 must clamp STRUCTURALLY (0.0 multiplier, LLM never names the number).
- Safe-RL SHIELDING literature (Alshiekh shielding AAAI; Pure-Past Action Masking AAAI; CACM Shields for Safe RL; MLR realizable shields; NeurIPS 2024 model-predictive shielding): THIS is the recognized formal pattern. A shield/action-mask can only RESTRICT the action set, never expand it; provably the agent can still learn any safe-optimal policy. "Separation of concerns: safety features need not be the safety constraints' = the agent's." DIRECT formal grounding for "model can silence but never amplify" = monotonic restriction. Where it breaks: shield must be correct+complete (a buggy/incomplete shield gives false safety); shield over wrong state space; if the constrained set is empty (over-restriction) the system silences everything (silence-by-default handles this gracefully). 
- TradingAgents: ANTI-PATTERN exemplar — LLM Portfolio Manager IS final authority; risk team only debates. Forbidden by charter.

### Sub-Q4 (contract evolution) — UNDER-SPECIFIED in ADR-0092, real systems have answers
- Greg Young "Versioning in an Event Sourced System": weak schema, type-based versioning, upcasting, copy-and-replace, NEVER mutate an event. Internal vs external models. Directly answers ADR-0091's carry-forward/interpretation-vs-mutation. The version-discriminated carry-forward FOLD (ADR-0091 option C) = upcaster-at-read = canonical ES practice. Confirms 0091-C.
- ScienceDirect empirical ES schema-evolution study: real-world ES schema change lessons.
- Monorepo vs published-pinned: "shared libraries become shared shackles" (steven stuart 2026) + Block "polyrepo fragmentation to monorepo leverage" + dtolnay semver-trick + nx multi-cadence discussion. Tension: monorepo = atomic cross-cut change (one commit updates both shells) BUT couples release; pinned package = independent cadence BUT coordinated 2-shell migration on every breaking change (ADR-0092's stated NEGATIVE). For TWO plugins + one core, single author -> monorepo-with-path-dep + internal semver tag is likely the right call (atomic contract changes; the ADR's feared coordination cost is a polyrepo cost). ADR-0092 leaves this OPEN -> name it.

### Sub-Q5 (multi-agent SOTA + anti-patterns) — rich negative-result corpus
- Profit Mirage (arXiv 2510.07920, 2025): LLM financial agents show "profit mirage" - backtest returns evaporate after knowledge-window ends due to INFORMATION LEAKAGE. FinLake-Bench. -> validates no-look-ahead rail (asof=publication time) as load-bearing, not optional.
- The Alpha Illusion (arXiv 2605): reported alpha from LLM trading agents should NOT be treated as deployment evidence.
- Look-Ahead-Bench (2601.13770), Time Travel is Cheating / DeepFund (2505.11065), StockBench (2510.02209), When Reasoning Fails (2511.08608), Can LLM strategies outperform long run (2505.07078): a whole 2025-26 literature documenting LLM-trading failure modes -> evidence the charter's eval-gating + no-lookahead are correct.
- DRL portfolio: "Your Offline Policy is Not Trustworthy" (2505.12759), DRL robustness benchmark (2306.10950) -> RL-on-portfolio-value is fragile/non-robust OOS. Validates charter's rejection of RL-on-portfolio-value.

## Open depth questions for loci
1. Where EXACTLY does AnalystView sit and is one seam enough (perception) or do we ALSO need a canonical Proposal/Fill contract on the reaction side (lean has BOTH Order and execution events)? ADR-0092 mentions Proposal + Fill - good, but is it specified?
2. Structural vs prompt gate enforcement — the ai-hedge-fund residual leak. How to make "0.0 multiplier never amplify" provably structural (multiply-only, LLM emits a [0,1] confidence not a size).
3. Monorepo vs pinned — the one genuinely OPEN ADR-0092 question. Decide it.
4. Single fold replayability vs materialized projection performance — is reconstruct-on-every-read viable at hermes scale, or is a rebuildable materialized projection needed (and does that re-open divergence)?
5. Does the two-host asymmetry (cowork forbids execution; hermes wants gated-live) stress the shared gate? The gate must support BOTH "advise-only shell" and "auto-execute shell" without forking — Vibe-Trading's pause-for-reauth vs auto shows both modes off one gate.

---
## ANALYTICAL LAYER (steps 3-9 done single-threaded — Task tool unavailable to this subagent)

### Contradiction graph / fight clusters
FIGHT-1 (sub-Q3/Q5, the central one): WHO is the final authority?
  - TradingAgents: LLM Portfolio Manager IS final authority (debate -> structured rating). Free-text fallback in loop.
  - Vibe-Trading + ai-hedge-fund + charter: DETERMINISTIC gate is final; LLM proposes/picks-within-envelope only.
  - Resolution: the empirical base rate (Profit Mirage 55.68% Sharpe decay; DeepFund frontier LLMs lose money live) decisively favors deterministic-gate-final. TradingAgents is the documented anti-pattern. CONFIRM charter.

FIGHT-2 (sub-Q4): shared core = leverage or shackle?
  - shackles(Stuart): shared internal libs couple teams, throttle tempo, almost never worth it.
  - ADR-0092 + hummingbot history + TigerBeetle "don't roll your own": centralize money-state or you re-derive+re-break it.
  - Resolution: NOT a true contradiction once you apply Stuart's OWN security-protocol exception (stable/catastrophic/thin/coupling-is-the-goal) — the money-core is exactly that exception. CONFIRM extraction, but heed: keep core MINIMAL; it is NOT a utility/SDK lib.

FIGHT-3 (sub-Q2): reconstruct-on-read vs materialized state.
  - Pure single-fold reconstruct = always-correct, but O(log) per read.
  - Materialized projection = fast, but re-opens divergence risk (the exact hermes bug).
  - Resolution (CQRS): ONE fold defines truth; materialized projections allowed ONLY as a cache that is provably rebuildable from the log and is byte-identical to the fold (a property test). Never a second writer. The fold is the spec; the projection is a derived index.

FIGHT-4 (sub-Q4, ADR-0091): producer-emits-delta (B) vs version-discriminated carry-forward fold (C).
  - ES practice (Dudycz/Young): never mutate; upcast at READ time. Write-side must not depend on derived state.
  - Resolution: C = read-time upcasting = canonical ES. B = write-time coupling into immutable log = the ES anti-pattern. Confirms ADR-0091's own durable lesson and points to C.

### Consensus claims (3+ independent agreements)
- CONSENSUS-A: heterogeneous producers feed a core via a CANONICAL normalized domain model + an abstract adapter contract (nautilus, lean, vn.py, hummingbot ALL do this). Ports-and-adapters is the proven seam.
- CONSENSUS-B: money state = append-only immutable log, reversals-as-new-entries, never mutate (TigerBeetle, Confluent-ES, Dudycz). Banking is "naturally event-sourced."
- CONSENSUS-C: the safety gate can only RESTRICT, never amplify; this is a recognized formal pattern (shielding / action-masking), with proven no-optimality-cost and a clean separation-of-concerns (safety state != agent state).
- CONSENSUS-D: LLM-as-final-authority + backtest-validated LLM alpha is a documented failure mode (Profit Mirage, DeepFund, MetaTrader). Demote LLM to evidence/strategy-generator.

### Loci (depth questions) — committed positions
LOCUS-1 (seam sufficiency): Is AnalystView (perception seam) ENOUGH, or does the core also need canonical Proposal + Fill contracts on the reaction side? COMMITTED: need BOTH. lean has Order(in) + execution-events(out); nautilus has order-commands + execution-reports. ADR-0092 DOES mention "shells produce AnalystView[] and feed Fills back; core returns a Proposal" — so it implicitly has 3 contracts (AnalystView in, Proposal out, Fill back). UNDER-SPECIFIED: it only names AnalystView as "the seam." Make the triad explicit: the seam is THREE typed contracts (AnalystView, Proposal, Fill), all host-blind. The leak test (optivem) applies to all three.

LOCUS-2 (structural vs prompt gate): COMMITTED: enforce structurally, not by prompt. ai-hedge-fund's residual leak ("pick qty <= max" in a prompt) is the trap. Correct design (shielding pre-shield + Vibe hardcaps): the core COMPUTES the sized envelope; the LLM/committee emits only a confidence in [0,1] (or a silence), and the core MULTIPLIES. The LLM never names a size/quantity. A 0.0 multiplier = silence; values in (0,1] can only scale DOWN a deterministically-computed max. Amplification is structurally impossible because the LLM's output is a bounded multiplier on a number it cannot see or set. This is monotonic-forward authority realized as: size = deterministic_max * clamp(committee_multiplier, 0, 1).

LOCUS-3 (monorepo vs pinned): COMMITTED: monorepo-with-path-dep + internal version tag, NOT separately-published-pinned-package across repos. Rationale: single operator/author (Stuart's tempo/coordination costs are a MULTI-TEAM cost — largely absent here); atomic cross-cut change (one commit updates core + both shells = the monorepo superpower, exactly what you want when the contract changes); the ADR's feared "coordinated two-shell migration" is a POLYREPO cost you avoid. Keep the core a path-dep package so each shell still pins a version for reproducibility/rollback, but co-locate so breaking changes are atomic. Revisit only if a second author/org takes a shell.

LOCUS-4 (reconstruct viability): COMMITTED: single fold is the truth; add a rebuildable materialized projection ONLY behind a property test asserting projection == fold (default-OFF until the test is green). Don't pre-optimize; hermes' bug was divergence, not latency.

LOCUS-5 (two-host gate asymmetry): COMMITTED: ONE gate, two host policies via a pluggable decision handler (AgentOS HITL ApprovalRequest->handler->ApprovalDecision; Vibe pause-for-reauth). Core emits authorized sized Proposal + ApprovalRequest artifact; cowork binds route-to-human/never-auto; hermes binds gated-auto-execute. auto-vs-human is a SHELL decision the core never sees (ADR-0092 already says this — CONFIRM, and name the contract).

### Source tensions (expert disagreements to surface in the report)
- TENSION-1: Stuart ("shared libs are shackles, almost never right") vs ADR-0092/hummingbot/TigerBeetle ("centralize money-state"). Engage explicitly; resolve via Stuart's security-protocol carve-out.
- TENSION-2: TradingAgents (LLM final authority, respected/popular) vs the charter + DeepFund/Profit-Mirage. Engage: popularity != correctness; the empirical record is against LLM-final-authority.
- TENSION-3: Pure-fold purists vs performance pragmatists on materialized projections. Engage via CQRS rebuildable-cache discipline.
- TENSION-4: vn.py risk-as-event-subscriber vs nautilus risk-as-inline-pre-trade-chokepoint. Engage: the subscriber model is exactly how a cap gets bypassed (async, can miss a path); ADR-0092's single synchronous execute() chokepoint is the correct choice. This maps to hermes' real 2-of-4-paths-bypass bug.

### VERDICT (forming): CONFIRM Option D, with 5 named sharpenings
1. The seam is a TRIAD of host-blind contracts (AnalystView, Proposal, Fill) + an explicit "no host/infra types" fitness test (optivem leak test).
2. Gate polarity must be STRUCTURAL: size = det_max * clamp(committee_multiplier,0,1); LLM emits a bounded multiplier, never a size. (= a pre-shield.)
3. Money-state = append-only immutable single-fold; consider DOUBLE-ENTRY (cash + position accounts) so "money never appears from nowhere" is structural; materialized projections only as rebuildable, property-tested caches.
4. Contract evolution = read-time upcasting (ADR-0091 option C), never mutate the log; monorepo-with-path-dep (not published-pinned) given single-author/two-shell.
5. Gate is a single SYNCHRONOUS chokepoint on every path (reject vn.py-style async risk-subscriber); fail-closed; auto-vs-human is a pluggable shell handler.
Under-specified in ADR-0092: the contract triad (only names AnalystView); the structural multiply-only enforcement; monorepo-vs-pinned (left open); double-entry option; the projection-rebuild property test. ADR-0092 is RIGHT on: Option D over A/B/C; AnalystView as host-blind seam; core owns state+gate+contracts; live-exec stays a shell concern; consuming ADR-0091's fold; strangler over rewrite.
