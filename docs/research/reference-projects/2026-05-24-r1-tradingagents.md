# R1 — TauricResearch/TradingAgents: analyst decomposition & bull/bear debate

**Source:** `/tmp/quant-research/sources/TradingAgents/` (commit at scatter time)
**Lens:** What can hermes-quant lift from the role decomposition + bull/bear debate, and what must we *not* copy from the trader / portfolio-approval boundary.

---

## 1. Role inventory

The framework is wired together as a LangGraph `StateGraph` of typed nodes. Every "agent" is a closure produced by a `create_*` factory that takes an LLM and returns a `def *_node(state) -> dict` that mutates a shared `AgentState` (a `MessagesState` subclass). Roles, in pipeline order:

### 1a. Analyst tier (parallel-eligible, but default sequential)

Path: `tradingagents/agents/analysts/*.py`. Each analyst is a ReAct-style tool-using agent over a tool subset bound at `graph/trading_graph.py:_create_tool_nodes`.

- **Market Analyst** — `analysts/market_analyst.py`. Tools: `get_stock_data`, `get_indicators`. Prompt picks ≤8 technical indicators from a curated catalog (SMA/EMA/MACD/RSI/Bollinger/ATR/VWMA), then writes a "very detailed and nuanced report" + a markdown summary table. Output → `state["market_report"]: str` (free-text markdown).
- **Sentiment Analyst** (key `social`) — `analysts/sentiment_analyst.py` (legacy `social_media_analyst.py`). Tools: `get_news`. Output → `state["sentiment_report"]: str`.
- **News Analyst** — `analysts/news_analyst.py`. Tools: `get_news`, `get_global_news`, `get_insider_transactions`. Output → `state["news_report"]: str`.
- **Fundamentals Analyst** — `analysts/fundamentals_analyst.py`. Tools: `get_fundamentals`, `get_balance_sheet`, `get_cashflow`, `get_income_statement`. Output → `state["fundamentals_report"]: str`.

I/O shape: every analyst's payload is a single free-text markdown blob keyed into `AgentState` (see `agents/utils/agent_states.py:AgentState`). There is **no structured AnalystView**. Each analyst's report is consumed verbatim by the next role via prompt interpolation.

Flow: `START → market → social → news → fundamentals → Bull Researcher` (sequential when `analyst_concurrency_limit=1`, the default; can fan out per `graph/analyst_execution.py`). Between each analyst and the next sits a `tools_*` ToolNode loop and a `Msg Clear *` cleanup node — see `graph/setup.py` and `graph/conditional_logic.py:should_continue_market` etc. The clear-message pattern keeps the next analyst's context window clean of upstream tool-calls.

### 1b. Researcher tier (the bull/bear debate)

- **Bull Researcher** — `agents/researchers/bull_researcher.py`. Reads all four analyst reports + `investment_debate_state["history"]` + the *last bear response* (`current_response`). Emits prose prefixed `"Bull Analyst: ..."`.
- **Bear Researcher** — `agents/researchers/bear_researcher.py`. Mirror image: same four reports + last bull response.

Output of each: appended to `investment_debate_state.history`, `bull_history` / `bear_history`, and `current_response`. Counter `count` increments per turn.

### 1c. Research Manager (the "judge")

- **Research Manager** — `agents/managers/research_manager.py`. Reads `investment_debate_state.history`. Produces a **structured** `ResearchPlan(recommendation: PortfolioRating, rationale, strategic_actions)` via `bind_structured` (`agents/utils/structured.py`). Writes to `state["investment_plan"]`. Uses the *deep-thinking* LLM, while the debaters use the quick-thinking one.

### 1d. Trader

- **Trader** — `agents/trader/trader.py`. Reads `investment_plan`. Emits `TraderProposal(action: Buy|Hold|Sell, reasoning, entry_price?, stop_loss?, position_sizing?)`. Renders to markdown ending with the literal `FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**` line — a string-grep contract used elsewhere in the system. Writes to `state["trader_investment_plan"]`.

### 1e. Risk-management triumvirate

- **Aggressive / Conservative / Neutral debators** — `agents/risk_mgmt/{aggressive,conservative,neutral}_debator.py`. A second debate, this time three-way. Each reads all four analyst reports + the trader's proposal + the other two debators' last responses. Round-robin sequenced by `should_continue_risk_analysis`.

### 1f. Portfolio Manager

- **Portfolio Manager** — `agents/managers/portfolio_manager.py`. Reads research plan + trader plan + risk-debate history + `past_context` (memory log). Emits structured `PortfolioDecision(rating, executive_summary, investment_thesis, price_target?, ...)`. Writes to `state["final_trade_decision"]`. Deep-thinking LLM. Terminal node before `END`.

