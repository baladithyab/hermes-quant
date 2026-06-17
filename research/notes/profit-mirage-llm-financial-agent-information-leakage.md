---
title: Profit Mirage LLM financial-agent information leakage
id: profit-mirage-llm-financial-agent-information-leakage
tags:
- pdr-core-host-adapter-arch-e99014
created: '2026-06-13T20:12:18.682308Z'
updated: '2026-06-17T15:42:46.593079Z'
source: https://arxiv.org/abs/2510.07920
status: review
type: note
tier: ground_truth
content_type: paper
deprecated: false
summary: 'Profit Mirage 2025: GPT-4o memorizes 85%+ of historical market QA; agents
  lose 55.68% Sharpe OOS; remedy=LLM as strategy generator not decision-maker'
---

# Profit Mirage — LLM financial-agent information leakage (arXiv 2510.07920, 2025)

**Source:** Li, Zeng, Xing, Xu, Xu. "Profit Mirage: Revisiting Information Leakage in LLM-based Financial Agents." arXiv:2510.07920 (2025). https://arxiv.org/html/2510.07920v1

## Core finding (LOAD-BEARING for no-look-ahead rail + sub-Q5 anti-patterns)
- "Most systems exhibit a 'profit mirage': dazzling back-tested returns evaporate once the model's knowledge window ends, because of the inherent information leakage in LLMs."
- Leakage is "baked into the LLM itself": foundation models ingest web-scale corpora with post-hoc explanations of past prices ("NVIDIA surged 190% in 2023 on AI boom") alongside contemporaneous news.

## The numbers (cite these)
- FinLake-Bench memorization probe: 2,000 historical QA pairs ("did the market rise on date T?"). **GPT-4o and peers answer correctly OVER 85% of the time** — far above chance — confirming the facts are MEMORIZED.
- Temporal segmentation: historical period (Q2-Q3 2021, backtest) vs latest period (Q3-Q4 2024, generalization). Market returns nearly identical (+13.79% vs +13.35%) to control for regime. **Agents suffer 55.68% Sharpe decay** out-of-window despite comparable conditions => "LLM-based agents are not genuinely forecasting but rather recognizing patterns from their training data."
- Leakage metrics (Prediction Consistency PC after counterfactual perturbation): FinMem PC=0.8213 (>82% predictions unchanged despite perturbation); all methods PC>0.69 => "reliance on memorized patterns over input-driven forecasting."

## Design prescription (matches charter polarity)
- FactFin mitigates by "using LLMs as STRATEGY GENERATORS rather than direct decision-makers, leveraging counterfactual reasoning and strategy evolution." => OOS Sharpe 1.4x higher than baselines.

## Relevance to ADR-0092
- This is the empirical backbone for two charter rails: (1) NO-LOOK-AHEAD (asof=publication time) is not optional hygiene — without it, reported alpha is a memorization artifact; (2) LLM-as-evidence-not-authority — the paper's own remedy is to demote the LLM from "decision-maker" to "strategy/evidence generator," exactly ADR-0092's polarity. Cite as the strongest 2025 evidence that LLM-final-authority + leaky backtests is a documented failure mode.
