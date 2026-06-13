# Scaffold — pdr-core-host-adapter-arch-e99014

> PRIVATE planning doc. MUST NOT appear in the final report.

## User Prompt (VERBATIM — gospel)

What is the best-possible architecture for a money-software trading system factored as ONE generalized, host-agnostic deterministic CORE plus N deeply-integrated agent-host PLUGINS (adapters)?

Concrete context this must serve: hermes-quant (a Hermes-agent plugin: Python analyst classes, cron/daemon firing, MCP tools) and cowork-quant (a Claude Cowork plugin: in-session LLM subagent committee, scheduled /watch turns) are PARALLEL plugins for two different agent hosts, same charter (Perception->Decision->Reaction; deterministic risk gate as FINAL authority; silence-by-default; no-look-ahead; new capability default-OFF behind flags + eval-gated). The proposal (ADR-0092) is to extract a shared core that owns ALL money-adjacent state + arithmetic + the gate + the typed contracts, with the charter's uniform AnalystView contract as the host-blind, modality-blind seam.

Sub-questions:
1. CORE/ADAPTER BOUNDARY — contract-seam design for heterogeneous producers; hexagonal/ports-and-adapters; strangler-fig; boundary-leak failure modes; how nautilus_trader / lean(QuantConnect) / vn.py / hummingbot factor venue/host adapters from a shared core and what they got wrong first.
2. MONEY-STATE MODEL — event-sourced append-only ledger w/ single fold vs materialized state; hash-chained ledgers; dual-write/cross-store atomicity; single-writer invariants; reconstruct-vs-store; auditable replayable position+P&L ledger read by multiple consumers (projection, settlement FIFO) without divergence.
3. DETERMINISTIC-GATE-AS-FINAL-AUTHORITY — LLM as evidence-only ("silence but never amplify/override"); structural (not prompt) enforcement of polarity; preventing committee/LLM runaway; is monotonic-forward authority a recognized pattern, where does it break.
4. CONTRACT EVOLUTION — schema versioning across independently-deployed plugins on a versioned core; carry-forward/interpretation-vs-mutation for append-only logs; backward/forward compat for two plugins on different cadences; monorepo path-dep vs separately-published-and-pinned core.
5. MULTI-AGENT TRADING SOTA — TauricResearch/TradingAgents, virattt/ai-hedge-fund, HKUDS/Vibe-Trading + 2025-2026 successors: perception->decision->reaction split, signal unification, analysis/deterministic-risk separation; what has NOT worked (LLM as final execution authority, free-text position sizing, RL-on-portfolio-value).

Deliverable: decisive evidence-cited architecture recommendation — CONFIRM shared-core+two-shells OR concrete improvements/alternatives; name the contract-seam, state-model, gate-enforcement, contract-evolution choices; flag where ADR-0092 is right / under-specified / contradicted by real systems.

## Run config

- vault_tag: pdr-core-host-adapter-arch-e99014
- query_file_path: research/query-pdr-core-host-adapter-arch-e99014.md
- modality: compare (proportionate per-option depth + a committed recommendation; CONFIRM-or-improve ADR-0092)
- wrapper requirements: none (no prompt.txt, no wrapper_contract.json). Caller (deep-work-loop Research stage) wants final report path + structured summary. Standard final report at research/notes/final_report_<vault_tag>.md.

## Modality classification rationale

The deliverable is a committed architecture recommendation that weighs ADR-0092's Option D against alternatives and either confirms it or names concrete improvements. That is fundamentally a COMPARE modality (proportionate depth across options/choices + a decisive recommendation), with a strong synthesize/argumentative streak (it must defend a thesis with evidence chains from real systems). The 5 sub-questions are each a named decision axis. Treat as compare with argumentative density.

## Tier rationale

**Classified: FULL tier, argumentative response_format, wikilink citation_style.**
This is a research-grade architecture question: 5 explicit decision axes, each demanding evidence from named real systems and papers (2024-2026), adversarial flagging of where ADR-0092 is right/under-specified/contradicted, and a decisive defended recommendation. It is a contested-tradeoff synthesis (event-sourced vs materialized; monorepo vs pinned; LLM-authority anti-patterns), not a bounded lookup — the textbook "full + argumentative" case. Default-OFF money-software stakes raise the cost of a shallow answer. citation_style=wikilink: personal-vault deliverable consumed by the deep-work-loop Research stage, no public/benchmark wrapper contract present.

## Local grounding (this run must serve)

- docs/adr/ADR-0092-shared-pdr-core-two-integration-shells.md — the proposal under test (Option D: shared host-agnostic pdr-core + two integration shells; AnalystView as host-blind+modality-blind seam; core owns all money-state+arithmetic+gate+contracts).
- Cited ADRs: ADR-0002 (AnalystView peer-view contract), ADR-0003 (numeric calibration-weighted BMA aggregator), ADR-0004 (deterministic risk gate, silence-by-default, FINAL authority), ADR-0079 (unified PDR), ADR-0085/0086/0091 (ledger reconcile / share-quantity-dollar accounting / reactors-emit-traded-delta — the live dual-ledger defect).
- ADR-0091 open 3-way fork on ledger fold semantics (A projection-only [rejected]; B producer-emits-delta [P0s found]; C version-discriminated carry-forward fold [endorsed]). ADR-0092 consumes whatever 0091 resolves to.
- Confirmed live defects ADR-0092 targets: dual-ledger divergence; 2-of-4 fire-paths bypass the cap seam; test-fixture-to-live-state leak (fictional +$167K P&L).
