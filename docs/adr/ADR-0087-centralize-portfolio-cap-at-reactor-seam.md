---
status: proposed
date: 2026-06-02
deciders: [codeseys]
---

# ADR-0087: Centralize the portfolio-cap clip at the PaperReactor.execute() seam

## Context and Problem Statement

ADR-0071 introduced portfolio-level caps (200% gross / 100% net / 20% cash
reserve) via `hermes_quant.risk.portfolio_normalize.clip_one_to_remaining_headroom`.
The cap was wired into **one** of the four trade-firing layers — the
autonomous-tick loop (`autonomous.py:~388`). The 2026-06-02 incident showed the
cost: the **advisor layer** (`quant-daily-interim.py`) had no cap call and stacked
the paper book to 41.6× gross / 32.4× net-short. A post-incident audit
(`docs/research/2026-06-02-firing-layer-cap-audit.md`) found that of the four
layers, only autonomous-tick capped; advisor was hot-patched on 2026-06-02; and
**playbook** (`quant-playbook-tick.py`) and **hourly**
(`quant-hourly-tick.py::maybe_run_autonomous_phase`) still do not cap.

The structural fact: **all four layers ultimately call
`PaperReactor.execute(proposal, *, fill_size_pct, ...)`** (`react/paper.py:81`)
to write a fill, and that method already hosts a cross-cutting REACTION-layer
precondition — the ADR-0077/0079 admissibility check runs inside `execute()`
before the record is appended. It does NOT host a portfolio-cap clip. The cap is
re-implemented per-layer, which is exactly why two layers were missed and a third
(advisor) drifted its own copy.

A risk control that only some firing layers honor is not a risk control.

## Decision Drivers

- **Can't-forget-a-layer.** A new firing path must inherit the cap by construction.
- **DRY / single test surface.** One cap implementation, not N drifting copies.
- **Consistency.** The cap is the same KIND of cross-cutting reaction precondition
  as the admissibility check already living in `execute()`.
- **Reversible rollout** (money-software addendum): default-OFF behind the
  existing `HERMES_QUANT_PORTFOLIO_CAPS` flag until verified.
- **No double-clipping** during migration.

## Considered Options

- **A. Per-layer clips (status quo + finish the job):** add the clip to playbook
  and hourly too, leaving four copies.
- **B. Centralize the clip inside `PaperReactor.execute()`** as a reaction
  precondition (alongside admissibility), behind `HERMES_QUANT_PORTFOLIO_CAPS=1`;
  remove the per-layer clips so the reactor is the single authority.
- **C. A decorator/middleware wrapper around every reactor call site.**

## Decision Outcome

Chosen option: **B — centralize at the reactor seam**, because `execute()` is the
single chokepoint every layer already passes through, it already hosts a
peer precondition (admissibility), and centralizing makes "a new layer forgot the
cap" structurally impossible — the failure mode that caused the incident.

Concretely:

1. On entry to `execute()`, when `HERMES_QUANT_PORTFOLIO_CAPS=1`, reconstruct the
   current book (the reactor already has `executions.jsonl` bus access), build
   `PortfolioState`, and `clip_one_to_remaining_headroom(symbol, fill_size_pct,
   state, caps)` with `PortfolioCaps.standard()`.
2. Clipped to ~0 → **silence**: return an `ExecutionRecord` flagged
   `reactor_metadata.silenced = True, silence_reason = "portfolio_cap_<reason>"`,
   and do NOT append a position-moving fill. Callers already tolerate a
   non-firing result (advisor records `auto_approve_error`, autonomous counts
   `silenced`).
3. Scaled down → execute at the clipped `fill_size_pct`, record
   `reactor_metadata.cap_scaled_from/to`.
4. **Remove the per-layer clips** (autonomous in-package; advisor in the deployed
   script) in the SAME wave the seam lands, so a pre-clipped fire is not clipped
   twice. Sequence: seam clip lands default-OFF → per-layer clips deleted →
   flag flipped on. Until the flag is flipped, behavior is bit-identical to today.

This decision is **coupled to ADR-0086**: once positions are stored in shares,
the cap's NAV-fraction headroom must derive `position_pct = qty × mark / equity`,
not read `quantity` as a fraction. The seam clip therefore consumes the same
marks the MTM read API uses. Both ADRs are promoted as a coherent pair.

### Consequences

- **Positive:** every current and future firing layer inherits the cap; the
  incident's root cause (a layer with no cap) is structurally prevented.
- **Positive:** one implementation + one test surface; the advisor and autonomous
  per-layer copies are deleted, ending copy drift.
- **Negative:** `execute()` now reconstructs portfolio state per fill when the
  flag is on (a `state.db` read). Mitigated: it's a local SQLite read, and the
  reactor already touches `state.db` via `apply_execution` immediately after.
- **Negative:** silencing inside the reactor changes the return contract subtly
  (a "fired" call may now return a silenced record). Every caller must treat a
  silenced record as not-a-fill (audited in the wave).
- **Neutral:** the env flag and `PortfolioCaps.standard()` thresholds are unchanged.

## Acceptance gate (must be green before status flips to accepted)

- [ ] `tests/unit/test_paper_reactor_cap.py::test_cap_silences_over_gross` — with flag on and a book at 200% gross, a new fire returns a silenced record, no position written.
- [ ] `::test_cap_scales_partial_headroom` — a fire that fits partially is scaled to remaining headroom; `cap_scaled_to` recorded.
- [ ] `::test_flag_off_is_bit_identical` — with the flag unset, `execute()` is byte-identical to pre-ADR behavior (no clip, no extra state read path taken).
- [ ] `::test_no_double_clip` — autonomous + advisor paths no longer pre-clip; a single clip happens at the seam (assert clip called exactly once per fire).
- [ ] `tests/integration/test_all_layers_inherit_cap.py` — advisor, playbook, hourly, autonomous all hit the cap when firing into a full book (the coverage the incident lacked).
- [ ] Per-layer clip code removed from `autonomous.py` and deployed `quant-daily-interim.py`; grep shows the only `clip_one_to_remaining_headroom` call site in a firing path is the reactor.
- [ ] Cross-family Phase-8 review finds no P0.

## More Information

- Firing-layer audit: `docs/research/2026-06-02-firing-layer-cap-audit.md`.
- Cap primitive: `hermes_quant/risk/portfolio_normalize.py`.
- Reactor seam: `hermes_quant/react/paper.py:81` (`execute`); admissibility
  precedent: ADR-0077/0079.
- Builds on ADR-0071 (portfolio caps); coupled to ADR-0086 (share accounting
  changes the unit the cap reads); incident:
  `docs/architecture/INCIDENT-2026-06-02-advisor-leverage-runaway.md`.
