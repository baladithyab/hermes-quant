# hermes-quant — Canonical Understanding (from the wiki)

> **Purpose:** definitive, citation-backed understanding of the hermes-quant
> algorithmic trading system, distilled from the operator's personal knowledge
> wiki at `/home/codeseys/wiki/`. Feeds a deep-work backlog-resolution loop.
> **Compiled:** 2026-05-30. **Author:** research agent.
>
> Wiki sources read (primary):
> - `projects/hermes-quant.md` (history / session log, the canonical project page)
> - `projects/hermes-quant-architecture-and-gaps.md` (gap tracker)
> - `projects/hermes-quant-research/{architecture-review-2026-05-28, six-model-critique-2026-05-28, v0.6.1-handoff, hkuds-gap-analysis-2026-05-27, tauric-gap-analysis-2026-05-27}.md`
> - `digests/2026-05-24-C6-quant-trading.md`
> - `decisions/{2026-05-26-moa-halt-vs-approve-all, 2026-05-27-tomorrow-prep-readiness-review}.md`
> - `_inbox/` trading items (paper-readiness NO-GO, hindsight-admissibility, regime-gates, baseline-drift, bma-conf, first-paper-fill, wave-d, eod-hang, watchlist-bottleneck, llm-trading-sota, amzn-social-arb, butterfly-graph, vibe-trading)
> - adjacent projects: `projects/{trend-arbitrage-engine, composer-replication-framework, weather-alpha}.md`

---

## 1. What IS hermes-quant?

