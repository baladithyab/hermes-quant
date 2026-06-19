---
title: HITL ApprovalRequest handler ApprovalDecision fail-closed
id: hitl-approvalrequest-handler-approvaldecision-fail-closed
tags:
- pdr-core-host-adapter-arch-e99014
created: '2026-06-13T20:13:54.522681Z'
updated: '2026-06-17T20:28:23.232753Z'
source: https://docs.agentos.sh/features/human-in-the-loop
status: evergreen
type: note
tier: practitioner
content_type: docs
deprecated: false
summary: 'AgentOS HITL: uniform ApprovalRequest->handler->ApprovalDecision; non-2xx=reject
  (fail-closed); auto-vs-human is a pluggable handler = SHELL decision (matches ADR-0092)'
---

# HITL ApprovalRequest->handler->ApprovalDecision, fail-closed, confidence-threshold (AgentOS, 2025)

**Source:** AgentOS Docs, "Human-in-the-Loop (HITL)". https://docs.agentos.sh/features/human-in-the-loop

## The host-pluggable gate-decision contract (sub-Q3 + two-host asymmetry)
- HITL = "pause an agent run at specific lifecycle events, route the pending action to a human (or an LLM judge, or both), and resume with an approve/reject/modify decision."
- Single uniform contract: **ApprovalRequest -> handler -> ApprovalDecision**, exposed on three surfaces (agency config, workflow/graph node, runtime manager) — all converging on the same contract.
- Handlers are composable functions (ApprovalRequest in, ApprovalDecision out): wrap for logging, fallback chains, conditional routing.
- HTTP handler: POSTs ApprovalRequest JSON to your endpoint, reads back ApprovalDecision; **"Non-2xx is treated as REJECTION with the status code as the reason"** = FAIL-CLOSED by default.
- LLM-judge handler returns {approved, confidence, reasoning}; **if confidence < confidenceThreshold (default 0.7), the request falls through to a fallback handler** (e.g. human).

## Relevance to ADR-0092 (the gate serves both shells off ONE contract)
- This is concrete prior art that the auto-execute-vs-route-to-human decision can live OUTSIDE the gate in a pluggable handler, while the gate emits a uniform request/decision contract. Maps directly to ADR-0092's "whether a host auto-executes or routes to a human is a SHELL decision the core never sees." The core's pdr-gate produces an authorized, sized Proposal + an ApprovalRequest-shaped artifact; hermes-quant's shell binds an auto-approve handler (gated-live), cowork-quant's shell binds a route-to-human / never-auto handler. SAME core gate, two host policies, no fork. Adopt: fail-closed default (non-decision = reject), and the gate as the single chokepoint that emits the decision artifact on every path. The confidence-threshold-with-fallback pattern is a clean way to express silence-by-default (low-confidence -> fall through to do-nothing/human).
