# INCIDENT — Advisor cap gate phantom gross (402% from corrupt state.db seed)

**Date discovered:** 2026-06-10
**Severity:** Medium (paper-only; no money loss — the gate erred SAFE, but it dark-firedᵃ the entire advisor layer, starving it of calibration data every run)
**Status:** Resolved — seed source corrected to the canonical paper-book projection; regression-pinned.

ᵃ "dark-fired" = produced signals but executed 0 fills, silently, on every premarket/midday/EOD run.

---

## Summary

Every advisor auto-fire (the premarket / midday / EOD interim briefs under
`HERMES_QUANT_AUTONOMY=paper`) was silenced with:

```
portfolio_cap_silenced (headroom_breached gross=402.133 cash=-401.133)
```

while the **real paper book was a single BA short at −0.20 = 20% gross, 80% cash
headroom**, and the real Alpaca paper account was healthy (equity $97.2k, cash
+$55.3k). The `gross=402.133` was a **phantom** — note `cash = 1 − gross`
(1 − 402.133 = −401.133), so the entire silence cascade traced to one bogus
`gross` number. The brief's own operator note flagged the symptom ("gross at
~402% with cash at −401% suggests positions aren't being closed as expected")
but read it as a real over-leverage; it was not.

The autonomous-tick layer (which uses the correct projection) was unaffected and
kept firing normally throughout.

## Root cause

The advisor cap gate (`auto_approve_actionables` in `ops/scripts/quant-daily-interim.py`)
was made "account-aware" by seeding its running book from
`state.db.positions WHERE account_id='paper-default'`, with the (incorrect)
comment: *"state.db.positions.quantity is the net cumulative target weight."*

**It is not.** `state.db.positions` is written by the **additive**
`state/portfolio_state.py::reconstruct_from` projection across **all** reactors,
where `quantity` accumulates raw **share counts**, not normalized signed target
weights. That table also still carried a **corrupt `AAPL` row at `quantity=399.93`**
— a residue of the 2026-06-08 `reconstruct_from`-after-flatten corruption
(documented in the `hermes-quant-operations` skill: *"never trust state.db
positions table — the two projections diverge by design"*).

Summing `|quantity|` over that table:

```
AAPL 399.93  +  (AAL 0.20, AVGO 0.20, BA 0.80, CBOE 0.20, CDNS 0.20,
                 CRM 0.20, META 0.20, ORCL 0.20)  =  402.133
```

The cap read `402.133` as a 40213% gross book → no headroom → every actionable
silenced. The phantom was introduced by the 2026-06-09 P1 cap-gate hardening
(PR #82): that change correctly flipped the gate **fail-open → fail-closed**, but
the new account-aware seed it added plugged into the one table the skill
explicitly says never to trust.

## Why it stayed safe (and why it still mattered)

The gate failed toward **silence**, not toward firing — so no runaway, no money
loss, the opposite of the 2026-06-02 incident. But a safety control that silently
no-ops the entire firing layer is still a latent failure: the advisor path
produced zero paper fills for ~1 day, so its calibrators received no feedback and
the research signal went dark with only a buried operator-note hint.

## Remediation (2026-06-10)

1. **Seed source corrected.** The cap now seeds `_running` from
   `hermes_quant.portfolio.state.reconstruct_portfolio_state(reactor_filter="paper")`
   — the **same** latest-target-supersedes, paper-only projection the ADR-0087
   reactor-seam cap and the autonomous tick already use. `state.db.positions` is
   no longer read for seeding at all. The advisor cap now agrees with every other
   firing surface by construction. Verified live: seed → gross 20%, headroom 80%.

2. **Regression-pinned** (`tests/ops/test_quant_daily_interim_cap_safety.py`):
   - `test_cap_seeds_from_paper_book_not_corrupt_state_db` — seeds a clean 20%
     paper bus + a poisoned `state.db` (AAPL=399.93); asserts the pick FIRES and
     `state.db` is **never read** (a `sqlite3.connect` tracker proves zero reads).
     Confirmed RED on the old code (reproduces `gross=400.733`), GREEN on the fix.
   - `test_phantom_gross_book_still_silences_a_real_breach` — seeds a genuine
     200%-gross paper book via the projection; asserts a new fire is still
     silenced. Proves the seed swap preserved the cap's protective behavior.

3. **The corrupt `state.db` AAPL row is now cosmetic only.** With nothing reading
   `state.db.positions` on the firing path, the 399.93 residue no longer affects
   trading. A clean paper-only rebuild of that table remains out of scope (the
   advisor/autonomous firing path never reads it; the bus +
   `reconstruct_portfolio_state` view is the source of truth).

## Lesson

When you make a safety gate "account-aware," the book it reads must be the **same
projection the rest of the firing path trusts.** This system has two reconstructors
over one bus that diverge by design — additive share-counts (`state.db`,
`reconstruct_from`) vs latest-target-weights (`reconstruct_portfolio_state`). A cap
seeded from the wrong one is either too loose or, as here, pathologically too
tight. The fail-closed direction (PR #82) was correct; the seed source was the bug.
A guard's *input* deserves the same scrutiny as its *direction*.