**One-paragraph definition.** hermes-quant is a **single-operator, paper-only,
LLM-assisted-but-execution-deterministic algorithmic trading system** built as a
**plugin/sidecar inside the operator's "Hermes" agent ecosystem**. It runs an
8-stage daily-cadence pipeline — `universe-scan → watchlist-evolve →
analysts(N) → BMA aggregator (regime-aware) → LLM risk committee (3-way) →
trader (LLM-structured) → deterministic risk gate → proposal/react (paper) →
reflector (post-trade memory)` — across **11+ cron-driven firing surfaces per
trading day**, against an **Alpaca paper account** ($100k equity, options level
3). It solves the problem of *"can a solo operator + agent run a disciplined,
auditable, continuously-analyzing daily stock/options picker that evolves through
self-critique without blowing up?"* — for an **individual operator (Codeseys),
explicitly NOT a fund** (`hermes-quant-architecture-and-gaps.md:145` "~95%
irrelevant for single-user paper trader"; "single user, <50 tickers"). The
**core architectural bet** has four pillars, all load-bearing:
1. **Sidecar / silence-by-default daemon** — `sidecar → JSONL → freqtrade`; the
   system stays silent unless it has a real signal (`hermes-quant.md:29`,
   `digests/2026-05-24-C6-quant-trading.md:161`).
2. **Multi-analyst Bayesian Model Averaging (BMA)** — analysts (ClassicalTA,
   MicrostructureLite, Kronos, FundamentalsAnalyst, SemanticAnalyst) vote; the
   aggregator requires ≥2 distinct analysts (`require_ensemble=True`) and
   silences degenerate single-source unanimity (`decisions/2026-05-26-moa-halt-vs-approve-all.md`).
3. **Deterministic risk gate as FINAL authority** — the LLM committee is
   *evidence, not authority* (ADR-0004; `architecture-review-2026-05-28.md:131`).
4. **Default-OFF, eval-gated rollouts** — every new capability ships behind an
   env flag, default OFF, promoted only after the operator audits a live
   side-by-side (`hermes-quant.md:50`).

**Provenance.** Architecture distilled from open-source LLM-trading codebases:
TauricResearch/TradingAgents (adversarial bull/bear/judge + 3-way risk
committee), HKUDS/Vibe-Trading (Data Grounding, Citation HARD RULE, Shadow
Account, Hypothesis Registry, AlphaZoo), virattt/ai-hedge-fund (trader protocol),
Mai0313 (TraderProposal schema, BM25 memory), Mantshimuli/Mwamba (regime-aware
BMA) — `hermes-quant-architecture-and-gaps.md:75`,
`_inbox/2026-05-27-llm-trading-sota-and-codebases-research-bundle.md`.

**By the numbers (2026-05-28 audit).** ~44,878 production Python LoC / 42,623
test LoC (~0.95:1) across 35 packages; ~2,153 tests; 65+ ADRs; 6 append-only
event stores; 12+ cron jobs; ~28 PRs merged
(`architecture-review-2026-05-28.md:26-45`,
`hermes-quant-architecture-and-gaps.md:42-50`). Version chain (perpetually stale
in the wiki router): v0.1.2 → v0.3 → v0.6.3 → ongoing. As of 2026-05-30 the live
book: **equity ~$105,957 (+5.96%), cash 96%, gross 4.0%, net −1.6%, 45 positions**
— conservative, the caps are biting (`six-model-critique-2026-05-28.md:5`).

---

## 2. What is it SUPPOSED to be — the vision / end-state

The operator's own statements describe a **continuously-running, full-trading-day,
multi-strategy options+equity decision engine that makes money risk-adjusted and
evolves itself through nested self-critique** — paper-perfect-fidelity-first, then
live.

**Vision quotes from the operator (in the wiki):**
- *"See if you can do this consistently everyday and ping me with possible
  stock/options plays."* — the founding directive
  (`digests/2026-05-24-C6-quant-trading.md:27`).
- *"hermes quant is supposed to evolve through self critique and retrospective
  review. Just see that we embed that in."* — self-critique is **architecture, not
  a feature** (`digests/2026-05-24-C6-quant-trading.md:62`, D3 at :163).
- *"review our vision and see if we are able to properly architect and schedule
  our entire pipeline … covered-calls, cash secured puts, options wheels, etc."*
  and *"we want everything armed because we want to make sure our entire system
  works during all hours of the trading day"*
  (`_inbox/2026-05-28-hermes-quant-regime-gates-and-strategy-retro-shipped.md:17-18`).
- *"we want paper trading to be as accurate as live env so we capture issues
  during paper trading rather than in live."* — the **fidelity north-star**
  (`_inbox/2026-05-28-hermes-quant-hindsight-admissibility-six-model-critique.md:21`).
- *"agent-assisted but execution-deterministic. Agents can research / summarize /
  debate / propose … They should not decide the final executable order call."* —
  the PDR (Perception→Decision→Reaction) backbone
  (`_inbox/2026-05-26-hermes-quant-first-paper-fill-audit.md:34`).

**"Next level" / end-state, as the wiki frames it:**
1. **Real multi-leg options execution** — covered calls / CSP / wheel / LEAPS
   that actually FIRE, not just rank. *"The brain knows what trades it wants to
   make … the hands can only buy/sell equity."*
   (`architecture-review-2026-05-28.md:102`). This is repeatedly called **the
   single biggest gap relative to the user's stated vision**.
2. **Paper→live fidelity parity** — a paper book that is honest enough that any
   bug surfaces in paper, not live. The six-model critique reframes this as the
   *true* P0: synthetic shorts, fill realism, exactly-once semantics, borrow-aware
   P&L (`six-model-critique-2026-05-28.md`).
3. **Catalyst / semantic awareness** — fuse probabilistic news/social signal with
   deterministic price signal so the system stops being blind to event catalysts
   (Catalyst Sense, ADR-0073/0074; `hermes-quant.md:110-309`).
4. **Self-improving loop** — three nested retro loops (per-trade postmortem →
   weekly pattern audit → monthly meta-retro with HITL amendment gate) that
   pattern-match *across* analysts/recipes for structural change, not just tune
   confidence numbers (`digests/2026-05-24-C6-quant-trading.md:64-69`).
5. **Then, and only then, live money** — gated behind ADR-0015 charter, a
   re-introduced approval gate, ≥weeks of honest paper data, and benchmark proof
   (STOCKBENCH is the stated north-star benchmark;
   `_inbox/2026-05-27-llm-trading-sota-and-codebases-research-bundle.md:70`).

**Explicit non-vision (NOT a fund, NOT productizable):** single-user paper
trader; HKUDS social/leaderboard features rejected as "~95% irrelevant"; no RL
post-training ("Hermes orchestrates frontier models")
(`hermes-quant-architecture-and-gaps.md:145-147`).

---

## 3. The complete pending-work picture (consolidated)

Every "NOT done (gated)", open-work, deferred, 🚨, blocker, TODO, and roadmap-wave
item across the wiki. Grouped by theme; ✅ items omitted unless they unblock
something. **Format: title — what it is — why it matters — blocker/dependency —
source.**

### 3.A 🚨 CRITICAL — the single biggest gap (multi-leg options)

- **ADR-0029 multi-leg options reactor** — the execution rail for covered
  call / CSP / wheel / LEAPS. *What:* PaperReactor is equity-only; the playbook
  layer scores+ranks+watchlists options plays but **cannot execute them**.
  *Why:* it IS the operator's stated vision; today 22-of-25 universe signals come
  back SHORT and gate-reject as `short_signal_deferred (… swing/leaps long-only
  until ADR-0029 multi-leg reactor lands)`. *Blocker:* integration work (not
  research); Alpaca paper supports options HTTP-direct. Plan exists:
  `~/.hermes/plans/2026-05-28_multi-leg-options-implementation.md` (6 PRs).
  Timeline debate: owner says 3–4 wks; six-model adjudication says **6–8 wks**.
  *Source:* `hermes-quant.md:93`, `architecture-review-2026-05-28.md:21,138,148`,
  `six-model-critique-2026-05-28.md:50`. Also prereq **D0: `OptionLeg`/`NetGreeks`
  (ADR-0028) don't exist yet** (`regime-gates…:91`).
  **NOTE — six-model 6/6 unanimous verdict: do NOT build options next** until the
  paper→live fidelity foundation (§3.B) lands, because the options rail would
  "stack a bigger lie on the unfixed short-book lie"
  (`six-model-critique-2026-05-28.md:7,19`).

### 3.B 🚨 CRITICAL — paper→live fidelity foundation (six-model P0, must precede options)

These are the synthesized P0 layer from the 6-model critique
(`six-model-critique-2026-05-28.md:37-43`) — the *honest* north-star per the
operator's "paper as accurate as live" directive.

- **Pre-trade admissibility engine / ShortabilityOracle** — ETB/HTB/NTB +
  locate/borrow-fee + whole-share/fractional-short enforcement + BPR/Reg-T debit +
  SSR/halt checks → PARTIAL/REJECTED/ACCEPTED. *Why:* 38 synthetic short equity
  positions are **untradeable live** (locate/borrow required); the reflector is
  *learning a reflexive-shorting habit the broker will refuse*; +5.96% is partly
  fiction. **6/6 reviewers flagged this independently.** This is also the
  "admissibility/fill-state foundation" the hindsight audit surfaced as the next
  ADR (`hindsight-admissibility…:54`).
- **Order-lifecycle state machine + fill realism** — `OrderState{PENDING,
  PARTIAL,FILLED,REJECTED}`; paper fills 100% @ one price today; the 2026-05-27
  positions reconciler **has never been exercised** against the partial/reject case
  it exists for (same re-fire family that produced the 880% artifact). 6/6.
- **Exactly-once idempotency + global tick semaphore** — dedup keys
  (`{cron}:{ts}:{symbol}:{hash}`) across all 6 event stores + serialize the 11
  synchronous firing surfaces (no locking today). Root cause per Kimi: "state.db is
  cached corruption" — 880% artifact was reconciled once, not root-fixed.
- **Borrow-aware P&L restatement** — re-run the 38-short book under live
  constraints; produce per-symbol live-valid qty / fee / accept-reject; restate the
  +5.96%. (This is Phase 1 option (b) the hindsight audit offered the operator.)
- **Regime-gate CC/CSP/wheel + reflexive-shorting OFF in BEAR** — *partially
  shipped* (PR #18 play-level regime gates), but six-model says selling premium into
  a downtrend has documented negative expectancy; the deny-on-BEAR is the cheap
  win. Also: **the "defined-risk-only" gate contradicts the vision** — a strict
  defined-risk gate would reject *every* CSP and CC; **pivot to collateral-secured**
  (puts backed by cash/BPR, calls backed by long equity) — Gemini unique catch
  (`six-model-critique-2026-05-28.md:23`).
- **P1 fidelity:** audit-trail timestamp reconciliation across all 45 positions
  (ADR-0068 may have contaminated derived slippage/holding-period/regime fields);
  corporate actions + dividend-on-short accrual + PDT modeling; **intraday-bar
  lookahead audit** (ADR-0069 only fixed the *daily* bar — microstructure_lite may
  still consume still-forming intraday bars).

### 3.C 🟢 Catalyst Sense / social-arbitrage — gated-but-flowing

- **Raise the consumer-trend confidence haircut (currently 0.5)** — *What:*
  `CONSUMER_TREND_CONFIDENCE_HAIRCUT` deliberately weakens brand_self social
  signals. *Blocker:* the profitability loop must clear **MIN_SAMPLE=20 brand_self
  propagations at ≥0.60 hit-rate on LIVE data** before the weight earns more
  influence. *Source:* `hermes-quant.md:305`.
- **Catalyst-driven universe onboarding (ADR-0075, not built)** — *Why:* 4/5
  consumer targets (CELH/CROX/DIIBF/NWL) are NOT in the Alpaca tradeable universe —
  packets are *perceived but un-actable*; DIIBF (OTC) never will be. *Source:*
  `hermes-quant.md:300-307`.
- **Live wiring of social producers into the ingester cron** — Reddit + Google
  Trends producers built (`social.py`) but **Reddit 403-blocks datacenter IPs** —
  production needs script-type OAuth or residential egress. *Source:*
  `hermes-quant.md:208-229`.
- **The actual learned-graph mining job** — the durable propagation-log JSONL
  corpus accrues (491 rows, 3 brand_self) but the job that mines corrected
  edge-signs from forward returns isn't built. *Source:* `hermes-quant.md:226,289`.
- **Profitability cron schedule** — `profitability.py` + runner exist but aren't
  on cron. *Source:* `hermes-quant.md:307`.
- **Larger labeled set at a higher threshold** before consumer-trend weights go
  live — Phase-0 eval was **GATE PASS but knife-edge** (precision exactly 3/5 =
  0.60 = the minimum; TPR −25% and NWL −35% were FALSE POSITIVES). *Source:*
  `hermes-quant.md:196-202,226`.
- **Structured world-event feeds (ADR-0073 Phase 3)** + LLM-tier classifier (the
  documented seam in `classify.py`) — deferred. *Source:* `hermes-quant.md:157`.

### 3.D 🟢 Validation / measurement / overfit discipline

- **Calibrator drift detection** — auto-refit weekly via bootstrap CLI; alert on
  raw→calibrated drift > 5%. **Still open.** *Source:* `hermes-quant.md:90`.
- **C3 Validation suite (MC + Bootstrap CI + Walk-Forward → `validation.json`)** —
  aspired-to per ADR-0006, not plumbed. Wave v0.8. *Source:*
  `hermes-quant-architecture-and-gaps.md:111,195`.
- **Committee-alpha measurement** — Grok argued the LLM committee is an expensive
  wrapper around a simple statistical edge (13.6% gate-pass = mostly filtered
  garbage). Can't measure marginal alpha while the P&L it's judged on is fictional →
  **A/B committee vs lightweight rules engine AFTER honest P&L exists** (P1).
  *Source:* `six-model-critique-2026-05-28.md:30,48`.
- **OOS overfit methodology rule (durable):** the AMZN "30% Sharpe peak" is
  OVERFIT to the 3-year window (IS peak 15%, OOS peak 70%). *Direction robust, point
  is not* — **use a RANGE (15–30%), never optimize to the decimal**; any
  single-name/sleeve weight must be re-derived IS→OOS before sizing. *Source:*
  `hermes-quant.md:254-263`, `_inbox/2026-05-30-amzn-social-arb…:21-45`.
- **PMCC shadow EOD mark cron** — recommended cheap next step: a daily cron to
  mark the AMZN PMCC shadow so a counterfactual track record accrues *before* the
  multi-leg reactor ships. *Source:* `_inbox/2026-05-30-amzn-social-arb…:59`.

### 3.E 🟡 HKUDS/Tauric gap-matrix backlog (roadmap waves v0.7–v0.9)

From `hermes-quant-architecture-and-gaps.md:103-204`:
- **v0.7 "Shadow Account real":** D1 broker journal parser; D2 auto-extract
  shadow rules from profitable trades; D3 delta-PnL attribution buckets
  (missed/noise/early/late/over); B1 Finance Research Goal Ledger (SQLite status
  machine); B3 research-only RiskTier keyword guard; F2 signal-engine source AST
  validation. *(The "evolving watchlist is what Shadow Account should become.")*
- **v0.8 "Validation Harness":** C3 (above) + walk-forward gold standard.
- **v0.9 "Multi-agent DAG" (only if operator wants it):** A1 SwarmRuntime + DAG
  topological scheduler; A5 AgentLoop goal-aware continuation. L5 load testing.
- **Smaller open Tauric/HKUDS items:** G3 markdown render layer over decisions
  JSONL; G5 ToolNode loops (deferred — HITL surprise risk); G7 PortfolioManager
  node; G10/G11 render/bind_structured helpers; G12 insider-transactions tool
  (Form 4/EDGAR); G15 same-ticker-rich vs cross-ticker-lean retriever split;
  C6 multi-source data fallback chain; A7 5-section system-prompt template.
- **Internal R-gaps:** R2/R2b quarantine ~25 alpaca-import test errors + 15
  pre-existing test failures (all pass in isolation — combinatorial pollution);
  R4 heuristic-fallback paths heavier than LLM paths; R6 HMM label-mapping
  brittleness.

### 3.F 🟡 Open ops issues / bugs (still standing)

- **Direction-vs-play-bias mismatch in autonomous-tick** 🚨 — *What:*
  autonomous-tick fires the advisor's direction through whichever play the symbol is
  *eligible* for, with no direction-vs-bias check. *Live evidence:* AXP fired SHORT
  via the `csp` play (CSP = sell-put = **bullish-bias**) — a SHORT should never route
  through CSP. *Fix:* add a direction-compatibility check in
  `quant-autonomous-tick.py:run_tick`; until then flag as `gate=DIRECTION_BIAS_MISMATCH`.
  *Source:* `hermes-quant.md:98`.
- **watchlist-evolve yfinance serial bottleneck** — 500 symbols × 3 serial HTTP
  calls ≈ 3.75 min, blows the timeout; cron was **erroring silently**
  (`last_delivery_error: null`). Timeout bumped to 600s; **the ThreadPoolExecutor
  parallelization fix (5-10×) is NOT yet applied.** *Source:*
  `_inbox/2026-05-27-quant-watchlist-evolve-yfinance-bottleneck.md:47`.
- **EOD scan hang** — `quant-daily-interim.py --eod` pegged 377% CPU for 42+ min,
  no output, no JSON written. High-likelihood cause: Kronos CPU fallback (75s/sym ×
  38 = 47.5 min). *Fixes (not all applied):* per-symbol wall-clock budget; heartbeat
  line per N symbols; assert GPU path at start. *Source:*
  `_inbox/2026-05-26-hermes-quant-eod-scan-hang-and-mdb-retro.md:31-36`.
- **BMA discriminator not observable in audit trail** — ~80% of post-lift
  approvals show `n_distinct_analysts=None` / `contributing_analysts=None`; the
  `require_ensemble` fix exists in code but isn't fully written through to
  `audit_log.jsonl`, so "did the BMA gate fire?" isn't queryable. *Action:* backfill
  metadata on every approval write path + P1 detector for None. *Source:*
  `_inbox/2026-05-27-bma-conf-1.0-legitimate-vs-degenerate.md:47-60`.
- **`play_tag` plumbing on executions.jsonl** — retro can't distinguish
  advisor/playbook/autonomous-tick fires (all read as `advisor`). Deferred to
  ADR-0029 (multi-leg fills carry `play_tag` naturally). *Source:* `hermes-quant.md:97`.
- **Codex review LOW/MED followups (standing):** journal-then-state ordering crash
  window (LOW); `_SNAPSHOT_CACHE` thread-safety with cron concurrency (MED); wheel
  composite eviction divergence (LOW). *Source:* `hermes-quant.md:101`.
- **`TraderProposal` missing fields** — `stop_loss`, `entry_price`,
  `time_horizon` (and Mai0313's `size_fraction`/`target_price`). *Source:*
  `_inbox/2026-05-27-llm-trading-sota-and-codebases-research-bundle.md:53`.
- **Operator-halt staleness warning** — emergency-stops have no auto-expiry (a May
  13 global halt sat stale 13 days). Suggested: warn in daily premarket when a halt
  is >7 days old. *Source:* `_inbox/2026-05-26-hermes-quant-first-paper-fill-audit.md:28`.
- **One-way deploy-sync** — deployed `~/.hermes/scripts/` copies drift from repo
  `ops/scripts/`. Issue #23 *immediate* drift closed (904-line interim vendored), but
  a proper deploy-sync is "still worth doing." *Source:* `hermes-quant.md:180`.

### 3.G 🟡 Governance / process

- **2-week ADR freeze** — recommended by the 2026-05-27 audit; broke on day 2 with
  ADR-0067; **re-committed through end of June.** 65 ADRs is "architecture-astronaut"
  territory — the decision surface grows faster than load-bearing code. *Source:*
  `hermes-quant.md:102`, `architecture-review-2026-05-28.md:143,152`.
- **Re-introduce the approval (HITL) gate before live money** — it was removed for
  the paper phase per operator directive. *Source:*
  `_inbox/2026-05-28-hermes-quant-regime-gates-and-strategy-retro-shipped.md:73-75`.
- **MANUAL operator step (may be done):** the `HERMES_QUANT_SEMANTIC_ENABLED=1`
  flag was guarded against the agent's tools and required an operator `.env` write —
  later confirmed set at `.env:437`. *Source:* `hermes-quant.md:170-174`.

### 3.H ⏳ Older deferred items (digest C6 backlog — mostly superseded)

Polymarket; full event-sourced perception; Kronos integration (✅ shipped GPU);
methodology-from-reel ingestion pipeline (Phase B prototype, not formalized); the
socalminh "10%/month premium yield" definitional ambiguity (premium÷strike vs
÷share-price vs annualized) — never clarified.
*Source:* `digests/2026-05-24-C6-quant-trading.md:60,167,171-176`.

---

## 4. Operator preferences & hard constraints (the philosophy)

Explicit, repeated, load-bearing principles. These are the *rails* the
backlog-resolution loop must not violate.

- **Money-software discipline / CLI-only execution.** *"Money goes through CLI
  only, never tools — the daily brief suggests plays, never auto-fires orders"* —
  an AGENTS.md invariant (`digests/2026-05-24-C6-quant-trading.md:162`, D2). A
  static scanner blocks direct `alpaca.submit_order` calls
  (`regime-gates…:93`).
- **Silence-by-default is correct discipline; a permanent silencer is not.** Both
  look identical at a glance — the audit log + diagnostics doc are how you tell them
  apart (`hermes-quant.md:107`). The calibrator deadlock-by-design lesson:
  `max(0,raw-0.20)` zeroed every signal until N=200 fits, but the system never
  traded enough to reach N=200 — a self-defeating loop. Fix: Beta(2,5) prior +
  bootstrap from historical replay (`hermes-quant.md:106`).
- **Default-OFF rollouts.** Every new capability ships behind an env flag, default
  OFF (`HERMES_QUANT_SEMANTIC_ENABLED`, `HERMES_QUANT_PAPER_SLIPPAGE_MODEL`,
  `HERMES_QUANT_PORTFOLIO_CAPS`, `HERMES_QUANT_FUNDAMENTALS_ENABLED`,
  `HERMES_QUANT_RESEARCH_DEBATE`, …). Flip on cron wrappers (one reversible line)
  after the operator audits a side-by-side tick log; promote to default after a day
  clean (`hermes-quant.md:50`). Arming is *always a separate explicit human
  decision*, never bundled (`tomorrow-prep-readiness-review.md:85-87`).
- **Eval-gate before any live influence.** *"A butterfly engine that cries wolf is
  worse than none."* Negative-control + forward-return eval is a **HARD
  prerequisite** before any semantic signal earns live weight; build the eval
  harness FIRST (`hermes-quant.md:137,141`).
- **Knife-edge eval discipline / honesty over hype.** When Phase-0 social-arb came
  back precision exactly 0.60 (the minimum) with 2/5 false positives, the verdict
  was *"enough to justify building the producer, NOT enough for live weight"* — and
  the research that framed all 5 cases as wins was explicitly corrected
  (`hermes-quant.md:196-202`). Same with OOS: report *direction + range*, never the
  in-sample-optimal point (`hermes-quant.md:263`).
- **Test the graph, don't trust it (person-alias contamination lesson).** Edge-SIGN
  and alias-breadth are the two failure modes. **`elon musk → tesla` person-alias
  caused a SpaceX explosion to falsely tag TSLA/RIVN/LCID — removed ALL
  person-aliases, ENTITY aliases only.** *"Run the eval gate after every graph
  edit."* (`hermes-quant.md:178,182`, `butterfly-graph…:37-46`).
- **Separate confidence from magnitude.** score→`confidence`, severity→`magnitude`,
  never conflate (ASTS moved most but scored less than RKLB). Packet `asof` = headline
  PUBLICATION time, ALWAYS — *"the one rule that keeps backtests honest"*
  (`hermes-quant.md:135-136`).
- **Lookahead / decision-time honesty (ADR-0068/0069).** Decision timestamp must be
  when the model ran, not the bar boundary; drop the still-forming daily bar; consume
  only packets with `asof <= decision_time` (`hermes-quant.md:45-46,118`).
- **VERIFY, don't assume.** *"'benefit from profitability' = VERIFY, don't assume"* —
  built a profitability loop that turns "we think it has edge" into "the data
  confirms it" before raising any weight (`hermes-quant.md:292-298`). Self-critique +
  retrospection is *architecture, not a feature* (D3,
  `digests/2026-05-24-C6-quant-trading.md:163`).
- **Paper must be as accurate as live.** Capture issues in paper, not in live —
  hence the entire admissibility/fidelity foundation (§3.B)
  (`hindsight-admissibility…:21`).
- **Never trust an `executions.jsonl` reconstruction over the `state.db` positions
  table** — the JSONL double-counts (this produced the phantom 880%-gross "blown-up
  book" panic that was actually a conservative 4%-gross book)
  (`hindsight-admissibility…:38`, `six-model-critique-2026-05-28.md:5`).
- **What NOT to build (explicit):** custom options pricing engine (optlib vendored);
  custom universe scanner (yfinance + ETF rejection works); a 6th playbook strategy
  before CC/CSP/wheel fire; ChromaDB (BM25+JSONL is ahead); multi-vendor routing
  (premature for 1 user); Pine Script export; AI-Trader social/leaderboard features;
  RL post-training; ToolNode-for-all-analysts (HITL surprise); **and do NOT switch to
  live execution** before options + regime gating + strategy retrospection are real
  (`architecture-review-2026-05-28.md:154-159`,
  `hermes-quant-architecture-and-gaps.md:139-147,203`).
- **No-paid-API / stdlib-first ethic.** Catalyst + social producers are
  stdlib-only (urllib + xml.etree), injectable fetcher, "No-X-API bet holds"
  (`hermes-quant.md:128,204-210`). Do NOT vendor AGPL code (worldmonitor) into the
  private repo — reuse the feed CATALOG (URLs=facts), reimplement patterns
  (`hermes-quant.md:139`).

---

## 5. Relationship to risk and to the LLM's authority

The operator's stance is **unambiguous and structurally enforced**: the
**deterministic risk gate is the FINAL authority; the LLM/committee is evidence,
never authority.** This is the PDR backbone — *"Agents can research / summarize /
debate / propose / generate candidate configs. They should not decide the final
executable order call. A deterministic policy/risk/execution engine converts
approved signals into orders."* (`first-paper-fill-audit.md:34`).

**How it is enforced in architecture:**
- **ADR-0004 deterministic gate is final** — *"The
  deterministic-gate-as-final-authority discipline prevents committee-runaway"*
  (`architecture-review-2026-05-28.md:131`). The gate is cost-aware + provenance-aware
  + fail-closed (`hermes-quant-architecture-and-gaps.md:56`).
- **Committee can only silence, never amplify.** Ported from TradingAgents CV5
  anti-pattern: *"committee can only silence via 0.0-multiplier, never amplify;
  deterministic risk gate ADR-0004 stays final authority"* (`hermes-quant.md:58`).
  The risk-debate output is a *sizing multiplier*, not a veto-override
  (`architecture-review-2026-05-28.md:56`).
- **LLMs empirically fail at risk management** — the arXiv:2605.19337 survey +
  STOCKBENCH both confirm it; *"never delegate risk to LLM output"* is an
  adopt-and-keep finding (`llm-trading-sota…:33`). Risk stays deterministic by
  design.
- **Semantic / social signal is a PEER, never an override.** Catalyst signal enters
  the BMA as a peer view; `require_ensemble=True` means *it cannot fire alone*
  (`hermes-quant.md:124,153`). Consumer-trend social signal gets an *additional*
  0.5 confidence haircut on top (`hermes-quant.md:276-283`). Live-verified: a weak
  semantic signal that disagreed with Kronos correctly did NOT fire.
- **Failure-closed everywhere.** LLM committee: timeouts / JSON errors / Pydantic
  validation drop the turn; 2 consecutive drops bail to the deterministic skeleton.
  Risk committee capped at 3 rounds; bull/bear capped at 2 (`hermes-quant.md:56`,
  `hermes-quant-architecture-and-gaps.md:61-66`). Halt-state mirror fail-closes on
  any non-list shape (`tomorrow-prep-readiness-review.md:39`).
- **Anti-degeneracy at the aggregator.** `require_ensemble=True` silences
  single-source unanimity — *"When confidence=1.00 appears UNIFORMLY across many
  candidates, that's a degenerate-aggregation signal, not strong conviction. Real
  ensemble unanimity is rare; uniform 1.00 is a mathematical artifact."*
  (`decisions/2026-05-26-moa-halt-vs-approve-all.md:69`).

**The defining episode (the risk philosophy in action):** on 2026-05-26 the
operator said *"approve all"* on 24 EOD picks AND explicitly empowered autonomy
(*"you need to be able to take initiative and have autonomy"*). The system
**HALTED and fired ZERO trades** — an MoA committee + a ground-truth drill found
the BMA was inflating lone-Kronos votes to confidence=1.00. The reasoning is the
operator's risk creed verbatim:
1. *"Calibration cycles are scarce"* — don't pollute the calibrator with noise from
   a known-broken channel.
2. ***"Autonomy ≠ blind obedience. Fiduciary discipline includes refusing corrupted
   inputs. The user empowered DELIBERATION, not arbitrary action."***
3. Margin reality: 24 × $20K notional would mostly be broker-rejected anyway.
   (`decisions/2026-05-26-moa-halt-vs-approve-all.md:23-30`).

The honest-disclosure coda matters: the committee's RCA was *partially wrong* (it
called CORRECT silence-by-default a "regression"), but the ground-truth drill
caught the nuance — *"this is exactly why the deliberation pattern is 'panel +
ground truth,' not 'panel alone'… directional discipline matters more than perfect
diagnosis under uncertainty"* (`moa-halt-vs-approve-all.md:62-65`). **The
human/committee is advisory; the deterministic verifier and gate decide.**

---

## Appendix — cross-pollination with adjacent trading projects

- **trend-arbitrage-engine** (`projects/trend-arbitrage-engine.md`) — *"sibling
  reactive-decision engine; shares the HITL approval-gate pattern."* Its
  "butterfly" entity-correlation graph and the **person-alias contamination
  lesson** flow DIRECTLY into hermes-quant's Catalyst Sense (the 8-sector graph
  expansion was literally a shared session;
  `_inbox/2026-05-29-trend-arbitrage-butterfly-graph-8-sectors.md`). Camillo-style
  social arbitrage (detect-narrative → map-to-ticker → size-bet) IS Catalyst Sense
  for the first two stages (`hermes-quant.md:190`).
- **weather-alpha** (`projects/weather-alpha.md`) — the **template for the
  paper-only → live discipline**: separate gated `live/` module, ≥30d paper data
  first, per-slice calibration, hard caps + kill switch, fresh wallet, key never in
  chat. Same Strategy-Protocol + PaperLedger + calibration-auto-populate-on-resolve
  shape. *"Path to live (NOT crossed yet)."*
- **composer-replication-framework** (`projects/composer-replication-framework.md`)
  — shares the **deep-work-loop + ADRs-for-decisions + parallel-Opus-workers +
  cross-family final-verify** working method and the
  "verify-via-`git diff`-don't-trust-the-600s-timeout" pitfall. Not a trading
  system (RL/data-gen for LMA), but the orchestration discipline is identical.

## Appendix — the recurring orchestration meta-lessons (apply to the loop itself)

- **Cross-family adversarial review is the discipline that catches real bugs**
  before ship (6-model critique caught synthetic-short fidelity lie; validation
  caught severity-lexicon gap + person-alias contamination; Codex CLI caught the
  `score_symbol`-returns-float-when-ineligible HIGH bug).
- **Built-in `mixture_of_agents` tool stalls (no internal timeout)** on long/parallel
  reviews → run MoA manually (feed scatter reviews to an aggregator via urllib).
  MiniMax/Kimi reasoning models **starve / time out in agent-loop delegation** at
  ≥5 sub-targets → route reasoning-only work through direct urllib.
- **Silent-error crons are worse than loud failures** — audit `last_run_at` AND
  `last_status` together, weekly.
