---
title: DeepFund Time Travel is Cheating leakage-free live eval
id: deepfund-time-travel-is-cheating-leakage-free-live-eval
tags:
- pdr-core-host-adapter-arch-e99014
created: '2026-06-13T20:13:53.492895Z'
updated: '2026-06-17T20:28:23.220647Z'
source: https://arxiv.org/abs/2505.11065
status: evergreen
type: note
tier: ground_truth
content_type: paper
deprecated: false
summary: 'DeepFund 2025: leakage-free LIVE eval; DeepSeek-V3 AND Claude-3.7-Sonnet
  incur NET trading LOSSES; backtest = time travel cheating'
---

# DeepFund / Time Travel is Cheating — leakage-free live eval, frontier LLMs LOSE money (arXiv 2505.11065, 2025, HKUST-GZ)

**Source:** Li, Shi, Wang et al. (HKUST-Guangzhou). "Time Travel is Cheating: Going Live with DeepFund for Real-Time Fund Investment Benchmarking." arXiv:2505.11065 (2025). https://arxiv.org/html/2505.11065v1

## Core finding (sub-Q5 + no-look-ahead rail)
- "A fundamental limitation of existing benchmarks ... is reliance on historical back-testing, inadvertently enabling LLMs to 'time travel' — leveraging future information embedded in their training corpora, thus resulting in information leakage and overly optimistic performance estimates."
- Example: DeepSeek-V3's training extends to July 2024; testing it on 2021-2023 data means "it will have effectively already seen those" outcomes -> cheating that inflates apparent performance, exacerbated by varying knowledge-cutoff dates.
- DeepFund connects to real-time market data published AFTER each model's pretraining cutoff -> fair, leakage-free.
- **HEADLINE: "even cutting-edge models such as DeepSeek-V3 and Claude-3.7-Sonnet INCUR NET TRADING LOSSES within DeepFund's real-time evaluation environment, underscoring the present limitations of LLMs for active fund management."**
- Multi-agent workflow mimics fund process: Financial Planner, Analyst Team, Portfolio Manager.

## Relevance to ADR-0092
- Independent 2025 confirmation (alongside Profit Mirage) that (1) backtest alpha from LLM agents is largely a leakage artifact -> the charter's no-look-ahead asof=publication-time rail is load-bearing and the eval gate must use point-in-time/live evaluation, not naive backtests; (2) frontier LLMs LOSE money in honest live conditions -> the charter's "LLM as evidence-not-authority + deterministic gate as final authority" is not conservatism, it is the only defensible posture given the empirical base rate. Strong support for default-OFF + eval-gated.
