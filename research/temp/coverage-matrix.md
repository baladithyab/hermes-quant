## Coverage Matrix — query phrase → atomic item mapping

| Query phrase (verbatim) | Mapped atomic item(s) | Scope check | Gap? |
|---|---|---|---|
| "host-agnostic deterministic CORE plus N deeply-integrated agent-host PLUGINS (adapters)" | Top sub-question; entities: ADR-0092 proposal, AnalystView contract | OK — full scope (core/adapter factoring is the spine of the whole report) | No |
| "money-software trading system" | scope_condition: money-software, correctness dominant | OK | No |
| "hermes-quant (Hermes-agent plugin: Python analyst classes, cron/daemon, MCP tools)" | Entity hermes-quant w/ host + integration fields | OK | No |
| "cowork-quant (Claude Cowork plugin: in-session LLM subagent committee, scheduled /watch turns)" | Entity cowork-quant w/ host + integration fields | OK | No |
| "PARALLEL plugins for two different agent hosts, same charter" | scope_condition: two parallel plugins, neither abandoned | OK | No |
| "Perception->Decision->Reaction" | Sub-Q5 PDR split; required heading 5; ADR-0079 grounding | OK | No |
| "deterministic risk gate as FINAL authority; silence-by-default" | Sub-Q3; required heading 3; scope_condition rails | OK | No |
| "no-look-ahead; new capability default-OFF behind flags + eval-gated" | scope_condition rails | OK | No |
| "AnalystView contract as the host-blind, modality-blind seam" | Entity AnalystView; Sub-Q1 contract-seam | OK — both host-blind AND modality-blind dimensions covered | No |
| "owns ALL money-adjacent state + arithmetic + the gate + the typed contracts" | Sub-Q2 (state), Sub-Q3 (gate), Sub-Q4 (typed contracts) | OK | No |
| "hexagonal/ports-and-adapters, the strangler-fig migration" | Entities: hexagonal pattern, strangler-fig; Sub-Q1 | OK | No |
| "actual failure modes when the boundary leaks" | Sub-Q1 boundary-leak failure modes | OK | No |
| "nautilus_trader, lean/QuantConnect, vn.py, hummingbot" | Entities (4 reference-systems); Sub-Q1b | OK — all four named individually | No |
| "what did they get wrong first" | required_field on each reference-system | OK | No |
| "event-sourced append-only ledger with a single fold vs materialized-state stores" | Entity event-sourced ledger; Sub-Q2 | OK | No |
| "Hash-chained ledgers, dual-write/cross-store atomicity, single-writer invariants, reconstruct-vs-store" | Sub-Q2; entities ledger + dual-write | OK — all four named sub-concepts covered | No |
| "auditable, replayable position+P&L ledger that multiple consumers (projection, settlement FIFO) read without divergence" | Sub-Q2b | OK | No |
| "the model can silence but never amplify/override" | Sub-Q3; monotonic-forward entity | OK | No |
| "structural (not prompt) enforcement of that polarity" | Sub-Q3 | OK — distinguishes structural vs prompt | No |
| "prevent committee/LLM runaway" | Sub-Q3 | OK | No |
| "monotonic-forward authority a recognized pattern, where does it break" | Sub-Q3b; monotonic entity required_fields | OK | No |
| "schema versioning, carry-forward/interpretation-vs-mutation choice for append-only logs" | Sub-Q4; entity schema-versioning | OK | No |
| "backward/forward compatibility for two plugins on different release cadences" | Sub-Q4 | OK | No |
| "Monorepo-with-path-dep vs separately-published-and-pinned for the shared core" | Sub-Q4b; entity monorepo-vs-pinned | OK | No |
| "TauricResearch/TradingAgents, virattt/ai-hedge-fund, HKUDS/Vibe-Trading" | Entities (3 named); Sub-Q5 | OK — all three named individually | No |
| "2025-2026 successors" | Entity 2025-2026 successors group; Sub-Q5 | OK | No |
| "perception->decision->reaction split, signal unification, analysis/deterministic-risk separation" | Sub-Q5 required fields | OK | No |
| "LLM as final execution authority, free-text position sizing, RL-on-portfolio-value" | Sub-Q5b; entity anti-patterns | OK — all three anti-patterns named | No |
| "CONFIRMS the shared-core+two-shells design or proposes concrete improvements/alternatives" | DELIVERABLE; required heading 6 + Opinionated Synthesis | OK | No |
| "specific contract-seam, state-model, gate-enforcement, contract-evolution choices named" | required_formats: decisive recommendation per axis | OK — maps to headings 1-4 | No |
| "where ADR-0092 is right, under-specified, where real systems suggest a different choice" | required heading 6; DELIVERABLE | OK — three-way flag covered | No |

**Gaps: ZERO rows with Gap? = YES.** Every significant query phrase maps to at least one atomic item at full natural scope. Proceeding.
