---
vault_tag: pdr-core-host-adapter-arch
created: 2026-06-13T19:58:20Z
source: user-prompt
---

What is the best-possible architecture for a money-software trading system factored as ONE generalized, host-agnostic deterministic CORE plus N deeply-integrated agent-host PLUGINS (adapters)?

Concrete context this must serve: hermes-quant (a Hermes-agent plugin: Python analyst classes, cron/daemon firing, MCP tools) and cowork-quant (a Claude Cowork plugin: in-session LLM subagent committee, scheduled /watch turns) are PARALLEL plugins for two different agent hosts, same charter (Perception->Decision->Reaction; deterministic risk gate as FINAL authority; silence-by-default; no-look-ahead; new capability default-OFF behind flags + eval-gated). The proposal (ADR-0092) is to extract a shared core that owns ALL money-adjacent state + arithmetic + the gate + the typed contracts, with the charter's uniform AnalystView contract as the host-blind, modality-blind seam.

Answer these specific sub-questions with evidence from real systems, papers, and production practice (2024-2026 where possible):
1. CORE/ADAPTER BOUNDARY: For systems where heterogeneous producers (here: LLM subagents vs Python classes vs foundation-model /predict) feed one decision core, what is the proven contract-seam design? Hexagonal/ports-and-adapters, the strangler-fig migration, and the actual failure modes when the boundary leaks. How do mature multi-broker / multi-venue trading systems (e.g. nautilus_trader, lean/QuantConnect, vn.py, hummingbot) factor venue/host adapters from a shared core, and what did they get wrong first?
2. MONEY-STATE MODEL: event-sourced append-only ledger with a single fold vs materialized-state stores. Hash-chained ledgers, the dual-write/cross-store atomicity problem, single-writer invariants, and reconstruct-vs-store tradeoffs in financial systems. What is current best practice for an auditable, replayable position+P&L ledger that multiple consumers (projection, settlement FIFO) read without divergence?
3. DETERMINISTIC-GATE-AS-FINAL-AUTHORITY with LLMs upstream as evidence-only: prior art for "the model can silence but never amplify/override," structural (not prompt) enforcement of that polarity, and how agentic trading systems prevent committee/LLM runaway. Is monotonic-forward authority a recognized pattern, and where does it break?
4. CONTRACT EVOLUTION across independently-deployed plugins sharing a versioned core: schema versioning, the carry-forward/interpretation-vs-mutation choice for append-only logs, and backward/forward compatibility for two plugins on different release cadences. Monorepo-with-path-dep vs separately-published-and-pinned for the shared core.
5. MULTI-AGENT TRADING ARCHITECTURE SOTA: what do TauricResearch/TradingAgents, virattt/ai-hedge-fund, HKUDS/Vibe-Trading, and 2025-2026 successors actually do for the perception->decision->reaction split, signal unification, and the analysis/deterministic-risk separation? What has been shown NOT to work (LLM as final execution authority, free-text position sizing, RL-on-portfolio-value)?

Deliverable: a decisive, evidence-cited architecture recommendation that either CONFIRMS the shared-core+two-shells design or proposes concrete improvements/alternatives, with the specific contract-seam, state-model, gate-enforcement, and contract-evolution choices named. Flag where ADR-0092 is right, where it is under-specified, and where real systems suggest a different choice.
