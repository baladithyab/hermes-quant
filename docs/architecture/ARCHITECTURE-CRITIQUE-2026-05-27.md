# Architecture Critique — answers to live questions

**Date:** 2026-05-27
**Branch:** main @ commit `6a61e2e` + this addendum

---

## Q1: Do we have back-and-forth deliberation loops (capped at ~5)?

**Yes — three nested loop systems, all explicitly capped:**

| Stage | Loop type | Default | Hard cap | Env var | Termination |
|---|---|---|---|---|---|
| **RiskCommittee** | TauricResearch `should_continue_risk_analysis`, 3-persona round-robin | 1 round (3 turns) | **3 rounds (9 turns)** | `HERMES_QUANT_RISK_ROUNDS` | `count >= 3 * max_rounds` |
| **Bull/Bear (in deliberative aggregator)** | Counter inside `_inject_llm_turns` | 1 round (2 turns) | `2 * max_debate_rounds` | `max_debate_rounds` | `bull_bear_count >= 2 * max_debate_rounds` |
| **LLM committee `_emit` failures** | Consecutive-fail counter | — | **2 consecutive failures** → bail | — | "Two consecutive drops -> we abandon the rest of this tick" |

**File evidence:**
- `hermes_quant/agents/risk_committee/committee.py:67-70` — `DEFAULT_MAX_ROUNDS=1, MAX_ALLOWED_ROUNDS=3`
- `hermes_quant/aggregators/deliberative.py:147-148` — `if max_debate_rounds <= 0: raise ValueError`
- `hermes_quant/aggregators/llm_committee.py:665-667` — `consecutive_failures = 0` with bail @ 2

**Honest critique:** The risk-committee 3-round cap is good. The bull/bear is **not a real adversarial back-and-forth** — it's parallel emission with a counter. Each turn does NOT see the previous opponent's argument explicitly. **This is gap G1 in the Tauric analysis below**: the largest delta vs TauricResearch.

---

## Q2: Are there any parallel systems in the pipeline?

**Almost no concurrency. Three pieces:**

| Location | What | Concurrency |
|---|---|---|
| `playbook/scorers.py:566-666` | `prewarm_snapshot_cache(ThreadPoolExecutor)` for yfinance HTTP fetches in watchlist-evolve | **Parallel** ✓ |
| `daemon/tick_loop.py:20` | Per-asset analyst→aggregator→gate loop | **Synchronous** (comment says "v0.2 may add asyncio.gather over assets, but...") |
| `analysts/*` fan-out | All 4 analysts (classical_ta, kronos, microstructure, semantic) per ticker | **Synchronous** — they run in sequence inside `advisor.analyze()` |

**The honest implication:**
- Watchlist-evolve: 503 tickers × yfinance prewarm = parallel ✓ (this is why it ran in 94s)
- Daily-interim: per-ticker pipeline runs **serially** — 5 tickers × ~3s LLM each = ~15s total
- Risk committee: 3 personas × `max_rounds` = **sequential LLM calls** (Aggressive → Conservative → Neutral → repeat)

This is fine for **paper trading at <50 tickers**. Won't scale to a full universe scan in real-time. **Not a problem for tomorrow's open.**

---

## Q3: Where does Kronos drop in?

Kronos is **one analyst voice in the BMA committee**, not the oracle. Per ADR-0018 §D1:

```
analysts = [
    ClassicalTA(),      # ICT-style technical analyst
    Microstructure(),   # ATR/orderbook flow
    Semantic(),         # NLP from news/social packets
    KronosAnalyst(),    # ← foundation-model forecast (lazy-loaded)
]
                ↓
            BMA aggregator (regime-aware weights)
                ↓
            AggregateSignal
```

**Wired in 4 places:**
- `advisor.py:351-353` — registered in default analyst list
- `advisor.py:826-830` — wired into chat-mode loadout (lazy-imported)
- `recipes.py:248-250` — included in default daily picker
- `training/bootstrap_calibrator.py:160-162` — opt-in for calibration

**Built-in safety mechanisms (per ADR-0018):**
- **Confidence clipped to [0.30, 0.85]** — guards against foundation-model overconfidence (the Kairos A-shares neg-IC failure mode)
- **Zero-confidence abstain on weight-load failure** — BMA filters views with conf < 0.10 so abstainers don't pollute
- **Path-agreement confidence** — uses 30 stochastic forecast paths, not point estimate
- **NEVER trains, only infers** — analyst pool is frozen per the charter