---

## 2. The bull/bear debate — mechanics

**Sequential, not parallel.** `graph/conditional_logic.py:should_continue_debate`:

```python
if state["investment_debate_state"]["count"] >= 2 * self.max_debate_rounds:
    return "Research Manager"
if state["investment_debate_state"]["current_response"].startswith("Bull"):
    return "Bear Researcher"
return "Bull Researcher"
```

So the loop is `Bull → Bear → Bull → Bear → ... → Research Manager`, alternating, each side seeing the **full prior history** plus the opponent's *last* utterance promoted to `current_response`. Bull always speaks first (the analyst-tier-final clear node always edges to `Bull Researcher`).

- **Shared context vs. private:** `bull_history` and `bear_history` are tracked separately, but both researchers read the unified `history` field plus the opponent's last reply, so it's effectively shared. There is no hidden chain-of-thought.
- **Termination:** purely a turn cap. `count >= 2 * max_debate_rounds` (default `max_debate_rounds=1` → 2 turns total: 1 bull, 1 bear; production configs typically run 2–3). **No convergence detection** — no semantic similarity check, no consensus signal, no early stop. The judge fires on a fixed budget.
- **Synthesis:** the Research Manager (`research_manager.py`) acts as judge. Reads only `investment_debate_state["history"]`. Picks one of `{Buy, Overweight, Hold, Underweight, Sell}` (a 5-tier `PortfolioRating` enum from `schemas.py:PortfolioRating`). Prompt explicitly says *"reserve Hold for situations where the evidence on both sides is genuinely balanced"* — i.e. Hold is intentionally a tiebreaker, not a default.

The risk-management debate (`aggressive/neutral/conservative`) follows the same template: round-robin by `latest_speaker` field, capped at `3 * max_risk_discuss_rounds`, judged by the Portfolio Manager.

---

## 3. AnalystView-equivalent schema comparison

TradingAgents has **no AnalystView equivalent**. Each analyst emits a markdown blob. Structured output only enters at the three decision-making roles:

| Field | hermes-quant `AnalystView` | TradingAgents analyst | TradingAgents `ResearchPlan` (judge) | TradingAgents `TraderProposal` |
|---|---|---|---|---|
| identity | `analyst: str` | implicit (state key) | — | — |
| direction | `Direction` enum | embedded in prose | `recommendation: PortfolioRating` (5-tier) | `action: TraderAction` (3-tier) |
| magnitude | `magnitude: float` | — | — | `entry_price`, `stop_loss` (raw prices) |
| horizon | `horizon: str` | — | — | — |
| confidence | `confidence: float` (calibrated) + `confidence_raw` | — | — | — |
| evidence | implicit in `rationale` | the markdown blob *is* the evidence | `rationale: str` | `reasoning: str` |
| counterarguments | — | — | rationale "ends with which arguments led to recommendation" | — |
| recommended_rule_change | — | — | `strategic_actions: str` (free prose) | `position_sizing: str` (free prose) |

**What TradingAgents has that we don't:**
1. Explicit role hierarchy with two-LLM tiers (`quick_thinking_llm` for analysts/debators, `deep_thinking_llm` for the two managers — `graph/trading_graph.py:88-95`).
2. Prose-as-handoff: each analyst's full markdown is consumed verbatim by the next stage, not a structured summary.
3. The 5-tier rating scale (`Buy/Overweight/Hold/Underweight/Sell`) instead of binary direction.

**What hermes-quant has that they don't (and shouldn't change):**
1. **Calibrated confidence** (per ADR-0002 + ADR-0009 §P0-2): confidence ∈ [0,1] tracking directional accuracy, with `confidence_raw` for calibrator training. TradingAgents has zero calibration — the judge picks a label, full stop.
2. **Magnitude as float** — TradingAgents only emits a discrete label, so the aggregator could never weight by expected effect size.
3. **Horizon explicit** — TradingAgents bakes "current trading round" implicitly; no sense of "this view holds for 1h" vs "1d".
4. **frozen=True dataclass** — they pass dicts through TypedDict, so any node could (and the trader does) inject orthogonal data.

---

## 4. Trader / portfolio-approval boundary — the anti-pattern

**Where TradingAgents leaps from "recommendation" to "order":**

`agents/trader/trader.py:create_trader → trader_node`. The Trader is invoked *unconditionally* after the Research Manager (see `graph/setup.py`: `workflow.add_edge("Research Manager", "Trader")`). It produces a `TraderProposal` carrying:

