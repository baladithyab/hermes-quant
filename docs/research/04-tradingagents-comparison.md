# 04 — TradingAgents (Tauric) comparison + steal list

**Date**: 2026-05-13
**Source**: https://github.com/TauricResearch/TradingAgents
**Researched via**: DeepWiki MCP (read pages 1-7 + memory + reflection + data layer + state)
**Status**: research note, not an ADR
**Authors**: ARIA orchestrator (under deep-work-loop)

## TL;DR

TradingAgents is a **LangGraph multi-LLM-agent state machine** for "research-grade
trade idea of the day on a single ticker." It's a different product than
hermes-quant, despite superficial overlap. We **should not fork their orchestrator**
and **should not put LLMs in our action path**. We **can usefully steal three
things**: (1) `yf_retry()` exponential backoff, (2) the markdown memory log
pattern as a settlement journal, (3) the `PortfolioDecision` Pydantic schema as
the future contract for an `LLMAnalyst` plug-in.

## What they are

Their architecture is a five-phase sequential pipeline orchestrated by
LangGraph, with `AgentState` (a TypedDict) as the shared memory:

```
Analyst Team (4 LLMs, tool-enabled)
  ├─ Market Analyst    → market_report
  ├─ Social Analyst    → sentiment_report
  ├─ News Analyst      → news_report
  └─ Fundamentals      → fundamentals_report
        ↓
Researcher Team (2 LLMs in adversarial debate)
  ├─ Bull Researcher   → arguments for the long thesis
  ├─ Bear Researcher   → arguments for the short thesis
  └─ Research Manager  → synthesizes ResearchPlan {recommendation, rationale, strategic_actions}
        ↓
Trader Agent (1 LLM)
  └─ trader_investment_plan {action, entry, stop_loss, position_sizing}
        ↓
Risk-Management Team (3 LLMs in 3-way debate)
  ├─ Aggressive Analyst → "go bigger"
  ├─ Conservative Analyst → "tighter risk"
  └─ Neutral Analyst    → "weighed view"
        ↓
Portfolio Manager (1 LLM, structured output)
  └─ PortfolioDecision {rating, executive_summary, investment_thesis,
                        price_target?, time_horizon?}
        ↓
SignalProcessor (regex extracts rating from rendered markdown)
        ↓
Final 5-tier rating: Buy | Overweight | Hold | Underweight | Sell
```

Per decision: **9-12+ LLM invocations**. Heavy.

The `TradingMemoryLog` is a **markdown-only memory** (no embeddings — they
explicitly removed BM25 in favor of grep-able plaintext). Two-phase entries:
store decision now (`pending`), resolve with realized return on next run
(`raw_return`, `alpha_return`, `holding_days`). LLM-driven `Reflector` writes a
2-4 sentence post-mortem after each resolved trade.

Data layer: pluggable vendors (`route_to_vendor()`) with rate-limit-aware
fallback chain (yfinance primary → Alpha Vantage fallback). yfinance calls
wrapped in `yf_retry()` — exponential backoff with 2s base, 3 max retries,
catches `YFRateLimitError`. OHLCV cache: file-based per-`{symbol, start, end}`
in `{cache_dir}/{symbol}-YFin-data-{start}-{end}.csv`, fixed 5-year rolling
window.

## What we are

Daemon ticking on cron, **deterministic** Analyst Protocol → BMAAggregator
(Beta-binomial posteriors) → DefaultRiskGate (8 hard rules from ADR-0004) →
JSONL bus with flock atomicity → freqtrade IStrategy consumer. Calibrated
probability + Kelly + durable halt registry. Continuous signal stream,
**code-first, money-software-first**.

Per decision: **0 LLM invocations** in the action path. Cheap, replayable,
auditable.

These are different products solving different jobs.

## What's worth stealing — by priority

### ⭐⭐⭐ P0 — `yf_retry()` exponential backoff (~30 min)

Their `yf_retry()` function wraps every yfinance call: 2s base, doubling per
retry, max 3 attempts, catches `YFRateLimitError` (HTTP 429).

**Our gap**: `data/yfinance_provider.py` has only a 100ms inter-call sleep
between fetches. On a real Yahoo throttle event, our fetch raises
`RateLimitError` and the chain falls back. But for transient 429s that
resolve in 2-4s, retry-then-fallback is strictly better than fallback-only —
we don't have a fallback provider yet (ccxt/alpaca arrive in v0.1.3+).

