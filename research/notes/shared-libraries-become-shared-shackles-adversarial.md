---
title: Shared libraries become shared shackles adversarial
id: shared-libraries-become-shared-shackles-adversarial
tags:
- pdr-core-host-adapter-arch-e99014
created: '2026-06-13T20:13:53.026190Z'
updated: '2026-06-17T15:42:46.619554Z'
source: https://stevenstuartm.com/blog/2026/01/06/the-false-economy-of-shared-libraries.html
status: review
type: note
tier: commentary
content_type: blog
deprecated: false
summary: 'Adversarial: shared libs couple/throttle teams; BUT concedes SECURITY protocols
  (stable/catastrophic/thin/coupling-is-a-feature) as the exception = exactly the
  money-gate profile'
---

# Shared libraries become shared shackles — ADVERSARIAL on the shared core (Stuart, 2026)

**Source:** Steven Stuart, "How Shared Libraries Become Shared Shackles", 2026-01-06. https://stevenstuartm.com/blog/2026/01/06/the-false-economy-of-shared-libraries.html

## The contrarian case against ADR-0092's shared core (sub-Q4 adversarial)
- Strong stance: "A shared library is almost never the right answer because the problem it solves (duplicated code) rarely justifies the problems it creates (coupling, versioning, blocked teams)."
- Uncounted costs: version conflicts/upgrade pain ("one place to maintain becomes one place that blocks everyone"); teams blocked waiting for changes (2-hour change -> 2-week dependency chain); debugging across boundaries; bloat-or-fragmentation; obscured accountability.
- Diagnosis when two services need the same function: (1) cohesion problem -> extract a SERVICE with an API (clear owner/contract, no implementation coupling); (2) coupling problem -> boundaries drawn wrong; (3) genuinely independent -> just duplicate ("coordination cost exceeds maintenance cost of duplication").

## CRUCIAL nuance — the exceptions that EXACTLY fit a money-gate
- **Exception: SECURITY protocols.** Shared libraries DO make sense when: "the domain is stable and well-understood"; "the cost of getting it wrong is CATASTROPHIC ... blast radius too large"; "the surface area is thin and focused"; "AUTONOMY ISN'T THE GOAL — you actually WANT teams to do it the same way; the coupling is a FEATURE, not a bug." Even then: keep it minimal, provide the primitive, get out of the way.
- SDK distinction: an SDK ("here's how to use our thing") is legitimate; a shared library ("here's how you should build your thing") is an imposition.

## Relevance to ADR-0092 (the strongest counter-argument, and its own rebuttal)
- This is the best adversarial case against extracting pdr-core: shared internal abstractions couple independent consumers and throttle tempo. ADR-0092 must answer it. The answer is IN the source: the money-core is the SECURITY-PROTOCOL exception, not the utility-wrapper anti-pattern. Money arithmetic + the gate + the ledger fold are (a) stable + well-understood (double-entry is centuries old), (b) catastrophic if wrong (a defect debits a real bank account), (c) thin + focused (ledger + gate + typed contracts), (d) autonomy is NOT the goal — you WANT both hosts to do money IDENTICALLY (the coupling is the entire point; divergence is the bug ADR-0092 fixes). So the shared core is precisely the case the adversary CONCEDES. BUT heed the warnings: keep pdr-core MINIMAL (ledger+gate+contracts+fold ONLY — do NOT let it accrete host utilities, "helpful" perception helpers, or API-client cruft, or it becomes the bloat-shackle). The host-specific perception/reaction stays in the shells. And ADR-0092's "third versioned artifact / coordination cost" negative is real — mitigate with monorepo-path-dep (single author, atomic cross-cut change) rather than a separately-published-pinned package across repos.
