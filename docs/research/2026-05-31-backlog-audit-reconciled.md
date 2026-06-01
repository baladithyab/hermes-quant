# Backlog-Drive PHASE 2 — Audit reconciled (2026-05-31)

Workflow w6ap3a6nl: 6 parallel auditors verified all 51 B-items vs HEAD 0f7de01 + reconciler.

## Headline
- **16 DONE** (verified shipped, mostly this session): B02, B03, B04, B47 + the catalyst/options/PDR/social work.
- **1 DO_NOT_BUILD**: B39 (LangGraph ToolNode-for-all-analysts — deliberate HITL-surprise deferral, gaps.md).
- **34 TRUE-OPEN** (OPEN+GATED+PARTIAL). Of these:
  - **21 agent-doable-now** (code, no external gate) → the build set.
  - **12 externally-gated** (operator/data/market/governance) → documented, not built:
    B05 (operator flag+eval-axis), B06 (operator cron-register), B07 (data: needs B06 firing),
    B10 (operator cron + data corpus), B11 (operator deploy+register), B12 (operator process day),
    B15 (governance), B38 (operator flag), B41 (strategy decision), B43 (skip-tier v0.9+),
    B45 (deferred v0.2 + RL=do-not-build), B48 (blocked on B01-LIVE, not paper).
  - **~9 need research first**: build-research B46/B20/B32/B36/B22/B27; research-then-decide-scope
    B23/B24/B29 (may collapse to DO_NOT_BUILD or fold into B25).

## Execution waves (agent-doable-now)
- **Wave 1 (mechanical, independent, no research):** B13 play_tag on ExecutionRecord; B14 (3 Codex
  follow-ups: journal-then-state ordering, _SNAPSHOT_CACHE Lock, wheel-eviction test); B21 retriever
  same/cross-ticker split wired into llm_committee; B18 render_X helpers wired to a real surface;
  B25 Hypothesis 'monitoring' status + run_cards[] linkage; B30 research-only RiskTier keyword guard.
- **Wave 2 (independent code, light research first):** B50 StackingAggregator cross-corr (default-OFF);
  B34 reporting-lag as_of in fundamentals_provider; B32 validation suite (MC+bootstrap-CI); B22
  AlphaVantageProvider + fallback chain; B36 PIT listed-at-asof universe (code mechanical, data gate).
- **Wave 3 (research-then-DECIDE scope):** B23 task-routing tree; B24 5-section prompt template;
  B29 Goal Ledger (vs fold into B25); B20 insider-transactions tool (after yfinance/EDGAR research).
- **Wave 4:** B26 broker trade-journal CSV parser (needs sample CSV); B46 data_grounding v0.2 5-layer.
- **Wave 5 (depends Wave-4 B26):** B27 shadow rule-extraction; B28 Delta-PnL attribution buckets.
- **Wave 6 (P0, heaviest, separate):** B01 multi-leg PRODUCER seam — CONSUME side DONE+tested
  (dedc8d0); build proposals.py multi_leg kind + recipe→gate→MultiLegProposal builder + quant_propose
  branch so the paper reactor fires end-to-end. B09 larger social-arb labeled set (offline, anytime).

## Safety frame (unchanged)
Every build default-OFF, eval-gated, byte-identical when OFF; deterministic gate / sizing ladder /
kill-switch immutable; no degrading flips. Externally-gated items get a precise operator runbook line,
never a silent drop.
