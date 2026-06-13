# Increment 0 — Code-Level Scope (the correctness core)

**Status:** plan (Increment 0 of ADR-0092; consumes ADR-0091 Option-E resolution)
**Date:** 2026-06-12
**Companion:** `docs/adr/ADR-0092-*.md`, `docs/plans/2026-06-12-shared-pdr-core-rearchitecture.md`, `docs/reviews/2026-06-12-adr0091-resolution/verdict.md`
**Seeds:** ra00 (epic), ra01 (dual-ledger), ra03 (conftest isolation), ra10 (atomicity — adjacent)

Increment 0 is the blocking first step: it lands the money-state correctness fix the whole rearchitecture stands on. It is **default-OFF** behind `HERMES_QUANT_DELTA_NORMALIZER`; with the flag off, behavior is bit-for-bit current (still inflated) so the change is reversible and the live paper system never goes dark. **No script touches `executions.jsonl`** — the historical correction is the new *interpretation* applied on the next full rebuild with the flag on.

This scope implements ADR-0091 **Option E** (carry-forward fold), NOT the superseded Option B. The producers stay unchanged; the fix lives at fold time in one shared normalizer.

---

## 0.0 — Precondition (blocking, do first): conftest isolation (seed ra03)

The `+$167K` fictional-P&L incident was a test fixture leaking into the live `state.db` via a default path + missing isolation. Today conftest isolates `state.db` + governance/evidence/kill-switch, but NOT `executions.jsonl`/`QUANT_HOME` (the real gap).

- Extend the autouse conftest fixture to also isolate `executions.jsonl`, `QUANT_HOME`, and `artifacts/`. Each test process gets a tmp `state_dir`; no test can reach live storage.
- **Acceptance:** a test asserting that, inside a test, the resolved `executions.jsonl` / `state.db` / `QUANT_HOME` paths are all under the tmp dir — never `~/.hermes/quant/`.
- This is cheap, blocking, and protects every subsequent parity test (they must run against a clean book).

## 0.1 — Contract fix: `react/base.py` (seed ra01)

The schema doc currently *codifies the bug*: `react/base.py:31` says `fill_size_pct # actual filled (paper=target...)` while every consumer treats it as a per-fill delta.

- Amend the `ExecutionRecord` docstring (lines 16-17) and the `:31` comment: state explicitly that the per-fill size field (`fill_size_pct`, and `reactor_metadata.quantity` for det-equity) is the **ABSOLUTE signed post-fill target/realized** for absolute-target schema versions, and that the per-fill **delta is DERIVED at fold time** by the shared normalizer.
- Add a new nullable field `schema_version: str | None = None` (same back-compat pattern as the existing `bar_ts`/`play_tag` nullable fields). **Absent/legacy ⇒ absolute-target interpretation** — which is exactly what every historical record already is, so no log rewrite is needed. New records stamp the absolute-target version explicitly.
- **Producers stay UNCHANGED**: `react/paper.py:266,269` keep writing the caller's absolute target into both fields; `react/deterministic_equity.py:373,385` keep writing the realized fraction + the backend's true `filled_qty` into `reactor_metadata.quantity`. Preserving `filled_qty` keeps the det-equity backend's true absolute shares as a **live-broker reconciliation anchor**.

## 0.2 — The one shared normalizer (the hard gate)

Write **exactly one** module — e.g. `hermes_quant/state/fill_delta_normalizer.py`:

- A stream-ordered transform, keyed `(account_id, asset_class, asset)`, carrying `running_net` per bucket.
- For absolute-target-version records, emit `delta = current_target − running_net`, then update `running_net = current_target`. (For any future true-delta-version records, pass through untouched — never double-difference.)
- Must handle **BOTH** the `fill_size_pct` path AND the `reactor_metadata.quantity` (leg_quantity) path — the det-equity AAPL-12× inflation is via the quantity field (`deterministic_equity.py:385` → `portfolio_state.py:983-984` leg_quantity branch), not `fill_size_pct`.
- It must **define and own ONE canonical per-bucket ordering** and its **own** `running_net` state — do NOT reuse `state.db`'s running-net or the FIFO's lot-list shape.
- **This single-shared-normalizer-with-one-canonical-ordering is the burden of proof.** Implemented as two independent carry-forwards, Option E drifts back into the rejected two-divergent-views failure. It is a hard architectural gate, not an aspiration.

## 0.3 — Wire BOTH consumers to the one normalizer (seed ra01)

No parallel reimplementation. Both consumers import and call the same module:

- **state.db projection** (`state/portfolio_state.py`):
  - `_replay_record` (rebuild): feed `pos_delta` from the normalizer instead of raw `fill_size_pct` (currently `:975`/`:984`).
  - `_apply_execution_unsafe` (incremental): same, with `running_net` **seeded from the persisted positions row** (`:594-606`) so the two state.db folds agree with each other.
  - **Leave `_update_position` (ADR-0011 OPEN/ADD/REDUCE/FLIP, `:929-951`) UNCHANGED** — it receives the normalized signed delta. A genuine ADD blends `avg_entry_price` (`:937-941`); a re-affirmation yields delta 0 → no-op (avg never overwritten — the precise difference from rejected Option A).
- **settlement FIFO** (`daemon/settlement_loop.py`):
  - Run the normalizer as a **pre-pass before `_normalize_exec_record`** (`:524-536`), on the **same asof-honest-sorted stream** `join_exit_fills` actually consumes (after the `:672` sort, NOT raw bus order — this closes the ordering-divergence P0 the adversarial hunt found).
  - **Leave the lot algebra (`:706-722`) UNCHANGED** — it receives the normalized signed delta; re-affirmation → delta 0 → no lot opened.

