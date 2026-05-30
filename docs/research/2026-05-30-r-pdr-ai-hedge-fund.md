# Research: virattt/ai-hedge-fund architecture for a unified PDR pipeline + multi-signal fusion

- Date: 2026-05-30
- Source: deepwiki `ask_question` on `virattt/ai-hedge-fund` (5 queries)
- Purpose: extract the decision-layer architecture (trader stage + risk arbitration + signal fusion)
  relevant to hermes-quant's Perception->Decision->Reaction pipeline.

---

## TL;DR (what to adopt)

- **Steal the trader-stage shape, NOT its authority model.** ai-hedge-fund's portfolio manager is a
  hybrid: deterministic code (`compute_allowed_actions`) computes the *legal* action set + max quantity
  per ticker, then an LLM only *picks within* that pre-pruned, capped envelope. The LLM literally cannot
  exceed the limits. This is a clean concrete pattern for hermes-quant's gate-then-LLM stance: deterministic
  risk gate computes the allowed sizing envelope first, LLM/committee only selects inside it.
- **Risk-before-decision sequencing is identical to ours, but weaker.** Their `risk_management_agent` runs
  *before* the portfolio manager and writes a `remaining_position_limit` (vol-adjusted, correlation-adjusted
  % of NAV) that the PM must respect. hermes-quant's DefaultRiskGate is already this, and is stronger
  (final authority, silence-by-default). Adopt the explicit "limit object handed downstream" wiring;
  reject their "LLM does the final fusion" model — for us fusion stays in BMAAggregator.
- **Their fusion is LLM-arbitrated and unbounded in direction — a rejected anti-pattern for us.** All analyst
  signals are compressed to `{sig, conf}` and dumped into one LLM prompt that decides action/qty. No
  deterministic aggregation, no require_ensemble, LLM can amplify. Keep our BMA + require_ensemble +
  silence-only-never-amplify rails; only borrow the *signal envelope contract*, not the arbitration.
- **The signal protocol is reusable verbatim:** every analyst emits
  `{signal: bullish|bearish|neutral, confidence: 0-100, reasoning}`; the final decision is
  `{action, quantity, confidence, reasoning}`. Sources (sentiment/news/fundamentals/technical/valuation)
  all enter as *peer analysts in parallel* writing to one `analyst_signals` map — directly analogous to
  semantic/social entering BMA as a peer (ADR-0074/0076). Their **v2 quant stack** (signals scored -1..+1,
  CPCV/PBO validation, point-in-time backtest, Black-Litterman/risk-parity portfolio) is the better north
  star than v1 persona agents.

---

## 1. Agent/stage pipeline (data -> trade signal)

Orchestrated with **LangGraph**; agents are graph nodes, a shared `AgentState` dict is the bus
(`state["data"]` holds `tickers`, `portfolio`, `analyst_signals`, plus `messages`/`metadata`).

Flow defined in `src/main.py::create_workflow`:

1. **start_node** — initializes `AgentState` (tickers, portfolio).
2. **Analyst agents (parallel fan-out)** — two families, all registered in
   `src/utils/analysts.py::ANALYST_CONFIG`:
   - *Analytical agents*: `valuation_analyst_agent`, `fundamentals_analyst_agent`,
     `technical_analyst_agent`, `sentiment_analyst_agent`, `news_sentiment_agent`.
   - *Investment-philosophy (persona) agents*: Warren Buffett, Ben Graham, Cathie Wood, Michael Burry,
     Peter Lynch, Phil Fisher, Stanley Druckenmiller, Aswath Damodaran, Nassim Taleb, Mohnish Pabrai, etc.
     Each fetches financial data + uses an LLM to synthesize one signal.
   - Every analyst writes `{signal, confidence, reasoning}` to
     `state["data"]["analyst_signals"][agent_id]`.
3. **risk_management_agent** — runs *after all analysts*. Fetches prices, computes daily/annualized
   volatility + a correlation matrix, derives a **volatility-adjusted position limit** (higher vol ->
   lower % of NAV), applies a **correlation multiplier**, and writes `remaining_position_limit` +
   `current_price` per ticker into `analyst_signals`.
