# R-2026-06-09 — Re-survey: multi-agent LLM trading frameworks (delta since 2026-05-24 baseline)

**Baseline:** `hermes-quant/docs/research/reference-projects/2026-05-24-r1..r6` (internal notes).
**Method:** live GitHub API pulls (2026-06-09), arXiv/HF paper lookups, web search. Every claim carries a URL.
**Lens per project:** (a) what to PORT into a Claude Cowork plugin (skills/commands/subagents + deterministic scripts); (b) which of hermes-quant's rejected anti-patterns it still exhibits (LLM-as-final-execution-authority, free-text→money, string-grep contracts, no-HITL, no-audit-trail, lookahead); (c) genuinely new capabilities (memory, debate, evaluation) hermes-quant lacks.

---

## TL;DR

1. **TradingAgents is converging toward our posture, slowly.** Since the May notes it shipped structured output for the Sentiment Analyst, a "verified market-data snapshot" so agents *never invent prices*, explicit temperature/reproducibility config, and env-var (non-interactive) configuration — i.e., the project is independently discovering "ground numeric claims deterministically." The `FINAL TRANSACTION PROPOSAL` string-grep contract and free-text sizing remain.
2. **Vibe-Trading broke its own "no live execution" boundary** — the single biggest delta since May. On 2026-05-29 it added Robinhood "Agentic Trading" (live, mandate-gated), and on 2026-06-02 bounded live order placement across 5 more brokers. The mitigation design (user-committed mandate, fail-closed pre-trade gate, filesystem kill switch, audit ledger) is itself the most port-worthy *deterministic-gate* reference now in the wild — but the r3 note's claim "Vibe-Trading never ships execution capability" is obsolete.
3. **AI-Trader pivoted to tournaments**: a monthly "challenge" competition with mark-to-market, drawdown leaderboards, verified-agent identity badges, and server-side price guards (no longer trusting agent-submitted prices for challenge trades — partially fixing the r2 anti-pattern).
4. **moon-dev's canonical repo is gone.** `moondevonyt/moon-dev-ai-agents` 404s; the author's account now hosts Hyperliquid data-layer and prediction-market bot repos. The `yolojewjitsu` mirror is stale (Oct 2025). The cautionary reference (`risk_agent.py:319`) survives only in forks — archive it.
5. **ai-hedge-fund finally got a backtester, an event-study engine, and (on 2026-06-08) an actual MIT LICENSE file.** Still no HITL/risk gate; the Portfolio Manager LLM remains the final authority on simulated orders.
6. **The evaluation layer is the new frontier**: live arenas (nof1 Alpha Arena — now VC-funded, Agent Market Arena, LiveTradeBench, StockBench, AI-Trader challenge) replaced static backtests as the credibility currency. FutureSim (arXiv 2605.15188, May 2026) formalizes chronological replay as the anti-lookahead standard. cowork-quant should plan an "arena adapter" surface, not build an arena.

---

## 1. TauricResearch/TradingAgents (+ paper)

**Repo:** <https://github.com/TauricResearch/TradingAgents> — 79,231 stars, 15,445 forks, 373 open issues, pushed 2026-06-01 (API pull 2026-06-09). Latest release **v0.2.5, 2026-05-11** (<https://github.com/TauricResearch/TradingAgents/releases>).

### Delta since 2026-05-24 notes

