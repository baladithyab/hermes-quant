---
status: proposed
date: 2026-06-10
deciders: [codeseys]
amends: ADR-0086
---

> **Amends ADR-0086 (2026-06-10):** ADR-0086 deferred the share-migration to a Phase 2
> and assumed `state.db.positions.quantity` is in NAV-fraction units. In the live
> deployed system both the `paper` and `deterministic-equity` reactors write a per-fill
> *absolute target* into the record's fill-size field, while every consumer of that log
> (the `reconstruct_from` projection, the ADR-0010 FIFO settlement matcher) reads it as
> an incremental *delta*. Re-affirming an unchanged target therefore inflates the
> derived position (BA short −0.2 re-affirmed 6× → −0.8; AAPL 5% re-affirmed 12× →
> 60%/399.93 sh). This ADR fixes the **producer** so a fill record carries the true
> traded delta, keeping `executions.jsonl` a faithful transaction log. ADR-0086's
> Phase-1 read-time MTM and its Phase-2 end-state target are otherwise preserved.

# ADR-0091: Reactors must emit the traded delta in `fill_size_pct`, not the absolute target

## Context and Problem Statement

On 2026-06-10 the advisor portfolio-cap gate silenced every paper auto-fire on a
phantom `gross=402%` (fixed separately in PR #85 by reseeding the cap from the paper
projection). Root-causing that phantom exposed a deeper, independent **accounting
defect in how reactors write fill records** — the subject of this ADR.

### What a fill record means, and what the reactors actually write

`executions.jsonl` is the authoritative append-only log of *executed fills* (ADR-0085).
Each record carries two size fields with distinct, well-established meanings:

- `fill_size_pct` — **the size of THIS fill**: the signed delta actually transacted by
  this execution (a partial broker fill of +3% adds +0.03). This is what every
  downstream fold consumes as an increment.
- `target_position_pct` — the absolute post-fill target weight the fill was steering
  toward (a snapshot, not a transaction).

Two consumers fold `fill_size_pct` as an **incremental delta**, both correctly by design:

- `state/portfolio_state.py::_replay_record` → `_update_position`:
  `new_qty = old_qty + pos_delta` (writes `state.db`; read by EOD snapshot cron, status,
  retro, reconcile).
- the ADR-0010 settlement journal FIFO matcher (`settlement_loop.join_exit_fills`),
  which pairs each fill as a transaction to compute realized P&L.

**The bug is in the producers.** Both reactors set `fill_size_pct` equal to the absolute
target instead of the traded delta:

- **Paper reactor** (`react/paper.py:266,269`):
  ```python
  target_position_pct=fill_size_pct,   # absolute target  ✓
  fill_size_pct=fill_size_pct,         # ALSO the absolute target  ✗ should be the delta
  ```
  When the advisor re-affirms an unchanged −0.2 BA short, the reactor writes
  `fill_size_pct=-0.2` ("this fill traded 20% of NAV") when the **delta actually traded
  was 0** (the position was already −0.2).

- **Deterministic-equity reactor** (ADR-0082): writes the absolute target share count
  into `reactor_metadata.quantity` (e.g. `33.33` = 5%×$100k/$150) on every re-affirmation,
  which `_replay_record` reads as `pos_delta` and adds.

### Observed live blast radius — 9 symbols, BOTH reactors

| Reactor | Symbol(s) | Re-affirmations | Additive fold | Intended |
|---|---|---|---|---|
| deterministic-equity | AAPL | 12 | 399.93 sh (60% NAV) | 33.33 sh (5%) |
| paper | BA | 6 | −0.80 | −0.20 |
| paper | AAL, AVGO, CBOE, CDNS, CRM, META, ORCL | 3 each | ±0.20 | 0.0 / ±0.20 |

Every inflated row is a sequence of records whose `fill_size_pct` (or
`reactor_metadata.quantity`) restates the **absolute target** each tick rather than the
zero-or-small **delta** actually transacted. The records are genuine, distinct proposals
(12 distinct AAPL `proposal_id`s over 65 min, all `target=0.05`/`requested_target_pct=0.05`)
— so this is not duplicate-fill noise; it is the reactor reporting "I traded the whole
target" when it re-affirmed an already-held position.