- `action: Buy|Hold|Sell`
- `entry_price: Optional[float]`
- `stop_loss: Optional[float]`
- `position_sizing: Optional[str]` — **free-text**, e.g. `"5% of portfolio"`

The `position_sizing` field is the production landmine. Two reasons:

1. **The model writes prose into a sizing field.** A free-text `"5% of portfolio"` is parsed downstream only by the Portfolio Manager, again an LLM. There is no schema-level constraint on sizing — the model could write `"50% of portfolio"`, `"max leverage"`, `"all-in"`, or `"size based on conviction"`, and nothing in the type system or runtime stops it. This is exactly what hermes-quant's discrete action space `{0, ±0.05, ±0.10, ±0.15, ±0.20}` (AGENTS.md "Action space is discrete") is designed to prevent.
2. **The Portfolio Manager rubber-stamps a free-text plan.** `portfolio_manager.py:portfolio_manager_node` produces `PortfolioDecision.executive_summary` ("entry strategy, position sizing, key risk levels, and time horizon. Two to four sentences"). Same shape — prose. The "decision" returned from `propagate()` is the rendered markdown of `PortfolioDecision`, then handed to `SignalProcessor.process_signal` which extracts the rating string. No deterministic risk gate, no hard cap, no NAV ceiling, no drawdown circuit breaker, no cost threshold check. Just "the LLM said Buy".

**Why this is wrong for production:**

- **Money is decided by an LLM in a chat session.** That's the very failure mode hermes-quant's "Money never goes through tools" rule (AGENTS.md) is shaped to dodge. An accidental `"yeah do that"` (or a prompt-injected analyst report) reaches the trader unfiltered.
- **No postmortem layer.** TradingAgents has a reflection step (`graph/reflection.py` + `agents/utils/memory.py:TradingMemoryLog`) that runs an LLM over realized returns to write reflections back into the prompt context for the *next* run. Useful for prompt-conditioning, but it is **not** a deterministic settlement journal — there is no per-trade ground truth check that hard-stops a misbehaving strategy.
- **Action space is continuous prose.** No discrete bucket means RL- or learned-policy aggregators (ADR-0006) couldn't be slotted in without a reward-hacking surface.

**How hermes-quant dodges it:**

- ADR-0004 (risk gate): deterministic, silence-by-default, ¼-Kelly cap, hard rules over learned policy. The aggregator's `AggregatedSignal` is the **input** to the gate, not the output. The gate can override any LLM-suggested action to flat.
- ADR-0015 (HITL propose-decide-react): proposals from agents go to a confirmation surface; only the CLI with explicit confirmation moves money.
- ADR-0010 (settlement journal): deterministic, no LLM in the decision path. Per-trade ground truth is recorded mechanically.
- ADR-0007 (plugin shape): tools are read-only views; the chat-session LLM has no path to a real-money order.

In short: TradingAgents fuses *recommendation*, *sizing*, and *order* into a single LLM-produced markdown blob. hermes-quant separates them at hard, typed seams, and the LLM is *upstream* of the gate, never downstream of it.

---

## 5. Memory / state

**Shared state, not per-agent memory.** All roles read and write the same `AgentState` TypedDict (`agents/utils/agent_states.py`). Inside it, two debate sub-states are nested:

- `InvestDebateState`: `bull_history`, `bear_history`, `history`, `current_response`, `judge_decision`, `count` — all `str` except `count: int`.
- `RiskDebateState`: per-debator histories (`aggressive_history`, `conservative_history`, `neutral_history`), unified `history`, `latest_speaker`, three `current_*_response` fields, `judge_decision`, `count`.

Conversation history is just **string concatenation** — `history = history + "\n" + argument`. No vector store, no embedding-based retrieval, no per-role scratchpad. Each debater receives the full transcript as one big prompt-time interpolation.

**Long-term memory.** `agents/utils/memory.py:TradingMemoryLog` — append-only markdown file at `cfg["memory_log_path"]`. Schema:

```
[YYYY-MM-DD | TICKER | RATING | pending]
DECISION: <PortfolioDecision rendered as markdown>
<!-- ENTRY_END -->
```

After realized returns, the entry is rewritten atomically (temp file + `os.replace`) into:

```
[YYYY-MM-DD | TICKER | RATING | +X.X% | +α.α% | Nd]
DECISION: ...
REFLECTION: <LLM-generated reflection on what went right/wrong>
```

`get_past_context()` injects up to N same-ticker entries plus N cross-ticker reflections into the next run's prompt for the Portfolio Manager. Optional rotation cap. **Not a vector DB; not stateful per-agent.** It's a prompt-context cache.

