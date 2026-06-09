# EVENT_RISK real-data ablation — verdict (2026-06-08)

> Reproducible eval result. The C2a instrument
> (`hermes_quant/backtest/event_risk_ablation.py`) made `HERMES_QUANT_EVENT_RISK`
> measurable; this is the real-data verdict it produced. NOT a synthetic plumbing
> test — real SPY prices, real FOMC dates.

## Setup

- **Flag:** `HERMES_QUANT_EVENT_RISK` (ADR-0084 pre-event blackout guard)
- **Symbol / window:** SPY, 2023-01-01 → 2024-12-31 (real yfinance daily bars)
- **Events:** `historical_fomc_calendar()` — the 16 public-record 2023–2024 FOMC
  rate-decision dates, asof-honest (`announced_at = scheduled_for − 365d`)
- **Strategy:** `EventRiskAblationStrategy` (full production analyst loadout),
  identical OFF vs ON; only the flag differs
- **Reproduce:** `HERMES_QUANT_RUN_BACKTEST=1` + the runner used to generate this
  (single-symbol real-data ablation; multi-symbol is intentionally hard-errored)

## Result

| metric | OFF | ON | Δ (ON−OFF) |
|---|---|---|---|
| Sharpe | −7.642 | −7.568 | **+0.075** |
| Sortino | −13880 | −13555 | +325 |
| max drawdown | −0.01257 | −0.01237 | +0.00020 (shallower) |
| total return | −0.01257 | −0.01237 | +0.00020 |
| n_trades | 63 | 62 | **−1** |
| DSR | 0.000 | 0.000 | — |

## Verdict: **HOLD** (keep EVENT_RISK default-OFF)

`d_sharpe +0.075 < +0.10` required (within noise band) **and** `ON DSR 0.000 ≤ 0.50`
(ON Sharpe not more-likely-than-not a real edge). The conservative promote policy
(ADR-style: a false-promote moves real capital and is strictly costlier than a
false-hold) holds the flag at default-OFF.

**The guard DID bite** — `d_n_trades = −1` proves the blackout silenced one fresh
open on an FOMC day (not a false null). ON was directionally better on every axis,
just not enough to clear the bar.

## Honest caveats

1. **Absolute Sharpes (−7.6) are a harness artifact**, not a live-book number: this
   is a single-symbol SPY passthrough through the eval advisor loadout. The verdict
   gates on the OFF-vs-ON **delta** under identical window/strategy — that is the
   valid signal; the absolute level is not.
2. **Single-name window.** The pre-FOMC drift literature
   (`docs/research/2026-06-08-r-macro-event-risk.md`) is a cross-sectional /
   index-level effect. A multi-name universe or a longer window could move the delta.
3. **No per-event tiering yet.** This ran FOMC-only (highest-impact). The research
   note tiers FOMC ≥ CPI > NFP ≈ PPI; a CPI/NFP-inclusive calendar is a future knob.

## Re-run conditions (what would flip HOLD → PROMOTE)

A real-data ablation clearing **both** `d_sharpe ≥ +0.10` **and** `DSR > 0.50` on a
representative window. Candidate changes to try: multi-name universe, longer
window (more FOMC events = more statistical power), FOMC-only vs all-Tier-1 tiering,
or a wider `event_risk_window_days`.

---

## UPDATE 2026-06-08 — multi-name fair re-test: Sharpe gate NOW CLEARS (HOLD on DSR only)

The re-open condition ("multi-name universe") was tested. On SPY/QQQ/TLT/XLF/IWM
(rate-sensitive cohort), EVENT_RISK's Sharpe delta is now **+0.231 — ABOVE the +0.10
bar** (single-SPY was +0.075, below). The blackout silenced 5 FOMC-day opens; every
metric improved. It HOLDs ONLY on the DSR sub-gate (`DSR 0.000 ≤ 0.50`), which is
suspect under the deeply-negative single-cohort-passthrough absolute Sharpe.
**EVENT_RISK is now the leading PROMOTE candidate** — blocked only by a DSR gate that
is plausibly a harness artifact. Next: a sizing-aware multi-name portfolio harness to
make DSR meaningful, then re-judge. NOT enabled yet. Full card:
`docs/research/2026-06-08-multiname-fair-verdict-retest.md`.
