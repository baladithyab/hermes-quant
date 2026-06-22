# SOTA Scan: LLM Agents for Trading/Investing (mid-2025 → June 2026)

- **Doc**: r-sota-llm-agents-markets
- **Date**: 2026-06-09
- **For**: cowork-quant roadmap (advisor/paper-only Cowork plugin; Claude committee in-session, deterministic Python gate/sizing/ledger)
- **Method**: HuggingFace paper_search, web search, arXiv abstract fetches. Every claim cited (arXiv ID or URL).

## TL;DR

The field moved from "can an LLM trade?" to "can we trust the evaluation and the loop?" Live, contamination-free benchmarks (StockBench, Agent Market Arena, LiveTradeBench, AI-Trader) now dominate, and two 2026 papers — KTD-Fin (2605.28359) and Look-Ahead-Bench (2601.13770) — show that most reported LLM trading "alpha" is memorization, market beta, or style exposure. Stress-testing work (TradeTrap, 2512.02261) and the real-money Alpha Arena experiment demonstrate that LLM agents with execution authority develop runaway exposure and concentration under small perturbations — directly validating hermes-quant's "LLM never executes" and deterministic-gate rails. Regulators (IOSCO AI Supervisory Toolkit, May 2026; SEC 2026 exam priorities; FINRA agentic-AI guidance) now explicitly expect governance and human-oversight controls around agentic AI in capital markets. The main *challenge* to our current design: plain walk-forward validation is measurably weaker than CPCV + deflated-Sharpe/PBO at false-discovery control, and return-level eval without attribution overstates skill. Concrete v0.2+ candidates: leakage-masked eval mode, Barra-lite attribution, DSR/PBO CI gates, Brier-scored analyst forecast ledger, TradeTrap-style adversarial drills, and debate-protocol controls.

---

## 1. LLM trading agents & multi-agent committees