**GPU acceleration (2026-05-26 migration):**
- Single-call batched `predict_distributional` (~30× faster than 30-iter loop)
- Multi-symbol `analyze_batch` (~300× wall-clock vs per-symbol CPU loop on RTX 5090)

---

## Q4: What did we NOT steal from external provenance?

Two parallel gap analyses ran. **Top gaps:**

### From HKUDS/Vibe-Trading (top 10):

| # | Gap | Effort | Value |
|---|---|---|---|
| **B1** | **Finance Research Goal Ledger** (SQLite, status machine, criteria, audit trail) | M | 🟢 |
| **D1+D2+D3** | **Trade journal parser + Shadow strategy extraction + Delta-PnL attribution** (broker CSV → 3-5 if-then rules → bucketed PnL diff) | M | 🟢 |
| **C3** | **Validation suite plumbing** (MC + Bootstrap CI + Walk-Forward → `validation.json`) — already aspired-to per ADR-0006 | M | 🟢 |
| **A1** | **SwarmRuntime + DAG execution** (topological scheduler with `input_from` graph) | L | 🟢 |
| **C6** | **Multi-source data fallback chain** (yfinance → ccxt → alpha_vantage with retry) | M | 🟢 |
| **G3** | **Task-routing decision tree in system prompt** | S | 🟢 |
| **A7** | **5-section structured System Prompt template** (Task Routing + frozen PM snapshot for prompt-cache stability) | S | 🟢 |
| **B2** | **Hypothesis Registry richer schema** (`monitoring` status, `run_cards[]` linkage) | S | 🟢 |
| **F6** | **pytest-socket no-network test enforcement** (defense-in-depth on factor purity) | S | 🟢 |
| **B3** | **Research-only RiskTier keyword guard** at goal creation | S | 🟢 |

### From HKUDS/AI-Trader: ~95% irrelevant (multi-user social/leaderboard). Skip.

### From TauricResearch/TradingAgents (critical):

