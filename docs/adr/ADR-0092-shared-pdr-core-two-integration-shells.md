---
status: proposed
date: 2026-06-12
deciders: [codeseys]
consulted: [hermes-rearchitect-assessment workflow (45 agents), hermes-rearchitect-research workflow (36 agents)]
amends: null
supersedes: null
---

# ADR-0092: Extract a shared host-agnostic PDR core; hermes-quant and cowork-quant become thin integration shells over it

> **This ADR ratifies an organizing decision and a migration direction; it builds nothing by itself.**
> Every increment it implies is scoped default-OFF and eval-gated in the companion plan
> (`docs/plans/2026-06-12-shared-pdr-core-rearchitecture.md`). With nothing yet extracted, both plugins
> remain exactly as they are today. This ADR does **not** pre-empt the open ADR-0091 ledger-semantics
> decision — it consumes whatever ADR-0091 resolves to as the canonical ledger fold.

**Cites:** [ADR-0002](ADR-0002-analyst-protocol.md) (the `AnalystView` peer-view contract — the seam that makes the core blind to both modality *and* host), [ADR-0003](ADR-0003-aggregator.md) (numeric calibration-weighted BMA — the canonical aggregator the core must own), [ADR-0004](ADR-0004-risk-gate.md) (deterministic risk gate, silence-by-default, FINAL authority — ported verbatim as the core kernel), [ADR-0079](ADR-0079-unified-pdr-architecture.md) (the PDR organizing model this ADR makes the literal code spine), [ADR-0085](ADR-0085-ledger-reconcile.md) (ledger reconcile), [ADR-0086](ADR-0086-ledger-share-quantity-dollar-accounting.md) + [ADR-0091](ADR-0091-reactors-emit-traded-delta.md) (the live dual-ledger/delta-semantics defect this core makes structurally impossible — **ADR-0091 remains the deciding authority on the fold semantics**).

**Grounded in:** the two adversarial assessment workflows run 2026-06-12 (inward fragmentation map of 184K LOC + outward research over charter/PDR/reference-repos/cowork-quant), `docs/charter/2026-05-13-hermes-quant-charter.md`, `cowork-quant/docs/PARITY.md`, `docs/plans/2026-06-09-cowork-quant-submodule-plan.md`, and `~/wiki/_inbox/2026-06-10-adr0091-reactor-delta-vs-absolute-target-fork.md`.

---

## Context and Problem Statement

hermes-quant grew ~4× in ~2 weeks (late-May wiki snapshot: 42K LoC / 60 ADRs / 2021 tests → 2026-06-12: 184K LoC / 89 ADRs / 4238 tests). That velocity is itself the fragmentation mechanism: capability was added signal-by-signal and path-by-path faster than any single spine could absorb it. The operator's lived experience — "hella buggy and disjoint/fragmented" — was tested against the code by two adversarial multi-agent workflows (81 agents total, every load-bearing finding independently refutation-checked).

The finding is precise and, importantly, **narrow**: the defects are not spread through the 184K lines. They are concentrated in the **connective tissue**. Subsystem coherence scored 3/10 for orchestration entry points, money-state correctness, and the flag/enablement surface; but the *leaves* scored well — the decision core (`advisor.recommend`), the analyst protocol, the BMA math, and the deterministic gate are clean, well-tested, low-coupling units (verified: `gate.py` imports only `protocol`+`kelly`; `kelly` has zero hermes imports). A "22-module circular-dependency blob" and a "58% deferred-import camouflage" claim were both **refuted** as measurement artifacts — the real runtime import graph is a clean DAG. A from-scratch rewrite would discard ~74K LOC of working leaves plus a 4238-test executable spec in order to fix a few hundred lines of spine, ledger, and one missing selector call. The cost asymmetry is overwhelming.

Three confirmed, load-bearing structural defects define the connective-tissue rot:

1. **Two structurally incompatible end-to-end pipelines** (CRITICAL): the *documented* spine (`daemon → signals.jsonl → freqtrade`) is vestigial; the *live* spine is a set of cron scripts that each re-glue Perception and Reaction by hand. The docs describe a system that does not run.
2. **Dual-ledger divergence is live** (CRITICAL): two reconstructors read the same `executions.jsonl` with incompatible semantics (cumulative-delta vs latest-target). Reproduced by a verifier: re-affirm +0.20 twice → 0.40 in one book, 0.20 in the other. The gate sizes against one; the always-on concurrent-position rail polices the other. This is the unfixed defect ADR-0091 is mid-decision on (its 12× AAPL inflation symptom is the same root cause).
3. **2 of 4 trade-firing layers bypass the cap seam** (CRITICAL): `autonomous.py:884` hardcodes `PaperReactor()` and never calls `select_reactor()`; `playbook-tick` POSTs raw to Alpaca through hand-rolled rails. The "final authority" cap is path-dependent — the mechanism class behind the 2026-06-02 41.6×-gross leverage incident.

