# Merge roadmap: `docs/rearchitecture-shared-pdr-core` → `main`

**Status:** DRAFT for operator sign-off. This is a *plan*, not an execution. Nothing
in this document merges anything; it enumerates the gates that must be green and the
decisions the operator must make before the branch lands on `main`.

**Date:** 2026-06-17 · **Author:** agent (deep-work loop) · **Branch HEAD:** `eb7f1db`
(moves as lanes land) · **Base:** `main` @ `0f90a1d`

---

## 1. Where we are (measured, not estimated)

| Fact | Value | Source |
|---|---|---|
| Commits ahead of `main` | **419** | `git rev-list --count main..HEAD` |
| Commits `main` is ahead | **0** | `git rev-list --count HEAD..main` |
| Fast-forward possible? | **YES** (`main` is an ancestor of HEAD) | `git merge-base --is-ancestor main HEAD` |
| Files changed | **671** (+146,141 / −6,206) | `git diff --stat main..HEAD` |
| Largest deltas | tests (239), research (153), hermes_quant (112), **cowork-quant (70)**, docs (56) | `git diff --name-only` |
| Open backlog seeds | **40** | `.seeds/issues.jsonl` |
| ADRs by status | 3 accepted · 14 proposed · 1 deprecated | `docs/adr/README.md` |

A clean fast-forward is *mechanically* possible, but a 671-file / 146K-line FF with no
review gate is exactly the kind of irreversible-on-`main` action this posture forbids.
The roadmap below converts "possible" into "safe."

---

## 2. The central decision: does `main` take both shells, or hermes-only?

**`cowork-quant/` (70 files) is branch-only — it does not exist on `main`.** Merging this
branch as-is introduces the entire second product shell (the Claude-Desktop "cowork" host)
to `main` in one shot. This is the single biggest governance question and it is **the
operator's to make**, not the agent's. Three shapes:

- **(A) Merge everything** — `main` becomes the home of `pdr_core` + both shells
  (hermes + cowork). Matches ADR-0092's "parallel plugins/shells" end-state. Simplest
  history, largest single review surface.
- **(B) Hermes-only merge, cowork lands later** — split the branch so the merge to `main`
  carries `pdr_core` + `hermes_quant` + shared docs/tests, and `cowork-quant` lands as its
  own follow-up PR (or stays on a cowork branch until that host is ready). Smaller blast
  radius; requires a clean file-partition (cowork-quant is already a top-level dir, so the
  partition is mechanical).
- **(C) Hold the merge** — keep accumulating on the rearchitecture branch until GATE-1+
  profitability evidence exists, then merge with that evidence attached. Lowest urgency,
  highest branch-drift risk.

> **Operator action required:** pick A / B / C. The rest of this roadmap is written so the
> gates apply under all three; only the *scope* of the final PR(s) differs.

The concurrent cowork session writes into `cowork-quant/` (and occasionally shared ADRs)
on this *same shared branch*. Whichever shape is chosen, the merge must not strand or
revert in-flight cowork work — coordinate the cut with that session (or pick B and let
cowork self-merge).

---

## 3. Pre-merge gates (all must be GREEN)

Each gate is a hard checklist item. A red gate blocks the merge.

### G1 — Full suite green on the branch tip
- [ ] `pytest -p no:cacheprovider` full run; record the exact pass count (evidence-backed,
      not a subset — a "sweep green" claim must paste the real number).
- [ ] Known pre-existing stub-dep failures (torch/ccxt/sklearn, ~28) are *separated* from
      regressions: run the relevant `-k` subsets and diff against an `origin/main` worktree
      so any new red is attributable to this branch, not the environment.
- [ ] Network-hang nodes (Kronos/HF) reproduced as pre-existing on a clean `origin/main`
      worktree (rc 124 on both) so they don't count as branch regressions.

### G2 — Default-OFF flag audit (no accidental live-on)
- [ ] `ops/scripts/quant-flag-inventory.py --write` is current; `tests/ops/test_flag_inventory_*`
      green. **75 flags** as of `eb7f1db`.
