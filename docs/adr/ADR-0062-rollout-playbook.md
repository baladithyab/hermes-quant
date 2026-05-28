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
