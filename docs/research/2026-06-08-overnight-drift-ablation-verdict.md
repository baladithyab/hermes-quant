# OVERNIGHT_DRIFT real-data ablation — verdict (2026-06-08)

> Reproducible eval result for ADR-0089's acceptance gate item 4. The
> `OvernightDriftAnalyst` is implemented + unit-tested; this is the real-data
> flag-ablation that decides whether it earns a live default.

## Setup

- **Flag:** `HERMES_QUANT_OVERNIGHT_DRIFT` (gates the analyst into the loadout)
- **Symbol / window:** SPY, 2023-01-01 → 2024-12-31 (501 real yfinance daily bars)
- **Strategy:** `AdvisorStrategy(analysts=None)` — full production loadout, which
  reads the flag at call time inside each leg's env-override (OFF leg excludes the
  analyst, ON leg includes it). No carrier needed — the flag gates the loadout, so
  it is directly measurable (unlike EVENT_RISK).
- **Reproduce:** `HERMES_QUANT_RUN_BACKTEST=1` + a single-symbol real-data ablation
  on `HERMES_QUANT_OVERNIGHT_DRIFT`.

## Result

| metric | OFF | ON | Δ (ON−OFF) |
|---|---|---|---|
| Sharpe | −7.642 | −8.530 | **−0.887** |
| Sortino | −13880 | −1.72e13 | (degenerate ON) |
| max drawdown | −0.01257 | −0.01495 | worse |
| total return | −0.01257 | −0.01495 | worse |
| n_trades | 63 | 75 | **+12** |
| DSR | 0.000 | 0.000 | — |

## Verdict: **HOLD** (keep OVERNIGHT_DRIFT default-OFF)

Enabling the analyst on this window **worsened** risk-adjusted return
(`d_sharpe −0.887`, far below the `+0.10` promote bar) and **added trades**
(63 → 75, deeper drawdown). The LONG hold-through-close nudge added conviction
that did not pay off.

This is **consistent with the spike** (`docs/adr/ADR-0089` §Spike findings):
SPY's trailing overnight tilt was weak (+1.6% annualized) and the high-beta names
were intraday-driven in 2023–2024 — the opposite of the longer-horizon meme-cohort
overnight tilt the research note cites. A weak/wrong-sign signal that nudges
conviction simply adds noise.

**The eval-gate working as designed.** The analyst is built, correct, asof-honest,
and unit-tested (13 tests green) — but the data says it does not earn a live
default on this window, and the gate caught that **before any capital moved**. The
analyst stays OFF. This is the deliberate opposite of flipping a flag on a
plausible thesis without evidence.

## Honest caveats

1. **Absolute Sharpes (−7.6/−8.5) are a single-symbol-SPY-passthrough harness
   artifact**, not a live-book number. The verdict gates on the OFF-vs-ON **delta**
   under identical window/strategy — that is the valid signal.
2. **SPY is the wrong universe for this signal.** The research note is explicit:
   the overnight premium concentrates in high-retail-attention / meme / ETF names,
   NOT dull broad-market index. Testing on SPY is a conservative (likely-to-fail)
   choice; a cohort-weighted universe is the honest re-test.

## Re-open conditions (what would flip HOLD → PROMOTE)

A real-data ablation clearing **both** `d_sharpe ≥ +0.10` **and** `DSR > 0.50` on:
- a universe weighted toward the strong-overnight cohort (retail/meme/ARKK-like),
- a longer / more regime-varied window, or
- tuned `spread_to_conf_scale` / `min_abs_spread` so the nudge fires only on a
  clearly-positive tilt.

Until then, OVERNIGHT_DRIFT stays default-OFF.
