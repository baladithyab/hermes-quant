---
status: proposed
date: 2026-06-17
deciders: [codeseys]
consulted: [deep-work-loop session 2026-06-17 (PDR profitability/safety + decouple)]
amends: null
supersedes: null
---

# ADR-0093: Adopt a host-neutral product name for the shared PDR core; "hermes-quant" is demoted to a shell name

> **This ADR names a thing; it moves no code.** It is a NAME-ONLY decision (operator
> directive 2026-06-17): the single-repo layout is unchanged, no package is split, no
> import path moves. It records WHAT the host-agnostic core is called so docs, ADRs,
> and future extraction (deferred) can speak about it without overloading "hermes-quant".
> The physical extraction of `pdr_core/` into a standalone installable package remains a
> SEPARATE, deferred decision (the operator declined a repo move this round).

**Cites:** [ADR-0092](ADR-0092-shared-pdr-core-two-integration-shells.md) (the structural
decision this names — shared host-agnostic core + N thin integration shells),
[ADR-0079](ADR-0079-perception-decision-reaction-architecture.md) (the PDR organizing
model the core IS), [ADR-0004](ADR-0004-risk-gate.md) (the deterministic gate the core
owns as final authority).

---

## Context and Problem Statement

ADR-0092 decided the architecture: a **host-agnostic PDR core** (`hermes_quant/pdr_core/`
today — frozen `AnalystView`/`Proposal`/`Fill` contract triad, the BMA aggregator, the
deterministic gate, the ledger fold) wrapped by **N thin integration shells**, each
providing host-native perception production and reaction routing:

- **`hermes_quant/`** — the shell for the **Hermes** agent host (Python analyst classes,
  cron/daemon orchestration, MCP).
- **`cowork-quant/`** — the shell for the **Claude Cowork** host (in-session subagent
  committee, scheduled `/watch` turns).

The core's own `contracts.py` docstring states it depends only on the stdlib and is
"trivially movable to a standalone repo." In other words: **the engine already runs
standalone in principle; it is wrapped, not owned, by either host.**

The problem is purely nominal but real: the repository, the package, and the whole
project are all called **"hermes-quant"** — which is the name of *one of the shells*.
This creates three concrete confusions:

1. A reader cannot tell whether "hermes-quant" means *the Hermes shell* or *the whole
   PDR system including the core and the cowork shell*.
2. The core has no settled name — ADR-0092 and design notes refer to it variously as
   `pdr-core`, `pdr_core`, `quantcore` (cowork's spine name), and "the core."
3. As the system is described to anyone outside the Hermes context (or run standalone),
   naming the product after one host is actively misleading — like naming an engine
   after one of the cars it ships in.

The 2026-06-17 deep-work session sharpened this: the per-position stop-loss, the
promotion gate, and the report-corruption fixes this round all live in the *core's*
concern (money-bearing arithmetic + rails), reinforcing that the valuable, host-neutral
asset is the core — and it deserves its own identity.

## Decision Drivers

- **Disambiguate core vs shell vs project** without churning code (name-only this round).
- **Settle on ONE core name** the codebase, ADRs, and a future extraction can all use,
  replacing the `pdr-core`/`pdr_core`/`quantcore`/"the core" scatter.
- **Don't break anything.** No import path, repo, package, or live cron name changes.
  Existing `hermes_quant` / `cowork-quant` package names persist as the SHELL names.
- **Leave physical extraction open.** A future ADR may split the core into its own
  package/repo; this ADR must not pre-empt or block that, only make it nameable.

## Considered Options

### What gets named
- The **core** (the host-agnostic engine).
- The **project / product** (the umbrella over core + all shells).
- The **shells** stay as-is: `hermes-quant` (Hermes shell), `cowork-quant` (Cowork shell).

### Naming options (operator picks the final string; this ADR records the slate + rationale)

| Candidate (core / project) | Rationale | Notes |
|---|---|---|
| **`pdr-core` / "PDR"** (Recommended) | Already the most-used informal name; PDR (Perceive-Decide-React, ADR-0079) is the literal architecture; zero novelty risk; matches the existing `pdr_core/` package dir | Most conservative — essentially ratifies current usage. Project = "PDR" or "PDR Trading System". |
| **`quantcore` / "Quantcore"** | The name cowork-quant already uses for its spine; promotes the clean-spine lineage ADR-0092 chose to build the core from | Slight collision risk with generic "quant core" phrasing elsewhere |
| **A fresh codename / "<codename>"** | A distinct product identity unburdened by either host or the PDR acronym (e.g. a mythology-neutral word); cleanest for standalone/external framing | Highest novelty; needs a name-search; more doc churn to adopt |
| **Keep "hermes-quant" as the project, add `pdr-core` only for the core** | Minimal disruption; only names the core, leaves the umbrella alone | Doesn't fix confusion #1 (project still named after one shell) |

## Decision Outcome

Chosen: **name the core `pdr-core` and the umbrella project "PDR" (Perceive-Decide-React),
demoting "hermes-quant" to mean specifically the Hermes integration shell** — name only,
no code move.

Rationale: `pdr-core` is already the dominant informal name and matches the `pdr_core/`
package directory, so it carries zero novelty risk and needs the least doc churn; "PDR"
as the umbrella is exactly what ADR-0079 already established the system to BE. This
ratifies and regularizes existing usage rather than inventing identity, which is the
right weight for a name-only round. A fresh external-facing codename can be chosen later
if/when the core is physically extracted and published (a separate, deferred ADR) —
nothing here blocks that.

Concretely, going forward:

- **"PDR core" / `pdr-core`** = the host-agnostic engine (`hermes_quant/pdr_core/` today).
- **"PDR"** = the umbrella project (core + all shells).
- **"hermes-quant"** = the **Hermes shell** specifically (no longer a synonym for the whole).
- **"cowork-quant"** = the **Cowork shell** specifically.
- The repo directory, the `hermes_quant` Python package, the `~/.hermes/` runtime home,
  the cron job names, and all import paths are **UNCHANGED** — they are shell-scoped names
  and remain correct as such.

### Consequences

- **Positive:** Docs/ADRs can now say "the PDR core does X" vs "the Hermes shell does Y"
  unambiguously; the scattered `pdr-core`/`quantcore`/"the core" usages converge on one term.
- **Positive:** Describing the system standalone or to a new host is no longer misleading.
- **Positive:** Zero code risk this round — no import, package, repo, or cron rename.
- **Negative / accepted:** A naming-vs-code mismatch persists temporarily — the package is
  `hermes_quant` but the umbrella is "PDR". This is the deliberate cost of name-only; it is
  resolved if/when the deferred extraction ADR moves `pdr_core` into its own package.
- **Negative / accepted:** Some existing docs still say "hermes-quant" to mean the whole
  system; they are not mass-edited this round. New/edited docs use the disambiguated terms;
  old ones are corrected opportunistically.

### Confirmation

This ADR is satisfied by: (1) this file existing and registered in `docs/adr/README.md`;
(2) the README/CLAUDE-facing docs using "PDR core" vs "shell" terms going forward. No test
or code gate — it is an identity decision, not a behavioral one.

## What this ADR does NOT do (explicit non-goals)

- It does **not** rename the `hermes_quant` package or the repo.
- It does **not** split `pdr_core/` into a standalone package (deferred; a future ADR).
- It does **not** change `~/.hermes/`, cron job names, flags, or any runtime path.
- It does **not** supersede ADR-0092; it names what 0092 structured.
