## 1. Graph Topology

```text
START
  -> Market Analyst -> (tools_market -> Market Analyst)* -> Msg Clear Market
  -> Sentiment Analyst -> (tools_social -> Sentiment Analyst)* -> Msg Clear Sentiment
  -> News Analyst -> (tools_news -> News Analyst)* -> Msg Clear News
  -> Fundamentals Analyst -> (tools_fundamentals -> Fundamentals Analyst)* -> Msg Clear Fundamentals
  -> Bull Researcher <-> Bear Researcher
       -> Research Manager
       -> Trader
       -> Aggressive Analyst -> Conservative Analyst -> Neutral Analyst -> Aggressive Analyst
            -> Portfolio Manager
            -> END
```

Analysts are ordered from `selected_analysts`; default is market, social, news, fundamentals. Each analyst has a tool-call cycle and then a message-clear node. Debate join point: either Bull or Bear routes to Research Manager. Risk join point: any risk debater can route to Portfolio Manager. See [setup.py](/tmp/quant-research/sources/TradingAgents/tradingagents/graph/setup.py:87).

## 2. State Flow

`AgentState` extends LangGraph `MessagesState`, then adds identity, reports, debate state, plans, final decision, and memory context: [agent_states.py](/tmp/quant-research/sources/TradingAgents/tradingagents/agents/utils/agent_states.py:46).

Initial state is created with `messages`, `company_of_interest`, `asset_type`, `trade_date`, `past_context`, empty reports, and zeroed debate states: [propagation.py](/tmp/quant-research/sources/TradingAgents/tradingagents/graph/propagation.py:18).

Mutations:
- Analysts append `messages` and fill `market_report`, `sentiment_report`, `news_report`, `fundamentals_report`: e.g. [market_analyst.py](/tmp/quant-research/sources/TradingAgents/tradingagents/agents/analysts/market_analyst.py:79), [sentiment_analyst.py](/tmp/quant-research/sources/TradingAgents/tradingagents/agents/analysts/sentiment_analyst.py:89).
- Clear nodes remove prior messages and add `HumanMessage("Continue")`: [agent_utils.py](/tmp/quant-research/sources/TradingAgents/tradingagents/agents/utils/agent_utils.py:54).
- Bull/Bear mutate `investment_debate_state.history`, side-specific history, `current_response`, and increment `count`: [bull_researcher.py](/tmp/quant-research/sources/TradingAgents/tradingagents/agents/researchers/bull_researcher.py:46), [bear_researcher.py](/tmp/quant-research/sources/TradingAgents/tradingagents/agents/researchers/bear_researcher.py:48).
- Research Manager sets `investment_plan` and debate `judge_decision`: [research_manager.py](/tmp/quant-research/sources/TradingAgents/tradingagents/agents/managers/research_manager.py:45).
- Trader sets `trader_investment_plan` and `sender`: [trader.py](/tmp/quant-research/sources/TradingAgents/tradingagents/agents/trader/trader.py:52).
- Risk debaters mutate `risk_debate_state`, `latest_speaker`, current responses, histories, count: [aggressive_debator.py](/tmp/quant-research/sources/TradingAgents/tradingagents/agents/risk_mgmt/aggressive_debator.py:38).
- Portfolio Manager sets risk `judge_decision` and `final_trade_decision`: [portfolio_manager.py](/tmp/quant-research/sources/TradingAgents/tradingagents/agents/managers/portfolio_manager.py:66).

Reads are downstream: researchers read all reports; trader reads `investment_plan`; risk debaters read reports plus trader plan; PM reads risk history, research plan, trader plan, and `past_context`.

## 3. Conditional Routing

Every condition is in [conditional_logic.py](/tmp/quant-research/sources/TradingAgents/tradingagents/graph/conditional_logic.py:14):

- Market: if last message has `tool_calls` -> `tools_market`; else `Msg Clear Market`.
- Social/Sentiment: `tool_calls` -> `tools_social`; else `Msg Clear Sentiment`.
- News: `tool_calls` -> `tools_news`; else `Msg Clear News`.
- Fundamentals: `tool_calls` -> `tools_fundamentals`; else `Msg Clear Fundamentals`.
- Investment debate: if `investment_debate_state.count >= 2 * max_debate_rounds` -> `Research Manager`; else if `current_response.startswith("Bull")` -> `Bear Researcher`; else `Bull Researcher`.
- Risk debate: if `risk_debate_state.count >= 3 * max_risk_discuss_rounds` -> `Portfolio Manager`; else if latest speaker starts `Aggressive` -> `Conservative`; if `Conservative` -> `Neutral`; else `Aggressive`.

## 4. Error Handling

Plain LLM calls are not locally caught. Analyst/debater nodes call `invoke(...)`; failures propagate out of `graph.invoke` or `graph.stream`, halting the run. There is no skip branch.

Structured-output nodes use one fallback: try structured invoke, catch any exception, log warning, then retry once as plain text; if plain text fails, it propagates: [structured.py](/tmp/quant-research/sources/TradingAgents/tradingagents/agents/utils/structured.py:62).

Checkpointing is opt-in and off by default: [default_config.py](/tmp/quant-research/sources/TradingAgents/tradingagents/default_config.py:67). If enabled, graph is recompiled with `SqliteSaver`, resumes by ticker/date thread, and clears checkpoints only on successful completion: [trading_graph.py](/tmp/quant-research/sources/TradingAgents/tradingagents/graph/trading_graph.py:310).

## 5. Streaming / Blocking

Normal mode is blocking synchronous `self.graph.invoke(...)`; debug mode uses synchronous `self.graph.stream(...)` over per-node chunks: [trading_graph.py](/tmp/quant-research/sources/TradingAgents/tradingagents/graph/trading_graph.py:350). No async nodes are defined. Graph-level parallelism is not used: setup chains analysts sequentially, and conditionals choose one branch. `analyst_concurrency_limit` is passed into the plan, but setup still wires `plan.specs[i] -> plan.specs[i+1]`: [setup.py](/tmp/quant-research/sources/TradingAgents/tradingagents/graph/setup.py:91).

## 6. Lessons for hermes-quant ADR-0023

1. Keep committee memory explicit and typed. TradingAgents separates reports, debate histories, judge decisions, and final decision in `AgentState`; ADR-0023 should define committee state keys up front, not pass opaque transcripts.

2. Put deliberation limits in routing, not prompts. Debate termination is deterministic count-based routing, independent of model compliance: [conditional_logic.py](/tmp/quant-research/sources/TradingAgents/tradingagents/graph/conditional_logic.py:55).

3. Treat resumability as orchestration infrastructure. Checkpointing is graph-level and ticker/date scoped, not agent-specific. Hermes should make retry/resume a runtime concern, while still requiring node-level fallbacks for model-format failures.