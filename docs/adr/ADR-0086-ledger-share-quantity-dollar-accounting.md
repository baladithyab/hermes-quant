---
status: proposed
date: 2026-06-02
deciders: [codeseys]
amends: ADR-0085
amended_by: ADR-0091
---

> **Amended-by ADR-0091 (2026-06-10):** this ADR's Phase-2 plan assumed positions are
> NAV-fractions and did not address that both the `paper` and `deterministic-equity`
> reactors write an *absolute target* into the per-fill size field while every consumer
> reads it as a traded *delta* — inflating derived positions on re-affirmation (BA −0.2
> ×6 → −0.8; AAPL 5% ×12 → 60%). ADR-0091 fixes the producers to emit the true traded
> delta (so `executions.jsonl` stays a faithful transaction log) plus a one-time
> backed-up log repair, and is a prerequisite-correctness fix the Phase-2 share migration
> must build on. Phase-1 read-time MTM and the Phase-2 end-state target here are preserved.

# ADR-0086: Migrate the paper ledger to share-quantity + dollar accounting with mark-to-market equity

## Context and Problem Statement

The 2026-06-02 leverage-runaway incident exposed that `~/.hermes/quant/state.db`
and the `hermes_quant.state.portfolio_state` projection engine carry a **unit
confusion** that corrupts every dollar figure the system reports:

- Positions are stored in **NAV-fraction units** (`quantity = -0.2` means "20% short"),
  because `_apply_execution_unsafe` (portfolio_state.py:477) sets
  `pos_delta = fill_size_pct` on the legacy equity path.
- Cash starts at a **dollar** value (`$100,000`) but is then mutated by
  `delta_cash = -fill_size_pct * fill_price` (portfolio_state.py:583) — a
  NAV-**fraction** times a per-**share** price. `0.2 × $112 = $22.40` is debited
  for what is conceptually a 20%-of-NAV ≈ `$20,000` position. The cash ledger is
  off by orders of magnitude and dimensionally meaningless.
- `equity_total` is computed as `cash + Σ abs(quantity) × avg_entry_price`
  (portfolio_state.py:376 and :593). Three independent errors compound here:
  (1) it uses `avg_entry_price` not the current mark, so **unrealized P&L is
  invisible by construction**; (2) `abs(quantity)` treats a short as a long
  asset, **inflating** equity for a net-short book instead of reflecting the
  short as a credit/liability; (3) it multiplies a NAV-fraction by a price.

The contract these violate is explicit: `hermes_quant.protocol.Portfolio`
documents `equity_total: float  # cash + sum(positions.mark_value)` (protocol.py:276)
and computes `position_pct` from `mark_price` (protocol.py:299). A correct
reference fold already exists in `hermes_quant.daemon.portfolio_loader.reconstruct_portfolio`
(portfolio_loader.py:155-166): it takes per-asset `mark_prices`, uses **signed**
`qty`, falls back last-fill→avg-entry when a mark is missing, and computes
`equity_total = cash + Σ qty × mark_price`. The daemon path is right; the
`state.db` projection path is wrong, and the daemon path isn't what gets written
to `state.db`.

This is below the deterministic risk gate (ADR-0004, unaffected) and is a
bookkeeping-integrity problem. But the paper track-record is the eval signal the
entire default-OFF rollout depends on (ADR-0080), so a ledger that can report
`equity_total = +$12k` while the book is `−$30k` mark-to-market poisons that signal.

### Relationship to ADR-0085