**Framework architecture matters more than model choice.** Agent Market Arena (AMA), a lifelong live benchmark running InvestorAgent/TradeAgent/HedgeFundAgent/DeepFundAgent architectures over GPT-4o/4.1, Claude, Gemini backbones, finds that agent *framework* drives behavior and performance more than the LLM backbone, and that backbones show distinct risk personalities (aggressive vs. conservative) ([arXiv 2510.11695](https://arxiv.org/abs/2510.11695)). This supports investing roadmap effort in committee structure/prompt contracts rather than model-shopping.

**Committee/contest designs post-TradingAgents.** TradingAgents (multi-analyst firm simulation: fundamental/sentiment/technical analysts + trader + risk management) remains the canonical reference ([arXiv 2412.20138](https://arxiv.org/abs/2412.20138)). Newer variants:
- **ContestTrade**: internal contest mechanism — analyst agents compete, a ranking mechanism continuously scores them on realized outcomes and reweights their influence ([arXiv 2508.00554](https://arxiv.org/abs/2508.00554)). Directly maps to a per-analyst track-record weighting feature.
- **TradingGroup**: self-reflection + a *dynamic risk-management model* distinct from the analyst agents ([arXiv 2508.17565](https://arxiv.org/abs/2508.17565)) — same proposer/risk-plane split as hermes-quant.
- **AlphaAgents** (BlackRock-affiliated authors): role-based multi-agent equity portfolio construction with explicit risk-tolerance parameterization; candid about practical challenges ([arXiv 2508.11152](https://arxiv.org/abs/2508.11152)).
- **ATLAS**: order-aware action space (orders, not just buy/sell/hold) + Adaptive-OPRO dynamic prompt optimization from realized feedback ([arXiv 2510.15949](https://arxiv.org/abs/2510.15949)). The prompt-optimization loop is an advisory-plane self-evolution pattern consistent with our restriction.
- **QuantAgent**: price-driven multi-agent decomposition (indicator/pattern/trend/risk agents) for HFT ([arXiv 2509.09995](https://arxiv.org/abs/2509.09995)) — out of scope cadence-wise, but its "risk agent has veto" pattern recurs across the literature.

**Debate is not free alpha.** A controlled study of multi-agent debate finds benefits hinge on intrinsic reasoning strength and *group diversity*; majority pressure produces incorrect consensus, and confidence visibility/debate order materially change outcomes ([arXiv 2511.07784](https://arxiv.org/abs/2511.07784)). "Why Do Multi-Agent LLM Systems Fail?" catalogs 14 failure modes across specification, inter-agent misalignment, and verification/termination ([arXiv 2503.13657](https://arxiv.org/abs/2503.13657)). A Byzantine-fault-tolerance treatment proposes confidence-probe weighted consensus for multi-agent reliability ([arXiv 2511.10400](https://arxiv.org/abs/2511.10400)). Implication: cowork-quant's committee protocol should engineer dissent (hidden initial confidences, independent first passes, structured devil's-advocate) rather than naive discussion rounds.

**Calibration.** Collaborative Calibration shows multi-agent deliberation improves post-hoc confidence calibration ([arXiv 2404.09127](https://arxiv.org/abs/2404.09127)). FutureX provides a live, contamination-resistant future-prediction benchmark for LLM agents ([arXiv 2508.11987](https://arxiv.org/abs/2508.11987)); Live-Evo evaluates agent memory on market returns and Prophet Arena using **Brier score** ([arXiv 2602.02369](https://arxiv.org/abs/2602.02369)) — i.e., probabilistic-forecast scoring of analyst opinions is now standard practice we can adopt cheaply.

## 2. Benchmarks & evaluation (post-FinMem/FinBen era)

**Live / contamination-free benchmarks now the norm:**
| Benchmark | Date | What it adds | Ref |
|---|---|---|---|
| StockBench | Oct 2025 | Contamination-free daily-decision stock trading; GPT-5/Claude-4-class agents struggle to beat buy-and-hold; reports return + max drawdown + Sortino | [2510.02209](https://arxiv.org/abs/2510.02209) |
| Agent Market Arena | Oct 2025 | Lifelong real-time, multi-market (equities + crypto), expert-checked news; framework > backbone | [2510.11695](https://arxiv.org/abs/2510.11695) |
| LiveTradeBench | Nov 2025 | Live portfolio management over US stocks **and Polymarket prediction markets**; consistency under live uncertainty | [2511.03628](https://arxiv.org/abs/2511.03628) |
| AI-Trader | Dec 2025 | First fully automated live benchmark, autonomous information processing across markets | [2512.10971](https://arxiv.org/abs/2512.10971) |
| DeepFund | Mar 2025 | Live arena; names the failure taxonomy: data leakage, "navel-gazing", **over-intervention** | [2503.18313](https://arxiv.org/abs/2503.18313) |
| InvestorBench | Dec 2024 | Multi-asset (stocks/crypto/ETF) agent decision benchmark (bridge from FinMem era) | [2412.18174](https://arxiv.org/abs/2412.18174) |
| FutureX | Aug 2025 | Live future-prediction (calibration) benchmark | [2508.11987](https://arxiv.org/abs/2508.11987) |

**The 2026 evaluation correction — leakage and attribution:**
- **KTD-Fin** (May 2026) masks tickers/dates/identifiers across prompts *and tools* to separate memory from reasoning, and adds Barra-style attribution (market/style/selection). Result: across ten frontier LLM agents on CSI300 2024–2026, leakage-controlled returns are "largely explained by passive market and style exposure, with limited evidence of persistent stock-selection alpha" ([arXiv 2605.28359](https://arxiv.org/abs/2605.28359)). This is the single most important eval paper for cowork-quant.
- **Look-Ahead-Bench** (Jan 2026) standardizes measurement of look-ahead bias via alpha decay across temporally distinct regimes; standard LLMs show significant lookahead bias vs. point-in-time-trained models ([arXiv 2601.13770](https://arxiv.org/abs/2601.13770), [code](https://github.com/benstaf/lookaheadbench)).

**Overfitting-control practice.** Deflated Sharpe Ratio (Bailey & López de Prado) remains the standard correction for selection bias under multiple testing and non-normal returns ([JPM 40(5):94](https://www.pm-research.com/content/iijpormgmt/40/5/94); [overview](https://en.wikipedia.org/wiki/Deflated_Sharpe_ratio)). A controlled synthetic comparison of out-of-sample methods finds **CPCV (combinatorial purged cross-validation) materially beats walk-forward** on Probability of Backtest Overfitting (PBO) and DSR test statistics; walk-forward shows "notable shortcomings in false discovery prevention" ([Knowledge-Based Systems, S0950705124011110](https://www.sciencedirect.com/science/article/abs/pii/S0950705124011110)). A Dec 2025 framework paper combines hypothesis-driven signals with rigorous walk-forward validation as a practitioner template ([arXiv 2512.12924](https://arxiv.org/pdf/2512.12924)).

## 3. Risk views: failure modes, incidents, regulation

**TradeTrap — system-level fragility (Dec 2025).** Stress-tests adaptive and procedural trading agents across four components (market intelligence, strategy formulation, portfolio/ledger handling, execution). Finding: *small perturbations at a single component propagate through the decision loop and induce extreme concentration, runaway exposure, and large drawdowns* — "current autonomous trading agents can be systematically misled at the system level" ([arXiv 2512.02261](https://arxiv.org/abs/2512.02261), [code](https://github.com/Yanlewen/TradeTrap)). Note their ledger-handling attack surface: an agent that *believes* a corrupted ledger misbehaves — our deterministic, Python-owned ledger removes that surface.

**Alpha Arena (nof1.ai, late 2025) — real-money natural experiment.** Six frontier LLMs traded $10k each of real capital in crypto perps. Lessons reported across coverage: most models failed on **risk management and position sizing**, not direction; benchmark intelligence (GPT-5, Grok-4) did not transfer to live P&L; several models suffered 60%+ drawdowns ([nof1.ai](https://nof1.ai/), [season-1 results analysis](https://www.iweaver.ai/blog/alpha-arena-ai-trading-season-1-results/), [datawallet explainer](https://www.datawallet.com/crypto/alpha-arena-nof1-ai-explained), [PANews](https://www.panewslab.com/en/articles/07cbee36-3e6e-44ed-8e48-bef321b3fc3e)). Confirms: sizing must be deterministic; LLM contributes view, not size.

**Practitioner post-mortems** echo the same architecture lesson: LLMs given execution authority "hallucinate a reason to take a risky trade to make it back" when down ([Medium post-mortem](https://medium.com/@kojott/i-lost-40-of-my-trading-account-by-trusting-an-ai-and-how-i-fixed-it-91b63d2f565a)); a 72-hour simulated free-run produced degenerate behavior at scale ([Stackademic write-up](https://stackademic.com/blog/he-let-an-ai-trade-a-fake-stock-market-for-72-hours-which-made-3000-but-then-it-nearly-broke-everything)). Anecdotal, but consistent with TradeTrap.

**Guardrail research validates deterministic gating, invalidates LLM-as-gate:**
- "Bag of Tricks for Subverting Reasoning-based Safety Guardrails": reasoning-based (LLM) guardrails are bypassable with subtle prompt manipulation at high attack success rates ([arXiv 2510.11570](https://arxiv.org/abs/2510.11570)). An LLM risk-checker is not a gate.
- **Type-Checked Compliance**: deterministic guardrails for *agentic financial systems* via Lean 4 theorem proving, encoding regulatory axioms (SEC Rule 15c3-5, FINRA 3110) as formally verified checks ([arXiv 2604.01483](https://arxiv.org/abs/2604.01483)). Strongest academic endorsement of "deterministic code owns the gate."
- **Symbolic Guardrails** for domain-specific agents: symbolic policy enforcement gives guarantees LLM-based methods cannot, without utility loss ([arXiv 2604.15579](https://arxiv.org/abs/2604.15579)); similarly ShieldAgent's verifiable probabilistic rule circuits ([arXiv 2503.22738](https://arxiv.org/abs/2503.22738)) and STPA/MCP-based verifiably safe tool use ([arXiv 2601.08012](https://arxiv.org/abs/2601.08012)).
- **Pre-execution screening**: AuraGen/Safiron build a *guardian model that screens plans before execution* ([arXiv 2510.09781](https://arxiv.org/abs/2510.09781)); SafePred does predictive risk-to-decision gating via world models ([arXiv 2602.01725](https://arxiv.org/abs/2602.01725)). Both are proposer→verifier→executor splits.

**Systemic/herding risk.** TwinMarket shows LLM-agent populations endogenously generate bubbles and crashes via social dynamics ([arXiv 2502.01506](https://arxiv.org/abs/2502.01506)); prompt-sensitivity can correlate actions across independently operated agents (raised in 2025–26 commentary, e.g., [Look-Ahead-Bench discussion](https://arxiv.org/pdf/2601.13770)). For an advisory tool this is a documentation point: our advice may be correlated with every other Claude user's advice.

**Regulators (2025–2026):**
- **IOSCO** published its final **"Supervisory Toolkit for AI Use in Capital Markets"** (FR/02/2026, 25 May 2026), explicitly covering GenAI and *emerging Agentic AI techniques*, framed around investor protection, market integrity, and financial stability, in collaboration with the FSB ([IOSCOPD823.pdf](https://www.iosco.org/library/pubdocs/pdf/IOSCOPD823.pdf), [press release](https://www.iosco.org/news/pdf/IOSCONEWS796.pdf), [analysis](https://www.regulationtomorrow.com/2026/05/iosco-final-report-supervisory-toolkit-for-ai-use-in-capital-markets/)).
- **SEC 2026 exam priorities**: firms must have "adequate policies and procedures to monitor and/or supervise their use of AI technologies"; **FINRA** 2026 report calls out agentic AI as requiring specific governance and controls ([Nasdaq regulatory roundup](https://www.nasdaq.com/articles/fintech/regulatory-roundup-february-2026)).
- Net: human-in-the-loop, audit trails, and deterministic supervision of agentic AI are becoming the regulatory baseline — our rails are ahead of, not behind, this curve.

## 4. The PDR analogy: proposer/verifier splits & self-evolution boundaries

- The **propose → verify → execute** decomposition is now an explicit research pattern: pre-execution guardian models (Safiron, [2510.09781](https://arxiv.org/abs/2510.09781)), predictive guardrails (SafePred, [2602.01725](https://arxiv.org/abs/2602.01725)), formally verified compliance gates ([2604.01483](https://arxiv.org/abs/2604.01483)), and trace-contract assurance with deterministic replay for agent orchestration ([arXiv 2603.18096](https://arxiv.org/abs/2603.18096)). No paper found uses the literal "perception-decision-reaction" naming for market agents, but TradeTrap's four-component decomposition (intelligence → strategy → ledger → execution) is effectively PDR plus ledger, and its attacks justify hardening each boundary independently ([2512.02261](https://arxiv.org/abs/2512.02261)).
- **LLM proposes, non-LLM executes** appears as a hybrid in quant RL: an LLM generates strategies, a (deterministic, trained-offline) RL agent executes them ([arXiv 2508.02366](https://arxiv.org/abs/2508.02366)) — adjacent to, but not contradicting, our RL-aggregator DO_NOT_BUILD: the RL piece is a separate, offline-trained plane, and Dr. MAS shows RL training of multi-agent LLM systems remains unstable ([arXiv 2602.08847](https://arxiv.org/abs/2602.08847)).
- **Self-evolution stays risky outside an advisory plane.** FATE shows self-evolution from verifier-scored *failure* trajectories can improve safety, but requires explicit Pareto-aware safety/performance optimization ([arXiv 2605.11882](https://arxiv.org/abs/2605.11882)); the self-evolving-agents survey flags safety as a primary open challenge ([arXiv 2507.21046](https://arxiv.org/abs/2507.21046)). Memory-plane evolution (experience bank + meta-guideline bank, Live-Evo [2602.02369](https://arxiv.org/abs/2602.02369); MemEvolve [2512.18746](https://arxiv.org/abs/2512.18746); Evo-Memory [2511.20857](https://arxiv.org/abs/2511.20857)) is the safest, best-validated form — exactly what "advisory plane only" permits.

---

## Rails confirmed / rails challenged

### Confirmed
1. **LLM never executes** — TradeTrap perturbation cascades ([2512.02261](https://arxiv.org/abs/2512.02261)); Alpha Arena real-money sizing failures ([nof1.ai](https://nof1.ai/), [analysis](https://www.iweaver.ai/blog/alpha-arena-ai-trading-season-1-results/)); IOSCO/SEC/FINRA supervision expectations ([IOSCOPD823](https://www.iosco.org/library/pubdocs/pdf/IOSCOPD823.pdf), [Nasdaq roundup](https://www.nasdaq.com/articles/fintech/regulatory-roundup-february-2026)).
2. **Deterministic risk gate (not an LLM judge)** — reasoning guardrails are jailbreakable ([2510.11570](https://arxiv.org/abs/2510.11570)); symbolic/formal gates give guarantees ([2604.01483](https://arxiv.org/abs/2604.01483), [2604.15579](https://arxiv.org/abs/2604.15579)).
3. **Deterministic ¼-Kelly + discrete sizing ladder** — sizing, not direction, was the dominant live failure mode (Alpha Arena, above); "embed stop-loss and position-sizing outside the model" is the consensus practitioner recommendation ([datawallet](https://www.datawallet.com/crypto/alpha-arena-nof1-ai-explained)).
4. **Silence-by-default** — DeepFund names "over-intervention" as a core LLM-fund failure ([2503.18313](https://arxiv.org/abs/2503.18313)); StockBench agents underperform buy-and-hold partly through overtrading ([2510.02209](https://arxiv.org/abs/2510.02209)).
5. **Multi-analyst committee** — framework > backbone (AMA, [2510.11695](https://arxiv.org/abs/2510.11695)); contest/ranking mechanisms add robustness (ContestTrade, [2508.00554](https://arxiv.org/abs/2508.00554)).
6. **Self-evolution restricted to advisory plane** — memory-plane evolution validated ([2602.02369](https://arxiv.org/abs/2602.02369), [2512.18746](https://arxiv.org/abs/2512.18746)); unrestricted self-evolution flagged as unsafe ([2507.21046](https://arxiv.org/abs/2507.21046), [2605.11882](https://arxiv.org/abs/2605.11882)).
7. **RL aggregator = DO_NOT_BUILD** — RL training of multi-agent LLM systems is documented as unstable ([2602.08847](https://arxiv.org/abs/2602.08847)); hybrid LLM+RL work keeps RL strictly out of the language loop ([2508.02366](https://arxiv.org/abs/2508.02366)).

### Challenged / needs upgrading
1. **Plain walk-forward is no longer best practice.** CPCV demonstrably beats walk-forward on PBO and DSR in controlled comparison ([S0950705124011110](https://www.sciencedirect.com/science/article/abs/pii/S0950705124011110)). Our CI gates should add DSR/PBO statistics and consider CPCV alongside walk-forward.
2. **Return-based eval without attribution overstates skill.** KTD-Fin: leakage-controlled LLM-agent returns are mostly beta/style ([2605.28359](https://arxiv.org/abs/2605.28359)). A Sharpe-only walk-forward gate could pass a committee that merely loads beta.
3. **No-lookahead needs to cover the model, not just the data.** Even point-in-time data pipelines leak via LLM weights (memorized prices/narratives) — Look-Ahead-Bench ([2601.13770](https://arxiv.org/abs/2601.13770)) and KTD-Fin masking ([2605.28359](https://arxiv.org/abs/2605.28359)). Backtests over pre-cutoff periods with a frontier Claude are structurally contaminated.
4. **Naive debate can hurt.** Majority pressure → incorrect consensus; diversity and confidence-hiding matter ([2511.07784](https://arxiv.org/abs/2511.07784)); 14 documented MAS failure modes ([2503.13657](https://arxiv.org/abs/2503.13657)). Committee protocol needs explicit anti-conformity design, not just N analysts.

---

## Concrete feature candidates for v0.2+

1. **Leakage-masked eval mode** (eval harness): KTD-Fin-style anonymization of tickers/dates/identifiers in committee prompts during backtest replay, so CI measures reasoning, not recall ([2605.28359](https://arxiv.org/abs/2605.28359)). Highest-value, cheap to implement on the deterministic side.
2. **DSR/PBO CI gates**: compute Deflated Sharpe and Probability of Backtest Overfitting over the strategy trial set; optionally CPCV folds in addition to walk-forward ([JPM DSR](https://www.pm-research.com/content/iijpormgmt/40/5/94), [CPCV comparison](https://www.sciencedirect.com/science/article/abs/pii/S0950705124011110)).
3. **Barra-lite attribution in the ledger**: decompose paper-P&L into market/style/selection; report "alpha after attribution" in eval output ([2605.28359](https://arxiv.org/abs/2605.28359)).
4. **Brier-scored analyst forecast ledger**: each committee analyst emits probabilistic forecasts; deterministic plane scores calibration over time and feeds weights ContestTrade-style ([2602.02369](https://arxiv.org/abs/2602.02369), [2508.00554](https://arxiv.org/abs/2508.00554), [2404.09127](https://arxiv.org/abs/2404.09127)).
5. **Debate-protocol controls**: independent first-pass opinions, hidden confidences until commitment, mandated dissenting analyst, diversity across analyst personas ([2511.07784](https://arxiv.org/abs/2511.07784), [2503.13657](https://arxiv.org/abs/2503.13657)).
6. **TradeTrap-style adversarial drill suite** in CI: inject poisoned news, corrupted ledger snapshots, and perturbed quotes into committee inputs; assert the deterministic gate caps resulting exposure ([2512.02261](https://arxiv.org/abs/2512.02261)).
7. **Pre-gate risk screener** (deterministic, rule-based, Safiron-pattern): categorize committee proposals (concentration, turnover, regime mismatch) *before* the Kelly/ladder gate; rejection reasons logged for the advisory memory plane ([2510.09781](https://arxiv.org/abs/2510.09781), [2602.01725](https://arxiv.org/abs/2602.01725)).
8. **Advisory memory plane v2**: experience bank + meta-guideline bank with decay, updated only from realized outcomes (Live-Evo pattern), never touching gate/sizing code ([2602.02369](https://arxiv.org/abs/2602.02369), [2512.18746](https://arxiv.org/abs/2512.18746)).
9. **Governance mapping doc**: map cowork-quant controls to IOSCO toolkit / FINRA agentic-AI expectations — useful positioning even for a paper-only advisor ([IOSCOPD823](https://www.iosco.org/library/pubdocs/pdf/IOSCOPD823.pdf)).
10. **Order-aware advice schema** (stretch): ATLAS shows richer action spaces (limit/stop semantics) change agent behavior; advice cards could optionally specify entry/exit bands rather than bare direction ([2510.15949](https://arxiv.org/abs/2510.15949)).

## Benchmark candidates to evaluate against

| Priority | Benchmark | Why for cowork-quant | Ref |
|---|---|---|---|
| 1 | **StockBench** | Daily-cadence, contamination-free, matches our decision rhythm; buy-and-hold baseline discipline | [2510.02209](https://arxiv.org/abs/2510.02209) |
| 1 | **KTD-Fin** | Leakage-masked + attribution-aware; the strictest test of "real reasoning" (CSI300 universe is a porting cost) | [2605.28359](https://arxiv.org/abs/2605.28359) |
| 2 | **LiveTradeBench** | Live US stocks + Polymarket; tests calibration and portfolio consistency forward-only | [2511.03628](https://arxiv.org/abs/2511.03628) |
| 2 | **Agent Market Arena** | Live multi-market arena; positions our committee against published agent frameworks | [2510.11695](https://arxiv.org/abs/2510.11695) |
| 3 | **Look-Ahead-Bench** | Audit our own backtest pipeline for model-weight lookahead | [2601.13770](https://arxiv.org/abs/2601.13770) |
| 3 | **TradeTrap** | Adversarial robustness suite (use as red-team harness, not leaderboard) | [2512.02261](https://arxiv.org/abs/2512.02261) |
| 3 | **FutureX / Prophet Arena** | Pure forecast-calibration scoring of the analyst plane | [2508.11987](https://arxiv.org/abs/2508.11987), [2602.02369](https://arxiv.org/abs/2602.02369) |

## Source index (primary)

- 2510.02209 StockBench · 2510.11695 Agent Market Arena · 2511.03628 LiveTradeBench · 2512.10971 AI-Trader · 2503.18313 DeepFund · 2412.18174 InvestorBench · 2508.11987 FutureX
- 2605.28359 KTD-Fin · 2601.13770 Look-Ahead-Bench · S0950705124011110 (CPCV vs walk-forward) · JPM 40(5):94 (DSR) · 2512.12924 (walk-forward framework)
- 2412.20138 TradingAgents · 2508.00554 ContestTrade · 2508.17565 TradingGroup · 2508.11152 AlphaAgents · 2510.15949 ATLAS · 2509.09995 QuantAgent · 2511.07784 (debate study) · 2503.13657 (MAS failures) · 2511.10400 (BFT consensus) · 2404.09127 (collaborative calibration)
- 2512.02261 TradeTrap · 2510.11570 (guardrail subversion) · 2604.01483 (Lean 4 deterministic compliance) · 2604.15579 (symbolic guardrails) · 2503.22738 ShieldAgent · 2601.08012 (verifiably safe tool use) · 2510.09781 Safiron · 2602.01725 SafePred · 2603.18096 (trace assurance) · 2502.01506 TwinMarket
- 2602.02369 Live-Evo · 2512.18746 MemEvolve · 2511.20857 Evo-Memory · 2605.11882 FATE · 2507.21046 (self-evolving agents survey) · 2508.02366 (LLM-guided RL) · 2602.08847 Dr. MAS
- IOSCO FR/02/2026 Supervisory Toolkit (iosco.org/library/pubdocs/pdf/IOSCOPD823.pdf) · SEC 2026 exam priorities & FINRA agentic-AI guidance (nasdaq.com regulatory roundup, Feb 2026) · nof1.ai Alpha Arena
