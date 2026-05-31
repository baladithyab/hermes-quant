---
status: proposed
date: 2026-05-30
deciders: [lead quant-systems architect]
consulted: [self-evolve capability-map synthesis, SOTA research pass, current-state audit]
informed: [operator]
---

# ADR-0081: Bounded decaying belief store with weekly/monthly distillation tiers (CVRF + FINMEM)

## Context and Problem Statement

W1 (commit `08326e1`) ignited the dark loop: `DecisionLog.record_decision()` now
fires at the live decision site, so `reflections.jsonl` finally accretes per-trade
lessons that the retriever can inject into the portfolio-manager prompt
(`retriever.py:281,452`). That closes the *per-trade* edge. But the corpus is now
a growing flat pile of individual reflection rows with **no distillation tier**:
nothing compares sustained winners against sustained losers, nothing condenses
recurring `lesson_category` patterns into a small set of durable beliefs, and
nothing ages stale-regime lessons out. This is the literal M14 gap (audit §1
"Per-trade only — no weekly/monthly aggregation"; SOTA §0 "The gap is a cadence,
not a component").

Worse, the promotion gate is built around a field nobody produces. `governance/
promotion.py` reads `weekly_retro_promotion_readiness` from `promotion_event`
audit rows (consumer at `promotion.py:136,158,235`; the precondition defaults
`False` at `promotion.py:82`), but **no producer ever writes it** (audit §4, O3).
The gate's weekly-retro precondition is therefore permanently `False` — a dangling
consumer that silently blocks promotion forever.

This is money-software, paper-only, and the memory/reflection loop we are building
is the **#1 documented self-evolution degradation vector** ("Misevolution", ICLR
2026: "degradation of safety alignment after memory accumulation"; SOTA §5). An
unbounded, ever-growing, ungoverned belief store is precisely the failure mode the
safety literature warns against. The distillation tier must therefore be **bounded,
decaying, external-truth-scored, and deterministic in its promote/expire rule** —
never an LLM grading its own lessons.

## Decision Drivers

- **Bounded store (anti-Misevolution / anti-model-collapse).** Cap active beliefs
  (Reflexion Ω≈1–3 per role; FINCON small-set) and decay them so stale-regime
  lessons fade. Unbounded accumulation measurably degrades safety alignment and
  blows the prompt budget while diluting signal.
- **External-truth scoring (alpha, not raw P&L).** A belief's evidence is realized
  alpha-vs-benchmark from market data (`Reflection.alpha_return`,
  `reflector.py:104`), never an LLM self-score, never the agent's own narrative
  re-ingested as truth. The agent cannot author the signal that grades its beliefs
  (reward-hacking taxonomy, SOTA §5).
- **Oracle-provenance carried through distillation.** Every distilled belief must
  remain tagged as the agent's OWN prior output (the `tau_observable` guard already
  present at `reflector.py:12-19` and enforced first in `retriever.py:351-362`) and
  must never be re-ingested as ground truth (model-collapse mitigation, SOTA §5
  item 7).
- **Deterministic (non-LLM) promote/expire.** Which lesson becomes a durable belief
  and which fades is decided by a FINMEM-style access-counter + half-life rule, not
  by an LLM deciding what to keep (no self-grading; bounds the store).
- **Closes O3 deterministically.** The weekly tier must write
  `weekly_retro_promotion_readiness` to a `promotion_event` payload so the existing
  gate consumer (`promotion.py:158`) stops being permanently blocked — without the
  belief loop ever touching a hard limit.
- **Selective propagation, not broadcast.** A belief is injected into the ONE
  relevant role's prompt (FINCON CVRF "selectively propagated only to the relevant
  analyst"), avoiding the echo-chamber where every role reads every lesson.

## Considered Options

- **(a) FINCON-CVRF verbal-belief distillation + FINMEM access-counter/half-life
  deterministic promotion** — weekly winners-vs-losers (by realized alpha) distilled
  into ≤N verbal belief-deltas, selectively propagated to one role's prompt; a
  monthly meta tier over episodes; a deterministic non-LLM promote/expire rule.