**Verified during Phase 8**: Gemini reviewer flagged the broader class
("transient-error handling") as a P1 follow-up; this is the concrete fix.

**Action (v0.1.1 or v0.1.2)**: add `_yf_retry` decorator on `fetch_bars` /
`fetch_latest`. Public API unchanged. Tests covered by mocked-yf existing
suite.

### ⭐⭐ P1 — `TradingMemoryLog`-style settlement journal (~half day)

Their markdown memory file is a near-perfect fit for a complementary
**operator-facing journal** alongside `signals.jsonl` (machine-facing bus).

Design:
- Path: `~/.hermes/quant/journal.md`
- Append-only with `<!-- ENTRY_END -->` delimiters (HTML-comment robustness)
- Two-phase entries:
  - **Phase A — store decision** (called by tick_loop after gate emits Action):
    `[2026-05-13T14:00:00Z | BTC/USDT | dir=+1 conf=0.72 size=+0.10 | pending]`
    + full Action.reason + per-analyst component breakdown
  - **Phase B — resolve outcome** (called by settlement_loop on close/exit):
    replaces `pending` tag with `[... | raw_return | alpha_return | hold_minutes]`
    + appends a `REFLECTION:` section (deferred to v0.2 with LLMAnalyst —
    initially the reflection can be a deterministic rule: "thesis held: +ret >
    0 and direction matched; magnitude error: |actual - expected| / expected").
- Atomic-rename writes (we already do this for `halt_state.json`)
- Cross-asset lessons: surface last 3 reflections from other assets in
  `quant_doctor` output (this is the high-leverage diagnostic UI moment)
- Optional rotation via `journal_max_entries` config, **always preserves
  pending entries**

This complements the JSONL bus rather than duplicating it:
- JSONL = wire format, schema-versioned, consumer interop, byte-perfect
  reproducibility
- journal.md = operator UX, post-mortem support, debugging "why did the BMA
  weight for ClassicalTA collapse over the last week" kind of question

**No vector store**. Their decision to remove BM25 maps directly onto our
"reproducibility, no semantic-search black box" principle.

**Cite in commit**: `Pattern adapted from TauricResearch/TradingAgents
TradingMemoryLog (https://github.com/TauricResearch/TradingAgents). Markdown
two-phase pending→resolved + atomic-rename + no-embedding design.`

**Action**: target v0.1.2 as a new ADR (ADR-0010 — settlement journal). Don't
ship in v0.1.1 — the v0.1.1 release is already fat.

### ⭐⭐ P1 — `PortfolioDecision` schema as the future LLMAnalyst contract (~1 hr)

Their structured output is clean:

```python
class PortfolioRating(StrEnum):
    BUY = "Buy"
    OVERWEIGHT = "Overweight"
    HOLD = "Hold"
    UNDERWEIGHT = "Underweight"
    SELL = "Sell"

class PortfolioDecision(BaseModel):
    rating: PortfolioRating
    executive_summary: str
    investment_thesis: str
    price_target: float | None = None
    time_horizon: str | None = None
```

Maps cleanly onto our `AnalystView` if we add a future `LLMAnalyst` subclass:

```
Sell        → direction = -1, confidence = 0.85
Underweight → direction = -1, confidence = 0.60
Hold        → direction =  0, confidence = 0.50  (silenced by Rule 3)
Overweight  → direction = +1, confidence = 0.60
Buy         → direction = +1, confidence = 0.85
```

`price_target` becomes `magnitude = (price_target - last_close) / last_close`
(or stays None and the analyst falls back to a default magnitude).
`time_horizon` maps to `horizon`. `investment_thesis` becomes `rationale`.

We currently only have `ClassicalTAAnalyst`. v0.1.2 adds Kronos (deterministic
neural). v0.3.0 adds a news-LLM analyst — that's where this schema becomes
load-bearing.

**Action**: file as v0.3.0 backlog entry ("LLMAnalyst Pydantic contract —
adapt PortfolioDecision schema, map 5-tier → discrete direction/confidence").

### ⭐ P2 — OHLCV file cache (deferred to v0.1.2)

Their `load_ohlcv()` caches to `{symbol}-YFin-data-{start}-{end}.csv`. Saves
Yahoo round-trips during backtest replay. We don't have backtest replay yet —
this is paired with the v0.1.2 backtest deliverable.

**Action**: list as v0.1.2 task. Prefer parquet over CSV (smaller, schema-typed)
but mirror the path layout.

### ⭐ P2 — Dual-LLM tier strategy (deferred to v0.3.0)

Their `deep_thinking_llm` vs `quick_thinking_llm` config split is useful for
cost control once we have LLM analysts. Map to our `model-roster` skill:
quick = `google/gemini-3.1-flash-lite-preview` or `stepfun/step-3.5-flash`,
deep = `anthropic/claude-opus-4.7` via Bedrock or `openai/gpt-5.5-pro`.

**Action**: list as v0.3.0 config-design task. Don't implement yet.

## What we deliberately do NOT steal

| Their pattern | Why we skip |
|---|---|
| **LangGraph state machine for orchestration** | We have a daemon tick loop with our own protocol-based design. Adopting LangGraph means replacing our spine with theirs. Out-of-scope per "minimize dependency surface" principle. |
| **Bull-vs-bear LLM debate as the decision mechanism** | We have BMA + Beta-posterior weights with calibrated probabilities. That IS the disagreement-aware aggregator. Their debate is one expensive way to get a confidence score; we already have one with replayable provenance. |
| **3-LLM risk-management debate (Aggressive/Conservative/Neutral)** | We have `DefaultRiskGate` with 8 deterministic rules. Per AGENTS.md "hard rules over learned policy" principle, three more LLMs is the wrong direction. Our risk gate is structurally non-bypassable; theirs is a prompt-injection away from misbehaving. |
| **Portfolio Manager LLM as final decision** | Money-software discipline: every LLM in the trade path is a place a prompt injection can move capital. Our `Action` is purely deterministic from the gate. **Don't add LLMs to the action path. Ever.** |
| **Markdown rendering as the wire format** | Our bus is JSONL for replayability + schema versioning. Markdown is for the journal, not the wire. |
| **CLI/TUI display layer** | We have Hermes plugin tools + slash commands; Hermes does the display. |
| **`stockstats` library wrapping for indicators** | Our `analysts/classical_ta.py` is 250 LOC of pure functions, no extra deps. Adding `stockstats` is a dependency-surface regression. |
| **Their reflector LLM over every trade** | Adds an LLM call per trade for a soft-signal a deterministic rule already produces. Defer to v0.2+ when we have an LLM analyst pipeline anyway. |

## Net assessment

**Steal** (ordered by ROI):
1. `yf_retry()` — directly addresses a Phase-8 follow-up. v0.1.1 if room, else v0.1.2.
2. Settlement journal (markdown + two-phase + atomic-rename, no embeddings) — v0.1.2.
3. `PortfolioDecision` schema shape — v0.3.0 backlog entry only.
4. OHLCV cache layout — v0.1.2 alongside backtest.
5. Dual-tier LLM config keys — v0.3.0.

**Leave**: their entire orchestration spine, debate mechanism, and LLM-in-action-path
design. Those are good for *their* product (research-grade idea-of-the-day for a
human to vet), not ours (autonomous daemon under hard risk rules).

## Validation that our architecture is sound

The biggest insight from reading them: **we made the right call** putting
determinism + Kelly + flock + halt-registry at the core, and treating LLM
analysts as just one Analyst Protocol implementation among many. Their
architecture validates the alternative; we should resist the temptation to
copy because it's cool. The composable Analyst Protocol means we can plug
their *output schema* into our system as `LLMAnalyst` later without
changing the spine.

## v0.1.2 / v0.3.0 roadmap deltas

```diff
   - **v0.1.2** — KronosAnalyst + KairosAnalyst with bootstrap calibration
+    - yf_retry() exponential backoff in YFinanceProvider
+    - Settlement journal (~/.hermes/quant/journal.md) — pending→resolved pattern
+      adapted from TauricResearch/TradingAgents TradingMemoryLog
+    - OHLCV file cache (5y window, parquet) for backtest replay
+    - test_no_lookahead.py CI gate (was promised in AGENTS.md, never landed in v0.1.1)

   - **v0.3.0** — options support + news-LLM analyst + audit-logging
+    - LLMAnalyst protocol with PortfolioDecision-style structured output
+      (5-tier rating → discrete direction/confidence mapping)
+    - dual-tier LLM config (quant.llm.deep / quant.llm.quick) per
+      TauricResearch pattern; map to model-roster slate
+    - Optional LLM-driven Reflector for journal entries (defer to ADR)
```

## Patterns I missed in v1 (added 2026-05-13 second pass)

> Mined from `TauricResearch/TradingAgents` deepwiki second pass — sections
> 4.5, 5.1, 5.6, 6.2, 6.3, 7.1, 7.3, 8.3, 9.1, 9.2, 9.3, plus checkpointer/
> safe_ticker/look-ahead helpers. Excludes patterns already covered above
> (yf_retry shipped in v0.1.1, OHLCV file cache, markdown TradingMemoryLog,
> PortfolioDecision 5-tier schema, bull/bear debate REJECTED, LangGraph
> spine REJECTED, 11-indicator list, route_to_vendor fallback).

### 1. Look-ahead-bias filtering at the data boundary ⭐⭐⭐ P0

**Their location**: `dataflows/load_ohlcv()`, `StockstatsUtils.get_stock_stats()`,
`_filter_reports_by_date()` — every data-access function takes a `curr_date`
param and slices with `data[data["Date"] <= curr_date_dt]` BEFORE returning.

**What it does**: Forces every read to accept an "as-of" date and drops every
row strictly newer at the leaf, never at the call site. Same primitive at
three layers (price rows, indicator series, fiscal-period columns).

**Adapt to hermes-quant**: We have `tests/test_no_lookahead.py` planned (CI
gate via `shuffle_timestamps_test`) but no enforced `as_of_date` parameter
threading through `data_provider.fetch_bars()` etc. **P0 immediate gap**:
push an `as_of: pd.Timestamp` param through the DataProvider Protocol; drop
rows past it at the leaf. Cheap to add now, expensive to retrofit later.
Pairs with the v0.1.2 `test_no_lookahead.py` gate.

### 2. `safe_ticker_component()` path-traversal guard ⭐⭐⭐ P0

**Their location**: `dataflows/utils/ticker_safety.py` — whitelist regex
(`A-Z0-9.-_^`, ≤32 chars, rejects `.`/`..`/`...`/whitespace/null bytes).
Used in `_db_path()`, `load_ohlcv()`, `_log_state()`.

**What it does**: Every code path that interpolates a ticker into a
filesystem path runs it through the whitelist first. Tests assert
`BRK-B`, `^GSPC` pass while `../etc` is rejected.

**Adapt to hermes-quant**: We embed `pair`/`symbol` directly into JSONL
filenames + cache paths. Crypto pairs like `BTC/USDT` already need slash
sanitization. **P0 immediate gap**: add `hermes_quant.utils.safe_symbol_component()`
and route every cache/JSONL/log path through it. Solves both the
slash-in-symbol problem AND the path-traversal class.

### 3. Per-symbol SQLite watermark store (resume idempotency) ⭐⭐ P1

**Their location**: `graph/checkpointer.py` — per-ticker DB at
`checkpoints/{TICKER}.db`, deterministic `thread_id(ticker, date)`,
LangGraph `SqliteSaver` wrapper.

**What it does**: Per-symbol DB isolation, deterministic ID so re-runs
*resume* mid-pipeline. Explicit clear after success.

**Adapt to hermes-quant**: Their LangGraph wrapper doesn't drop in (we have
no DAG), but the IDEA maps: tiny `state/<symbol>.sqlite` recording
`(symbol, last_processed_bar_ts, indicator_snapshot_hash)` so daemon restart
skips already-emitted JSONL rows. Effectively idempotency keys per
`(symbol, bar_ts)` — strictly better than current "append and pray no dupes."
**P1 v0.1.2**.

### 4. `ConditionalLogic` centralized routing class — NOT-APPLICABLE

**Their location**: `graph/conditional_logic.py` — every "where do we go
next" decision concentrated in one class with method-per-decision.

**Why we skip**: We have no DAG to route. Mark NOT-APPLICABLE for v0.1.x.
The transferable subprinciple ("all branching predicates in one module")
might marginally help `route_to_vendor` family, but we already have that.

### 5. Partial-dict TypedDict state merge (`BarSnapshot` schema) ⭐⭐ P1

**Their location**: `agents/utils/agent_states.py:AgentState(MessagesState)`
— named slots (`market_report`, `sentiment_report`, ...). Each agent node
returns a *partial* dict, LangGraph merges. Adding an analyst = adding a
TypedDict field.

**What it does**: Single typed schema; agents only mutate their slot.

**Adapt to hermes-quant**: Our JSONL row schema is currently ad-hoc dicts
per writer. **P1 v0.1.2**: define `BarSnapshot` Pydantic model with named
slots: `ohlcv`, `indicators`, `regime_label`, `signal_proposal`, `risk_check`,
`final_decision`, `meta`. Each pipeline stage returns its slot; daemon
merges. Schema-validated JSONL out, clean upgrade path when LLM analysts
add their own slot. Aligns with how freqtrade stores `populate_indicators`
columns.

### 6. `MessageBuffer` content-presence-driven status (for `quant_doctor`) ⭐⭐ P1

**Their location**: `cli/main.py:MessageBuffer` — `agent_status` derived
from PRESENCE OF CONTENT in `report_sections`, not explicit completion
signals. `_processed_message_ids: set` for dedup.

**What it does**: UI mirror of pipeline state, hydrated from `graph.stream()`.
Status is implicit from "has this section been written yet?" — no extra
event channel.

**Adapt to hermes-quant**: Today `quant_doctor` ad-hoc tails JSONL. **P1
v0.1.2**: replace with a `DaemonState` mirror reading JSONL events and
inferring per-symbol pipeline status from which JSONL keys are present in
the latest row. Same content-presence trick. `_seen_event_ids` set keyed
on `(symbol, bar_ts)` for dedup.

### 7. Two-tier LLM cost split — ⭐ P2 v0.3.0+

**Their location**: `default_config.py` (`deep_think_llm`, `quick_think_llm`),
`graph/trading_graph.py` builds two clients. Deep tier: Research Manager,
Portfolio Manager. Quick tier: every analyst, both researchers, Trader,
3 risk debators, Reflector.

**Adapt to hermes-quant**: Not applicable until LLM analysts land. When
they do (v0.3.0), bake in this split: `config.llm.deep_model` for rare
synthesis, `config.llm.quick_model` for routine narration. Don't single-tier.

### 8. `MagicMock` structured-output tests + autouse dummy keys ⭐⭐ P1

**Their location**: `tests/test_memory_log.py:_structured_pm_llm()`,
`tests/conftest.py:_dummy_api_keys` (autouse, injects placeholder keys).
`MagicMock.with_structured_output()` returns binding capturing prompt;
`.invoke()` returns a real Pydantic instance via `side_effect`.

**What it does**: Test asserts on captured prompt content + parsed
structured output. Autouse `_dummy_api_keys` stops CI hangs when API
keys absent.

**Adapt to hermes-quant**: **P1 NOW (don't wait for LLMs)**. Two takeaways:
(1) `conftest.py` autouse fixture setting dummy env vars for ALL
third-party SDKs (CCXT exchanges, Telegram, OpenRouter, etc.) → CI never
blocks on missing creds. (2) When LLM analyst arrives, mirror `_structured_*_llm()`
pattern instead of `langchain.fake`. The autouse-keys fixture is a 30-line
patch with immediate CI stability payoff.

### 9. Alpha-return settlement (non-LLM half of `Reflector`) ⭐⭐ P1

**Their location**: `graph/reflection.py:Reflector` invokes after
`_fetch_returns()` produces both **raw return** AND **alpha return**
(vs benchmark). 2-4 sentence reflection covers directional correctness.
Then `TradingMemoryLog.batch_update_with_outcomes()` flips `[pending]` to
actual returns.

**What it does**: Closes the loop with alpha-vs-benchmark, not just raw.

**Adapt to hermes-quant**: TWO halves to split:
- **(a) Alpha-vs-benchmark return as first-class settlement metric ⭐⭐ P1**:
  compute against BTC for crypto / SPY for equities. Mechanical, no LLM
  needed. Should land in v0.1.2 alongside the calibrator-gate lift.
- **(b) LLM-generated reflection text ⭐ P2**: park until v0.3.0 when LLM
  analysts land.

### 10. `get_past_context(n_same=5, n_cross=3)` recency retrieval ⭐⭐ P1

**Their location**: `agents/utils/memory_log.py:TradingMemoryLog.get_past_context()`
returns: last 5 entries for SAME ticker (full + reflection) + last 3
reflection-only entries from OTHER tickers. They EXPLICITLY removed
`FinancialSituationMemory`/ChromaDB/BM25 in favor of this.

**What it does**: Two-axis recency retrieval (same-ticker depth +
cross-ticker breadth). Zero similarity scoring. Zero embeddings.

**Adapt to hermes-quant**: **P1 v0.1.2**. Add `get_recent_lessons(symbol,
n_same=5, n_cross=3)` accessor surfacing cross-symbol reflections so
LLM analysts (v0.3.0) get pattern transfer for free. Strong validation
that we picked the right side of embeddings vs recency-tail.

### 11. `VENDOR_METHODS` 2D dispatch table ⭐⭐ P1

**Their location**: `dataflows/vendor_routing.py` —
`VENDOR_METHODS: dict[method_name, dict[vendor_name, callable]]`,
`TOOLS_CATEGORIES: dict[category, list[method]]`, `VENDOR_LIST`.
`route_to_vendor()` looks up the configured vendor for the method's
category, falls back on rate-limit error.

**What it does**: Method × vendor 2D table makes vendor surface
*enumerable* — static check that every vendor implements every method.

**Adapt to hermes-quant**: We have `route_to_vendor()` documented but
need to verify the structure is genuinely a 2D dict (not if/elif chains).
Plus a `tests/test_vendor_completeness.py` asserting every vendor
implements every method in its category. **P1 v0.1.2**.

### 12. Category-level + per-method vendor config ⭐⭐ P1

**Their location**: `default_config.py` — `data_vendors: dict` keyed by
category (`core_stock_apis`, `technical_indicators`, ...) plus
`tool_vendors: dict` for per-method overrides that win over category.

**What it does**: Two-level config — set "use yfinance for everything"
coarsely, then override `tool_vendors["get_news"] = "alpha_vantage"`.

**Adapt to hermes-quant**: **P1 v0.1.2**. Pairs with #11. Add
`config.data.vendors_by_category` + `config.data.vendor_overrides_by_method`.
Future-proofs the inevitable "Binance for klines but Bybit for funding
rates" requirement.

### 13. Env-vars for paths only, not behavior ⭐⭐ P1

**Their location**: `default_config.py` — only `TRADINGAGENTS_RESULTS_DIR`,
`TRADINGAGENTS_CACHE_DIR`, `TRADINGAGENTS_MEMORY_LOG_PATH` are env-readable.
LLM model names, debate rounds, vendor selection are NOT env-overridable.

**What it does**: Filesystem paths differ per env (CI vs prod vs dev) →
env var. Behavior knobs stay in code/config so they're code-reviewed.

**Adapt to hermes-quant**: **P1 v0.1.2**. Codify the same rule:
`HERMES_QUANT_DATA_DIR`, `HERMES_QUANT_RUN_DIR`, `HERMES_QUANT_JOURNAL_PATH`
env-readable; everything else (timeframes, indicator params, vendor list)
in YAML/dict only. Reduces 12-factor accidents where someone sets
`MAX_DEBATE_ROUNDS=99` in `.env` and nobody finds it.

### 14. Bulk indicator computation amortization — ⭐ P2

**Their location**: `dataflows/stockstats_utils.py:_get_stock_stats_bulk()`
loads OHLCV once, runs `stockstats.wrap(df)`, builds date→value dict for
WHOLE historical span. Then walks back N days looking up that dict.

**What it does**: Single load + N dict lookups vs N reloads.

**Adapt to hermes-quant**: Mostly NOT-APPLICABLE — our daemon already
computes indicators on a rolling pandas frame per symbol. Note for
v0.3.0 *if* we expose a "historical indicator window" tool to LLM analyst:
structure as one bulk load + N lookups.

### 15. `invoke_structured_or_freetext()` graceful fallback — ⭐ P2

**Their location**: `bind_structured()` wraps LLM with
`with_structured_output(schema)`. `invoke_structured_or_freetext()` tries
structured first, falls back to free-text + `parse_rating()` post-hoc parser.

**What it does**: Guards against models that don't reliably honor
`with_structured_output` (function-calling failures, schema-too-complex).

**Adapt to hermes-quant**: Pure LLM-analyst territory. Park for v0.3.0.
When the time comes: every LLM call emitting Pydantic model gets
`try structured → except → free-text → regex_fallback`.

### 16. `SignalProcessor` markdown-to-rating extractor — NOT-APPLICABLE

**Their location**: `graph/signal_processing.py:SignalProcessor.process_signal()`
calls `parse_rating()` on rendered markdown, returns 5-tier rating. Class
still accepts an `llm` arg for back-compat but doesn't use it.

**Why we skip**: Our `PortfolioDecision` (when v0.3.0) already has a typed
`rating: Literal[...]` field — we read the field directly, no parsing
needed. Worth knowing the pattern exists ONLY for the case where we render
markdown to user-facing reports and a downstream consumer needs to extract
rating from text.

### 17. Pydantic model → markdown render layer ⭐⭐ P1

**Their location**: Trader/RM/PM all `bind_structured(schema)` → Pydantic
instance → `render_to_markdown(model)` helper → return both. LLM never
emits markdown directly.

**What it does**: One source of truth (Pydantic model), two consumers
(machines read fields, humans read rendered markdown).

**Adapt to hermes-quant**: **P1 NOW (enforce before LLMs land)**. Settlement
journal entries should be model-derived, never model-prompted prose. Make
`TradingMemoryLog.append_entry(model: SettlementEntry)` only accept the
Pydantic model — never raw strings. Spliced into the v0.1.2 settlement
journal ADR-0010.

## Summary table (round 2)

| # | Pattern | Priority | Target |
|---|---|---|---|
| 1 | Look-ahead `as_of_date` filter at data leaf | ⭐⭐⭐ | v0.1.2 |
| 2 | `safe_symbol_component()` path-traversal guard | ⭐⭐⭐ | v0.1.2 |
| 3 | Per-symbol SQLite watermark store | ⭐⭐ | v0.1.2 |
| 5 | `BarSnapshot` partial-dict TypedDict | ⭐⭐ | v0.1.2 |
| 6 | `quant_doctor` content-presence status | ⭐⭐ | v0.1.2 |
| 8 | Autouse dummy keys + structured-output mocks | ⭐⭐ | v0.1.2 |
| 9a | Alpha-vs-benchmark return at settlement | ⭐⭐ | v0.1.2 |
| 10 | `get_recent_lessons(n_same, n_cross)` | ⭐⭐ | v0.1.2 (settles into journal ADR) |
| 11 | `VENDOR_METHODS` 2D dispatch | ⭐⭐ | v0.1.2 |
| 12 | Category + per-method vendor config | ⭐⭐ | v0.1.2 |
| 13 | Env-vars for paths only | ⭐⭐ | v0.1.2 |
| 17 | Pydantic → markdown render layer | ⭐⭐ | v0.1.2 (settles into journal ADR) |
| 7  | Two-tier LLM config (deep/quick) | ⭐ | v0.3.0 |
| 9b | LLM-reflection text | ⭐ | v0.3.0 |
| 14 | Bulk indicator amortization | ⭐ | v0.3.0 (if tool surface added) |
| 15 | `invoke_structured_or_freetext` fallback | ⭐ | v0.3.0 |
| 4  | `ConditionalLogic` centralized routing | NOT-APPLICABLE | — |
| 16 | `SignalProcessor` markdown extractor | NOT-APPLICABLE | — |

**Net new P0 work surfaced**: patterns 1 + 2 (look-ahead enforcement +
symbol-path guard). These were NOT in v1 of this comparison and are
genuine production gaps.

**v0.1.2 backlog (compared to original CHANGELOG roadmap)**: add patterns
3, 5, 6, 8, 9a, 10, 11, 12, 13, 17. Most are small + composable with the
existing v0.1.2 plan.

**v0.3.0 backlog**: add patterns 7, 9b, 14, 15 alongside the existing
LLMAnalyst design.

## References

- TradingAgents repo: https://github.com/TauricResearch/TradingAgents
- TradingAgents wiki via DeepWiki: https://deepwiki.com/TauricResearch/TradingAgents
- LangGraph: https://github.com/langchain-ai/langgraph
- Their `TradingMemoryLog`: see DeepWiki page 5.5 "Memory and Decision Log"
- Their data vendor system: see DeepWiki page 4 "Data Layer"
- Their `PortfolioDecision` schema: see DeepWiki page 1.3 "Trading Decision Pipeline"
