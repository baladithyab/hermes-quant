---
title: ai-hedge-fund deterministic risk gate constrains LLM
id: ai-hedge-fund-deterministic-risk-gate-constrains-llm
tags:
- pdr-core-host-adapter-arch-e99014
created: '2026-06-13T20:05:15.887146Z'
source: https://deepwiki.com/virattt/ai-hedge-fund
status: draft
type: note
tier: practitioner
content_type: docs
deprecated: false
summary: 'ai-hedge-fund: DETERMINISTIC risk manager computes position limits; LLM
  picks action<=max within computed envelope'
---

# virattt/ai-hedge-fund — DETERMINISTIC risk gate constrains LLM (deepwiki, 2026-06)

**Source:** DeepWiki Q&A over virattt/ai-hedge-fund, https://deepwiki.com/virattt/ai-hedge-fund

## Architecture (LangGraph)
- Analyst Agents (e.g. aswath_damodaran_agent, nassim_taleb_agent): each LLM generates signal + confidence + reasoning -> stored in AgentState.analyst_signals. THIS is the perception layer.
- Risk Manager (`risk_management_agent`): **DETERMINISTIC, not an LLM.** Computes position limits in pure Python/numpy/pandas: calculate_volatility_metrics -> calculate_volatility_adjusted_limit -> calculate_correlation_multiplier -> remaining_position_limit + current_price.
- Portfolio Manager (`portfolio_management_agent`): final decision.

## KEY FINDING — polarity matches ADR-0092 charter
- `generate_trading_decision` FIRST deterministically computes `compute_allowed_actions` + max quantities per ticker (considers cash, positions, margin, max_shares from risk manager's remaining_position_limit).
- THEN the LLM is given the analyst signals AND the deterministically-computed allowed_actions, with prompt: **"Pick one allowed action per ticker and a quantity <= the max."**
- So the LLM is STRUCTURALLY BOUNDED: it can only select within a deterministically-computed envelope. The LLM cannot amplify beyond the cap. **Position sizing is deterministic; the LLM only picks within bounds.**
- This is concrete prior art for "deterministic gate computes the envelope, LLM operates within it, can silence (choose hold/less) but cannot amplify (exceed max)."

## Caveat / where it's weaker than ADR-0092
- The enforcement is partly via PROMPT ("pick a quantity <= max") not purely structural — a misbehaving LLM could in principle emit qty > max; whether code clamps it is the question. ADR-0092's "0.0 multiplier, never amplify" wants the clamp to be STRUCTURAL (code multiplies, LLM never names the number). ai-hedge-fund computes the envelope deterministically (structural) but delegates the final pick to the LLM with a prompt-level constraint -> a residual leak unless post-clamped.

## Relevance to ADR-0092
- Strongest positive prior art: a respected multi-agent fund where the risk manager is DETERMINISTIC code and sizing is computed, not free-text. Validates ADR-0092's separation. Use to show the charter's polarity is industry-realizable, AND to sharpen ADR-0092's edge (clamp structurally, don't trust the LLM to honor "<= max").