- **(b) Raw reflection replay into the prompt with no distillation** — prepend the
  last K raw reflection rows directly (what TradingAgents' markdown buffer does).
- **(c) LLM-judged belief curation** — let an LLM read the reflection corpus and
  decide which lessons are durable, which to keep, and rewrite/merge them.

## Decision Outcome

Chosen option: **(a) FINCON-CVRF distillation + FINMEM deterministic promote/expire**,
because it is the only option that produces a *bounded, decaying, externally-scored,
provenance-preserving* belief store with a *non-LLM* promote/expire rule — satisfying
every safety-frame primitive (capability-map §5) while closing O2 + O3. Option (b)
blows the token budget and is a pure echo chamber (no winners-vs-losers signal,
unbounded growth); option (c) is rejected outright as self-grading — an LLM curating
the agent's own lessons is exactly the reward-hacking / model-collapse vector the
rails forbid.

The decision specifies four things, detailed in **More Information**: the belief
schema, the weekly distillation, the monthly meta tier, and the deterministic
promote/expire rule (including how it closes O3).

### Consequences

- **Positive**: Converts the now-live per-trade reflection corpus (W1) into a small,
  governed set of durable beliefs re-injected into prompts — the literal M14 edge,
  closing O2.
- **Positive**: Closes O3 — the weekly tier writes `weekly_retro_promotion_readiness`
  to a `promotion_event`, un-blocking the gate consumer at `promotion.py:158` that
  has been permanently `False`.
- **Positive**: The store is bounded and decaying by construction (FINMEM half-life +
  budget cap), directly countering the Misevolution memory-accumulation degradation
  vector; provenance (`tau_observable`) survives distillation, so no belief is ever
  re-ingested as ground truth.
- **Positive**: Promote/expire is fully deterministic and reproducible — no LLM grades
  its own beliefs; the same corpus + asof always yields the same active belief set.
- **Negative (belief staleness)**: A half-life tuned too long lets a stale-regime
  belief survive a regime change and mis-advise a role until it decays or is
  out-evidenced; tuned too short, a real durable lesson decays before it pays off.
  The half-life constants are a tunable that itself must be jitter-tested, not
  decimal-optimized (MT3 / AMZN-weight rule).
- **Negative (distillation losing signal)**: Compressing many reflections into ≤N
  belief-deltas necessarily discards detail; a rare-but-correct minority lesson can
  be averaged away by the winners-vs-losers split, and the alpha-based ranking can be
  dominated by a few large-magnitude trades. Mitigated by keeping raw
  `reflections.jsonl` immutable and append-only (the belief store is a *derived,
  rebuildable* view, never the source of truth).
- **Negative (budget cap dropping a real lesson)**: The per-role belief budget (≤N)
  can evict a genuinely useful belief when N is exceeded, because eviction is by the
  deterministic score (lowest access-counter × recency × importance), which is a
  proxy, not ground truth. Accepted as the explicit anti-Misevolution trade: a
  bounded store that occasionally drops a good lesson is safer than an unbounded one
  that degrades alignment. The evicted belief is recoverable on the next weekly pass
  if its pattern recurs in the (immutable) reflection corpus.
- **Neutral**: Adds two crons (`quant-weekly-retro`, `quant-monthly-meta-retro`) and a
  new derived artifact (`beliefs.jsonl`); both default-OFF behind
  `HERMES_QUANT_WEEKLY_RETRO` / `HERMES_QUANT_MONTHLY_META_RETRO`. Off-state is
  byte-identical to today.
- **Neutral**: Selective propagation means a belief mined from one role's history
  changes only that role's prompt; cross-role transfer is deliberately not attempted
  in this ADR.

## Pros and Cons of the Options

### (a) FINCON-CVRF distillation + FINMEM deterministic promote/expire

- Good, because the output is *verbal beliefs only* — context injected into a prompt,
  never a parameter, limit, or sizing-ladder change. The advisory plane stays
  strictly advisory (capability-map §5 invariant).
- Good, because winners-vs-losers is split by realized **alpha** (external truth),
  not raw P&L or an LLM score — the agent cannot author its own reward (SOTA §5 #1).
- Good, because promote/expire is a deterministic FINMEM rule (access-counter +
  half-life + importance), so no LLM grades what to keep, and the store is provably
  bounded and reproducible.
- Good, because it closes both O2 (distillation tier) and O3 (writes
  `weekly_retro_promotion_readiness`) with one mechanism.
- Good, because selective propagation (FINCON) injects a belief only into the one
  relevant role's prompt, avoiding the echo chamber.
- Bad, because distillation loses detail — a rare correct minority lesson can be
  averaged out, and half-life mis-tuning causes staleness (see Negative consequences).
- Bad, because it adds two crons, a derived artifact, and tunable half-life constants
  that themselves need jitter-testing and an eval gate before the flags flip.

### (b) Raw reflection replay into the prompt (no distillation)

- Good, because it is trivial to build — prepend the last K rows; this is essentially
  what the retriever already does per-ticker (`retriever.py:372`).
- Bad, because it is unbounded: the corpus grows forever and the prompt budget blows
  out — the exact Reflexion-Ω failure (SOTA §0) and the Misevolution accumulation
  vector (SOTA §5).
- Bad, because it is a pure echo chamber: no winners-vs-losers signal, no external-
  truth ranking, no decay; stale-regime lessons never fade and dilute the live signal.
- Bad, because it does nothing to close O3 — no `weekly_retro_promotion_readiness`
  is ever produced.

### (c) LLM-judged belief curation

- Good, because an LLM could merge near-duplicate lessons and write fluent,
  human-readable beliefs.
- Bad, because it is **self-grading** — the agent's own model decides which of the
  agent's own lessons are "true" and durable. This is precisely the reward-hacking /
  Goodhart co-adaptation vector (SOTA §5 #1) and the model-collapse feedback loop
  (SOTA §5; "training a model on its own un-validated outputs accelerates
  degradation"). It violates the external-truth-evaluator-only rail.
- Bad, because it is non-deterministic and non-reproducible: the same corpus yields
  different belief sets across runs, defeating the held-out gate and the audit trail.
- Bad, because it offers no principled bound on the store size — the LLM has no
  deterministic budget or decay discipline.

## More Information

### 1. Belief schema (`beliefs.jsonl`, derived/rebuildable — never source-of-truth)

A belief is a distilled, decaying, provenance-tagged verbal delta. One row =

| Field | Meaning |
|---|---|
| `schema_version` | int, for forward migration |
| `belief_id` | SHA-stable id over `(tier, role, lesson_category, asof_distilled)` |
| `tier` | `weekly` \| `monthly` (FINMEM layer analogue; sets the half-life) |
| `role` | the ONE committee role this belief is propagated to (`portfolio_manager` \| `bull` \| `bear` \| `risk_*`) — CVRF selective propagation |
| `lesson_category` | the `LessonCategory` enum it generalizes (`reflector.py:75-83`) |
| `verbal_delta` | the distilled belief text (≤1–2 sentences; "what to do differently") |
| `alpha_evidence` | mean realized **alpha** of the winners-vs-losers split that produced it (external truth; from `Reflection.alpha_return`) — never raw P&L, never an LLM score |
| `support_n` | number of reflections backing it (gates against single-trade beliefs) |
| `half_life_days` | by tier (weekly = shorter, monthly = longer) — drives decay |
| `access_counter` | FINMEM counter; +1 each time the belief is surfaced into a prompt |
| `importance` | FINMEM importance; +K on a pivotal *profitable* event; drives promotion/eviction |
| `recency` | last-touched decay value in `(0,1]`; reset to `1.0` on access |
| `oracle_provenance` | `{ "source": "agent_reflection", "tau_observable_max": <ISO>, "decision_ids": [...] }` — every belief tagged as the agent's OWN prior output; `tau_observable_max` = the max over the backing reflections so the retriever's `tau_observable < asof` guard still applies at the belief level |
| `asof_distilled` | ISO-8601 UTC when this belief was distilled (the distillation tick's asof) |
| `status` | `active` \| `expired` (append-only; expiry is a new row, never an in-place edit) |

The Oracle guard is preserved end-to-end: a belief is eligible for injection only if
`oracle_provenance.tau_observable_max < asof` of the decision being made — the same
invariant the retriever enforces today (`retriever.py:351-362`), lifted to the belief
level so distillation never smuggles future information into a prompt.

### 2. Weekly distillation (`quant-weekly-retro`, `HERMES_QUANT_WEEKLY_RETRO=1`, default-OFF)

CVRF lower-half. Per role:

1. Load the trailing-week `reflections.jsonl` rows resolvable as of the tick
   (`tau_observable < asof`).
2. **Split winners vs losers by realized alpha** (`alpha_return`), not raw P&L —
   closing the SOTA "tauric" gap. Group by `lesson_category` + ticker/sector.
3. Distill ≤N belief-deltas per role (CVRF "conceptualize the difference into a small
   set of investment-belief insights"); attach `alpha_evidence`, `support_n`,
   `oracle_provenance`.
4. **Selective propagation**: write each belief to `beliefs.jsonl` tagged with the ONE
   relevant `role`; the retriever prepends active beliefs for that role into the
   `lessons_block` via `format_context_block_split` (`retriever.py:488`).
5. **Close O3**: emit a `promotion_event` audit row whose payload sets
   `weekly_retro_promotion_readiness: true` (read at `promotion.py:158`) when the
   weekly pass completes successfully and the active-belief count is under the budget
   cap. This is the single missing producer for the dangling gate field.

### 3. Monthly meta tier (`quant-monthly-meta-retro`, `HERMES_QUANT_MONTHLY_META_RETRO=1`, default-OFF)

FINCON over-episode + RD-Agent Trace. Aggregates the four trailing weekly belief sets:

- which `lesson_category` patterns *repeat* across weeks (these get promoted to the
  `monthly` tier with a longer half-life — durable beliefs);
- per-week beliefs that did not recur and whose recency/importance fell below
  threshold are expired;
- emits **candidate hypotheses** (RD-Agent-style; novelty/dedup-gated downstream by
  W3/W6) for `HypothesisRunner` — recommendations-only, never auto-promoted;
- telemetry-first: logs "what would have changed" before any gating (Eidolon Stage-1,
  byte-identical off-state).

### 4. Deterministic promote/expire rule (FINMEM, non-LLM)

Per belief, on each distillation tick:

- **Decay**: `recency ← recency * α(tier)` where `α(weekly) < α(monthly)` (shorter
  half-life for weekly). Effective half-life encoded as `half_life_days`.
- **Promote on access**: when a belief is surfaced into a prompt,
  `access_counter += 1` and `recency ← 1.0`. A belief pivotal to a *profitable*
  (positive-alpha) closed trade gets `importance += K` and is upgraded weekly→monthly
  (slower decay) — FINMEM's access-counter promotion.
- **Expire / purge**: append an `expired` row when `recency < ε` OR `importance` falls
  below threshold OR the per-role active count exceeds the budget cap N (evict lowest
  `access_counter × recency × importance` first). Expiry is append-only; the
  `beliefs.jsonl` is a rebuildable projection of the immutable `reflections.jsonl`.

No LLM participates in promote/expire. The rule is pure arithmetic over external-truth
(alpha) evidence and deterministic decay — reproducible, auditable, and bounded.

### Rail compliance check (capability-map §5)

- Writes ONLY to the advisory plane (`beliefs.jsonl` → prompt context). Never touches
  the deterministic risk gate, the hard limits, the discrete sizing ladder
  `{0,±0.05,±0.10,±0.15,±0.20}`, or the kill-switch.
- External-truth evaluator only (realized alpha; never LLM self-score; never the
  agent's narrative re-ingested as truth).
- Held-out gate is necessary-not-sufficient: the flag flips only after the SkillOpt
  gate (digest-injected prompt must not regress hit-rate/alpha on an OOS window the
  optimizer never saw; belief count under cap; every belief carries provenance +
  half-life). Select on plateau, never the in-sample peak.
- Bounded, decaying, Oracle-provenance-tagged store; deterministic promote/expire.
- Default-OFF + eval-gated for both crons.

### Links

- Supersedes nothing. Extends ADR-0042 (persistent memory & reflection) with the
  distillation tiers it deferred.
- Builds on W1 (commit `08326e1`, `record_decision` on open).
- Capability-map: `docs/research/2026-05-30-selfevolve-capability-map.md` §2 (O2/O3),
  §3 (CVRF, FINMEM, Oracle-provenance rows), §4 W2/W3, §5 (safety frame).
- SOTA: `docs/research/2026-05-30-r-selfevolve-sota.md` §3a (FINCON CVRF), §3d (FINMEM),
  §5 (Misevolution / reward-hacking / model-collapse).
- Audit: `docs/research/2026-05-30-r-selfevolve-current-audit.md` §1, §4 (O3 dangling
  consumer), §5 C2/C4.
- Consumer closed (O3): `hermes_quant/governance/promotion.py:82,136,158,181,235`.
- Surface extended: `hermes_quant/memory/{reflector,retriever,decisions}.py`.
- Implementation tracked in the W2/W3 wave plans (this ADR is the decision record, not
  the plan).
