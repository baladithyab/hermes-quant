# R-2026-06-12 — Full re-survey + architecture-refinement inputs

> **Method:** five parallel research streams (Cowork subagent fan-out, 2026-06-12) over
> web + arXiv + HuggingFace + live GitHub/HF API pulls + docs.claude.com. Every claim
> carries a URL or arXiv ID. This note is the *evidence base*; decisions and specs live in
> the sibling doc `docs/2026-06-12-v0.2-architecture-refinement.md`.
>
> **Relationship to prior notes:** supersedes nothing — extends the three 2026-06-09 notes
> (`r-llm-trading-frameworks`, `r-sota-llm-agents-markets`, `r-foundation-models`). The
> 3-day window means few *project-code* deltas; the real movement is in the **literature**
> (a debate-failure cluster, an evaluation correction with hard numbers, a determinism/
> governance subfield) and in **platform mechanics** (Cowork hooks/artifacts/scheduling).
>
> **Headline:** nothing weakens the charter. The new evidence *tightens* it and hands us a
> falsifiable name for the thesis (Coordination Primacy), a quantified worst-case attack
> surface (ledger poisoning), and a platform lever (PreToolUse deny) that converts our
> strongest rail from prompt-discipline into platform-enforced invariant.

---

## 0. Data-hygiene corrections to the 2026-06-09 notes

- **Commit dates off by days.** The 06-09 framework note cited June-dated commits for
  TradingAgents (structured Sentiment Analyst, verified-snapshot) and Vibe-Trading
  (Dhan/Shoonya brokers, swarm presets). Fresh API pulls (2026-06-12) show
  `pushed_at` = **2026-05-17** (TradingAgents) and **2026-05-28** (Vibe-Trading). The
  *substance* of those entries stands; treat the specific June dates as uncertain.
- **Trading-R1 mis-dated.** It is **arXiv 2509.11420 (14 Sep 2025)**, a *single* financially-
  aware reasoning model (SFT+RL curriculum), **not** a Jan-2026 multi-agent report. The
  "Terminal" code is still unreleased (checked 2026-06-12).
- **ai-hedge-fund license:** API still shows `license: null`; the MIT-merge claim is
  post-snapshot. **Re-verify before vendoring.**
- **AI-Trader star count:** sources disagree (17.1k vs 19.5k); don't cite a number.

---

## 1. Frameworks & committee architecture (stream A)

### 1.1 Project deltas
- **TradingAgents** static this window (v0.2.5, 2026-05-11). `FINAL TRANSACTION PROPOSAL`
  string-grep contract + free-text sizing in `trader.py` unchanged — still the canonical
  anti-pattern. **Trading-R1** (2509.11420) is the real signal: a model trained to emit
  *disciplined, evidence-grounded, volatility-aware theses* — i.e. the **output our
  committee+gate already target without training a model**. Validates posture; changes no rail.
- **Vibe-Trading** static (mandate object, fail-closed gate, kill switch, audit ledger,
  Alpha Zoo strict-bench, Research-Goal ledger, cache staleness-guard remain the best
  deterministic-gate prior art in the wild). No new boundary move.