ADR-0085 (event-sourced ledger: `executions.jsonl` authoritative, `state.db`
a derived projection) is **preserved in full**. This ADR does not change *what is
authoritative* — it changes *the units and math the projection uses when folding
the authoritative log*. ADR-0085 §Decision point 2 ("state.db is reconstructable
by replaying executions.jsonl") still holds; the fold function is corrected, not
relocated. Formally this `amends` ADR-0085 by tightening the projection's
accounting contract; it supersedes no decision in it.

## Decision Drivers

- **Honest P&L is the product.** A paper-trading system whose reason to exist is
  an auditable track record cannot misreport equity by 30%+ of NAV.
- **Dimensional coherence.** Cash, position value, and equity must all be in
  dollars; position quantity must be in shares/contracts; conversions explicit.
- **Reuse the correct reference.** `daemon.portfolio_loader` already folds
  correctly — converge on its shape rather than invent a third.
- **Network-free hot path** (money-software addendum): `apply_execution` runs
  inside `PaperReactor.execute`; it must NOT make a network call for a live mark.
- **Backward compatibility** with ADR-0085 authority + existing replay tooling.

## Considered Options

- **A. Minimal patch:** keep NAV-fraction positions; just multiply cash/equity
  math by NAV and use signed marks at report time.
- **B. Full migration to share-quantity + dollar accounting** (positions in
  shares, cash in real dollars, `equity_total = cash + Σ qty × mark`), with marks
  injected (never fetched in the hot path), and a one-time reconstruct from the
  authoritative `executions.jsonl`.
- **C. Drop `state.db` equity entirely; compute equity only in the daemon/report
  paths** that already have marks.

## Decision Outcome

Chosen option: **B — full migration to share-quantity + dollar accounting** is the
correct end-state, because the unit confusion is the root cause of the whole bug
family (cash, short sign, and stale-equity are three faces of "fractions, shares,
and dollars are conflated"). **But it ships in two phases** (decided 2026-06-02
after the pre-mortem at `docs/research/2026-06-02-premortem.md` surfaced 5
catastrophic failure modes — NAV_at_fill bootstrapping circularity, share-migration
blast radius across ≥4 consumers, reconcile divergence — that make a same-session
full migration unsafe during incident cleanup):

- **Phase 1 (THIS session — ship now): read-time mark-to-market, no schema change.**
  Positions stay NAV-fraction in `state.db` (the working firing/cap path is
  untouched). Add a `get_marked_equity(account, mark_prices)` read API that
  computes honest signed MTM equity on demand from injected marks, and fix the
  cash-delta + `abs()`-short-sign math so the cached `equity_total` is at least
  *cost-basis-coherent* (signed, dollar-denominated) rather than dimensionally
  nonsensical. Reporting tools call `get_marked_equity`. This makes every reported
  P&L figure honest today without touching `executions.jsonl` schema or the
  fraction→shares conversion. **No NAV_at_fill bootstrapping is introduced** — the
  circularity the pre-mortem flagged (#1) is entirely avoided in Phase 1.

- **Phase 2 (DEFERRED — planned follow-up arc, gated by the pre-mortem): full
  share migration.** Positions stored in signed shares; `executions.jsonl` gains
  a signed `qty`; the fraction→shares conversion happens once at fill time with
  `NAV_at_fill`; every `quantity`-reading consumer (`cli/status.py`,
  `ops/scripts/quant-admissibility-restate.py`, playbook scripts,
  `risk/portfolio_normalize.py`) is migrated; `quant-ledger-reconcile` rebuilds
  under new accounting. This phase MUST clear every failure mode in the pre-mortem
  before execution and is a 3-4 wave arc in its own session.

The Phase-2 end-state below documents the full target; the Acceptance Gate is
split into Phase-1 (must pass to ship now) and Phase-2 (gates the deferred arc).

Concretely (Phase-2 end-state target):

1. **Position quantity is stored in SHARES/CONTRACTS** (signed). The fill record
   already carries `fill_price`; a fill of `fill_size_pct` of NAV at `fill_price`
   converts to `shares = (fill_size_pct × NAV_at_fill) / fill_price`. The
   `NAV_at_fill` is the equity_total **before** the fill (sourced the same way
   `autonomous.py:_account_nav` does — `state.db` cash.equity_total, the
   materialized NAV after prior fills). The multi-leg path (reactor_metadata.quantity)
   already stores signed shares/contracts — this generalizes that to the equity path.
2. **Cash is in dollars.** `delta_cash = -shares × fill_price` (a long buy
   spends cash; a short sale credits cash). No fraction × price.
3. **`equity_total` is mark-to-market:** `cash + Σ qty × mark` with **signed**
   qty. Marks are **injected** via a `mark_prices: dict[str,float]` argument
   (mirroring `daemon.portfolio_loader.reconstruct_portfolio`). When a mark is
   absent the fold falls back last-fill-price → avg-entry, and the write records
   `equity_basis = "mark" | "last_fill" | "entry"` so a reader knows whether the
   number is true MTM or a cost-basis stand-in. **No network call in
   `apply_execution`.** The hot path writes cost-basis equity (mark absent); a
   read-time/report-time API marks to market with injected quotes.
4. **A read API `get_marked_equity(account, mark_prices)`** computes true MTM on
   demand from the stored share quantities + injected marks. Reporting tools
   (`quant-portfolio-daily`, `quant_status`) call it with marks from the existing
   `DataProvider`/`YFinanceProvider` seam.
5. **One-time reconstruct.** After the migration lands, `quant-ledger-reconcile`
   (ADR-0085's tool) rebuilds `state.db` from `executions.jsonl` under the new
   accounting. The current live book is already reset to flat $100k (incident
   remediation), so there is no legacy fractional book to convert — the first
   real fills post-migration write shares from the start.

### Consequences

- **Positive:** every dollar figure (cash, position value, equity, P&L) becomes
  dimensionally correct and mark-to-market-honest. The incident's "−$30k that the
  system reported as +$12k" cannot recur.
- **Positive:** converges `state.db` projection onto the already-correct
  `daemon.portfolio_loader` accounting — one accounting model, not two.
- **Positive:** short positions correctly reduce/credit equity per their sign.
- **Negative:** `quantity` changes units (fraction → shares). Every consumer that
  read `quantity` as a NAV-fraction must be audited and updated — notably the
  portfolio-cap code (`risk/portfolio_normalize.py`, which operates on NAV
  fractions) now needs `position_pct = qty × mark / equity` to derive the
  fraction, not read `quantity` directly. This is the migration's main blast radius.
- **Negative:** a `NAV_at_fill` lookup is now on the fill path (read of prior
  equity); must be ordered before the cash mutation and be concurrency-safe
  (the existing `BEGIN IMMEDIATE` covers this).
- **Neutral:** `executions.jsonl` schema is unchanged (still records
  `fill_size_pct` + `fill_price`); only the *projection* changes how it folds them.

## Acceptance gate

### Phase 1 (must be green to ship THIS session)

- [ ] `tests/unit/test_portfolio_state_accounting.py::test_marked_equity_signed_mtm` — `get_marked_equity(account, mark_prices)` returns `cash + Σ signed_weight × equity × (mark/entry)` with correct sign for a mixed long/short book; reproduces the incident book's ≈−$30k under the same marks (regression lock against the "+$12k while −$30k" bug).
- [ ] `::test_marked_equity_short_reduces_equity` — an adverse mark on a short REDUCES marked equity (no `abs()` inflation).
- [ ] `::test_marked_equity_falls_back_when_mark_absent` — missing mark → falls back to entry, records `equity_basis != "mark"`.
- [ ] `::test_get_marked_equity_no_network` — `get_marked_equity` makes no network call (marks are injected).
- [ ] Reporting path (`quant-portfolio-daily` / `quant_status`) calls `get_marked_equity` with provider marks; flat-$100k book reports flat $100k.
- [ ] Full `pytest` sweep green; no change to `executions.jsonl` schema or the firing/cap path.

### Phase 2 (gates the DEFERRED share-migration arc — not required to ship Phase 1)

- [ ] `::test_long_fill_cash_and_equity_in_dollars` — a 20%-of-$100k long at $100 stores 200 shares, debits $20,000 cash.
- [ ] `::test_short_fill_credits_cash_and_reduces_net_equity` — a 20% short stores −200 shares, CREDITS $20,000 cash.
- [ ] `::test_no_network_in_apply_execution` — hot path network-free with `NAV_at_fill` sourced correctly (no bootstrapping circularity — pre-mortem #1).
- [ ] `::test_portfolio_cap_reads_fraction_from_shares` — `risk/portfolio_normalize` derives NAV-fraction from `qty×mark/equity`, not raw `quantity`.
- [ ] Every `quantity`-as-fraction consumer migrated (pre-mortem #2 blast-radius list): `cli/status.py`, `ops/scripts/quant-admissibility-restate.py`, playbook scripts.
- [ ] `quant-ledger-reconcile` rebuilds the flat-$100k book to flat-$100k under share accounting (idempotent; no reconcile divergence — pre-mortem #4).
- [ ] Cross-family Phase-8 review (≥3 families) finds no P0 in the accounting math.

## More Information

- Bug-hunt research: `docs/research/2026-06-02-portfolio-state-bug-hunt.md`.
- MTM seam design: `docs/research/2026-06-02-mtm-equity-and-mark-price-seam.md`.
- Correct reference fold: `hermes_quant/daemon/portfolio_loader.py:155-166`.
- Incident: `docs/architecture/INCIDENT-2026-06-02-advisor-leverage-runaway.md`.
- Preserves ADR-0085 (event-sourced authority), ADR-0004 (gate untouched),
  ADR-0080 (paper track-record is the eval signal this protects).
