# Scored URLs + fetch batches — pdr-core-host-adapter-arch-e99014

Reference-repo content already captured directly as vault notes (nautilus, lean, vn.py, hummingbot, TradingAgents, ai-hedge-fund, Vibe-Trading). These batches fetch full bodies of the high-utility web/paper sources surfaced in width sweep.

## Batch 1 — Sub-Q1 core/adapter seam + leak failure
- https://journal.optivem.com/p/hexagonal-architecture-your-driven-ports-are-leaking-infrastructure  (driven ports leaking infra — leak failure mode; 2026; authority 1 novelty 3 coverage 3)
- https://alistair.cockburn.us/hexagonal-architecture  (canonical hexagonal source; authority 3 canonical)
- https://jmgarridopaz.github.io/content/therightboundary.html  (the right boundary of a hexagon; depth)
- https://learn.microsoft.com/en-us/azure/architecture/patterns/anti-corruption-layer  (ACL pattern; authority 2)
- https://dev.to/gabrielanhaia/the-anti-corruption-layer-that-saves-your-next-vendor-migration-3m5i  (ACL vendor migration; practitioner)

## Batch 2 — Sub-Q2 money-state ledger
- https://github.com/tigerbeetle/tigerbeetle/blob/main/docs/concepts/safety.md  (TigerBeetle safety; authority 3)
- https://docs.tigerbeetle.com/concepts/debit-credit  (debit/credit OLTP schema; authority 3)
- https://tigerbeetle.framer.website/blog/2024-07-23-rediscovering-transaction-processing-from-history-and-first-principles  (first-principles ledger; 2024 depth)
- https://github.com/tigerbeetledb/tigerbeetle/blob/fe09404d465df46b2bdfc017633eff37b4ab2343/docs/DESIGN.md  (design doc; authority 3)

## Batch 3 — Sub-Q2 dual-write / outbox / event-sourcing
- https://www.confluent.io/blog/dual-write-problem/  (dual-write canonical; authority 2)
- https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html  (outbox; authority 2)
- https://auth0.com/blog/handling-the-dual-write-problem-in-distributed-systems/  (dual-write distributed; practitioner)
- https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing  (event-sourcing pattern + pitfalls; authority 2)
- https://www.sciencedirect.com/science/article/pii/S0164121221000674  (empirical ES schema-evolution study; authority 3 academic)

## Batch 4 — Sub-Q3 gate / shielding (formal monotonic restriction)
- https://cacm.acm.org/research/shields-for-safe-reinforcement-learning/  (CACM Shields for Safe RL; authority 3)
- https://ojs.aaai.org/index.php/AAAI/article/view/30163/32063  (Pure-Past Action Masking; authority 3 academic)
- https://cdn.aaai.org/ojs/11797/11797-13-15325-1-2-20201228.pdf  (Safe RL via Shielding, Alshiekh; authority 3 canonical)
- https://papers.nips.cc/paper_files/paper/2024/file/b589d92785e39486e978fa273d0dc343-Paper-Conference.pdf  (Dynamic Model Predictive Shielding NeurIPS 2024; authority 3)

## Batch 5 — Sub-Q3 HITL / policy-gated execution (LLM advisory)
- https://docs.agentos.sh/features/human-in-the-loop  (HITL ApprovalRequest->handler->ApprovalDecision; 2025 practitioner)
- https://kla.digital/docs/core-concepts/policy-gated-execution  (policy-gated execution; practitioner)
- https://developers.openai.com/api/docs/guides/agents/guardrails-approvals  (OpenAI guardrails + human review; authority 2)
- https://cordum.io/blog/human-in-the-loop-ai-patterns  (5 HITL production patterns; practitioner)

## Batch 6 — Sub-Q4 contract evolution / event versioning
- https://leanpub.com/esversioning/read  (Greg Young Versioning in an ES System; authority 3 canonical)
- https://www.infoq.com/news/2017/07/versioning-event-sourcing/  (event versioning summary; authority 2)
- https://event-driven.io/en/simple_events_versioning_patterns/  (Oskar Dudycz versioning patterns; practitioner)
- https://martendb.io/events/versioning.html  (Marten event versioning upcasters; practitioner)

## Batch 7 — Sub-Q4 monorepo vs pinned shared core
- https://stevenstuartm.com/blog/2026/01/06/the-false-economy-of-shared-libraries.html  (shared libraries become shared shackles; 2026 adversarial)
- https://engineering.block.xyz/blog/from-polyrepo-fragmentation-to-monorepo-leverage  (Block polyrepo->monorepo; 2025 practitioner)
- https://github.com/dtolnay/semver-trick/blob/master/README.md  (semver-trick for breaking shared core; authority 2)
- https://www.jamesrossjr.com/blog/monorepo-vs-polyrepo  (monorepo vs polyrepo tradeoff; 2026)

## Batch 8 — Sub-Q5 LLM-trading failure modes (no-lookahead, alpha illusion)
- https://arxiv.org/html/2510.07920v1  (Profit Mirage: information leakage in LLM financial agents; 2025; authority 3 LOAD-BEARING)
- https://arxiv.org/html/2505.11065v1  (Time Travel is Cheating / DeepFund live benchmark; 2025 authority 3)
- https://arxiv.org/html/2510.02209  (StockBench: can LLM agents trade profitably; 2025 authority 3)
- https://arxiv.org/pdf/2511.08608  (When Reasoning Fails: evaluating thinking LLMs for stock prediction; 2025 authority 3)

## Batch 9 — Sub-Q5 RL-on-portfolio-value fragility + TradingAgents paper
- https://arxiv.org/html/2505.12759v1  (Your Offline Policy is Not Trustworthy; RL portfolio; 2025 authority 3)
- https://arxiv.org/html/2306.10950  (Benchmarking Robustness of DRL Online Portfolio Mgmt; authority 3)
- https://arxiv.org/abs/2412.20138  (TradingAgents paper — search by title if abs wrong)
- https://martinfowler.com/bliki/StranglerFigApplication.html  (Strangler Fig canonical; authority 3)
- https://martinfowler.com/articles/2024-strangler-fig-rewrite.html  (Rewriting Strangler Fig 2024; authority 3)