Concurrently — and this is the decisive context — a from-scratch clean rebuild of the *same charter* **already exists in the repo**: `cowork-quant`, a lean (~3.9K core LOC / 212 tests) PDR implementation whose hash-chained append-only ledger with a single `Ledger.portfolio()` fold makes the dual-ledger bug **structurally impossible**, whose injected `state_dir` makes the test-fixture-to-live-state leak (the +$167K fictional-P&L incident) **impossible by construction**, and whose port of the money-core surfaced three latent P0 money bugs in hermes-quant. The operator clarified the relationship: hermes-quant (plugin for the Hermes agent) and cowork-quant (plugin for Claude Cowork) are **parallel plugins for two different agent hosts** — same charter, same rails, same PDR model, different integration semantics. They are not competitors; neither is to be abandoned.

The problem this ADR solves: **how do two parallel plugins stop independently re-deriving — and re-breaking — the money-bearing core, while each keeps its own host-native integration?**

## Decision Drivers

- **Money-correctness is the dominant axis.** The confirmed defects are accounting defects; a defect subtracts from a real bank account. Any decision is judged first on whether it makes the dual-ledger / cap-bypass / fixture-leak *class* of bug structurally impossible, not merely patched.
- **Non-negotiable rails must survive verbatim:** silence-by-default; the deterministic risk gate as FINAL authority; the LLM committee as evidence-not-authority (may silence via a 0.0 multiplier, never amplify or re-select); no-look-ahead reproducibility (`asof` = publication time); new capability default-OFF behind flags and eval-gated before live influence.
- **Two hosts, one truth.** Hermes (Python analyst classes, crons/daemon, MCP) and Claude Cowork (in-session subagent committee, scheduled `/watch` turns) have genuinely different integration semantics. The money-bearing arithmetic and state must not fork between them.
- **Do not discard working assets.** ~74K LOC of verified-clean leaves and a 4238-test executable spec have real value; so does cowork's 212-test clean spine. The decision should *merge each one's strength*, not pick a loser.
- **The live paper system should not go dark.** A migration that requires a big-bang cutover or stands up a second live writer against the un-isolated `executions.jsonl` is unacceptable (the latter re-arms the exact fixture-leak class).
- **Legibility.** A newcomer (or the operator three weeks from now) must be able to hold the system in their head: one flow, one ledger, one gate, one place that says "what is ON."

## Considered Options

- **Option A — From-scratch rewrite.** Abandon the 184K LOC, build one new PDR system.
- **Option B — Strangler-fig refactor *inside* hermes-quant only.** Extract a clean spine within hermes; leave cowork-quant a separate sibling with a PARITY map maintained forever.
- **Option C — Fold hermes-quant into cowork-quant.** Promote cowork as *the* system; retire hermes.
- **Option D — Extract a shared host-agnostic `pdr-core`; both plugins become thin integration shells over it.** The core owns 100% of money-adjacent state + arithmetic + the gate + the contracts; hermes and cowork each provide only host-native perception production and host-native reaction routing.

## Decision Outcome

Chosen option: **"Option D — shared host-agnostic `pdr-core` + two integration shells"**, because it is the only option that makes the money-bug *class* structurally impossible for **both** plugins at once while discarding neither the proven leaves nor the proven clean spine — it reconciles the inward map's "rebuild the spine, keep the leaves" verdict with the outward research's "promote cowork-core to the spine" verdict at the altitude the operator named (two parallel plugins, not one system).

The core is built by promoting cowork-quant's `quantcore` spine (the clean ledger/gate/manifest/replay/deny-hook) and porting hermes-quant's proven-but-cowork-missing leaves onto its contracts (the numeric calibration-weighted BMA, options/Greeks gate, Kronos foundation-model analyst, the CPCV/DSR/PBO eval harness, settlement/horizon math). The contract boundary is the charter's `AnalystView` — the same seam that makes the aggregator blind to which *analyst* produced a view also makes the core blind to which *host* produced it. Shells produce `AnalystView[]` and feed `Fill`s back; the core returns an authorized, sized `Proposal`. Whether a host auto-executes or routes to a human is a **shell** decision the core never sees — which is how the deferred live-execution question (B48) stays open without blocking anything.

**This ADR does not decide the ledger fold semantics.** ADR-0091's open 3-way fork (A projection-only [rejected]; B producer-emits-delta [P0s found in two review rounds]; C version-discriminated carry-forward fold [fable-5-endorsed]) remains the deciding authority. The core *consumes* whatever ADR-0091 resolves to. This ADR explicitly inherits the durable ADR-0091 lesson: **a fix that requires a producer to read derived state to compute a value is a write-time race into an immutable log — strictly worse than a read-time projection bug.** The core keeps the ledger a pure record of intent.

### Consequences

