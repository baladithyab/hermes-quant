---
title: Vibe-Trading LiveOrderGuardTool fail-closed deterministic gate
id: vibe-trading-liveorderguardtool-fail-closed-deterministic-gate
tags:
- pdr-core-host-adapter-arch-e99014
created: '2026-06-13T20:05:16.312955Z'
source: https://deepwiki.com/HKUDS/Vibe-Trading
status: draft
type: note
tier: practitioner
content_type: docs
deprecated: false
summary: 'Vibe-Trading: fail-closed LiveOrderGuardTool hard-caps notional/exposure/leverage
  downstream of LLM; versioned Mandate; kill switch'
---

# HKUDS/Vibe-Trading — LiveOrderGuardTool fail-closed deterministic gate (deepwiki, 2026-06)

**Source:** DeepWiki Q&A over HKUDS/Vibe-Trading, https://deepwiki.com/HKUDS/Vibe-Trading

## Architecture (Swarm DAG)
- Swarms = specialized agents in a DAG (YAML presets e.g. crypto_trading_desk). AgentLoop = Thought/Action/Observation reasoning engine. SwarmRuntime schedules by topological layer.
- Perception: funding_basis_analyst, liquidation_analyst, flow_analyst etc. gather + analyze, emit reports.
- Decision: a "manager"/"strategist" agent (desk_risk_manager / portfolio_manager) synthesizes -> executable trading plan w/ proposed sizing.
- Execution: guarded by a SEPARATE risk-control module.

## KEY FINDING — deterministic gate as FINAL enforcement (matches ADR-0092 exactly)
- `LiveOrderGuardTool` = deterministic risk gate SEPARATE from the LLM. **Fail-closed**: any violation OR inability to verify -> DENY.
- Enforces a "Mandate" with schema version + expiry + halt flag (kill switch).
- HardCaps (quantitative ceilings enforced regardless of LLM): account_funding_usd, max_order_notional_usd, max_total_exposure_usd, max_leverage, allowed_instruments whitelist, max_trades_per_day.
- UniverseConstraint: permitted asset classes, min market cap, min ADV, exclude_symbols denylist.
- Reconciles any quantity into an authoritative notional so caps are enforceable. Daily trade counter increments only on confirmed placement. Failure -> deny or pause-for-reauth + BreachEvent.
- **The LLM PROPOSES sizing/direction; LiveOrderGuardTool VALIDATES + ENFORCES against pre-configured limits. LLM recommendation is overridden by the gate.** This is "LLM is evidence/proposal, deterministic gate is final authority" realized in a 2025 system.
- Note the "Mandate schema version" field: the gate's contract is explicitly versioned — direct prior art for sub-Q4 (versioned contract on the gate).

## Relevance to ADR-0092
- Vibe-Trading is the closest external analog to the ADR-0092 charter: deterministic, fail-closed, versioned-mandate gate that hard-caps notional/exposure/leverage downstream of an LLM committee. Strong CONFIRMATION that "deterministic gate as final authority with LLM upstream as proposal" is a recognized, implemented 2025 pattern. Its "kill switch / halt flag" + "pause-for-reauth + BreachEvent" are features ADR-0092 should explicitly adopt.