| # | Gap | Effort | Severity |
|---|---|---|---|
| **G1** | **TRUE Bull/Bear adversarial debate** as separate stage (each turn reads opponent's last argument) — currently we have parallel emission, not adversarial back-and-forth | M | 🚨 **HIGH** |
| **G4** | **FundamentalsAnalyst** (balance sheet, earnings, cashflow) — equities trader with zero balance-sheet input is structurally lobotomized | M | 🚨 **HIGH** |
| **G3** | **Markdown render layer** over decisions.jsonl (human-readable HITL log) | S | MEDIUM |
| **G7** | **PortfolioDecision Pydantic schema** + executive summary (final node before END) | S–M | MEDIUM |
| **G12** | **Insider transactions tool** (Form 4 / SEC EDGAR — high signal/effort ratio) | S–M | MEDIUM |
| **G6** | **ResearchPlan Pydantic schema** with `with_structured_output()` binding | S | LOW–MED |
| **G13** | **5-tier PortfolioRating enum** (Buy/Overweight/Hold/Underweight/Sell) | S | LOW–MED |
| **G15** | **Same-ticker rich vs cross-ticker lean** in `get_past_context` retriever | S | LOW–MED |
| **G17** | **Conversational debate prompts** (rewrite bull_bear.md once G1 lands) | S | LOW |

### Specifically what we did NOT steal (and why):

- **ChromaDB / FinancialSituationMemory** — TauricResearch removed it in v0.2.4. Our ADR-0042 BM25+JSONL+Oracle-Fallacy stack is **architecturally ahead**. Don't regress.
- **ToolNode loops** — frontier models can decide tool calls dynamically, but creates HITL surprise (operator can't predict cost/latency). Defer until G1 lands.
- **Multi-vendor `route_to_vendor`** — premature for v0.1 (single-user, <50 tickers). One provider with retry suffices.

---

## Q5: Risks for tomorrow's open — what should we check?

**Top 3 risks** (from the architecture review §12):

### Risk 1: yfinance Yahoo cookie-jail
- **Mitigation shipped in PR #11:** abort guard in watchlist-evolve aborts (exit 2) if `error_rate > 50% AND success_rate < 10%`
- **Verification for tomorrow:** `python ~/.hermes/scripts/quant-watchlist-evolve.py --dry-run` simulates the run path before 03:30 PT
- **Residual:** if Yahoo rate-limits us at 03:30 PT, watchlist won't refresh; existing plays will stale-out via `>25h universe` guard (exit 1)

### Risk 2: Cron clock-drift / timezone
- **Critical assumption:** Hermes scheduler runs in PT
- **Verify NOW:** `hermes cron list 2>&1 | grep -A2 "quant-daily-premarket"`
- **Residual:** if the scheduler is UTC, premarket-interim fires at 05:30 UTC = 22:30 PT (wrong day). Should verify before tomorrow.

### Risk 3: Proposals 24h TTL silent expiry
- **Current contract:** proposals are created with 24h TTL; if Discord notification fails, you have 24h to approve before they expire
- **No watchdog yet** for proposals approaching TTL with no decision
- **Add:** `quant-proposals-watchdog-daily` cron at 06:30 PT that posts to Discord any proposal with >18h elapsed and no decision

---

## Q6: Should regime be in state for analysts to use? **YES — and we don't currently do that.**

**Current state:**

```python
# protocol.py line 80:
"""Provider-specific extras (orderbook, news, regime, ...). Read-only Mapping"""
```

The DOCSTRING claims regime is in `MarketContext.extras` — but **searching the codebase confirms it's NOT actually populated there.** Regime is currently used only:
- `aggregators/bma.py` — for BMA weight selection (regime-aware)
- `regime/hmm.py` — for classification
- `regime/per_regime_weights.py` — for weight tables
- `observability/fallback_probe.py` — for surfacing in reports

**No analyst sees the regime label.** This is a real gap. Right now:

```
HMM → AggregateSignal weights (only)
       ↓
       Analysts run BLIND to regime
```

**What it should be:**

```
HMM → MarketContext.extras["regime"] = {"label": "RISK_OFF", "p": 0.83, "asof": ...}
       ↓
       Each analyst can read regime and adjust:
       - ClassicalTA: trend-following weight DOWN in chop regime
       - Microstructure: orderbook flow MORE meaningful in low-vol
       - Semantic: news weight UP in event-driven regime
       - Kronos: confidence DOWN in regime transitions
```

**This is small effort, high value. Recommend:**
1. Wire `RegimeClassifier` into the per-tick flow BEFORE analyst fan-out
2. Inject `regime` into `MarketContext.extras`
3. Each analyst reads `ctx.extras.get("regime")` and uses it as a sanity check / weight modifier

**Effort:** S (a few hours). One file change in `daemon/tick_loop.py`, four small reads in each analyst.

---

## Recommended Wave 6 / v0.6 Roadmap

Based on these findings, here's the prioritized backlog:

### v0.6.0 — "Regime + Risk Refinement" (1-2 days, high value/effort)
- **R1:** Wire regime into `MarketContext.extras` (Q6 above)
- **R2:** Quant proposals TTL watchdog cron (06:30 PT, Discord alert)
- **R3:** Verify Hermes scheduler timezone (one-liner)

### v0.6.1 — "Tauric Parity" (3-5 days)
- **G1:** True Bull/Bear adversarial debate stage (separate from risk committee)
- **G4:** FundamentalsAnalyst (yfinance balance_sheet/earnings/cashflow)
- **G6+G7+G13:** Pydantic schema discipline (ResearchPlan, PortfolioDecision, PortfolioRating enum)

### v0.6.2 — "Operator UX" (2-3 days)
- **G3 (Tauric):** Markdown render layer over decisions.jsonl
- **G3 (HKUDS):** Task-routing decision tree in system prompt
- **A7:** 5-section structured System Prompt template

### v0.7 — "Shadow Account real" (1 week)
- **D1+D2+D3:** Trade journal parser + Shadow strategy extraction + Delta-PnL attribution

### v0.8 — "Validation harness" (1 week)
- **C3:** MC + Bootstrap CI + Walk-Forward → `validation.json` artifact protocol

### Skip / explicit "do not build"
- ChromaDB (we're ahead)
- Multi-vendor `route_to_vendor` (premature)
- Pine Script export (brittle, low payoff)
- All HKUDS/AI-Trader social/leaderboard features

---

2026-05-27T19:50:00 PDT — Hermes scheduler interprets naive cron schedules as system-local TZ (`America/Los_Angeles -0700`). `next_run_at: 2026-05-28T05:30:00-07:00` confirms PT alignment. **Timezone is NOT a risk for tomorrow's open.**

---

## ✅ Verified non-risks (struck through after live verification)

- ~~Risk 2: Cron clock-drift / timezone~~ — verified PT-aligned via `next_run_at`
