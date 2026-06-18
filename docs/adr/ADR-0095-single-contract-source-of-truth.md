---
status: proposed
date: 2026-06-17
deciders: [codeseys]
consulted: [cowork session 2026-06-17 (decouple + contract audit)]
amends: null
supersedes: null
---

# ADR-0095: One canonical contract — collapse the duplicated AnalystView / Proposal / Fill definitions

> **The "frozen cross-host contract" is currently defined TWICE and has already drifted.**
> This ADR makes the contract triad a single source of truth owned by the Aegis core, so the
> divergence class ADR-0092 set out to kill cannot reappear at the seam itself.

**Cites:** [ADR-0092](ADR-0092-shared-pdr-core-two-integration-shells.md) (the contract triad IS
the host-blind seam — "one truth, two hosts"), [ADR-0093](ADR-0093-host-neutral-product-name.md)
(the Aegis core that must own the canonical contract), [ADR-0002](ADR-0002-analyst-protocol.md)
(the `AnalystView` protocol), [ADR-0091](ADR-0091-reactors-emit-traded-delta.md) (the `Fill`
absolute-target schema the canonical contract must carry).

---

## Context and Problem Statement

ADR-0092's entire thesis is "one money-truth, two hosts," anchored on a frozen contract triad
(`AnalystView` in, `Proposal` out, `Fill` back). But that triad is defined in **two places today,
and they have already diverged** (verified 2026-06-17):

- **`hermes_quant/pdr_core/contracts.py`** — frozen **stdlib dataclasses**. `AnalystView` carries
  `confidence_raw` and `metadata`, `magnitude` is a *normalized* expected-move strength, `asset_class`
  includes the options family (`option` / `us_option`), `bar_ts` is typed `Any`, and there is a
  *signed* `POSITION_LADDER` frozenset `{0, ±0.05, ±0.10, ±0.15, ±0.20}` with bool/str/NaN/off-ladder
  guards enforced at construction.
- **`cowork-quant/scripts/quantcore/quantcore/schemas.py`** — **Pydantic** `BaseModel`s. `AnalystView`
  has **no** `confidence_raw` and **no** `metadata`, `magnitude` is "expected |return|", `asset_class`
  **lacks options**, `asof_decision` must be tz-aware UTC, `bar_ts` is `datetime | None`, `rationale`
  has a `max_length`, and the ladder is an *unsigned* `SIZING_LADDER` tuple with direction applied
  separately.

The drift is concrete and money-adjacent: differing field sets (`confidence_raw`), differing
`asset_class` vocabularies (options present in one, absent in the other), differing ladder
representations (signed frozenset vs unsigned tuple), differing validation (UTC-required vs not). An
`AnalystView` that validates in the Cowork shell is **missing a field the Aegis core declares** — the
two "frozen" contracts are not the same contract. Two definitions of the seam = two seams = exactly
the divergence class ADR-0092 exists to eliminate, reappearing one level up. The standalone-Aegis
goal (ADR-0093) makes this blocking: the core must own one canonical contract before it can be the
thing that runs without either shell.

## Decision Drivers

- **The contract IS the seam (ADR-0092).** If it forks, the "host-blind core" guarantee is fiction.
- **Money-correctness.** A contract mismatch silently drops or defaults a field on the money path
  (e.g. `confidence_raw` feeding the calibrator; the options multiplier keyed on `asset_class`).
- **Core purity (ADR-0092 / ADR-0093).** The Aegis core's **contract layer** (the
  `AnalystView` / `Proposal` / `Fill` triad and the gate read-interfaces — the seam dataclasses)
  depends only on the stdlib and must stay "trivially movable to a standalone repo" — and that
  contract layer cannot import Pydantic. The compute core (`pdr_core/gate.py`) may use pandas/numpy
  for the numeric math; Pydantic is the one boundary library kept out of the core entirely (it lives
  only in the shell ingress that validates LLM/JSON into typed views).
- **The LLM/JSON boundary still needs real validation.** Free text must never drive state; the shell
  ingress that turns model output into a typed view needs schema validation (ABSTAIN on failure).

