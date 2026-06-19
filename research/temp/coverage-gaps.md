# Coverage report — pdr-core-host-adapter-arch-e99014

18 substantive vault notes, all real/dated/named sources. Mapping to atomic items:

| Atomic item | Sources | Status |
|---|---|---|
| Sub-Q1 hexagonal/ports seam | nautilus, lean, vn.py, hummingbot, optivem(leak), strangler | WELL-COVERED (6) |
| Sub-Q1 boundary-leak failure | optivem (driven-ports-leak), hummingbot (early-connector divergence), vn.py (risk-as-subscriber caution) | WELL-COVERED (3) |
| Sub-Q1 strangler-fig | Fowler strangler (+rewrite) | ADEQUATE (1-2) |
| Sub-Q1b nautilus/lean/vnpy/hummingbot | 4 dedicated deepwiki notes | WELL-COVERED (4) |
| Sub-Q2 append-only/single-fold vs materialized | dual-write(ES), TigerBeetle, (orchestrator: CQRS rebuildable) | WELL-COVERED |
| Sub-Q2 hash-chain/immutability | TigerBeetle (append-only immutable, reversals-as-entries) | ADEQUATE |
| Sub-Q2 dual-write/atomicity | Confluent dual-write (canonical), (outbox/listen-to-yourself covered within) | WELL-COVERED |
| Sub-Q2 single-writer | TigerBeetle (single-writer, in-ledger invariants), nautilus (single exec engine) | ADEQUATE |
| Sub-Q2 reconstruct-vs-store / multi-consumer no-divergence | dual-write(ES rebuild), hummingbot (divergence when not centralized) | WELL-COVERED |
| Sub-Q3 silence-not-amplify / monotonic | shielding (CACM+AAAI pre/post-shield, can-only-restrict, proven no-optimality-cost, breaks when empty), ai-hedge-fund, Vibe-Trading | WELL-COVERED |
| Sub-Q3 structural-not-prompt enforcement | Vibe-Trading(fail-closed hardcaps), ai-hedge-fund(envelope but prompt-level pick - caveat), shielding(separation of concerns) | WELL-COVERED |
| Sub-Q3 prevent committee/LLM runaway | Vibe-Trading kill-switch/BreachEvent, TradingAgents(anti-pattern), HITL(fail-closed) | WELL-COVERED |
| Sub-Q3 where it breaks | shielding(empty admissible set; incomplete shield=false safety; wrong state space) | ADEQUATE |
| Sub-Q4 schema versioning / carry-forward vs mutate | ES-versioning (upcast=read-time=carry-forward-fold; never mutate; copy-replace) | WELL-COVERED |
| Sub-Q4 backward/forward compat two cadences | ES-versioning (two-phase deploy, short streams), shackles(version conflict pain) | ADEQUATE |
| Sub-Q4 monorepo vs published-pinned | shackles(adversarial + security exception), (orchestrator: Block/dtolnay semver-trick/nx noted in width) | ADEQUATE-WELL |
| Sub-Q5 TradingAgents/ai-hedge-fund/Vibe + successors | 3 dedicated deepwiki + DeepFund + Profit-Mirage | WELL-COVERED |
| Sub-Q5 PDR split / signal unification / analysis-risk separation | TradingAgents, ai-hedge-fund, Vibe-Trading (all 3 mapped) | WELL-COVERED |
| Sub-Q5 NOT-work: LLM-final-authority | TradingAgents (the exemplar), DeepFund (frontier LLMs lose money live) | WELL-COVERED |
| Sub-Q5 NOT-work: free-text sizing | ai-hedge-fund(deterministic envelope is the fix), Vibe(hardcaps override) | ADEQUATE |
| Sub-Q5 NOT-work: RL-on-portfolio-value | RL-portfolio (MetaTrader offline-policy-not-trustworthy + Velay DRL robustness) | WELL-COVERED |
| no-look-ahead rail validation | Profit-Mirage (85% memorized, 55.68% Sharpe decay), DeepFund (time-travel cheating) | WELL-COVERED |

UNCOVERED items: NONE. All atomic items >= adequate; most well-covered.
Note: deepwiki-derived reference-repo notes ARE primary (sourced from the actual code wikis), high-fidelity for the "what did they get wrong first" requirement.