## 0.4 — `pdr-core` extraction seam (sets up later increments)

Increment 0 also stands up the `pdr-core` package boundary by promoting cowork's clean ledger as the *reference design* the normalizer-fixed state core converges toward. In this increment, the deliverable is the normalizer + the frozen-contract stubs; the full ledger swap is Increment 1-2. Concretely here: place the normalizer and the `schema_version`-bearing `ExecutionRecord` behind the import seam that `pdr-core` will own, so later increments move the module, not rewrite it.

## 0.5 — Reconcile semantics (seed ra01)

After Option E, the log (absolute target) and the projection (carry-forward delta) **intentionally differ in the raw field but agree in derived net**.

- Update `quant-ledger-reconcile` to compare **derived net**, not raw `fill_size_pct`, or it will start false-alarming.
- Add a test: OLD-fold vs NEW-fold over historical data reports **non-zero** divergence (proving the fix actually moved the projection — it reported 0 before because log and projection shared the wrong fold).

## 0.6 — Acceptance gate (must be green before the flag goes on in live)

Mirror of the ADR-0091 Option-E gate (see the ADR). The load-bearing additions:

- `test_fill_delta_normalizer_shared.py`: PARITY (state.db rebuild + incremental, settlement FIFO, AND the immune `portfolio/state.py::reconstruct_portfolio_state` all agree on net for the AAPL/BA fixtures); ORDERING (same-asof ties → identical delta streams in both consumers); INCREMENTAL-vs-REBUILD parity; ARCHITECTURAL (only one module computes the carry-forward).
- `test_reaffirmation_does_not_inflate`, `test_target_change_and_flip`, `test_det_equity_quantity_path`, `test_legacy_records_interpreted_as_absolute_target`.
- Full pytest sweep green; firing/cap path unchanged; **`executions.jsonl` byte-identical before/after (checksum gate)**.

## What stays the operator's call (money-accounting, hard-to-reverse)

1. Running `quant-ledger-reconcile --apply` against the **live** `state.db` (overwrites the live projection — safe under E because the log is untouched and re-runnable, but it is a human-gated money op).
2. Flipping `HERMES_QUANT_DELTA_NORMALIZER` on in the live daemon (default-OFF eval-gated rollout — paper-only first vs live book).
3. Confirming the live `executions.jsonl` still holds the incident's genuinely-distinct re-affirmation records (12 AAPL `proposal_id`s, BA 6×) before enabling, so the heal produces the expected `AAPL=33.33sh/5%`, `BA=−0.20`.
4. Whether to also consolidate the dual reconstructor at the source long-term (E converges all three folds on the same semantics; full consolidation is a later rearchitecture seed, not Increment 0).

## Implementation status (2026-06-13) — what landed vs what remains

**LANDED (committed, default-OFF behind `HERMES_QUANT_DELTA_NORMALIZER`):**
- §0.0 conftest isolation (QUANT_HOME + executions/signals bus) — `cc9a2f9`.
- §0.1 `ExecutionRecord.schema_version` + `is_absolute_target_record` + contract docstring — `18a04d4`.
- §0.2 `FillDeltaNormalizer` (the one shared carry-forward, 7 unit tests) — `de23eb9`.
- §0.3 wired into the **rebuild fold** (`reconstruct_from`); cr09 keystone flipped xfail→PASS — `a5dc0a6`.
- Adversarial review: all 6 failure-mode checks HOLD; flag-OFF bit-for-bit (169 passed); cr09 non-vacuous.

> **⚠️ OPERATOR CONSTRAINT — the flag is REBUILD-ONLY; do NOT flip it on a live daemon yet.**
> Increment 0 wired the normalizer into `reconstruct_from` (the source-of-truth rebuild fold used
> by heal/reconcile). The **incremental** `apply_execution` path that PaperReactor calls live on
> every fill is NOT yet normalized. So with the flag ON, a live session would inflate incrementally
> while a rebuild deflates to the correct value — the live `state.db` and a rebuild would DIVERGE
> mid-session. Safe uses today: (a) offline `reconstruct_from` / `quant-ledger-reconcile` heals, and
> (b) the test suite. The live-daemon flip waits on the incremental-path wiring below.

**REMAINS (scoped follow-ups, filed as seeds):**
- **Incremental-path wiring** (the reviewer's gap): wire the normalizer into `apply_execution`
  (`portfolio_state.py:517-717`) with `running_net` seeded from the persisted positions row, so the
  incremental and rebuild folds agree. This is the prerequisite for flipping the flag live.
- **asof-ordering guard**: the carry-forward is file-sequential; the final net is order-invariant for
  a true append log, but the "executions.jsonl is asof-ascending per bucket" invariant is load-bearing
  and currently unverified at the read site — add a guard or an explicit per-bucket sort.
- **Settlement-FIFO pre-pass**: wire the same normalizer into `daemon/settlement_loop.join_exit_fills`
  (currently has no production caller; needed when settlement is wired).
- **det-equity quantity-lane unit-unification (cr00)**: a single `(paper-default, equity, SYM)` bucket
  can receive BOTH a paper NAV-fraction fill and a det-equity true-shares fill (same account+class);
  the position fold then mixes units. The normalizer keeps the two lanes separate internally but the
  downstream fold mixing is pre-existing (occurs flag-OFF too) — needs the read-time mark seam
  (`position_pct = qty×mark/equity`). Out of Increment-0 scope.

**Earliest kill criterion (unchanged):** if the single shared normalizer cannot make all three folds agree on the AAPL/BA parity fixture without per-consumer special-casing, Option E's shared-derivation premise is false → escalate. (Not tripped: the rebuild fold + cr09 parity pass cleanly.)