## Considered Options

- **A — Keep two definitions, add a field-parity drift test.** Cheap, but two definitions persist and
  semantic differences (dataclass guards vs Pydantic validators, UTC requirement) still diverge.
- **B1 — One canonical module, stdlib frozen dataclasses, both shells import it.** Core-pure; but the
  LLM/JSON boundary loses Pydantic ergonomics unless re-added.
- **B2 — One canonical module, Pydantic.** Rich validation; but puts Pydantic in the core, breaking
  the stdlib-only purity contract.
- **B3 — Canonical stdlib dataclasses in the Aegis core + ONE derived/generated Pydantic mirror at the
  LLM/JSON ingress.** Validation where it is needed, a single definition behind it.
- **C — Promote cowork's Pydantic schemas as canonical; port hermes to import them.** Validation-first,
  but inverts the purity contract and drags Pydantic into the core.

## Decision Outcome

Chosen: **Option B3.** The Aegis core owns ONE canonical contract triad as **stdlib frozen
dataclasses** (`AnalystView` / `Proposal` / `Fill` + the signed `POSITION_LADDER`), preserving the
construction-time arithmetic guards (off-ladder / bool / NaN) that are the anti-leverage-gambling
invariant. A **single derived Pydantic mirror** sits at the LLM/JSON ingress in each shell to validate
free-text → struct before anything reaches the core — but it is generated from (or parity-tested
against) the one dataclass definition, never hand-maintained as a second source. The `asset_class`
vocabulary and ladder representation are unified to the core's (signed ladder; options family
included). `cowork-quant/scripts/quantcore/quantcore/schemas.py` becomes a thin re-export / mirror of
the core, not an independent definition.

Rationale: B3 keeps the core stdlib-pure and standalone-movable (ADR-0092/0093), keeps the guards that
protect every host including standalone Aegis, and still gives the LLM boundary Pydantic validation —
from one definition, so drift becomes *impossible by construction*, not merely *caught*.

### Consequences

- **Positive:** one contract = the seam ADR-0092 promised; a cross-host `AnalystView` is byte-identical
  in shape; the divergence is removed structurally, not policed.
- **Positive:** the core stays stdlib-only; the LLM boundary keeps Pydantic validation via the derived
  mirror.
- **Positive:** the construction guards (bool/str/NaN/off-ladder) now protect the Cowork shell and
  standalone Aegis, not just hermes.
- **Negative / accepted:** a one-time migration — cowork's schemas re-pointed; fields cowork lacked
  (`confidence_raw`, `metadata`, options `asset_class`) threaded or explicitly defaulted; the UTC
  requirement reconciled (core stores `Any` timestamps; the mirror enforces UTC at ingress).
- **Negative / accepted:** deriving the Pydantic mirror adds a small codegen step (or a hand-checked
  mirror behind a parity test as the interim).
- **Neutral:** this is a prerequisite for ADR-0092's deferred physical extraction — unify the contract
  first so the extraction is mechanical.

### Confirmation

Satisfied by: (1) a parity test asserting the Pydantic mirror's field set + types equals the dataclass
triad (now a *generation* check against one source, not a reconciliation of two); (2) a cross-host
round-trip test — a Cowork-produced `AnalystView` JSON validates and constructs the core dataclass with
no field loss; (3) the existing off-ladder / bool / NaN rejection tests run against the single
definition; (4) a CI grep asserting `quantcore/schemas.py` defines no independent triad (only
re-exports/mirrors the core).

## More Information

- **Sequence:** land this **before** [ADR-0094](ADR-0094-deliberation-adapters.md) adds
  `deliberation_provenance` — the field is added to the one contract, not two.
- Clears the path for ADR-0092's deferred extraction of `pdr_core/` → the `aegis` package
  ([ADR-0093](ADR-0093-host-neutral-product-name.md)).
- Build per repo convention; this is a refactor of an existing contract, so it ships behind the same
  test corpus both shells already run (the migration is "green tests in both shells against one
  definition").
