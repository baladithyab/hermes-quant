# Scheduled Macro-Event Risk Management: CPI / PPI / FOMC / NFP

> Research note for the event-guard / blackout subsystem (ADR-0084 tuning).
> Compiled 2026-06-08 via Tavily + Exa. Focus: realized-vol spikes around
> scheduled prints, the pre-FOMC drift, blackout-window sizing, and whether
> event-avoidance helps risk-adjusted returns or just variance.

## 1. How big and predictable is the vol spike?

| Event | SPX |move| event day | vs avg day | Notes |
|---|---|---|---|---|
| **CPI (2021–25)** | ~1.9% | ~1.0% | **~2×** normal (tastylive, SPY 2019–25) |
| **CPI (2019–20)** | ~1.0% | ~0.8% | ~1.25×; barely elevated |
| **FOMC (since 6/2022)** | ±1.20% (NDX ±1.77%) | ±0.84% | Reliably > avg (Rhoads) |
| **PPI** | < CPI | — | Smaller equity reaction than CPI |
| **NFP** | elevated equity vol | — | A *rates-vol suppressor* (Capstone) |

Non-obvious: **implied vol usually *falls* on release** (uncertainty resolved). The
risk is the realized jump/gap, not a sustained vol regime. The CPI spike is
**regime dependent** — negligible pre-2021, ~2× now because month-to-month inflation
*variability* rose. PPI now matters materially less than CPI; CPI is the dominant
inflation print. NFP moves equities but dampens rates vol.

## 2. The pre-FOMC announcement drift (Lucca & Moench 2015, NY Fed)
Robust: since 1994 the SPX rose **+49 bp in the 24h (2pm→2pm) before scheduled FOMC
announcements**, vs ~0 on other 2pm→2pm windows. Excludes the decision itself →
purely anticipatory. ~**80% of the post-1994 US equity premium** earned here;
hold-only-pre-FOMC Sharpe ≈ **1.14**. Returns at/after the 2:15 release average ~0
and don't revert.
- 2018 NY Fed update: drift **persists only on press-conference meetings** (~40 bp).
- Cieslak–Morse–Vissing-Jørgensen: premium concentrated in **even weeks** of the
  FOMC cycle; "out in odd weeks" Sharpe **0.8 vs 0.4** buy-and-hold.

**Implication:** the drift argues for being **LONG into FOMC, not flat.** A naive
blackout that *flattens* before FOMC systematically forfeits the single largest
documented equity-premium window — the opposite of the edge.

## 3. Blackout windows desks actually use
Narrow and event-day-centric, not wide:
- **T-1 + event-day** standard (not T-5).
- **Size down ≥50%** rather than full flat.
- Avoid *new* breakout/overnight entries into the event; **hold existing swing
  positions through**, re-sync at the next session.

Current default **N_macro = 1 day** aligns with practice. **N_earnings = 5 days** is
conservative but defensible for single-name idiosyncratic gap risk.

## 4. Does avoiding macro-print days improve risk-adjusted returns?
Mostly **reduces variance/drawdown**, not raw return:
- Capstone "don't trade FOMC": 10-yr DD ~$50k→$45k — tail/variance benefit, modest
  return cost.
- CMV Sharpe doubling is an even/odd-week **timing** edge, not pure avoidance.
- Šafár & Mešťan (2026), 48 CPI prints 2021–25: cooler-CPI surprises → +0.88% to
  +1.19% abnormal SPX over 1–2d (significant); negative surprises symmetric but not
  significant — a mild positive skew that penalizes being flat/short.

Net: avoidance is a **risk-control lever** (cuts gap/jump variance + tail DD), not
an alpha lever. For an interday paper system the value is not getting whipsawed into
bad fills / forced exits.

## 5. Blackout NEW opens vs flatten existing — HOLD EXISTING
Evidence favors **blackout new/increasing positions, hold existing through the
event**:
- Pre-FOMC drift + announcement premium (Savor–Wilson: +11.4 bp announcement days
  vs +1.1 bp otherwise) → *existing* exposure is, on average, *paid* to sit through.
- Desks "avoid new / size down" but routinely hold swing positions through FOMC.
- Flattening forces round-trip costs and forfeits the premium → negative-EV for a
  multi-day-hold book absent a position-specific reason.

## 6. PPI vs CPI
PPI reaction now materially < CPI. **Severity tier: FOMC ≥ CPI > NFP ≈ PPI.** PPI
could be downgraded to medium (size-down only, no hard blackout).

## Recommendations for the guard (ADR-0084 tuning)
1. Keep **N_macro = 1 day** (T-1 + event day); apply to **opening/increasing** only.
2. **Hold existing multi-day positions through events** — do not flatten (the
   announcement premium / pre-FOMC drift make sitting through positive-EV on avg).
   *(NOTE: ADR-0084's guard is already "opening/increasing only, never blocks
   de-risking" — this validates the existing design.)*
3. **Tier severity:** FOMC > CPI > NFP/PPI (PPI medium; optional no hard blackout).
4. Prefer **size-down (≈50%)** over hard reject where continuous sizing exists.
5. Optionally **exempt the long side into press-conference FOMC** to avoid
   forfeiting the pre-FOMC drift; treat no-presser FOMC as lower severity.

## Sources
- Lucca & Moench (2015) NY Fed Staff Report; NY Fed Liberty Street 2018 update
- Cieslak, Morse & Vissing-Jørgensen, "Stock Returns over the FOMC Cycle"
- Savor & Wilson (2014) *JFE* "Asset Pricing: A Tale of Two Days"
- Rhoads, historical index/option action around CPI & FOMC (Substack)
- tastylive CPI study; Capstone "Market Volatility Around Economic Data Releases"
- Šafár & Mešťan (2026), *Applied Economics Letters*