### Why `state.db` and the settlement journal are BOTH wrong (not just the projection)

Because `fill_size_pct` is mis-populated at the source, **every delta-consuming reader is
corrupted in the same direction**:

- `state.db` positions inflate (the EOD/status/retro symptom).
- The ADR-0010 FIFO matcher sees 12 AAPL "buys" of 33.33 sh and 6 BA "sells" of 0.2, so
  realized-P&L accounting is inflated too.

A projection-only fix (the rejected Option A below) would heal `state.db` but leave the
settlement journal reading the same corrupt deltas — two divergent accounting views of
one log. The defect must be fixed where the meaning is set: the **producer**.

The firing/cap path is **not currently affected** — PR #85 reseeded it from
`reconstruct_portfolio_state`, which folds latest-`target_position_pct` and ignores
`fill_size_pct`. (It is *not* structurally immune: any future caller of `reconstruct_from`
or a `fill_size_pct`-based sizing read would reopen the exposure — see Consequences.)

### Why the ADR-0085 reconcile tool reported "0 divergence" (and why that is not safety)

`quant-ledger-reconcile.py` rebuilds a scratch `state.db` from the log with the *same*
`reconstruct_from` fold and diffs it against live `state.db`. Both fold the corrupt deltas
identically, so they agree (0 phantom / 0 changed). The tool answers "does the cache match
the log under our fold rule?" — not "are the deltas in the log correct?". After this fix,
re-emitted records carry true deltas and the rebuild produces the intended positions.

### Relationship to prior ADRs (mandatory audit)

- **ADR-0085 (authority) — PRESERVED.** `executions.jsonl` stays authoritative and
  append-only; `state.db` stays a derived projection. This ADR does not rewrite history;
  it (a) fixes the producer going forward and (b) repairs the already-written corrupt
  records via an explicit, backed-up migration (see Decision §3) — the log stays the
  single source of truth, now with correct deltas.
- **ADR-0086 (share-quantity + MTM) — AMENDED.** ADR-0086's Phase-2 share migration must
  build on records whose fill sizes are true deltas; this ADR is its prerequisite. ADR-0086
  Phase-1 read-time MTM and the end-state target are preserved.
- **ADR-0011 (sign convention) — PRESERVED and RELIED ON.** ADR-0011's
  `new_qty = old_qty + signed_qty` OPEN/CLOSE/FLIP algebra is *correct* and stays the fold
  rule. The fix makes the producer emit the `signed_qty` (delta) ADR-0011 already assumes,
  so no fold change is needed.
- **ADR-0029 (multi-leg) — PRESERVED.** `reactor_metadata.quantity` for a genuine option
  leg is the signed count actually opened by that leg — already a delta. Only the
  deterministic-equity *equity* path mis-uses it as an absolute target; the fix is scoped
  to that producer and does not touch the multi-leg leg semantics.
- **ADR-0010 (settlement journal) — BENEFITS, no contract change.** The FIFO matcher keeps
  reading `fill_size_pct` as a transaction delta; once the producer emits true deltas, its
  realized-P&L output is correct with zero matcher changes.
- **ADR-0082 (deterministic-equity reactor) — MODIFIED.** This reactor is one of the two
  producers corrected here.

## Decision Drivers

- **`executions.jsonl` must remain a faithful transaction log.** A "fill" record must
  state what was actually traded, so that *every* delta-consuming reader (state.db,
  settlement FIFO, future broker reconcile) is correct by construction — not just one.
- **Idempotence under re-affirmation.** Re-emitting an unchanged target must record a
  zero-delta fill, so the derived position is identical whether emitted once or N times.
- **Single accounting model.** Don't teach one projection that "some executions are really
  snapshots" while the settlement journal believes the opposite.
- **Authoritative-log integrity.** Repair existing corrupt records explicitly and reversibly
  (backed up), never silently.
