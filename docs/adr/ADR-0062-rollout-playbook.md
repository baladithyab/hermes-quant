# ADR-0062: Production Rollout Playbook for v0.2 LLM Surfaces

**Status:** Accepted  
**Date:** 2026-05-27  
**Author:** Hermes-Quant Subagent (v0.4-4 task)  
**Supersedes:** —  
**Related:** ADR-0031 (Governance plane / silence-by-default), ADR-0041 (Audit-trail
observability), ADR-0054 (LLM-Caller & TraderNode v0.2), ADR-0056 (RiskCommittee v0.2),
ADR-0057 (Reflector v0.2), ADR-0058 (HMM Regime v0.2)

---

## Context

By the end of v0.3 we had four LLM-wired surfaces in the codebase, all feature-flagged and
default OFF:

| Surface             | Flag                                  | ADR       |
|---------------------|---------------------------------------|-----------|
| TraderNodeLLM       | `HERMES_QUANT_TRADER_LLM`             | ADR-0054  |
| RiskCommittee v0.2  | `HERMES_QUANT_RISK_COMMITTEE_LLM`     | ADR-0056  |
| Reflector v0.2      | `HERMES_QUANT_REFLECTOR_LLM`          | ADR-0057  |
| HMM regime          | `HERMES_QUANT_REGIME_HMM`             | ADR-0058  |

Each surface is individually well-tested and ships with a silent fallback to its v0.1
deterministic path on any LLM failure (per ADR-0031). However, the operator (Codeseys)
is a single human running production paper-trading and needs **a documented sequence**
for turning these flags on safely — including:

1. What to verify *before* flipping any flag.
2. What order to flip them in (lowest blast-radius first).
3. How long to wait between flips.
4. How to detect drift / regression while live.
5. How to roll back per-surface.
6. How to halt everything if something is wrong.

Without a written playbook, the rollout decisions get made implicitly during the rollout,
which is exactly when calm pre-commitment is most valuable. The four flags also happen
to be the **only** LLM-affecting knobs in the system, so a single document can be
canonical: there is no "which playbook applies here?" ambiguity.

A second motivation is **checkability**. We have a strong norm in this repo that
documentation describes real code, not aspirational code. ADRs reference real files;
test counts in PRs reference real tests. The rollout playbook should be subject to the
same norm: if an env var named in the playbook is not actually read by the code, that
should fail in CI, not in production.

## Decision

### 1. Single canonical document at `docs/operations/ROLLOUT.md`

The rollout playbook is one Markdown file with a fixed seven-section structure:

| §  | Section                                |
|----|----------------------------------------|
| 0  | Pre-flight checklist                   |
| 1  | Activation order (one flag at a time)  |
| 2  | Smoke-test sequence                    |
| 3  | Rollback procedure                     |
| 4  | Monitoring KPIs                        |
| 5  | Kill-switch                            |
| 6  | Cross-references (ADR list)            |
| 7  | Append-only event stores reference     |

The structure is fixed because it is the **shape of the rollout decision**, not a
content detail: pre-flight gates entry, activation order is the path forward, smoke
tests are the per-step gate, rollback is the per-step exit, KPIs are the dwell-time
observation surface, kill-switch is the panic exit, cross-references are the
provenance, and the event-store table is the operational substrate.

### 2. Activation order: HMM → Reflector → RiskCommittee → Trader

The order is by blast-radius, lowest first. This is a deliberate sequencing decision and
not just convention:

- **HMM regime** is purely advisory. It changes BMA aggregator weights but does not
  directly approve or reject any proposal.
- **Reflector** writes to the memory store but does not affect any in-flight or future
  proposal directly. The memory hints it produces are surfaced by the retriever, but
  the gate logic is unchanged.
- **Risk Committee v0.2** can shift approval outcomes but preserves the 3-of-5 consensus
  invariant from ADR-0043 and the silence-by-default rejection from ADR-0031.
- **Trader v0.2** changes proposal text and structured fields but **does not** change
  P&L math — the deterministic helpers for stop-loss, target-price, and alpha-return
  are always recomputed and override the LLM's numeric outputs (per ADR-0054).

Each step has a stated dwell time (24h / 24h / 48h / 7 days). The dwell increases as
blast-radius increases: longer observation buys more confidence that approval-rate and
P&L distributions have not shifted.

### 3. The kill-switch is the existing halt mechanism

We do **not** introduce a new kill-switch primitive. The halt CLI documented in ADR-0009
§P0-4 (`hermes quant halt '*' --reason …`) already halts every gate in the system, and
silence-by-default already covers schema-level LLM failures. Adding a dedicated
"LLM-disable" kill switch would be operator surface area that duplicates an existing
mechanism without adding capability. Instead the playbook documents the halt CLI as the
canonical kill-switch and clarifies the division of labour:

- **Schema / format failures** → silent fallback to v0.1 (no operator action).
- **Logical / strategic failures** → operator-issued halt.

### 4. Lightweight consistency tests at `tests/docs/test_rollout_consistency.py`