LangGraph checkpointing (`graph/checkpointer.py`, opt-in via `checkpoint_enabled`) gives crash-resume by storing per-thread state in SQLite — orthogonal to the memory log.

---

## 6. Three patterns to STEAL for hermes-quant

1. **Two-LLM tier (`quick_thinking_llm` vs `deep_thinking_llm`).** `graph/trading_graph.py:88-95` instantiates two clients: cheap-fast for analysts and debators, expensive-slow for the two synthesis roles (Research Manager, Portfolio Manager). For us this maps to "small model writes per-analyst rationale; big model arbitrates committee disagreement." Concrete adoption: extend ADR-0023 (deliberative-committee-decision-layer) so the committee judge can use a dedicated `deep_model` config key; analysts keep the cheap one.

2. **Bull/bear adversarial pairing as a *disagreement amplifier*.** Right now hermes-quant aggregators (ADR-0003 BMA + stacking) average analyst views. TradingAgents' debate forces an explicit adversarial pass before averaging — a single LLM playing devil's advocate against the prevailing view, capped at 1–2 turns. Concrete adoption: add `BullBearProbe` as an optional pre-aggregation step that takes the current top-2 highest-confidence `AnalystView` objects with conflicting `Direction`, lets each defend its view in one turn, and emits a single `counterarguments` field on the resulting `AnalystView` (which our schema already has space for — see ADR-0002, but it's currently unfilled).

3. **`PortfolioRating` 5-tier scale (`Buy/Overweight/Hold/Underweight/Sell`).** `agents/schemas.py:PortfolioRating`. Cleaner than binary direction at the *advisory surface* (ADR-0014 chat-mode-advisor), where a user reading "Underweight" is given finer-grained guidance than a coarse "long/short/flat". Important: this is a **display layer** addition; under the hood the discrete action space `{0, ±0.05, ..., ±0.20}` stays. Map `Overweight → +0.10`, `Buy → +0.20`, etc. Concrete adoption: add to `protocol.py` an `AdvisoryRating` enum and a `rating_from_aggregated_signal()` helper for ADR-0014.

---

## 7. Three anti-patterns to AVOID

1. **Free-text `position_sizing` in the trader proposal.** `schemas.py:TraderProposal.position_sizing: Optional[str]`. We must never accept prose for sizing. hermes-quant's sizing is enum-discrete and gate-enforced. Mitigation: any LLM-emitted size must be parsed into a discrete bucket *before* hitting the gate; parse failures default to flat.

2. **The `FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**` string-grep contract.** `agents/trader/trader.py` + downstream string parsing. This is a brittle natural-language API across roles. hermes-quant must keep all inter-role contracts as typed dataclasses (`AnalystView`, `AggregatedSignal`); never grep an LLM's prose for control flow.

3. **No convergence check on the debate; pure turn-cap loop.** `conditional_logic.py:should_continue_debate` runs to a fixed `count`. We can do better: compute embedding-cosine between successive `current_response` payloads and early-stop on saturation (or hard-stop on disagreement that won't close). For our risk gate's silence-by-default posture, persistent disagreement after N turns should produce a *flat* signal, not force a winner — i.e. the absence of convergence is itself a signal.

---

## 8. Where this lands in our ADRs

**Primary target — extend ADR-0023 (deliberative-committee-decision-layer):**
ADR-0023 already establishes the committee concept. Extend it with: (a) the bull/bear adversarial-probe pattern as an optional pre-aggregation node, (b) sequential turn-by-turn structure inspired by `should_continue_debate`, (c) the explicit "judge" role using a `deep_model` LLM tier, (d) hard ban on free-text sizing in any committee-tier output.

**Secondary — extend ADR-0014 (chat-mode-advisor-surface):**
Add the 5-tier `AdvisoryRating` enum (`Buy/Overweight/Hold/Underweight/Sell`) for the chat surface only, with deterministic mapping back to the discrete sizing buckets. Cite TradingAgents `schemas.py:PortfolioRating` as the source.

**New ADR — ADR-0031 (proposed): "Adversarial probe + judge before aggregation":**
Worth its own ADR if (a) the probe runs as a separate phase before the BMA/stacking aggregator (ADR-0003), (b) it has its own metrics surface for "did the probe change the aggregated signal vs. naive averaging," and (c) it has its own backtest fixture. Otherwise fold into ADR-0023. Recommendation: defer the new ADR until a prototype shows the probe meaningfully changes outcomes on the existing fixture; until then, extend ADR-0023.

WROTE r1-tradingagents.md