- **Do not regress genuine incremental fills or multi-leg legs** (ADR-0011 / ADR-0029).

## Considered Options

- **A. Projection-side idempotent fold (discriminator flag + absolute-target branch).**
  Tag absolute-target records and have `reconstruct_from` *set* rather than *add* quantity.
  *(This was the first draft of this ADR; rejected after cross-family review — see below.)*

- **B. Producer-side delta emission + one-time log repair.** Both reactors compute the
  **delta actually traded** (`delta = target − current_position`) and write THAT into
  `fill_size_pct` (and `reactor_metadata.quantity` for the deterministic path), while
  `target_position_pct` keeps the absolute target. Existing corrupt records are repaired by
  an explicit, backed-up migration that recomputes each record's delta from the running
  position. No fold changes; no new fold-time branch.

- **C. Universal latest-target-supersedes in `reconstruct_from`.** Rejected: breaks genuine
  incremental fills and multi-leg legs where additive accumulation is correct (ADR-0011/0029),
  and does nothing for the settlement journal.

- **D. Consumer-only fix (EOD reads the paper projection).** Rejected: treats one symptom,
  leaves the corrupt log feeding settlement P&L, status, retro, and every future reader.

## Decision Outcome

Chosen option: **B — producer-side delta emission + one-time log repair**, because the
defect is that fill records misstate what was traded; fixing the producer makes the
authoritative log correct so *all* delta-consuming readers (state.db projection AND the
ADR-0010 settlement FIFO) become correct with **no fold-time branching and no new
per-record semantics flag to keep in sync**. (This reverses the first draft's Option A
after a 4-family adversarial review unanimously found Option A corrupts cost basis on
target *changes* and cannot fix the settlement-journal view — see More Information.)

Concretely:

1. **Reactors emit the traded delta.** At fill time each reactor reads the symbol's current
   signed position from the authoritative projection
   (`reconstruct_portfolio_state(reactor_filter=<book>)`, the same network-free fold the cap
   already uses) and writes:
   - `fill_size_pct = target − current` (the signed delta actually transacted; **0** when
     re-affirming an unchanged target);
   - `target_position_pct = target` (unchanged — still the absolute post-fill target);
   - deterministic-equity additionally writes `reactor_metadata.quantity` as the **delta**
     share count (`(target − current) × NAV / fill_price`), consistent with the ADR-0029
     leg meaning (count opened by *this* execution).
   This is not a new "back-reference coupling" — a reactor deciding what order to place must
   already know the current position; it now records the delta it computed instead of
   discarding it. A re-affirmation writes a `fill_size_pct=0` record (an audit trail that the
   target was re-evaluated and held), which folds to a no-op in every consumer.

2. **Folds are unchanged.** `_replay_record`/`_update_position` (ADR-0011) and the ADR-0010
   FIFO matcher keep reading `fill_size_pct` as a signed delta. Cost basis, cash
   (`Δcash = −delta × fill_price`), realized P&L, and `state.db` positions are then all
   correct without modification — because the input is finally correct. No `quantity_semantics`
   flag, no absolute-vs-delta fold branch, no `reactor_name` compat shim.

3. **One-time log repair (explicit, backed up, reversible).** A migration script
   (`ops/scripts/quant-repair-fill-deltas.py`) reads `executions.jsonl`, recomputes each
   record's correct `fill_size_pct`/`reactor_metadata.quantity` as the delta vs the running
   reconstructed position **in timestamp order per (account, asset)**, and writes a repaired
   log. Posture: dry-run default (diff only); `--apply` backs up the original
   (`executions.jsonl.bak-deltarepair-<ts>`) before replacing. This honors ADR-0085 — the log
   stays the source of truth, the repair is an auditable, reversible operation healing
   records that were wrong at write time, not a silent rewrite of trade history. (A target
   that genuinely *changed* between two records yields a real non-zero delta and is preserved;
   only re-affirmations of an unchanged position collapse to 0.)