- **Positive:** The dual-ledger divergence becomes impossible — one `Ledger.portfolio()` fold read identically by every consumer, replacing 26 files of position truth.
- **Positive:** The fixture-leak / fictional-P&L class becomes impossible — storage location is injected (`StateConfig/state_dir`), so a test process literally cannot reach live storage.
- **Positive:** The cap-bypass class becomes impossible — there is exactly one `execute()` chokepoint and one gate node on every path to a decision; a control wired once is inherited by every caller by construction.
- **Positive:** Both plugins share one audited money-core; a correctness fix lands once and both hosts inherit it. The charter's never-run empirical proof can finally run on a trustworthy book.
- **Positive:** The leaves and the 4238-test corpus are preserved as the leaf spec, not discarded.
- **Negative (REQUIRED):** Introduces a third versioned artifact (`pdr-core`) that two plugins depend on — a packaging/versioning/release surface that does not exist today, with cross-repo coordination cost on every contract change. A breaking contract change now requires a coordinated two-shell migration.
- **Negative:** A substantial one-time porting cost (numeric BMA, options, Kronos, eval harness from hermes onto cowork's contracts) with a window where each capability is mid-port and present in only one place.
- **Negative:** cowork-quant must be git-init'd and published before it can be a dependency, and its 212-test green count must be reproduced in CI (it could not be reproduced in the assessment environment — no `pydantic` in system Python, separate venv, 9p-mount git-metadata corruption warnings).
- **Negative:** Forces resolution of latent scope questions that were comfortably ambiguous: BTC-first (charter) vs equities (current center of gravity) for the proof; whether the two-`require_ensemble`-layer distinction (cross-source at perception vs cross-analyst at decision) survives the merge intact.
- **Neutral:** The `docs/plans/2026-06-09-cowork-quant-submodule-plan.md` framing (sibling, reference-by-URL) is the right *mechanism* but the wrong *end-state* — the end-state is one shared core, not two coexisting systems with a parity map forever.
- **Neutral:** Most of the visible "184K LOC" is the test corpus + docs + vendored/build dirs; the production surface (~82K LOC) shrinks as duplicate state machinery and the vestigial daemon cluster retire.

## Pros and Cons of the Options

### Option A — From-scratch rewrite

- Good, because the end-state is maximally legible — no legacy at all.
- Bad, because it discards ~74K LOC of verified-clean leaves and a 4238-test executable spec to fix a few hundred lines of spine — the cost asymmetry is indefensible given the bugs are *not* in the leaves.
- Bad, because the live paper system goes dark, or a parallel rewrite runs for months before reaching parity.
- Bad, because cowork-quant already *is* a from-scratch rebuild — a second one would be the third implementation of the same charter.

### Option B — Strangler-fig inside hermes-quant only

- Good, because it keeps the live paper system running throughout and never stands up a second writer (the inward map's top-scored proposal, 35/40).
- Good, because the sequencing is correct and verified (ledger-first, then cap-seam, then perception, then spine).
- Bad, because it re-writes a clean spine that cowork-quant *already wrote and tested* — duplicated effort.
- Bad, because it leaves cowork-quant on a divergent money-core, requiring a PARITY map to be maintained by hand forever; the two plugins keep drifting.
- Bad, because it solves the fragmentation for one host while the other re-accumulates it.

### Option C — Fold hermes-quant into cowork-quant

- Good, because cowork's clean spine becomes the single system immediately.
- Bad, because cowork only *spec'd* (did not build) the load-bearing capabilities hermes has proven: numeric calibrated BMA, options/Greeks gate, Kronos, the eval harness. It is not a drop-in.
- Bad, because cowork forbids order execution *ever* (rail #4) — folding hermes in would silently kill the gated-live-execution ambition (B48) the operator chose to keep open.
- Bad, because it abandons hermes-quant as a Hermes-host plugin, which the operator explicitly does not want.

### Option D — Shared `pdr-core` + two integration shells

- Good, because it makes the entire money-bug class structurally impossible for *both* hosts at once.
- Good, because it keeps both plugins (both hosts served) and merges each one's strength: cowork's clean spine + hermes's proven leaves.
- Good, because the `AnalystView` contract is *already* the host-blind seam — the factoring is natural, not forced.
- Good, because the live-execution question stays a shell concern and need not be decided now.
- Bad, because it introduces a shared versioned dependency and its coordination cost (the required negative above).
- Bad, because it has the largest up-front porting surface of the in-place options.

## More Information

- Companion design doc: `docs/design/2026-06-12-shared-pdr-core-architecture.md` (the target architecture in detail).
- Companion migration plan: `docs/plans/2026-06-12-shared-pdr-core-rearchitecture.md` (the increment-by-increment sequence, eval gates, and the operational-safety precondition).
- **Blocked on:** ADR-0091 resolution (ledger fold semantics) before Increment 0 lands.
- **Operational-safety note (operator's call, recorded not actioned):** the assessment *reproduced* that the live book is currently untrustworthy (dual-ledger divergence live; 2-of-4 fire-paths bypass the cap; an armed `playbook-tick` cron is POSTing real paper orders through hand-rolled rails). Recommendation: make the armed crons observe-only until Increment 0 lands. One-line, reversible, removes money risk without slowing the rearchitecture. The operator runs the crons; this ADR records the recommendation and does not action it.
- Findings should be filed as seeds (`.seeds/file_seed.py`), not GitHub issues, per repo convention.
- Review date: re-evaluate this ADR's `proposed` status once ADR-0091 is decided and Increment 0 is scoped.
