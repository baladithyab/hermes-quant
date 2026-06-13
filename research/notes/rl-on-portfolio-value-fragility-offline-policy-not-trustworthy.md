---
title: RL-on-portfolio-value fragility offline policy not trustworthy
id: rl-on-portfolio-value-fragility-offline-policy-not-trustworthy
tags:
- pdr-core-host-adapter-arch-e99014
created: '2026-06-13T20:13:53.993589Z'
source: https://arxiv.org/abs/2505.12759
status: draft
type: note
tier: ground_truth
content_type: paper
deprecated: false
summary: 'MetaTrader+Velay: RL portfolio offline policies overfit in-sample max, fail
  OOS due to non-stationarity; validates charter rejecting RL-on-portfolio-value'
---

# RL-on-portfolio-value fragility — offline policy not trustworthy (arXiv 2505.12759 + 2306.10950)

**Sources:** "Your Offline Policy is Not Trustworthy: Bilevel RL for Sequential Portfolio Optimization" (MetaTrader), arXiv:2505.12759 (2025), https://arxiv.org/html/2505.12759v1 ; Velay et al. "Benchmarking Robustness of Deep RL approaches to Online Portfolio Management", arXiv:2306.10950.

## Core finding (sub-Q5 "RL-on-portfolio-value does not work")
- MetaTrader: standard offline RL learns "the offline policy — transactions that yield the highest profits WITHIN the dataset — even though such a policy may not be generalizable outside the dataset's scope." "Offline policies are less generalizable as they fail to account for the NON-STATIONARY nature of the market." Remedy requires explicit OOD market-data generation + distribution-shift handling (a bilevel partial-offline reformulation) — i.e. naive reward=portfolio-value RL overfits the in-sample maximum.
- Velay et al.: DRL approaches to online portfolio selection are highly sensitive to market representation, behavior objectives, and training process; robustness is poor.

## Relevance to ADR-0092
- Validates the charter's REJECTION of RL-on-portfolio-value as a decision mechanism. A portfolio-value reward maximizes the in-sample profit path, which is non-stationary and does not generalize; it is the RL analog of the LLM "profit mirage." Supports keeping the decision core = deterministic, calibration-weighted aggregation + a deterministic gate, with learning confined to eval-gated, OOS-validated, default-OFF components — NOT an end-to-end RL agent optimizing book value.