- **AI-Trader** still markets one-message `SKILL.md` self-registration for Claude Code/Cursor
  — a prompt-injection-shaped surface ([repo](https://github.com/HKUDS/AI-Trader)) we must
  never replicate for money-adjacent flows.
- **ai-hedge-fund** confirmed backtester + event-study engine; 19-persona vote → PM-LLM still
  terminal authority. Persona-committee UX remains the best *legibility* idea.
- **New entrant — QuantaAlpha** (arXiv [2602.07085](https://arxiv.org/abs/2602.07085)):
  evolutionary LLM alpha-mining with **hypothesis↔factor-expression↔code semantic-consistency
  enforcement** + complexity caps. Alpha-zoo direction stays DEFERRED for us (needs compute +
  eval harness), but the *semantic-consistency gate* is a portable idea.
- **Name-collision caution:** QuantAgent (HFT, [2509.09995](https://arxiv.org/abs/2509.09995))
  ≠ QuantAgents (simulated-trading, 2510.04643) ≠ QuantaAlpha (alpha-mining, 2602.07085).

### 1.2 The debate-failure cluster (the most decision-relevant new literature)
Vanilla multi-agent debate (MAD) does **not** beat majority vote; what works is (i) diversity
of initial views and (ii) calibrated-confidence-weighted updates — not more rounds, not bigger
models:
- **Debate or Vote** ([2508.17536](https://arxiv.org/abs/2508.17536)): debate is a *martingale*
  over belief trajectories under homogeneous agents ⇒ cannot improve expected correctness;
  majority voting explains most apparent MAD gains.
- **Demystifying MAD** ([2601.19921](https://arxiv.org/abs/2601.19921)): two lightweight fixes
  that *do* beat both MAD and majority vote — **diversity-aware initialization** + a
  **confidence-modulated protocol**.
- **Can LLM Agents Really Debate?** ([2511.07784](https://arxiv.org/abs/2511.07784)): intrinsic
  reasoning + group diversity dominate; "majority pressure suppresses independent correction."
- **Multi-Agent Teams Hold Experts Back** ([2602.01011](https://arxiv.org/abs/2602.01011)):
  teams underperform their best member by up to 37.6% via "integrative compromise" (averaging
  expert + non-expert) — *worsening with team size*. Nuance: consensus-seeking *improves*
  Byzantine robustness → a real **alignment vs expertise-utilization trade-off**.
- **CP-WBFT** ([2511.10400](https://arxiv.org/abs/2511.10400)): confidence-probe-weighted
  Byzantine-fault-tolerant consensus stays accurate at 85.7% fault rate — confidence-weighted
  aggregation is robust where flat averaging collapses.
- **Spark to Fire** ([2603.04474](https://arxiv.org/abs/2603.04474)): minor errors solidify into
  system-level false consensus via message dependencies; a genealogy-graph governance layer
  raises defense 0.32→0.89.
- **Science of Scaling Agent Systems** ([2512.08296](https://arxiv.org/abs/2512.08296)):
  independent agents amplify errors **17.2×**; centralized coordination contains to 4.4× and
  gives +80.9% on parallelizable financial reasoning, but *all* multi-agent variants degrade
  sequential reasoning 39–70%.
- **AlphaAgents** ([2508.11152](https://arxiv.org/abs/2508.11152), BlackRock authors) is the
  best worked "investment committee" — and embodies the anti-pattern: **"debate until
  consensus"** with a consensus-forcing terminator, homogeneous backbone (GPT-4o ×3), equal
  weighting. Cite as "learn the roles, avoid the termination rule."

### 1.3 Keystone: financial-MAS taxonomy ([2603.27539](https://arxiv.org/abs/2603.27539))
Turns our slogans into falsifiable claims:
- **Coordination Primacy Hypothesis (CPH):** coordination-protocol design drives decision
  quality more than model scaling — the rigorous form of "framework > backbone" (corroborated
  by AMA and the 2512.08296 topology numbers). Explicitly *not yet empirically validated*
  (eval infra doesn't exist) — license to invest in process + eval, not models.
- **Five evaluation failures that can reverse the sign of returns:** look-ahead bias,
  survivorship bias, backtest overfitting, transaction-cost neglect, regime-shift blindness.
- **Coordination Breakeven Spread (CBS):** does coordination add value *net of costs*? — a
  concrete deterministic metric.

### 1.4 Portable mechanisms
- **ContestTrade** ([2508.00554](https://arxiv.org/abs/2508.00554)): selection-by-trailing-
  performance (down-weight/cut poor analysts) > equal-vote averaging → `analyst_weights.py`
  over our Brier ledger.
- **TradingGroup** ([2508.17565](https://arxiv.org/abs/2508.17565)): a deterministic "Hybrid
  Gate" clamps the LLM forecast (our pattern in the wild); last-20-days outcome summary
  injected next run (cheap memory); **transaction-cost-penalized reward** (never score on gross).

---

## 2. Evaluation, honesty, anti-lookahead (stream B)

### 2.1 The 2026 evaluation correction — now with numbers
- **KTD-Fin** ([2605.28359](https://arxiv.org/abs/2605.28359)): on the leakage-controlled
  CSI300 long window (548 days), frontier agents **DO beat buy-and-hold on return**
  (qwen3.6 +85.3%, gpt-5.5 +61.3%, claude-opus-4-7 +58.8% vs index +36.9%). **But** Barra
  attribution shows the edge is "largely passive market and style exposure, with limited
  evidence of persistent stock-selection alpha." Attribution *reorders* the leaderboard
  (claude-opus #1 on α-rank, #4 on raw). **Returns ≠ skill; skill = beta+style.**
- **The Alpha Illusion** ([2605.16895](https://arxiv.org/abs/2605.16895)): reported LLM-agent
  alpha "should not be treated as deployment evidence"; proposes a **P1–P6 reporting protocol**
  (temporal integrity, frictions, counterfactual robustness, calibration, numerical execution,
  multi-agent disaggregation) and explicitly endorses **our** modular architecture (LLMs as
  auditable info interfaces upstream of independent calibration/risk/execution). Quotable:
  *"language confidence is not tradable probability,"* *"narrative reasoning is not numerical
  execution,"* *"model priors may become undisclosed implicit factor exposures."*
- **Profit Mirage / FinLake-Bench / FactFin** ([2510.07920](https://arxiv.org/abs/2510.07920)):
  counterfactual perturbations force causal (not memorized) reasoning.

### 2.2 The existential honesty risk — model-weight lookahead
- **The Memorization Problem** (Lopez-Lira et al., [2504.14765](https://arxiv.org/abs/2504.14765)):
  LLMs have "selective perfect recall" of pre-cutoff data and perform **"motivated reasoning"
  — working backward from memorized outcomes** — which you *cannot distinguish from skill by
  inspecting the rationale*. Pre-cutoff forecast accuracy is high; post-cutoff directional
  accuracy collapses to ~40%.
- **KTD-Fin §4.2:** anonymizing tickers/dates drives a memory-only agent to **0.00% voluntary
  cash** (it literally can't act) — direct proof the edge rides on pretraining memory.
  De-anon probe: frontier "attackers" recover joint ticker+date ≤1.5% (masking certified).
- **LAP test** ([2512.23847](https://arxiv.org/abs/2512.23847)) + **Fake-Date Tests**
  (Bank of Russia, [2601.07992](https://arxiv.org/abs/2601.07992)): no modern LLM passed a
  macro-forecasting lookahead test. **asof-honesty on the data pipeline does nothing against
  this — the leak is in the weights.**
- Consequence: every historical replay / `/retro` / backtest over a pre-cutoff window is
  contaminated by default. **Forward-only paper-trading is the only clean track record.**

### 2.3 Calibration reality (incl. Claude)
- **KalshiBench** ([2512.16030](https://arxiv.org/abs/2512.16030)): all five frontier models
  systematically overconfident; best is **Claude Opus 4.5 at ECE 0.120** (still substantial);
  reasoning-tuned models can be *worse*; only one beats base-rate on Brier Skill Score.
- **FutureSim** ([2605.15188](https://arxiv.org/abs/2605.15188)): replaying Jan–Mar 2026,
  "many models have worse Brier skill than making no prediction at all."

### 2.4 StockBench hard numbers (the right external harness)
March–June 2025, top-20 DJIA, 82 days, 3-run avg ([2510.02209](https://arxiv.org/abs/2510.02209),
[site](https://stockbench.github.io/)): passive +0.4% / −15.2% MDD / 0.0155 Sortino. Above
baseline: Claude-4-Sonnet +2.2% (Sortino 0.0245), Qwen3-235B +2.4%, GLM-4.5 +2.3%, Kimi-K2 +1.9%.
**Below baseline: GPT-5 (+0.3%), DeepSeek-V3, GPT-OSS (negative).** Spreads ±2% over a flat tape
are inside the noise — the *protocol* is the asset, not the leaderboard. Daily-cadence, US
large-cap, contamination-free, open-source → **best self-eval fit** (no porting cost).

---

## 3. Risk, deterministic guardrails, governance, regulation (stream C)

### 3.1 Determinism is now a subfield
- **DFAH / Replayable Financial Agents** ([2601.15322](https://arxiv.org/abs/2601.15322)):
  across 4,700+ runs, **decision-determinism and accuracy are statistically uncorrelated**
  (r = −0.11). "No model achieves both perfect determinism and high accuracy." Tier-1 models
  with **schema-first architectures** hit audit-replay determinism — direct support for our
  Pydantic-at-the-boundary rail. Ships a portfolio-constraints replay benchmark + stress harness.
- **CGAE — Comprehension-Gated Agent Economy** ([2603.15639](https://arxiv.org/abs/2603.15639)):
  gates economic agency on *verified robustness*, not capability, via a **weakest-link gate**;
  *proves* **bounded economic exposure** (max liability is a function of verified robustness,
  not cleverness) + monotonic safety scaling + temporal decay/re-auditing. The closest formal
  theory of "the gate caps exposure regardless of how clever the LLM is."
- **Determinism survey** ([2605.23955](https://arxiv.org/abs/2605.23955)); **Trace-Based
  Assurance** ([2603.18096](https://arxiv.org/abs/2603.18096)): Message-Action Traces + step/
  trace contracts → machine-checkable verdicts, first-violating-step localization, deterministic
  replay, budgeted counterexample search.

### 3.2 The worst attack surface is the ledger
- **TradeTrap** ([2512.02261](https://arxiv.org/abs/2512.02261)) full-text: portfolio/ledger
  attacks **append fabricated position entries with authentic-looking metadata**; persistent
  and cascading. "Pipeline systems are robust to noisy inputs but **highly vulnerable to direct
  corruption of state or memory**." Recommendation: "explicit state verification and cross-module
  consistency checking." Fake-MCP works because the model consumes tool data "without
  cryptographically verifying integrity or provenance."
- **CrAIBench** ([2503.16248](https://arxiv.org/abs/2503.16248)): memory-poisoning of financial
  agents; "prompt-based defenses are insufficient when adversaries corrupt stored context."
- Our hash-chained append-only JSONL removes the *naive* version — **but only if something
  verifies the chain and analysts can't see unverified state.**

### 3.3 Prompt rules don't bind; deterministic enforcement does
- **Institutional AI / Cournot** ([2601.11369](https://arxiv.org/abs/2601.11369)): N=90/condition.
  Institutional regime (public immutable manifest + Oracle/Controller + SHA-256 append-only
  governance log) cut collusion (**Cohen's d = 1.28**, severe 50%→5.6%). **Prompt-only
  "constitution" gave no reliable improvement** — "declarative prohibitions do not bind under
  optimisation pressure." Strongest empirical refutation of "put the risk rules in the prompt."

### 3.4 Guardrail prior art — cost/benefit for us
- **Symbolic Guardrails** ([2604.15579](https://arxiv.org/abs/2604.15579)): of safety
  benchmarks with *specified* policies, **74% are enforceable by symbolic guardrails using
  simple low-cost mechanisms, no utility loss**. Most safety value is cheap symbolic checks.
- **Type-Checked Compliance / Lean 4** ([2604.01483](https://arxiv.org/abs/2604.01483)):
  strongest *endorsement* of "deterministic code owns the gate," but depends on an external
  auto-formalizer + Lean toolchain → **overkill for our 5-rung ladder + handful of caps.**
- **FinHarness** ([2605.27333](https://arxiv.org/abs/2605.27333)) & **LLM-Gated FinRL**
  ([rs-9837922](https://www.researchsquare.com/article/rs-9837922/v1)): published instances of a
  **deterministic pre-gate screener** (Critical Decision Detector fires on extreme RSI, elevated
  vol, large drawdown, momentum reversal, indicator disagreement, excessive turnover).
- LLM-based screeners (AgentDoG [2601.18491](https://arxiv.org/abs/2601.18491), ToolSafe
  [2601.10156](https://arxiv.org/abs/2601.10156), SafePred [2602.01725](https://arxiv.org/abs/2602.01725))
  are *categorizers, never our gate* (rail #2).

### 3.5 Regulation (sharper)
- **IOSCO Supervisory Toolkit** (FR/02/2026, [IOSCOPD823](https://www.iosco.org/library/pubdocs/pdf/IOSCOPD823.pdf)):
  four focus areas — **Governance & Risk Mgmt, Third-party/Outsourcing, Disclosure,
  Recordkeeping & Reporting**; full AI lifecycle, explicit "Agentic AI"; stress on **continuous
  testing**.
- **FINRA 2026** ([Debevoise](https://www.debevoisedatablog.com/2025/12/11/finras-2026-regulatory-oversight-report-continued-focus-on-generative-ai-and-emerging-agent-based-risks/)):
  top concern = **"AI agents acting autonomously with no human in the loop"**; expects written
  governance, **audit trails**, monitoring, recordkeeping for AI-driven decisions.
- **SEC 2026 exam priorities:** adequate policies to supervise AI use.
- Net: **HITL + append-only audit + recordkeeping + continuous testing + documented governance**
  is the baseline. Our rails (#2, #4, #7) are *ahead* — but only if we produce the *artifacts*
  (verified logs, replay, drill evidence) an examiner asks for.

---

## 4. Foundation models & data layer (stream D)

### 4.1 Model landscape (stable; one cautionary new paper)
- **Kronos/Kairos** byte-identical since 06-09; Kairos remains the only finance-evidenced
  family (crypto h30 Rank-IC +0.076 / ICIR +0.484, honest about baselines). `kairos-serve`
  `/predict` takes `{symbol, market_type, freq, bars[...]}`, does not fetch data itself.
- **TimeGPT-2.1** (Dec 10 2025): first **multivariate** in the family, **reduced minimum-sample
  requirement** (helps cold-start), one-line **on-prem / SOC-2-ready** (neutralizes privacy +
  cold-start objections). Still private preview/waitlist.
- **BigQuery `AI.FORECAST`** now supports **TimesFM 2.5** + returns 10 quantiles — but
  univariate-per-series and OHLCV must transit BigQuery (plumbing-heavy; low priority).
- **Chronos-2 finance paper** ([2605.21504](https://arxiv.org/abs/2605.21504)): MV beats UV on
  **RMSE/MAPE** for Mag-7 + Treasuries — but **RMSE on price levels is not directional alpha**
  (a naive persistence forecast wins RMSE on a random walk). Transferable finding: **mixing
  equities + rates *degraded* accuracy** ("noisy context degrades") → feed only *related*
  covariates. Cite as the cautionary "benchmark wins ≠ alpha" example, not a green light.

### 4.2 FoundationModelAnalyst design (the durable asset is the interface)
- **Wire contract:** request `{symbol, asset_class, timeframe, horizon, asof, bars[closed only]}`;
  two response shapes — **(A) sample paths** (Kronos/Kairos wrapper must expose the *pre-mean*
  per-path terminal returns, since `predict()` averages internally) or **(B) quantiles**
  (TimeGPT/BigQuery).
- **Local AnalystView math (deterministic, plugin-side):** direction = sign(median r) with a
  per-timeframe dead-band; magnitude = clip(|median r|) (a *forecast* size, not a position
  size); confidence = path-agreement `2·max(p_up,p_dn)−1` **or** quantile-sharpness
  `1/(1+k·spread)`, **clamped [0.30, 0.85]** (ADR-0018 D3), then cold-start shrinkage toward
  the floor until N₀≈30–50 settled outcomes exist.
- **Abstain-on-error** (timeout / non-200 / schema-invalid / asof-echo mismatch / NaN /
  weights_rev not in pinned allow-list) → analyst omitted from `aggregate()`; **never substitute
  a model** (contrast: junshunG silent Kronos→GBM fallback). Same rule at the data seam: never
  synthesize bars to fill a gap.
- **Rolling-IC + negative-edge kill-switch lives plugin-side** keyed by
  `(model_id, asset_class, timeframe, horizon)` (backends are stateless).
- **Recommendation:** PRIMARY = self-hosted Kronos-small/Kairos-base `/predict` pinned to
  SHA + weights-rev, exposing the sample cube; OPTIONAL = TimeGPT-2.x (self-host or OFF by
  default). Defer Chronos-2/FinCast to a torch-capable worker.

### 4.3 Data layer (closes the gap the 06-09 note left open)
| Need | Source | Keyless | asof discipline | Note |
|---|---|---|---|---|
| Crypto OHLCV | **CoinGecko remote MCP** (`mcp.api.coingecko.com/mcp`) | Yes (Beta/shared limit) | closed candles only; drop forming bar | hosted → sidesteps sandbox egress |
| Equity quotes/bars | **yfinance demoted to best-effort** | n/a | closed bars only; abstain on 429 | scraping; rate-limited/IP-banned from shared egress |
| Fundamentals / 8-K catalysts | **SEC EDGAR MCP** (stefanoamorelli) | Yes (UA string) | gate on `accepted_ts ≤ asof` | **best asof story**; **AGPL-3.0 → call as external process, never vendor** |
| Options chains / positions | **read-only broker MCP tools** | needs auth | snapshot at asof | broker MCP also exposes `place_*_order` → **must withhold write tools (rail #4)** |
| News (broad) | advisory context only | — | no timestamp ⇒ inadmissible to a scored view | real-time social needs a daemon → ❌ |

---

## 5. Cowork plugin platform mapping (stream E)

### 5.1 Five platform facts that change v0.2
1. **Commands and skills have merged** — `commands/foo.md` and `skills/foo/SKILL.md` both
   produce `/foo`; the split is now stylistic ([slash-commands](https://code.claude.com/docs/en/slash-commands)).
2. **PreToolUse hooks are a deterministic deny lever** — a plugin-root `hooks.json` can
   `permissionDecision: "deny"` any tool call (incl. `mcp__*broker*__place_*order`) with a reason
   fed back to Claude ([hooks](https://code.claude.com/docs/en/hooks)). **This makes rail #4
   platform-enforced, not prompt-enforced.**
3. **An unattended scheduled `/watch` cannot self-approve** — `AskUserQuestion` "requires user
   interaction and normally blocks in non-interactive mode" unless a hook supplies the answer.
   It *stalls* (fail-closed), it does not invent approval.
4. **The dashboard is currently a static snapshot, not a live artifact** — and Cowork **live
   artifacts use connectors WITHOUT asking** ([live artifacts](https://support.claude.com/en/articles/14729249-use-live-artifacts-in-claude-cowork)).
   A live dashboard is only safe bound to **read-only** sources.
5. **Installed plugins cannot reference files outside their directory** — validates "reference
   hermes-quant by URL only" and dictates `quant-state/` lives in the **user's workspace folder**
   (also: not the plugin data dir, which is wiped on uninstall — the ledger must outlive the plugin)
   ([plugins-reference](https://code.claude.com/docs/en/plugins-reference)).

### 5.2 Mapping table (condensed)
| hermes-quant construct | Cowork primitive | Gotcha |
|---|---|---|
| Sidecar daemon + tick loop | **local Desktop scheduled task** running `/watch` | cloud routines have no local-file access; computer-asleep ⇒ skipped + 1 catch-up → must self-gate on `asof` |
| Hermes cron | `mcp__scheduled-tasks__*` via `/schedule` | each create user-approved; 1-min min |
| Signal bus (signals.jsonl/ticks.db) | append-only files in workspace `quant-state/` + `quantcore.cli` seam | ledger *is* the bus; hash-chain = tamper evidence |
| Plugin read-only tools | slash-commands shelling `quantcore.cli` (JSON in/out) | optionally a bundled MCP later |
| LLM committee stages | in-session Claude + `analysts` skill + bull/bear/risk-skeptic subagents | plugin subagents can't self-scope MCP/hooks/permission — security via `tools:` allow-list only |
| Gate / sizing / ledger | bundled `scripts/quantcore/` via Bash, path via `${CLAUDE_PLUGIN_ROOT}` | cache the install in the plugin persistent data dir |
| `~/.hermes/quant` state | `<workspace>/quant-state/` | user-visible = audit feature |
| optional-MCP registry | `.mcp.json` (auto-start, per-server approval) | keep every server read-only |
| HITL approve | `AskUserQuestion` + deterministic `decide`/`fill` CLI | blocks unattended (safety property) |
| Live status surface | **v0.2: live artifact** (`create_artifact`) read-only | static `/dashboard` stays the safe default |
| Env FLAGS | `quant-state/config.json` validated by `quantcore.config` | default-OFF at config layer, not prompt |

### 5.3 What Cowork makes easier / harder
- **Easier:** no process lifecycle; native HITL; **a deterministic deny hook the daemon never
  had**; fail-closed unattended turns; one-file distribution; user-visible auditable state.
- **Harder/impossible (with substitution):** no continuous presence (interday only — already
  ADR-0083); cadence floor 1-min + best-effort on sleep (→ asof-gating + 24h TTL); no shared RAM
  (→ files + replay); plugin subagents can't self-scope (→ `tools:` allow-list + plugin-root
  hook); no reach outside plugin dir (→ bundle everything, state in workspace); in-sandbox
  `requests.get` may be egress-limited (→ prefer MCP data servers).

---

## 6. Consolidated "what to build" — pointer

The 25+ recommendations across streams converge into the v0.2 plan in
`docs/2026-06-12-v0.2-architecture-refinement.md`. The highest-leverage, lowest-regret set:

1. **Leakage-masked replay as the only sanctioned backtest mode** (KTD-Fin) + forward-only
   gold standard — the one control without which no eval number is interpretable.
2. **Alpha-after-attribution (Barra-lite)** in the ledger — stop the gate rewarding a beta-loader.
3. **Ledger integrity verifier + cross-module consistency check** (TradeTrap) — close the
   empirically-worst attack surface.
4. **Confidence × track-record weighted, regime-conditioned aggregation** (debate cluster) —
   the only debate intervention shown to beat majority vote, in deterministic form.
5. **Engineered dissent + deterministic (not consensus-forced) termination**; dissent biases the
   gate toward smaller size / silence.
6. **Deterministic pre-gate risk screener** (Safiron/FinHarness) — concentration / turnover /
   regime-mismatch / staleness, reason-coded; flags & down-ranks, never a second gate.
7. **PreToolUse deny-hook + `/watch` `disallowed-tools: AskUserQuestion`** — platform-enforced
   rail #4 and no-self-approval.
8. **Immutable gate-config manifest digest-stamped into the ledger** (Institutional AI) +
   determinism-replay test (DFAH) — examiner-grade governance.
9. **Stay on hypothesis property tests, not Lean 4** (cost/benefit) — add an invariant-coverage
   manifest for the "what's proven" property cheaply.
10. **FoundationModelAnalyst interface first, backend second**; data layer = CoinGecko + SEC
    EDGAR MCPs with strict asof; rolling-IC kill-switch before any FM voice gets weight.

---

## 7. Master source index (new/deepened this pass)

**Committee/debate:** 2508.17536 · 2601.19921 · 2511.07784 · 2602.01011 · 2511.10400 ·
2603.04474 · 2512.08296 · 2508.11152 · 2603.27539 (taxonomy/CPH/CBS) · 2508.00554 ContestTrade ·
2508.17565 TradingGroup · 2602.07085 QuantaAlpha · 2509.11420 Trading-R1
**Eval/honesty:** 2605.28359 KTD-Fin · 2605.16895 Alpha Illusion · 2510.07920 Profit Mirage/
FactFin · 2504.14765 Memorization Problem · 2512.16030 KalshiBench · 2605.15188 FutureSim ·
2512.23847 LAP · 2601.07992 Fake-Date · 2510.02209 StockBench · 2603.27539
**Risk/governance:** 2601.15322 DFAH · 2603.15639 CGAE · 2512.02261 TradeTrap · 2503.16248
CrAIBench · 2601.11369 Institutional AI · 2604.15579 Symbolic Guardrails · 2604.01483 Lean4
Type-Checked · 2605.27333 FinHarness · rs-9837922 LLM-Gated FinRL · 2603.18096 Trace Assurance ·
2605.23955 determinism survey · 2601.18491 AgentDoG · 2601.10156 ToolSafe · 2602.01725 SafePred
**Foundation models/data:** Kronos (github.com/shiyu-coder/Kronos) · Kairos
(github.com/Shadowell/Kairos) · TimeGPT-2.1 (nixtla.io/blog/timegpt-2-1-announcement) ·
Chronos-2 finance 2605.21504 · CoinGecko MCP (docs.coingecko.com) · SEC EDGAR MCP
(github.com/stefanoamorelli/sec-edgar-mcp)
**Regulation:** IOSCO FR/02/2026 (IOSCOPD823) · FINRA 2026 (Debevoise) · SEC 2026 (Akin)
**Platform:** code.claude.com/docs/en/{plugins-reference, slash-commands, sub-agents, hooks,
desktop-scheduled-tasks} · support.claude.com live-artifacts / schedule / use-safely

**Two honest gaps:** (a) Alpha Arena Season 2 results not yet published (S1 sizing-failure
lesson stands). (b) IOSCO four-focus-area structure corroborated via multiple law-firm summaries,
not the 100-page PDF directly.
