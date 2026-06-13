---
title: Strangler Fig gradual replacement transitional architecture seams
id: strangler-fig-gradual-replacement-transitional-architecture-seams
tags:
- pdr-core-host-adapter-arch-e99014
created: '2026-06-13T20:14:58.542808Z'
source: https://martinfowler.com/bliki/StranglerFigApplication.html
status: draft
type: note
tier: institutional
content_type: article
deprecated: false
summary: 'Strangler Fig: gradual replacement beats big-bang rewrite; insert SEAMS
  (ADR-0092 already has AnalystView); transitional architecture is worth it'
---

# Strangler Fig — gradual replacement, transitional architecture, seams (Fowler)

**Source:** Martin Fowler, "Strangler Fig" (bliki, rev. 2024) + "Rewriting Strangler Fig" (2024). https://martinfowler.com/bliki/StranglerFigApplication.html

## The pattern (sub-Q1 migration)
- Metaphor: a vine grows on a host tree, draws nutrients until self-sustaining; the host may die leaving the fig in its shape. = gradual replacement of legacy.
- WHY not a big-bang replacement: "We've seen this simple-sounding [rewrite] plan go down in flames most of the time. Replacing a serious IT system takes a long time, users can't wait, replacements seem easy to specify but it's hard to figure out existing behavior — much of which isn't really wanted, so building it is a waste."
- Four activities (Cartwright/Horn/Lewis): (1) understand desired outcomes; (2) decide how to break the problem into smaller parts; (3) successfully deliver the parts; (4) change the organization to sustain it.
- KEY mechanism: "identifying SEAMS that we can insert into the system to allow it to be split. In a well-designed system these seams would already exist. But such systems are unicorns." Replace small components first (low risk, early ROI, learning).
- Accept TRANSITIONAL ARCHITECTURE: "people often balk at building transitional architecture to allow the new and legacy system to coexist, code that will go away once modernization is complete. While this may appear a waste, the reduced risk and earlier value outweigh its costs."

## Relevance to ADR-0092
- ADR-0092's chosen path (extract a shared core incrementally, keep the live paper system running, port leaves onto cowork's clean spine over time, default-OFF + eval-gated each increment) IS a strangler-fig. Fowler's framing validates rejecting the from-scratch rewrite (Option A) and the big-bang. The crucial enabler is the SEAM — and ADR-0092 already HAS its seam (AnalystView), which is rare ("seams are unicorns"). Adopt Fowler's discipline: be crystal-clear on outcomes (money-correctness first), break into small parts (ledger-first, then cap-seam, then perception), accept transitional coexistence (the shells wrap the core while leaves migrate), and note this is also organizational (one operator -> the "org change" item is lightweight, favoring monorepo single-author cadence).