The playbook is *checked* in CI by a small test module. The tests verify cheap structural
properties only — they do not reason about content correctness:

- `ROLLOUT.md` exists at the canonical path.
- Every env var named in `ROLLOUT.md` is actually read by `hermes_quant/` code.
- Every LLM-feature env var read by `hermes_quant/` code is named in `ROLLOUT.md`.
- Every ADR referenced in `ROLLOUT.md` exists as a file in `docs/adr/`.
- ADR-0062 itself has `Status: Accepted` in its frontmatter.
- All seven required section headers are present in `ROLLOUT.md`.
- The `## 4. Monitoring KPIs` section lists at least five KPIs.
- The pre-flight checklist has at least six items.

These tests are intentionally lightweight: they are fast, have no flakes, and have an
obvious failure mode (rename a flag in code → playbook test fails → developer either
updates the playbook or reverts the rename). They are the cheapest way to keep
documentation honest.

## Consequences

**Positive:**
- Operator has a single-page rollout sequence with no "what next?" ambiguity.
- Consistency tests guarantee that env-var renames cannot silently desync from the playbook.
- ADR cross-references in ROLLOUT.md are validated at test time, so dead links are caught.
- The playbook is reusable for future LLM surfaces: add a new env var to the table, add
  a new step to §1, the consistency tests will enforce the wiring.

**Negative / Risks:**
- The playbook reflects v0.4 surface area; future surfaces will require playbook edits
  (this is by design — the consistency test enforces it).
- Dwell times (24h / 24h / 48h / 7 days) are operator judgement calls, not data-driven.
  In the absence of long-running paper-trading data, they are conservative defaults.
- The playbook assumes one operator. Multi-operator coordination (e.g. one person rolling
  back while another rolls forward) is out of scope and would need a separate ADR.

**Mitigations:**
- Dwell times can be revised by amending this ADR once enough live-paper data exists to
  argue from history rather than caution.
- The `## 0 Pre-Flight Checklist` includes a "no open governance halts" check, so
  starting a rollout while a halt is active is structurally prevented.

## Alternatives Considered

1. **Verbal handoff / wiki page (no doc, no tests).** Rejected: violates the repo norm
   that operational decisions are durable and checkable. A wiki page that drifts from
   reality is worse than no documentation, because it gives a false sense of safety.

2. **Fully automated rollout (a `quant rollout activate` command that flips flags one at
   a time).** Rejected as too aggressive for the paper-trading boundary. The whole point
   of the dwell times is that a human looks at KPIs between steps. Automating the flag
   flips would either skip the human-judgement step (bad) or require the command to
   block for 7+ days waiting for sign-off (worse). A documented playbook that the human
   drives is the right shape.

3. **Per-surface rollout playbooks (one Markdown per ADR).** Rejected. The flags are
   coupled (you would never activate Trader v0.2 without RiskCommittee v0.2 already on
   in our deployment context), and the per-step dwell rules are easier to compare in a
   single document. Splitting them would invite drift.

4. **Heavyweight consistency tests (e.g. assert that the ROLLOUT.md commands actually
   produce expected output when run).** Rejected as too brittle and too slow. The
   cheap structural tests catch the failure mode that matters (env-var renames /
   ADR deletions desyncing from docs); behaviour-level checks are already covered by
   the per-surface test suites under `tests/agents/`, `tests/memory/`, and
   `tests/regime/`.

## Files Changed

| File                                              | Change                                             |
|---------------------------------------------------|----------------------------------------------------|
| `docs/operations/ROLLOUT.md`                      | **NEW** — canonical seven-section rollout playbook |
| `docs/adr/ADR-0062-rollout-playbook.md`           | **NEW** — this ADR                                 |
| `tests/docs/test_rollout_consistency.py`          | **NEW** — eight lightweight consistency checks     |
| `docs/adr/README.md`                              | **EXTENDED** — index entry for ADR-0062            |

---

## Amendment — 2026-06-15: the five-gate LLM-production-default criteria (seed `8db9`, B41-g)

**Status of amendment:** Accepted (governance close-out). **Scope:** documentation only — this
amendment records the criteria a default-flip MUST clear; it flips **no** flag and changes **no**
running behavior, so it is compatible with the ADR-freeze (seed `d9d8`) if active. Every flag named
below remains **default-OFF**; flipping any of them is an operator decision gated on the criteria here.

### Why this amends §1–§2

The original ADR-0062 gated each per-stage default-flip on **dwell time (24h/24h/48h/7d) + a ±10%
KPI-drift eyeball** (§2, §4). That is a *time-and-observation* heuristic — necessary (it is gate 5
below) but **not sufficient**: it never asks whether turning the stage ON actually *improves the
decision*. The B41 research (`docs/research/2026-06-01-r-llm-production-default.md` §7) established
the load-bearing gap: **all four LLM stages already clear "default-OFF / byte-identical" and the
silence-by-default half, but none clears the OOS-beats-fallback gate** — because that gate did not
exist. As of 2026-06-15 it does (see "Now-merged instruments"), so the implicit drift heuristic is
replaced by the explicit five-gate criteria.