4. **`quant-ledger-reconcile` then heals `state.db`.** After the log repair, the standard
   ADR-0085 reconcile (`--apply`, backed up) rebuilds `state.db` from the corrected log:
   AAPL → 33.33 sh / 5%, BA → −0.20, the seven 3× paper symbols → their single intended
   target. The settlement journal, replayed on the repaired log, reports correct realized P&L.

5. **Regression guard at the seam.** A test asserts every reactor's emitted `fill_size_pct`
   equals `target − current` (delta), and that a re-affirmation emits `fill_size_pct == 0`.
   This guards the producer contract directly — a future reactor that writes an absolute
   target into `fill_size_pct` fails the test, rather than silently inflating (the failure
   mode the rejected Option A's discriminator-flag default left open).

### Consequences

- **Positive:** the authoritative log becomes a faithful transaction record, so `state.db`,
  the ADR-0010 settlement FIFO, status, retro, and any future broker-reconcile are all correct
  from one fix — no divergent accounting views.
- **Positive:** no new fold-time branch, no `quantity_semantics` flag, no permanent
  `reactor_name` compat shim. Less long-term complexity than the rejected Option A.
- **Positive:** re-affirmation records (`fill_size_pct=0`) are a useful audit trail (target
  was re-evaluated and held) that fold to no-ops everywhere.
- **Negative:** the one-time repair **modifies historical records in `executions.jsonl`**
  (backed up, reversible, dry-run-first). This is a stronger action than ADR-0085's usual
  "rebuild the projection, never touch the log" posture and must be operator-gated and
  reviewed — it is justified only because the records were *incorrect at write time*, not a
  reinterpretation of correct history. (REQUIRED negative.)
- **Negative:** reactors now perform a position read at fill time (network-free, from the
  existing projection). It must be ordered before the fill is recorded and be consistent with
  the cap's own read; a stale read would mis-compute the delta. Mitigated by reusing the exact
  `reconstruct_portfolio_state` call the cap already makes in the same execute path.
- **Negative:** `state.db` stays inflated until the operator runs repair + reconcile (two
  explicit, backed-up steps). Not automatic — consistent with ADR-0085 reconcile posture.
- **Neutral:** record count in `executions.jsonl` is unchanged by the repair (deltas replace
  absolute values in-place); new re-affirmations add `fill_size_pct=0` rows going forward.

## Pros and Cons of the Options

### B. Producer-side delta emission + log repair (chosen)
- Good, because it fixes the meaning at the source, so ALL delta-consumers become correct
  (state.db + settlement FIFO), not just one projection.
- Good, because folds, cost-basis algebra, and the settlement matcher need zero changes.
- Good, because no permanent discriminator flag / compat shim / fold branch to maintain.
- Bad, because the one-time repair edits historical records (mitigated: backed up, dry-run,
  reversible, operator-gated).
- Bad, because reactors gain a fill-time position read (mitigated: reuse the cap's existing
  network-free projection read).

### A. Projection-side idempotent fold (rejected after review)
- Good, because it touches only `reconstruct_from` and leaves the log untouched.
- Bad (P0, unanimous cross-family): setting `avg_entry_price` from the re-affirmation fill
  corrupts cost basis on any target *change* (10sh@$100 then target 20sh@$200 → basis wrongly
  $200, not blended $150); unrealized P&L poisoned.
- Bad (P0): heals `state.db` but the ADR-0010 settlement FIFO still reads the corrupt deltas
  from the same log → two divergent accounting views; the "realized P&L out of scope" carve-out
  is invalid because both read one log.
- Bad: `reactor_name`-keyed compat shim is non-deterministic under reactor rename/version/fork;
  the missing-flag default silently re-inflates for a future absolute-target reactor.

### C. Universal latest-target-supersedes in `reconstruct_from`
- Good, because it makes the two reconstructors identical.
- Bad, because it BREAKS genuine incremental fills and multi-leg legs (ADR-0011 / ADR-0029)
  and does nothing for the settlement journal.

### D. Consumer-only fix
- Good, because smallest change to stop one report's phantom.
- Bad, because the corrupt log keeps feeding settlement P&L, status, retro, and every future
  reader.

## Acceptance gate (must be green before status flips to accepted)

- [ ] `tests/unit/test_reactor_fill_delta.py::test_paper_reactor_emits_delta_not_target` — a fill into an existing matching position emits `fill_size_pct = target − current`; a re-affirmation of an unchanged target emits `fill_size_pct == 0`. RED on current `react/paper.py:269`.
- [ ] `::test_deterministic_equity_emits_delta_shares` — `reactor_metadata.quantity` is the delta share count `(target−current)×NAV/price`, 0 on re-affirmation. RED on current ADR-0082 reactor.
- [ ] `::test_target_change_emits_correct_signed_delta` — 5%→10% emits +5% delta; 5%→−5% emits the signed flip delta with correct sign (ADR-0011 FLIP path exercised on the resulting delta).
- [ ] `tests/unit/test_portfolio_state_accounting.py::test_reaffirmation_does_not_inflate` — replaying N delta-correct re-affirmation records folds to the single intended position (not N×); cost basis and cash unchanged across re-affirmations. (Reproduces the AAPL 12× / BA 6× cases against repaired records.)
- [ ] `tests/unit/test_settlement_fifo_parity.py::test_realized_pnl_correct_on_repaired_log` — the ADR-0010 FIFO matcher on the repaired log reports realized P&L consistent with state.db cash (no double-count from re-affirmations).
- [ ] `tests/ops/test_quant_repair_fill_deltas.py::test_repair_recomputes_deltas` — the repair script converts the 9 known inflated symbols' records to correct deltas in timestamp order; idempotent (re-running on a repaired log is a no-op); dry-run writes nothing; `--apply` backs up first.
- [ ] `::test_repair_preserves_genuine_target_changes` — records where the target genuinely changed between ticks keep a non-zero delta (only unchanged re-affirmations collapse to 0).
- [ ] Live heal verified end-to-end on the real book: `quant-repair-fill-deltas --apply` (backup written) → `quant-ledger-reconcile --apply` → `state.db` shows AAPL=33.33sh/5%, BA=−0.20, the seven paper symbols at their single intended target; EOD snapshot shows no phantom positions; both backups present.
- [ ] Full `pytest` sweep green; firing/cap path (PR #85's `reconstruct_portfolio_state` seed) unchanged; `executions.jsonl` record *count* unchanged by the repair.
- [ ] Cross-family adversarial review (≥3 families) of the REVISED ADR finds no P0 (the prior review's P0s — cost-basis overwrite, settlement desync, compat-shim fragility — must be confirmed resolved by the producer-side approach).

## More Information

- Triggering investigation: PR #85 advisor-cap phantom-gross fix
  (`docs/architecture/INCIDENT-2026-06-10-advisor-cap-phantom-gross.md`).
- **Cross-family review that reversed the first draft (Option A → B):**
  `docs/reviews/2026-06-10-adr0091/` — GPT-5.5, Gemini 3.1 Pro, DeepSeek V4 Pro, Grok 4.3,
  unanimous P0 on Option A's cost-basis overwrite and settlement-journal desync; two
  explicitly recommended the producer fix.
- Producer root cause: `react/paper.py:266,269` (both fields set to the target);
  deterministic-equity absolute `reactor_metadata.quantity` (ADR-0082).
- Fold consumers (must stay delta-reading): `state/portfolio_state.py::_replay_record`,
  ADR-0010 settlement FIFO `settlement_loop.join_exit_fills`.
- The two-projection divergence is documented operationally in the `hermes-quant-operations`
  skill ("never trust state.db positions table").
- Amends ADR-0086; preserves ADR-0085 (authority), ADR-0011 (sign convention), ADR-0029
  (multi-leg), ADR-0010 (settlement), ADR-0080 (paper track-record is the eval signal);
  modifies ADR-0082 (deterministic-equity reactor).
- Reopen/extend when: ADR-0086 Phase-2 share migration executes (builds on delta-correct
  records); or a new reactor is added (must satisfy the delta-emission seam test).
