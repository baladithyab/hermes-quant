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

| Seed | P | What it is | ADR-0092 increment | Gate / blocker | Status (updated 2026-06-15) |
|---|---|---|---|---|---|
| `ra00` | 1 | EPIC: extract the shared pdr-core | the whole plan (Inc 0–6) | sequenced | **OPEN — epic root.** Inc 0 DONE (ADR-0091 accepted); Inc 1 substantially landed (pdr_core contracts+gate+kelly+aggregate ported, 436 parity tests green); Inc 2 shadow-gate landed. Tracks the remaining cutover increments. |
| `ra01` | 1 | Dual-ledger divergence (cumulative-delta vs latest-target reconstructors) | **Increment 0** (one canonical fold) | — | **✅ CLOSED 2026-06-15.** ADR-0091 Option E ACCEPTED; the one shared FillDeltaNormalizer feeds all four folds; 4-fold parity gate green. |
| `ra02` | 1 | 2 of 4 fire-paths bypass the cap/reaction seam (`autonomous` hardcodes `PaperReactor`; `playbook-tick` POSTs raw) | **Increment 2** (point all fire-paths at the core selector) | Inc 0 (DONE) → the cutover | **OPEN — Increment 2.** Core gate runs as a default-OFF SHADOW (b34afa0, observes core-vs-live parity); the cap-bypass CUTOVER (route all fire-paths through the one seam) is the remaining behavioral step. cs55/cs60/cap2 closed the live cap-correctness holes this session. |
| `ra05` | 2 | Three competing signal contracts | **Increment 1** (freeze contracts) | parity-tested + eval-gated increment | **DEFERRED-WITH-GATE 2026-06-15.** LIVENESS-TRACED (wps0a9wcl): premise PARTLY REFUTED — analysts DO emit the typed `protocol.AnalystView`; the "three" are 1-live + 1-future-core + 1-serialization-view. pdr_core.contracts TRIAD frozen (av1/cs82/cs83). Collapsing them touches the pdr_core seam + protocol money types = its own parity-tested increment; wiring EvidenceRecord is behavioral not additive. Gate: that increment. |
| `ra06` | 2 | Three classes named `PortfolioState` | **Increment 2** (retire the dups; point at the core's one reader) | the state-core increment | **OPEN — Increment 2.** Same risk-class as ra05 (touches money types under a parity grid). Now-2 live `PortfolioState` (risk/portfolio_normalize + state/portfolio_state) + the protocol view. Sequenced behind the Inc-2 cutover. |
| `ra07` | 2 | Orphaned `governance.promotion.evaluate()` (paper→live gate, zero live callers) | **Increment 4** | blocked on B48/B01-LIVE | **DEFERRED-WITH-GATE 2026-06-15.** LIVENESS-TRACED (wps0a9wcl): DECISION = neither wire nor quarantine; leave STAGED. Zero non-test callers verified. Gate: a LIVE reactor (B48/`243d`) lands → then the spine wires the promotion gate. |
| `ra08` | 2 | LLM committee layer (built, default-OFF shadow) | **Increment 4** + ADR-0062 five-gate (`8db9` DONE) | the five-gate eval PASS | **DEFERRED-WITH-GATE 2026-06-15.** LIVENESS-TRACED (wps0a9wcl): NOT dead — a default-OFF shadow-only eval-gated rollout, reachable from the enabled playbook-tick when the flag flips. The five-gate criteria are now documented (ADR-0062 amendment, `8db9`). Gate: an eval PASS + operator flag flip. |
| `ra09` | 2 | No single source of truth for "what is ON in production" | **Increment 4** (deploy lineage) | Inc 4 | **OPEN — Increment 4.** Partially served by `docs/FLAGS.md` + the 2026-06-14 action packet + the `9048` CRON/DEPLOY destale this session. The single deploy-lineage SoT is the Inc-4 deliverable. |

> **Increment 0 is COMPLETE (2026-06-15).** ADR-0091 Option E is accepted, the canonical fold is
> proven across all four folds, and ra01 is closed. The traced-and-dispositioned items (ra05, ra07,
> ra08) are deferred-with-gate, each with a named, re-verified unblock condition.
>
> **STATUS 2026-06-16 — every concrete ra* child is closed or deferred-with-gate; ra00 (root) is now
> tracking-only.** Closed: ra01 (canonical fold / Inc-0), ra03 (conftest isolation), ra04, ra09 (single
> flag SoT + drift gate; rt03 then completed the scanner), ra10, ra11. Deferred-with-gate (each at a
> named parity-gated increment): ra02 (half-1 autonomous→select_reactor DONE byte-identical; half-2
> playbook-tick is a behavioral cutover — `docs/plans/ra02-playbook-reactor-cap-centralization.md`),
> ra05 (signal contracts), ra06 (pdr_core `CorePortfolioSnapshot` canonical type + parity grid LANDED;
> consumer-collapse gated), ra07 (orphaned promotion gate, blocked on a live reactor), ra08 (LLM
> committee, five-gate eval-gated). Increment 1 substantially landed (pdr_core contracts+gate+kelly+
> aggregate+portfolio_snapshot, 450+ parity tests green). **ra00's original blocker premise ("blocked
> on ADR-0091 before Inc 0") is now STALE — ADR-0091 is accepted and Inc 0/1 landed.** ra00 remains as
> the epic tracking root (deferred-with-gate): the remaining work is the per-increment behavioral
> cutovers above, each already individually gated. The agent has driven every byte-identical/additive
> slice to merge; what is left is genuinely the behavioral consumer-migrations + the operator-gated
> live flips — no un-dispositioned epic work remains.

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
