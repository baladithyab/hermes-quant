---
title: TradingAgents LLM-as-final-authority anti-pattern
id: tradingagents-llm-as-final-authority-anti-pattern
tags:
- pdr-core-host-adapter-arch-e99014
created: '2026-06-13T20:03:41.050741Z'
updated: '2026-06-17T15:42:46.570200Z'
source: https://deepwiki.com/TauricResearch/TradingAgents
status: review
type: note
tier: practitioner
content_type: docs
deprecated: false
summary: 'TradingAgents: LLM Portfolio Manager is FINAL authority, risk team only
  debates - the charter''s forbidden anti-pattern'
---

# TauricResearch/TradingAgents — LLM committee with LLM-as-final-authority (deepwiki, 2026-06)

**Source:** DeepWiki Q&A over TauricResearch/TradingAgents, https://deepwiki.com/TauricResearch/TradingAgents

## Architecture (PDR-ish, LangGraph state machine)
- Analyst Team (Fundamentals/Market/Technical, News, Sentiment) -> reports in AgentState (perception).
- Researcher Team: Bull vs Bear debate -> Research Manager synthesizes investment_plan.
- Trader Agent: synthesizes reports + investment_plan -> structured TraderProposal.
- Risk Management Team: Aggressive/Conservative/Neutral debators debate the trader plan (multi-perspective).
- **Portfolio Manager = "the final authority"**: synthesizes risk debate + plans + past_context -> structured PortfolioDecision (5-tier Buy/Overweight/Hold/Underweight/Sell) = final_trade_decision.

## CRITICAL FINDING (contradicts ADR-0092 charter directly)
- TradingAgents puts an **LLM (the Portfolio Manager) as the FINAL EXECUTION AUTHORITY.** The "risk management team" only ADVISES/debates via dialectic; it does NOT deterministically gate or veto.
- Fallback `invoke_structured_or_freetext` ensures a decision always emerges, falling back to FREE-TEXT then rendering to a rating — i.e. free-text decisioning is in the loop.
- This is precisely the anti-pattern hermes/cowork charter forbids: LLM as final authority, no deterministic risk gate with veto power, dialectic-only "risk."
- The "5-tier rating" is a structured-output schema, NOT a deterministic risk gate. There is no arithmetic gate that can hard-clamp size to zero independent of the LLM's judgment.

## Relevance to ADR-0092
- TradingAgents is the most-cited multi-agent trading SOTA, and it is structurally on the WRONG side of the charter's gate-polarity invariant. It validates ADR-0092's decision driver "LLM committee as evidence-not-authority" by being a concrete counter-example: a respected framework that lets the committee BE the authority. Use as the canonical "what NOT to do" exemplar for sub-Q3 and sub-Q5.