4. **portfolio_management_agent** — final decision node (see #2).
5. **END** — returns per-ticker `PortfolioDecision`.

The graph enforces: analysts -> risk -> portfolio. Risk limits are established *before* any trade decision.

## 2. How analyst opinions are FUSED (arbitration: deterministic vs LLM)

There is **one arbitration step: the portfolio manager**, and it is a **hybrid**:

1. **Collect + compress** — gathers every analyst's signal per ticker (excluding the risk agent),
   compresses to a `{sig, conf}` map.
2. **Read risk limits** — pulls `remaining_position_limit` + `current_price` from the risk agent,
   computes `max_shares`.
3. **`compute_allowed_actions` (DETERMINISTIC)** — per ticker, computes the legal action set and a max
   quantity for each:
   - sell <= long_shares held; buy <= min(cash-affordable, max_qty);
   - cover <= short_shares; short <= min(max_qty, available_margin-bounded);
   - hold always allowed.
   - **Actions with qty 0 are pruned** (except hold) to shrink the LLM prompt. Tickers where only `hold`
     survives get a pre-filled hold and never reach the LLM.
4. **`generate_trading_decision` (LLM)** — the LLM receives the compressed signals + the pruned/capped
   allowed-action menu, with the instruction "pick one allowed action per ticker and a quantity <= the max."
   Output parsed via `PortfolioManagerOutput` -> `{ticker: PortfolioDecision}`. On invalid output it
   defaults to `hold`. **The LLM cannot exceed the deterministic caps** — code bounds the envelope, LLM
   only selects inside it.

Note: a `weighted_signal_combination` exists in `technicals.py` but is *internal* to the technical agent
(combining its own sub-strategies), not the cross-agent fusion. Cross-agent fusion is the LLM step above —
there is **no deterministic multi-analyst aggregator** (contrast hermes-quant's BMAAggregator).

## 3. Where sentiment / news / fundamentals enter vs price/technical

All enter as **peer analyst nodes in the same parallel block** — none is privileged over price/technical:

- `sentiment_analyst_agent` — insider trades + company news, fixed weights (insider 30% / news 70%).
- `news_sentiment_agent` — fetches company news, uses an LLM to classify articles lacking sentiment, then
  aggregates (counts of bullish/bearish/neutral).
- `fundamentals_analyst_agent` — financial metrics; profitability, growth, financial health, valuation ratios.
- `technical_analyst_agent` — historical prices; trend-following, mean-reversion, momentum, volatility,
  stat-arb, combined via weighted ensemble.
- `valuation_analyst_agent` — DCF, owner-earnings, EV/EBITDA, residual-income; bullish if weighted
  intrinsic value >15% over market cap, bearish if < -15%.

All emit the same `{signal, confidence(0-100), reasoning}` shape and write to one `analyst_signals` map.
Persona agents additionally fold sentiment/news/fundamentals/technicals into their own LLM reasoning.

## 4. Decision / signal protocol (object fields)

Pydantic models.

Analyst signal (per-agent, e.g. `AswathDamodaranSignal`, `WarrenBuffettSignal`; generic `AnalystSignal`
in `src/data/models.py`):
```python
class <Agent>Signal(BaseModel):
    signal: Literal["bullish", "bearish", "neutral"]
    confidence: float  # 0..100  (int in some agents)
    reasoning: str
```

Final trade decision (`src/agents/portfolio_manager.py`):
```python
class PortfolioDecision(BaseModel):
    action: Literal["buy", "sell", "short", "cover", "hold"]
    quantity: int          # number of shares
    confidence: int        # 0..100
    reasoning: str

class PortfolioManagerOutput(BaseModel):
    decisions: dict[str, PortfolioDecision]   # keyed by ticker
```

## 5. Reusable for hermes-quant's Decision layer (trader stage + risk arbitration)

**Adopt:**
- **Deterministic-envelope-then-LLM-select pattern** (`compute_allowed_actions`). This is the cleanest
  external precedent for our rail "deterministic gate is final authority, LLM is evidence/selection only."
  Map: DefaultRiskGate emits the allowed discrete sizing envelope {0,±0.05,±0.10,±0.15,±0.20} per ticker
  *before* any committee/LLM step; the LLM/committee may only pick within it. The pruning trick
  (drop zero-capacity actions, pre-fill forced holds) is a free token/latency win for any LLM trader stage.
- **Explicit risk-limit object handed downstream** — a typed `remaining_position_limit`/envelope object
  the decision stage must consume, rather than implicit coupling. Strengthens the ADR-0004 contract.
- **Uniform analyst signal envelope** `{signal, confidence, reasoning}` as the perception->decision contract,
  with all sources (semantic, social-arb, numerical, catalyst, fundamentals, Kronos) writing to one
  signal map as peers — exactly the PDR "all signals as data points" north-star and consistent with
  ADR-0074/0076 (semantic/social as BMA peers).
- **v2 quant stack as a design reference** (signals normalized to -1..+1, CPCV/PBO overfitting guards,
  point-in-time backtest with txn-cost modeling, Black-Litterman/risk-parity portfolio construction).
  This validation discipline matches hermes-quant's eval-gated, lookahead-honest rails better than v1.

**Reject (conflicts with hermes-quant rails):**
- **LLM as the fusion/arbitration authority.** Their PM lets an LLM choose direction+size by reading raw
  signals; no deterministic aggregator, no require_ensemble, LLM can effectively amplify a lone signal.
  hermes-quant must keep fusion in BMAAggregator (ADR-0003), keep require_ensemble (no signal fires alone),
  and keep silence-only-never-amplify. Borrow the *envelope contract*, not the arbitration.
- **`short` as a first-class action** — out of scope for paper-only long/flat posture today; the action
  enum should stay aligned to discrete-sizing semantics, not buy/sell/short/cover.

**Gaps it does NOT solve (still hermes-quant-specific):** social-arbitrage trend VELOCITY detection,
cross-source data-convergence at the perception layer, and edge-time/saturation-based EXIT — ai-hedge-fund
has none of these (it sizes on confidence, like our current system).
