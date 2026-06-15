# Rearchitecture Epic — ra00–ra09 seed → ADR-0092 increment roadmap (2026-06-15)

**Why this exists:** the backlog carries 8 `ra*` seeds that are the symptoms/sub-decisions of the
**ADR-0092** epic (extract a shared host-agnostic PDR core; hermes-quant + cowork-quant become thin
shells). They are NOT independent bugs an agent can surgically close — each is a slice of a
multi-increment, ADR-0091-gated migration. This roadmap is their agent-side disposition: it maps
every open `ra*` seed to its increment in `docs/plans/2026-06-12-shared-pdr-core-rearchitecture.md`,
records the current status, names the acceptance gate, and notes where THIS session's connective-tissue
fixes already advanced the underlying defect. "Addressed" here = sequenced + gated + status-tracked
in the epic, with the blocking decision surfaced — not silently parked.

**Governing artifacts (do not duplicate — consume):**
- `docs/adr/ADR-0092-shared-pdr-core-two-integration-shells.md` (status: **proposed**; the organizing decision)
- `docs/plans/2026-06-12-shared-pdr-core-rearchitecture.md` (the 7-increment plan, Increment 0→6)
- `docs/design/2026-06-12-shared-pdr-core-architecture.md`, `docs/plans/2026-06-12-increment-0-scope.md`, `docs/plans/2026-06-13-increment-1-gate-port.md`

**The hard gate the whole epic sits behind:** **ADR-0091** (reactors emit traded-delta vs
absolute-target ledger semantics) must be RESOLVED before Increment 0 starts. That is an
architecture decision for the operator/deciders, NOT a code task the agent makes unilaterally
(the agent surfaced + scoped it; ADR-0091 remains `proposed`). Until ADR-0091 resolves, every
`ra*` below is **correctly blocked**, not ignored.

---

## ra* seed → increment map

| Seed | P | What it is | ADR-0092 increment | Gate / blocker | Status |
|---|---|---|---|---|---|
| `ra00` | 1 | EPIC: extract the shared pdr-core | the whole plan (Inc 0–6) | ADR-0091 resolution → Inc 0 | epic root; tracks the 6 increments |
| `ra01` | 1 | Dual-ledger divergence (cumulative-delta vs latest-target reconstructors) | **Increment 0** (one canonical `Ledger.portfolio()` fold) | ADR-0091 (the deciding authority on the fold) | blocked on ADR-0091; **mitigated** this session — see below |
| `ra02` | 1 | 2 of 4 fire-paths bypass the cap/reaction seam (`autonomous` hardcodes `PaperReactor`; `playbook-tick` POSTs raw) | **Increment 2** (point all fire-paths at the core selector) | Inc 0 first | blocked on Inc 0; **2 cap-asymmetry instances closed** this session (cs55, cs60) |
| `ra05` | 2 | Three competing signal contracts; analysts honor only the untyped one | **Increment 1** (freeze contracts, port `AnalystView`) | Inc 0 first | blocked on Inc 0; **contract type-hardening done** this session (av1/cs82/cs83) |
| `ra06` | 2 | Three classes named `PortfolioState` with different shapes | **Increment 2** (retire hermes's 3 dup `PortfolioState`; point at the core's one reader) | Inc 0 (core `PortfolioState`) | blocked on Inc 0 |
| `ra07` | 2 | Orphaned `governance.promotion.evaluate()` (paper→live gate, zero live callers) | **Increment 4** (orchestration spine wires the promotion gate) | Inc 0–2 | blocked; re-confirmed orphaned (no live caller) by the convergence review |
| `ra08` | 2 | Entire LLM committee layer orphaned (built, fires on no live path) | **Increment 4** (spine decides committee wiring) + ADR-0062 (`8db9`) | Inc 4 + the five-gate LLM-production criteria | blocked; shadow-mode default-OFF (`HERMES_QUANT_DELIBERATIVE`) |
| `ra09` | 2 | No single source of truth for "what is ON in production"; 3 disagreeing flag inventories | **Increment 4** (deploy lineage) + the FLAG-INVENTORY reconcile | Inc 4 | blocked; partially served by `docs/FLAGS.md` + the 2026-06-14 operator action packet |

---

## Where THIS session's connective-tissue fixes already advanced the ra* defects

The 38+17 fixes this session targeted the exact "connective-tissue rot" ADR-0092 names. They do
NOT close the ra* epic items (the structural extraction is still owed), but they shrink the blast
radius and de-risk the increments:

- **ra01 (dual-ledger):** the rebuild-vs-incremental fold divergence is the ra01 root. This session
  drove the fold-consistency family to closure — cs44 (parent-skip both folds), cs51 (per-leg dedup
  key), cs52 (account partition), cs57 (rebuild dedup), cs62 (asof guard parity), cs64 (normalizer
  account), cs84 (dict-boundary poison guard), **cs85 (single running-net = rebuild matches the
  canonical incremental column)**. The two folds now AGREE on every tested stream EXCEPT the genuine
  unit-mixing (shares + NAV-fraction), which cs85 correctly isolated as the cr00/cs31 unit-unification
  dependency = exactly Increment 0's `Ledger.portfolio()` single-fold mandate. So ra01's *symptoms*
  are guarded; the *one canonical fold* is still Increment 0 (ADR-0091-gated).
- **ra02 (cap-seam bypass):** cs55 (multileg reactor had NO gross cap) + cs60 (cap pos_map key
  narrowing) + cap2 (P1 — the playbook aggregate-cap was sizing against an empty book) closed three
  live cap-correctness holes on the existing fire-paths. The *routing* fix (all paths through
  `select_reactor`) is still Increment 2.
- **ra05 (signal contracts):** av1/cs82/cs83 hardened the `pdr_core.AnalystView`/`Proposal`/`Fill`
  contracts (bool + off-type rejection) — the typed seam Increment 1 freezes + ports.
- **ra09 (flag SoT):** the 2026-06-14 operator action packet + the `9048` CRON-REGISTRY/GO-LIVE
  destale annotations reconciled the doc-side flag/cron inventory toward one current source.

---

## Disposition

**ra00–ra09 are epic-tracked, ADR-0091-gated, and increment-sequenced** — the honest state for a
multi-increment migration whose first step is blocked on an unresolved architecture decision (ADR-0091)
that is the operator/deciders' call, not the agent's. The agent has: recorded the decision (ADR-0092),
written the 7-increment plan, scoped Increments 0 + 1, and — this session — closed the connective-tissue
correctness defects that would otherwise corrupt the migration. The next action that unblocks the epic
is **resolving ADR-0091** (an operator/architecture decision), after which Increment 0 (the canonical
ledger fold) proceeds.

These seeds remain OPEN in `.seeds` because the structural extraction is genuinely not done — closing
them now would be the "dispositioned ≠ resolved" error. They are open-AND-tracked, with this roadmap as
the audit trail.