### The five gates (ALL must hold simultaneously, in order; none is "the LLM produced plausible output")

A stage's default flips OFF→ON only when it clears all five:

1. **Determinism / byte-identical rail.** Flag OFF ⇒ path byte-identical to today. Flag ON ⇒ the
   *decision* the gate sees is reproducible from the audit log (`prompt_hash` + parsed dump), not
   the prose. (Already true for all four stages.)
2. **Cost ceiling.** A per-decision USD/token budget with a zero-call local kill-switch, surviving
   restart, enforced *before* the call; child-cost-counts-against-parent for the research debate;
   fall back to the $0/no-network v0.1 path on exhaustion. **Merged:** `hermes_quant/agents/llm_budget.py`
   (seed `ed7c`/B41-a).
3. **OOS eval gate — the LLM stage beats its own heuristic fallback out-of-sample.** Not in-sample,
   not one window, not prose quality: the stage's decision-relevant metric across ≥2 regimes incl. a
   drawdown regime, contamination-guard clean (the W7 red-team eval-gate template). **Merged:**
   `hermes_quant/eval/llm_beats_fallback_gate.py` (approval-quality for RiskCommittee, proposal-quality
   for Trader) + `hermes_quant/eval/debate_dissent_gate.py` (dissent-quality for ResearchDebate) — seed
   `20b6`/B41-b. **This is the keystone gate and the one the original §1 omitted.**
4. **HITL / gate-still-final invariant intact.** The deterministic risk gate (ADR-0004), the discrete
   sizing ladder, the 3-of-5 committee quorum (ADR-0043), and per-order human confirmation remain
   downstream of and authoritative over every LLM stage. The LLM is evidence — never the ballot, never
   the executor. (Architecturally enforced; re-verify per stage at flip time.)
5. **Silence-by-default proven live.** The fallback probe (ADR-0060) passes for the stage AND a live
   observation window shows the fallback firing rate is low and bounded. **This subsumes the original
   dwell-time + KPI-drift step** — dwell time is how long you observe gate 5, not a gate by itself.

### Per-stage default-flip checklist (supersedes the implicit §2 order for the FLIP decision)

The §2 activation *order* (HMM → Reflector → RiskCommittee → Trader, lowest blast-radius first) stands.
What changes is the *admission test* at each step — replace "dwelled 24h, KPIs within ±10%" with:

| Stage | Flag | G1 byte-id | G2 cost ceiling | G3 OOS-beats-fallback | G4 gate-final | G5 silence-live | Flip when |
|---|---|---|---|---|---|---|---|
| Reflector v0.2 | `HERMES_QUANT_REFLECTOR_LLM` | ✅ | ✅ (`llm_budget`) | **N/A-shape** — write-only, no decision metric; use a faithfulness/no-leakage check instead (§5.1) | ✅ off-path | needs a clean observation week | G1+G2+G4 hold + faithfulness check + 1 clean week |
| RiskCommittee v0.2 | `HERMES_QUANT_RISK_COMMITTEE_LLM` | ✅ | ✅ | **instrument exists** (`llm_beats_fallback_gate`, approval-quality axis) — must be RUN and PASS on the fixed corpus | ✅ 3-of-5 quorum preserved | run the probe | all five PASS (G3 = approval-quality beats deterministic-voted approval OOS) |
| TraderNode v0.2 | `HERMES_QUANT_TRADER_LLM` | ✅ | ✅ | **instrument exists** (proposal-quality axis); close the numeric-override gap first (§5.3) | ✅ deterministic helpers override numerics | run the probe | all five PASS |
| ResearchDebate | `HERMES_QUANT_RESEARCH_DEBATE` | ✅ | ✅ (child-cost rollup) | **instrument exists** (`debate_dissent_gate`, dissent-quality) | ✅ off the execution path | run the probe | all five PASS; most expensive — flip last |

"Instrument exists" means the eval axis is built and merged; the **gate is cleared only by running it
on the fixed corpus and getting a PASS** — building the axis ≠ passing it. No stage has a recorded PASS
yet, so **every LLM default remains OFF** pending the operator running the eval + observation.

### Now-merged instruments (provenance)

- Gate 2: `hermes_quant/agents/llm_budget.py` — `ed7c` (B41-a), closed.
- Gate 3: `hermes_quant/eval/llm_beats_fallback_gate.py`, `hermes_quant/eval/debate_dissent_gate.py` — `20b6` (B41-b), closed.
- Gate 5: `docs/adr/ADR-0060-fallback-probe.md` (fallback probe).
- Gate 4: `docs/adr/ADR-0004-risk-gate.md` (deterministic gate, final authority), ADR-0043 (committee quorum).

### Consequence for the consistency test

`tests/docs/test_rollout_consistency.py` continues to check ROLLOUT.md ↔ code flag parity. This
amendment adds no new flag (the four are unchanged), so the existing checks remain green. The
five-gate criteria are a *governance* layer over the same flags, not new env vars.
