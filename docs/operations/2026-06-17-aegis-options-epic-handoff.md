# AEGIS full-P/D/R options epic — plan + operator decisions (2026-06-17)

> The vision: **automated stock + options + multi-leg stock/option combos; full P→D→R —
> Perceive (incl. option chains), Decide (best play + risk + SL/TP), React (act + lock in
> the watchlist + adjust/close).** This is the architected, sequenced path to it. Nothing
> is built yet — this is the plan, the honest gaps, and the decisions you must make first.

## Where the vision is vs reality (verified against code)

| Stage | Built? | Autonomous-wired? | The real gap |
|---|---|---|---|
| Perceive | data stack ✅ | ❌ equity-only | tick builds no option-chain frame; **no IV-rank compute seam exists**; watchlist rejects options |
| Decide | gate+selector ✅ | ❌ | `structure_select` never called in the tick; produces **only CC/CSP/wheel** — verticals/spreads are a **producer extension** |
| React | broker ✅ tested | ❌ flag-off | tick never originates a `MultiLegProposal`; multi-leg reactor "set nowhere"; CC has a serial-leg orphan risk |
| Monitor | stop ✅ (this session) | partial | **take-profit is NEVER enforced** (advisory metadata only — the exact gap the stop had); no watched-position registry; no options-aware exit |
| **Slippage realism** | shadow log ✅ | ❌ **no consumer** | Alpaca-paper fills optimistically vs live; **nothing haircuts it** → the evidence record would overstate live profitability (ADR-0097) |

## The honest combo verdict

- **Buildable today (HITL-only):** `covered_call`, `cash_secured_put`, `wheel`. CC and wheel
  *are* stock+option combos. They work end-to-end on paper/broker — just not autonomously.
- **NOT buildable (a real producer extension, large):** verticals, spreads, condors, calendars,
  PMCC, straddles. The producer is structurally **single-short-leg**; `structure_select`
  deliberately abstains on defined-risk multi-leg because the producer can't build them.

## The sequenced plan — 14 increments + the slippage gate (all default-OFF)

**Equity-edge-first** (the book is 0/11, −4.64%; options' larger decision surface + convexity
must not compound an unproven signal — and ADR-0029 requires evidence before live).

1. **Equity foundation** (`AG-EQ-1..3`): prove SL+**enforce TP** on a clean 30-day window; composite (asset_class,symbol) keying; the **watched-position registry** (your "lock it in & keep watching").
2. **Slippage gate** (`aegis-sl01`, **ADR-0097**): the paper-vs-live haircut — **must land before any evidence window** so the record is live-realistic. This is the increment that directly answers your slippage concern.
3. **Perceive options** (`AG-PERC-1..3`): `options_eligible` watchlist flag + **as-of-honest IV-rank compute seam** + chain fields on the frame + (later) live-chain fetch.
4. **Decide options** (`AG-DEC-1..2`): wire `structure_select`→`options_gate`→producer into the tick; autonomous origination behind `HERMES_QUANT_AUTONOMOUS_OPTIONS`.
5. **React + Monitor options** (`AG-REACT-1`, `AG-MON-1..2`, `aegis-sl02`): route multi-leg through the existing chokepoint (atomicity); options-aware SL/TP exits; the options/MLEG slippage penalty.
6. **Evidence gate** (`AG-OPT-EV-1`): 30-day / N≥30 paper options window — the ADR-0029 checkpoint.
7. **Extend the producer** (`AG-OPT-2`) to verticals/defined-risk spreads — *only after* the income-structure window proves out.
8. **Live chain + the N≥100 ADR-0029 D7 window** (`AG-OPT-3`) before any live flip.

Seeds: `aegis-ao00` (epic root) + `aegis-ag*` (the 14) + `aegis-sl01/02` (slippage). ADR-0097 committed.

## DECISIONS I need from you before building (the gate to start)

1. **Equity-edge threshold.** What must the clean-window equity record show before options origination turns on? (e.g. `win_rate ≥ 0.45 + realized_sharpe ≥ 0.5` over N≥20 round-trips — or is the ADR-0029 options evidence gate sufficient alone?) Today's 0/11 makes any threshold trivially unmet.
2. **First options structure scope.** CSP-only first (simplest, cash-collateral, no leg-atomicity risk) — or CC+wheel too (CC has a serial-leg orphan risk)?
3. **Options take-profit fraction.** Start at 50%-of-max-premium (the theta-capture standard) or a different fraction? (Not yet eval-gated on our data.)
4. **CC autonomous timing.** Defer CC autonomous origination behind its own flag until an orphan-hedge reconciliation cron exists — or accept it at `AG-REACT-1` with the no-fill parent record as protection?
5. **Multi-leg concurrency counting.** Does a covered call count as **1** position (the combo) or **2** (equity + option leg) against `max_concurrent_positions`?
6. **IV-rank min window + slippage prior.** Min data points for a valid IV-rank (proposed 30/252); and how conservative the initial slippage static prior should be before shadow samples accrue (ADR-0097 fails closed — bigger = safer/fewer trades).
7. **Build order confirm.** Proceed equity-foundation + slippage-gate first (my recommendation), or do you want options capability built in parallel behind flags immediately?

## Honest scope

This plan makes the system *capable* of the full vision and makes its paper record *trustworthy*
(armed rails + clean book + **live-realistic slippage**). It does not by itself create edge —
that's the strategy work the trustworthy record is meant to reveal. Options multiply the decision
surface; they earn "live" only through the ADR-0029 evidence window on a live-realistic record.