The r1/r5 notes captured the v0.2.4/0.2.5 architecture (structured `ResearchPlan`/`TraderProposal`/`PortfolioDecision`, 5-tier `PortfolioRating`, decision log with outcome-grounded reflections, checkpoint resume). New commits May 31 – June 1, 2026 (<https://github.com/TauricResearch/TradingAgents/commits/main>):

- **`feat(sentiment): structured output for the Sentiment Analyst`** — the first *analyst-tier* role to move off free-text markdown blobs. The analyst tier was previously 100% prose-as-handoff (r1 §3); structure is now creeping upstream.
- **`feat(market): verified market-data snapshot to ground numeric claims`** and **`fix: support commodity/forex/crypto tickers and never invent prices (#781)`** — deterministic price grounding so LLM reports can't hallucinate numerics. This is TradingAgents independently re-inventing hermes-quant's "deterministic code computes, LLM interprets" split.
- **`feat(config): expose sampling temperature and document reproducibility`** — first explicit reproducibility surface in the project.
- **`feat(cli): skip interactive LLM selection when configured via environment (#873)`** + `TRADINGAGENTS_*` env overrides (v0.2.5) — headless/scriptable runs, relevant for invoking it as a comparison baseline from CI.
- China A-share benchmarks, GPT-5.5 default, MiniMax/Qwen/GLM dual-region (v0.2.5 release notes, URL above).

**Paper:** arXiv 2412.20138, still at v7 (last revised 3 Jun 2025 — no new revision since the notes; <https://arxiv.org/abs/2412.20138>; HF: <https://hf.co/papers/2412.20138>).

**Forks of note:** `hsliuping/TradingAgents-CN` — 28,163 stars, 5,981 forks, pushed 2026-04-20 (<https://github.com/hsliuping/TradingAgents-CN>): a Chinese-market enhanced fork with its own web UI and A-share data plumbing; now over ⅓ the star count of upstream and arguably the most-deployed variant. Also `TradingGoose` (portfolio-level multi-stock extension, <https://github.com/TradingGoose/TradingGoose.github.io>). Beware SEO-squatting clones (`Tauric-Research-Trading/TradingAgents`, <https://github.com/Tauric-Research-Trading/TradingAgents>) that impersonate the org — a supply-chain caution when citing install instructions.

### Lens

- **(a) Port:** the *verified market-data snapshot* pattern → cowork-quant skill rule: every analyst subagent receives a deterministic, script-generated price/indicator block (computed by a Python script, not the LLM) and is forbidden from emitting numerics not present in it. Also port the 5-tier advisory rating (display layer only) and the outcome-grounded decision log (v0.2.4) as a settlement-journal-lite for the advisor surface.
- **(b) Still exhibits:** `FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**` string-grep contract (still in `trader.py`); free-text `position_sizing`; no HITL gate between PortfolioDecision and `SignalProcessor`; no deterministic risk gate. Lookahead: improved (date-aware fetching since v0.2.3) but no `available_at` discipline for news.
- **(c) Genuinely new:** per-ticker decision log that auto-resolves pending entries with realized return + alpha vs SPY + one-paragraph reflection on the *next* run (v0.2.4) — a cheap, file-based "did my last call work" memory hermes-quant's ADR-0010 journal could feed into prompts.

---

## 2. HKUDS/AI-Trader

**Repo:** <https://github.com/HKUDS/AI-Trader> — 19,478 stars, 2,963 forks, created 2025-10-23, pushed **2026-06-09** (same-day activity). Platform: <https://ai4trade.ai>.

### Delta since 2026-05-24 notes

The r2 note described the signal bus (`/api/signals/realtime`), blind 1:1 copy-trading, and agent-submitted prices. Since then the project pivoted hard into a **public tournament platform** (commits 2026-06-03 → 06-09, <https://github.com/HKUDS/AI-Trader/commits/main>):

- **Monthly challenge competition**: "Add dedicated challenge trading", "challenge track filtering", "monthly challenge monitor script", "Document challenge competition endpoints" (PR #244, merged 2026-06-03).
- **Drawdown-aware leaderboards**: "Expose drawdown leaderboard tab", "Add drawdown chart to leaderboard", "Clarify challenge drawdown metrics" — evaluation is no longer return-only.
- **Verified agent identity badge** — first anti-sybil/anti-impersonation primitive on the platform.
- **`Guard challenge trade prices`** (PR #246, merged 2026-06-09) + "challenge mark-to-market and yfinance fallback" (2026-06-08) — server-side price validation for challenge trades. This directly addresses the r2 anti-pattern #3 ("accepting `price: 51000` from the agent payload"), at least for the tournament track.
- README now markets one-message onboarding for any agent ("Read https://ai4trade.ai/SKILL.md and register") incl. Claude Code/Cursor — i.e., its SKILL.md *is* a prompt-injection-shaped integration surface (<https://github.com/HKUDS/AI-Trader/blob/main/README.md>).

### Lens

- **(a) Port:** the *drawdown leaderboard* shape (rank by max-DD and risk-adjusted metrics, not raw return) for cowork-quant's offline analyst-ranking command; and the *verified identity badge* concept → sign cowork-quant analyst outputs with the analyst version hash so the committee log can prove which prompt/persona produced which view.
- **(b) Still exhibits:** copy-trading auto-mirrors leader positions without confirmation (no-HITL, free-text→(paper)-money); token god-keys without capability scopes; the SKILL.md onboarding pattern is literally "LLM reads a webpage and self-registers" — an injection vector cowork-quant must never replicate for anything money-adjacent. Paper-only, so blast radius is contained, but the architecture would be catastrophic if mapped to real funds (unchanged r2 verdict).
- **(c) Genuinely new:** a live, adversarial, multi-agent *social* evaluation environment (signals + followers + monthly challenge with mark-to-market). Nothing in hermes-quant evaluates analysts against external competitors; an optional read-only "publish paper signal to AI-Trader / read peer signals as an ExternalAnalyst stream" adapter remains the right framing (r2's proposed ADR "Peer Signal Ingestion").

---

## 3. HKUDS/Vibe-Trading

**Repo:** <https://github.com/HKUDS/Vibe-Trading> — 8,933 stars, 1,839 forks, pushed **2026-06-09** (multiple commits/day). v0.1.9 on PyPI 2026-06-01. Wiki: <https://vibetrading.wiki/>.

### Delta since 2026-05-24 notes — the big one

The r3 note's §2 ("Research-vs-Execution Boundary... maintained by absence of execution code") is **no longer true**. From the README news log (<https://github.com/HKUDS/Vibe-Trading/blob/main/README.md>):

- **2026-05-29 — Robinhood Agentic Trading (opt-in, bounded autonomy):** live order relay through Robinhood's remote MCP behind OAuth, a **user-committed mandate** (symbol universe / order size / exposure / leverage / daily cap), **filesystem kill switch**, preemptive flatten, mandate auto-expiry, **full audit ledger**, persistent autonomous runner. "Off and read-only by default."
- **2026-06-02 — six broker connectors** (Tiger / Longbridge / Alpaca / OKX / Binance / Futu): read-only + paper order placement for all; five support "**bounded, mandate-gated order placement** behind the same safety model"; Longbridge capped at paper+read-only because "a broker with no structural paper/live guard is capped at paper + read-only."
- **2026-06-05 — Dhan + Shoonya (India), 10 brokers total**, same structural-guard rule; their `place_order` "hard-refuse any non-paper config at the first line."

Other significant additions since the notes:

- **Alpha Zoo v1 (2026-05-17):** 452 pre-built alphas (qlib158, Kakushadze alpha101, GTJA 191, academic factors) with AST purity gate, lookahead-guard test, `pytest-socket` network kill-switch; `alpha bench` CLI. **2026-05-28:** `run_bench_strict()` adds a same-universe **random control + OOS split** "to catch factors that just track market beta." **2026-06-06:** `alpha compare` head-to-head IC/IR ranking across CLI/Web/REST/agent-tool (47 read-only agent tools now).
- **Research Goal runtime (2026-05-24/26):** session-scoped goals persisting claims, acceptance criteria, evidence rows, budgets, completion policy; "blocked live-trading risk tiers through agent tools."
- **Hypothesis Registry CLI (2026-05-20)**; **Trust-Layer run card surfaced in the run UI (2026-05-15)**; **tool-call trace `call_id` correlation for replay (2026-06-03)**; **fsync'd session JSONL writes (2026-05-30)**; **swarm DAG blocks downstream tasks on upstream failure (2026-05-28)**; **live swarm status cards incl. "investment committee / quant desk / risk committee" presets (2026-06-07/08)**; opt-in local data cache with a staleness guard that "never caches a range ending today (its last bar is still forming)" (2026-06-04) — a subtle lookahead/recency guard worth copying.

### Lens

- **(a) Port (highest-value source in this survey):**
  - The **mandate object** (symbols/size/exposure/leverage/daily-cap + auto-expiry) as cowork-quant's HITL contract: the human commits a mandate once via a Cowork elicitation, deterministic scripts enforce it, and the *only* thing the LLM can do is propose intents inside it. Maps cleanly onto hermes ADR-0004/0015.
  - **"No structural paper/live guard ⇒ capped at paper+read-only"** as a written connector-admission rule for any future broker MCP cowork-quant might read from.
  - `run_bench_strict()`'s **random-control + OOS split** for cowork-quant's analyst evaluation script (does the analyst beat a same-universe random signal out-of-sample?).
  - Run cards + Hypothesis Registry + Research Goal ledger were already flagged in r3; they are now battle-tested with a CLI/Web surface — port the schemas as-is.
  - Trace entries carrying `call_id` so tool_result ↔ tool_call replay correlation works — adopt in cowork-quant's audit JSONL.
- **(b) Now exhibits (new since May):** an execution surface exists. It is *not* free-text→money (mandate-gated, fail-closed, audited, kill-switched) and arguably the best-engineered open-source version of "LLM proposes / deterministic code disposes" — but the autonomous runner means **no per-trade human confirmation** inside an active mandate, which is weaker than hermes-quant's per-action HITL. Label: boundary moved from "structurally impossible" to "policy-bounded." cowork-quant stays advisor/paper-only regardless.
- **(c) Genuinely new:** Alpha Zoo + strict benching (evaluation), Research Goal evidence ledger (memory-with-acceptance-criteria), swarm committee presets with live per-worker status (debate/orchestration UX). All three are things hermes-quant lacks today.

---

## 4. moon-dev-ai-agents (moondevonyt / yolojewjitsu)

**Status: canonical repo gone.** `https://api.github.com/repos/moondevonyt/moon-dev-ai-agents` returns 404 (checked 2026-06-09); the account `https://github.com/moondevonyt` now hosts `Hyperliquid-Data-Layer-API` (pushed 2026-06-05), `Limitless-Prediction-Market-Bots` (2026-04-28), housecoin DCA bots, etc. — no trading-agents monorepo. The mirror in our notes, `yolojewjitsu/moon-dev-ai-agents` (<https://github.com/yolojewjitsu/moon-dev-ai-agents>), is **stale since 2025-10-20** (100 stars / 1,639 forks — fork count inherited from the original network). Other community mirrors: `daydy-dev/moon-dev-ai-agents-for-trading` (225 stars, frozen Jan 2025, <https://github.com/daydy-dev/moon-dev-ai-agents-for-trading>). The original open-sourcing announcement survives on X (<https://x.com/MoonDevOnYT/status/1980285117705998827>).

### Delta & lens

- **Delta:** the project was taken down or made private sometime after Oct 2025; development energy moved to per-exchange bot repos (Hyperliquid, Limitless prediction markets). No new architecture to study; the r6 anti-pattern catalog (`risk_agent.py:319` — LLM substring-match override of the loss limit) is now only reproducible from forks.
- **(a) Port:** nothing (unchanged r6 verdict). Action item: **vendor a snapshot of the r6-cited files from a fork into cowork-quant's test fixtures** so the "worst single pattern" example used in docs/tests doesn't rot when forks disappear too.
- **(b) Still exhibits (as archived):** all six — LLM as final execution authority, free-text→money, string-grep contracts, no HITL, no audit trail, no lookahead discipline.
- **(c) New:** none.

---

## 5. virattt/ai-hedge-fund

**Repo:** <https://github.com/virattt/ai-hedge-fund> — 59,584 stars, 10,520 forks, pushed **2026-06-09**. CalVer releases (2026.6.9 on 2026-06-09).

### Delta since 2026-05-24 notes

(Not covered by a dedicated May note, so delta vs. general baseline.) From commits (<https://github.com/virattt/ai-hedge-fund/commits/main>):

- **`Add backtester`** (2026-05-14) and **`Build event study engine`** (2026-05-05) + PEAD (post-earnings-announcement-drift) work culminating in `Remove PEAD-specific context from Trade data class` (2026-05-19) — the project finally has an in-repo evaluation loop instead of vibes-only agent reports, and is experimenting with event studies as the evaluation primitive (closer to real quant methodology than equity-curve screenshots).
- **`Add MIT LICENSE file`** (merged 2026-06-08, PR #645) — until last week the most-starred LLM-trading repo had **no license file**, a non-trivial legal point for anyone who vendored it.
- Model churn: `Add opus 4.8` (2026-05-28), `Add Fable 5` (2026-06-09) — same-day adoption of new frontier models; release cadence is now CalVer bumps (`2026.5.9`, `2026.5.14`, `2026.6.9`) via a release script (2026-05-09).
- Architecture unchanged: 19 agents — 13 investor personas (Damodaran, Graham, Ackman, Wood, Munger, Burry, Pabrai, Taleb, Lynch, Fisher, Jhunjhunwala, Druckenmiller, Buffett) + 4 functional analysts + Risk Manager + Portfolio Manager; "the system does not actually make any trades" (README, <https://github.com/virattt/ai-hedge-fund/blob/main/README.md>).

### Lens

- **(a) Port:** the **persona-investor committee** remains the best UX idea for a Cowork chat surface — named personas with stable philosophies make disagreement legible to a human reviewer ("Burry says short, Wood says buy, here's why"). Map onto cowork-quant analyst subagents as optional *persona skins over the same typed AnalystView contract* (personas affect rationale style, never the schema). Also port the **event-study engine** concept: a deterministic script that measures abnormal return around evidence events (earnings) is a better per-analyst scorecard than raw P&L for an advisor product.
- **(b) Still exhibits:** Portfolio Manager LLM is the final decision authority (simulated); risk manager outputs position limits but they are LLM-computed, not a deterministic gate; no HITL; no append-only audit trail; signals are JSON-ish dicts passed through LangGraph state (better than string-grep, weaker than frozen dataclasses); educational/paper-only so no live money path.
- **(c) New:** event-study evaluation; nothing on memory or debate (personas don't debate — they vote in parallel, the PM weighs).

---

## 6. FinMem & FutureSim — memory / evidence-store architectures

### FinMem

- **Paper:** arXiv 2311.13743, v2, no revision since the baseline (submitted 23 Nov 2023; <https://arxiv.org/abs/2311.13743>; HF: <https://hf.co/papers/2311.13743>). **Repo:** `pipiku915/FinMem-LLM-StockTrading` — 907 stars, **last pushed 2024-08-18, effectively dead** (<https://github.com/pipiku915/FinMem-LLM-StockTrading>).
- The lineage moved on: the same group (TheFinAI / Stevens) shipped **InvestorBench** (arXiv 2412.18174, <https://hf.co/papers/2412.18174>) and now **Agent Market Arena** ("When Agents Trade: Live Multi-Market Trading Benchmark for LLM Agents", arXiv 2510.11695, <https://hf.co/papers/2510.11695>), a "lifelong real-time benchmark" comparing InvestorAgent/TradeAgent/HedgeFundAgent/DeepFundAgent architectures across crypto + stocks — finding *agent framework matters more than model backbone*. Repo seed: <https://github.com/The-FinAI/Agent_Market_Arena>.
- **Lens:** (a) the durable FinMem idea is **layered memory with per-layer decay rates** (working/episodic/semantic, slower decay for deeper layers) + retrieval scored by recency×relevance×importance. For cowork-quant: implement as a deterministic script over the evidence store / committee journal — decay is a query-time scoring function over `available_at`-stamped records, not a vector DB. (b) FinMem's own pipeline is single-agent LLM-decides-trades, no gate — don't copy the decision layer. (c) AMA's "framework > backbone" result is the citable justification for cowork-quant investing in process (committee + gate) over model choice.

### FutureSim

- **Paper:** "FutureSim: Replaying World Events to Evaluate Adaptive Agents", arXiv 2605.15188, **v1 submitted 14 May 2026** — published *after* our r4 note was written from a preprint/draft; no v2 yet (<https://arxiv.org/abs/2605.15188>; HF: <https://huggingface.co/papers/2605.15188>). Abstract confirms the r4 mechanics: agents forecast events beyond knowledge cutoff inside a chronological replay (real news arriving day-by-day, questions resolving), evaluated *in their native harness*.
- **Delta:** the r4 evidence-store design (three-timestamp model, `available_at` contract, DuckDB+JSONL, CI lookahead gate) needs **no revision** — the published paper matches. New context: FutureSim now sits in a family of contamination-resistant temporal benchmarks (e.g., Look-Ahead-Bench for point-in-time LLM finance, arXiv 2601.13770, <https://arxiv.org/pdf/2601.13770>), and Vibe-Trading's cache staleness-guard ("never cache a range ending today") is a small production instance of the same invariant.
- **Lens:** (a) port the r4 `EvidenceRecord` schema into cowork-quant as the deterministic script layer backing the news/sentiment analyst skill, with `test_analyst_never_reads_future_evidence` as a release-blocking pytest; (b) n/a (benchmark, not a trader); (c) "evaluate the agent in its native harness" is new methodological cover for evaluating cowork-quant *as a Cowork plugin* (skills + subagents end-to-end) rather than unit-testing prompts.

---

## Port / Reject / Watch table

| Source | PORT into cowork-quant | REJECT (anti-pattern, with which of the rejected list) | WATCH |
|---|---|---|---|
| TradingAgents | Verified market-data snapshot (LLM forbidden to invent numerics); 5-tier advisory rating (display only); outcome-grounded decision log; deep/quick two-model tier; count-based debate termination in routing | `FINAL TRANSACTION PROPOSAL` string-grep contract; free-text `position_sizing` (free-text→money); PM-LLM as terminal authority (no HITL, no deterministic gate) | v0.2.6 structured-output spread to remaining analysts; paper v8; TradingAgents-CN divergence |
| AI-Trader | Drawdown-first leaderboard for analyst ranking; signed/verified analyst identity; `strategy`/`operation`/`discussion` signal-type split | One-message SKILL.md self-registration (prompt-injection surface); blind 1:1 copy-trading (no HITL); token god-keys | Monthly challenge results as a free external eval venue (read-only adapter, v0.4+) |
| Vibe-Trading | **Mandate object** (universe/size/exposure/daily-cap + expiry + fail-closed gate + kill switch + audit ledger); connector-admission rule ("no structural paper/live guard ⇒ paper-only"); run cards; Hypothesis Registry; Research Goal evidence ledger; `run_bench_strict` random-control+OOS; `call_id` trace correlation; cache staleness guard | Autonomous runner = no per-trade HITL inside an active mandate (cowork-quant keeps per-proposal confirmation); 47-tool surface breadth (keep cowork-quant tool surface minimal) | Whether mandate-gated live trading causes an incident; Alpha Zoo licensing churn; swarm preset evolution |
| moon-dev | Nothing; vendor fork snapshots as anti-pattern test fixtures before they vanish | Everything (all six rejected anti-patterns, esp. `risk_agent.py:319` LLM override of loss limit) | Author's new Hyperliquid/prediction-market repos for repeat patterns |
| ai-hedge-fund | Persona-skin committee UX over typed contracts; event-study engine as analyst scorecard; CalVer + release script discipline | PM-LLM final authority; LLM-computed "risk limits" (not a gate); no audit trail | Whether the backtester grows walk-forward/OOS discipline; license now MIT so vendoring is clean |
| FinMem / AMA | Layered memory w/ per-layer decay as deterministic query-time scoring over the evidence store | FinMem single-LLM trade decision loop | Agent Market Arena as standing external eval; InvestorBench lineage |
| FutureSim | r4 evidence-store schema unchanged; `available_at` CI release-blocker; native-harness evaluation framing | — | v2 / leaderboard releases; Look-Ahead-Bench (2601.13770) adoption |

---

## New entrants since mid-2025 (with real traction)

| Project | Traction (2026-06-09) | What it is / why it matters | URL |
|---|---|---|---|
| **nof1 Alpha Arena** | Season 1/1.5 done; **$15M raise (May 2026)** led by SUI Group & Karatage; Season 2 in prep with web search + multi-step execution; consumer "coding agents for markets" planned after S2 | Real-money ($10k/model) autonomous LLM crypto-perp trading on Hyperliquid; only 6 of 32 model-runs finished profitable — the strongest public evidence that raw LLM trading loses money, i.e., the advisor-not-trader posture is empirically backed | <https://nof1.ai/> ; funding: <https://www.finsmes.com/2026/05/nof1-raises-15m-in-funding.html> ; results recap: <https://www.iweaver.ai/blog/alpha-arena-ai-trading-season-1-results/> |
| **ValueCell** | 10,791 stars (created 2025-09); push paused since 2026-03 | Community multi-agent platform for financial applications; fast star growth, now possibly stalling — a cautionary traction-vs-maintenance data point | <https://github.com/ValueCell-ai/valuecell> |
| **ContestTrade** | 649 stars; paper arXiv 2508.00554 | Multi-agent system with **internal contest mechanism**: agents compete, a real-time ranking allocates influence — an alternative to BMA-style aggregation worth a design note (deterministic scoring of analysts → weight) | <https://github.com/FinStep-AI/ContestTrade> ; <https://hf.co/papers/2508.00554> |
| **QuantAgent** | Paper (Sep 2025), 16 HF upvotes; many community reimplementations | Price-driven multi-agent LLM for HFT (indicator/pattern/trend/risk agents); claims beating rule-based baselines on BTC/Nasdaq futures; no canonical repo — treat as paper-only | <https://hf.co/papers/2509.09995> |
| **StockBench** | Paper (Oct 2025), 57 HF upvotes | Contamination-free benchmark: can LLM agents trade profitably with daily signals? (GPT-5, Claude-4, Qwen3, Kimi-K2 vs buy-and-hold) — candidate harness for evaluating cowork-quant's committee offline | <https://hf.co/papers/2510.02209> |
| **Agent Market Arena (AMA)** | Paper (Oct 2025); TheFinAI | Lifelong live multi-market benchmark; key finding: **agent architecture beats model backbone** | <https://hf.co/papers/2510.11695> ; <https://github.com/The-FinAI/Agent_Market_Arena> |
| **LiveTradeBench (ulab-uiuc/live-trade-bench)** | 159 stars, pushed 2026-02-17 | Live evaluation of trading agents (UIUC) — smaller, open, self-hostable arena | <https://github.com/ulab-uiuc/live-trade-bench> |
| **ATLAS** | Paper (Oct 2025) | Adaptive prompt optimization (Adaptive-OPRO) + order-aware action space for trading agents — first serious *prompt-as-tunable-parameter* work in this space | <https://hf.co/papers/2510.15949> |
| **microsoft/RD-Agent** | 13,397 stars, pushed 2026-06-05 | Automates quant factor/model R&D loops (RD-Agent(Q)); adjacent to Vibe-Trading's Alpha Zoo — the "LLM proposes factors, deterministic backtest disposes" pattern at industrial scale | <https://github.com/microsoft/RD-Agent> |
| **TradingAgents-CN** | 28,163 stars | Fork exceeding most originals; signals where the user demand actually is (CN retail, web UI, local models) | <https://github.com/hsliuping/TradingAgents-CN> |
| **PolyBench** | 11 stars, 2026-03 | Contamination-proof LLM prediction-market benchmark — small but on-theme for Polymarket-style evidence resolution | <https://github.com/PolyBench/PolyBench> |

**Trend read:** (1) evaluation/arena infrastructure is where the field moved in late 2025–2026 — every credible project now has a live or contamination-free eval story; (2) the execution boundary is being re-drawn industry-wide from "never" to "mandate-bounded autonomy" (Vibe-Trading + Robinhood Agentic Trading is the bellwether); (3) structured output is slowly replacing prose handoffs even in the projects our notes criticized; (4) the empirical record (Alpha Arena S1) supports the advisor-only posture.

---

## Implications for cowork-quant v0.1–v0.4

**v0.1 (committee MVP — skills/commands/subagents + deterministic scripts):**
- Adopt TradingAgents' *verified market-data snapshot* as a hard rule: a `scripts/market_snapshot.py` produces the only numerics any analyst subagent may cite; CI greps reports for out-of-snapshot numbers. (<https://github.com/TauricResearch/TradingAgents/commits/main>)
- Typed `AnalystView` JSON contract from day one (no prose handoffs, no string-grep); count-based debate termination lives in the orchestrating command, not prompts (r1/r5 lesson, unchanged).
- HITL: every proposal renders as a card requiring explicit user confirmation in Cowork; nothing auto-advances. Anti-pattern fixtures vendored from a moon-dev fork for the test suite (do this *now*, before forks rot).

**v0.2 (evidence + audit):**
- Implement the r4 `EvidenceRecord` store (SQLite/DuckDB + JSONL) with `available_at` and the `test_analyst_never_reads_future_evidence` release blocker — FutureSim v1 is now published and citable (<https://arxiv.org/abs/2605.15188>).
- Port Vibe-Trading's run card schema + `call_id`-correlated tool-call traces into every committee run directory (<https://github.com/HKUDS/Vibe-Trading/blob/main/README.md>, 2026-05-12 / 2026-06-03 entries).
- Add the outcome-grounded decision log (TradingAgents v0.2.4 pattern): resolve prior advisory calls with realized return + alpha and surface them at the top of the next committee run.

**v0.3 (evaluation):**
- Analyst scorecards: event-study abnormal returns (ai-hedge-fund pattern, <https://github.com/virattt/ai-hedge-fund/commits/main>) + Vibe-Trading's `run_bench_strict` random-control/OOS discipline; drawdown-first leaderboard (AI-Trader pattern). All deterministic scripts; LLM never grades itself.
- Consider ContestTrade-style influence weighting: analyst weight in aggregation = deterministic function of trailing scorecard, not LLM judgment (<https://hf.co/papers/2508.00554>).
- Layered-memory decay (FinMem) as a scoring function over the evidence store for "what should the committee remember."

**v0.4 (external surfaces — still advisor/paper-only):**
- Optional read-only arena adapters: publish paper signals to AI-Trader's challenge track / ingest peer signals as `ExternalAnalyst` evidence (never to execution); evaluate on StockBench/LiveTradeBench harnesses for credibility.
- If any broker-read connector is ever added, adopt Vibe-Trading's admission rule verbatim: *no structural paper/live discriminator in the broker API ⇒ read-only forever* — and regardless, cowork-quant never places orders; the mandate-gate pattern is studied as prior art, not implemented as autonomy.

**Posture confirmation:** nothing found in this survey weakens the charter. The best-funded experiment (Alpha Arena) showed frontier LLMs mostly lose real money trading autonomously; the most safety-engineered framework (Vibe-Trading) still needed mandates, kill switches, and audit ledgers the moment it touched a live broker; and the cautionary example (moon-dev) literally disappeared. "LLM proposes, deterministic code disposes, human decides" remains the defensible position — now with three more empirical citations.
