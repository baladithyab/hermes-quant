---
title: Safe-RL shielding formal monotonic-restriction prior art
id: safe-rl-shielding-formal-monotonic-restriction-prior-art
tags:
- pdr-core-host-adapter-arch-e99014
created: '2026-06-13T20:12:20.010209Z'
updated: '2026-06-17T15:42:46.606355Z'
source: https://cacm.acm.org/research/shields-for-safe-reinforcement-learning/
status: review
type: note
tier: ground_truth
content_type: paper
deprecated: false
summary: 'Shielding: runtime gate can only RESTRICT never expand action set; pre/post-shield;
  separation of concerns (safety state != agent state); proven no optimality cost;
  breaks when no safe action exists'
---

# Safe-RL shielding — formal monotonic-restriction prior art (CACM + AAAI)

**Source:** "Shields for Safe Reinforcement Learning", Communications of the ACM. https://cacm.acm.org/research/shields-for-safe-reinforcement-learning/ (+ Alshiekh et al. "Safe RL via Shielding" AAAI 2018; Pure-Past Action Masking AAAI 2024; Dynamic Model Predictive Shielding NeurIPS 2024)

## The formalism (THE recognized pattern for sub-Q3 "monotonic-forward authority")
- Three categories of safe RL: (1) constrain to safe actions, (2) add a cost function, (3) **shielding = blocking unsafe actions at runtime**. CACM focuses on shielding: "mechanisms that provide formal safety guarantees by preventing unsafe actions from being executed at runtime" (a runtime enforcement / runtime assurance procedure) -> "verified AI."
- "The shield prevents unsafe behavior of the learning agent during both learning and deployment, **while restricting the agent as little as possible**." (minimal interference)
- **Post-shielding**: shield sits between agent and environment, monitors, and REPLACES an unsafe action with a safe one. **Pre-shielding**: shield provides the LIST of all safe actions; agent picks the best available. Either way the shield can only REMOVE actions from the agent's choice set, never add — this is the structural "can only restrict, never expand" = monotonic.
- Pure-Past Action Masking (AAAI 2024): actions masked per a temporal-logic spec; KEY claim — "the features used in the safety constraint NEED NOT be the same as those used by the learning agent, allowing a clear SEPARATION OF CONCERNS between safety constraints and the agent's reward." Proven: a PPAM-trained agent can still learn ANY optimal policy that satisfies the safety constraints (restriction does not cost optimality).

## Where it BREAKS (sub-Q3 "where does it break")
- "The shield is unable to provide a safe action if NO safe action exists. In such cases the practical solution is to LOWER the safety requirements, if possible." => over-restriction can empty the action set. (For a trading gate, silence-by-default handles the empty set gracefully: if no admissible trade exists, do nothing.)
- Implicit: a shield only guarantees safety w.r.t. the abstraction/state it watches — an incomplete or wrong-state shield gives FALSE safety. The shield must be correct + complete over the right state.

## Relevance to ADR-0092
- This is the missing formal name for the charter's gate polarity. "LLM can silence but never amplify/override" == a pre-shield where the deterministic gate computes the admissible (safe) set and the LLM/committee may only choose within it (or choose less). Map directly: gate = shield; AnalystView confidence in [0,1] = the agent's preference; gate's sized envelope = the masked action set; 0.0 multiplier = the committee removing itself (silence), structurally unable to add size. The "separation of concerns: safety state need not equal agent state" justifies the gate using its OWN deterministic risk state, not trusting LLM-reported numbers. Break cases to handle: empty admissible set (-> silence), and gate-completeness (the gate must see every path = ADR-0092's single execute() chokepoint).