- [ ] Every capability flag defaults `0`. The flags that default **non-`0`** must each be
      justified as an intended-on safety rail or value flag, not an accidental enablement:
      `HERMES_QUANT_FUNDAMENTALS_REPORTING_LAG=1`, `HERMES_QUANT_MEMORY_INJECT=1`,
      `HERMES_QUANT_OPEN_GUARD=1`, `HERMES_QUANT_REFLECTION=1`, `HERMES_QUANT_SEMANTIC_ENABLED=1`,
      `HERMES_QUANT_TICK_LOCK=1`, `HERMES_QUANT_WEEKLY_RETRO=1`, `..._SHADOW_CONFIDENCE_FLOOR=0.85`,
      `..._SHADOW_CORR_CEILING=0.99`, `..._PAPER_SLIPPAGE_MODEL=v0.2`.
      (Most are rails/normalizers that *should* be on — but verify each, don't assume.)
- [ ] No money-path behavior changes when ALL `HERMES_QUANT_*` capability flags are unset
      (the byte-identical invariant the whole rearchitecture preserved).

### G3 — Review-findings closed
- [ ] The 4 review-findings filed 2026-06-18 are resolved or explicitly deferred-with-gate:
      `3e87` (P1 spread-dispatch), `b61c` (P1 slippage orphan), `d83b` (P2 run-card rail),
      `adb3` (P2 ADR hygiene). *(In flight: workflow `wvfr57g7a`.)*
- [ ] No P1 open seed touches a live money path.

### G4 — ADR status sweep
- [ ] The 14 "proposed" ADRs are triaged: the architecture-defining ones the merge *enacts*
      (ADR-0092 shared core, ADR-0093 Aegis name, ADR-0095 contract, ADR-0096 gates,
      ADR-0097 slippage, ADR-0098 taxonomy, ADR-0099 TP/SL+gates, ADR-0100 OpenBB) should be
      **accepted** before merge if the code lands them; ones still genuinely open stay
      proposed and the merge note says so. README status must match each ADR file's own
      front-matter (the `adb3` lane fixes the known ADR-0093 mismatch).
- [ ] No ADR cites a nonexistent file (the `adb3` lane fixes the ADR-0099 → ADR-0125 dangle
      and the broken 0027/0079/0085 links).

### G5 — Packaging / entry-point sanity
- [ ] `pyproject.toml` delta reviewed: the `hermes-quant-daemon` console script was REMOVED
      (vestigial daemon spine; live spine is the cron tick scripts). Confirm nothing on
      `main`'s deploy path still invokes the removed entry point.
- [ ] `pip install -e .` resolves cleanly from the branch tip in a fresh venv.

### G6 — Live-state / deploy reconcile
- [ ] Re-read `~/.hermes/{.env,config.yaml}` + `jobs.json` mtimes: the operator runs
      enablement between sessions, so confirm the branch's flag/cron assumptions match the
      *actual* live state (the standing reconcile discipline).
- [ ] `ops/scripts/quant-flag-inventory` + deploy-drift watchdog (#83) show no drift between
      repo and deployed tooling.

---

## 4. Merge mechanics (after gates green + decision made)

`main` lands work via **numbered PRs** (squash-style; see `#82`/`#83`/`#84`). This branch is
too large for one squash, so:

1. **Do NOT fast-forward `main` directly** even though it's possible — open a PR so the
   merge is reviewable + revertable as a unit, and CI runs.
2. **Choose squash vs merge-commit:** 419 commits carry the TDD/RED-proof history and the
   archaeology audit trail (ar01–ar37). A **merge commit** preserves that forensic history
   (valuable for money-software provenance); a **squash** gives `main` one clean commit but
   loses the per-fix RED-proofs. *Recommendation: merge-commit (preserve provenance), or a
   small number of squashed PRs partitioned by subsystem if shape (B) is chosen.*
3. If shape (B): partition into `pdr_core + hermes` PR first, `cowork-quant` PR second.
4. Tag the merge base + the merged tip so a revert target is unambiguous.

---

## 5. Operator sign-off checklist

- [ ] Decision §2 made (A / B / C).
- [ ] G1–G6 all green (evidence pasted, not asserted).
- [ ] cowork session coordinated (no in-flight work stranded).
- [ ] PR opened (not direct FF); squash-vs-merge decided per §4.
- [ ] Post-merge: branch retained until the first clean GATE-1 window confirms no
      profitability/behavior regression on `main`.

---

## Appendix — why "clean FF possible" is not "ready to merge"

`main` being 0-ahead means there are no conflicts to resolve — a real convenience. But the
merge introduces an entire architecture (`pdr_core`), a second product shell, 14 proposed
ADRs, and 146K lines. The risk is not *conflict*; it's *unreviewed surface landing on the
trunk that runs money software*. The gates above exist to make the merge boring.
