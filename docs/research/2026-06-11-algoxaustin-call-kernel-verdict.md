# algoxaustin call-buying kernel — backtest verdict (2026-06-11)

> Honest grade of the reconstructable kernel of the `@algoxaustin` "Claude Code scans the whole
> market, $500→$3,800 in a week" reel. Methodology extraction + UW auth finding:
> `methodology/algoxaustin-claude-code-flow-screener.md`.
> **Verdict: DEAD — the signal loses to random call-buying and goes negative net of spread.**

## What was tested

The reel's "point system" has 6 factors (unusual flow / volume / P/C / GEX / momentum / theta).
Only **momentum + unusual-volume** are reconstructable on free Alpaca data (GEX/flow/P/C need a
historical OI+greeks time series Alpaca does not serve). So we tested exactly that kernel:

- **Universe:** 16 liquid high-options-volume names (NVDA TSLA AMD SMCI PLTR AAPL META AMZN MSFT
  GOOGL COIN MSTR MARA SOFI AVGO NFLX)
- **Signal:** cross-sectional z(20d momentum) + z(volume / 20d-avg-volume), ranked weekly
- **Trade:** buy top-K=3 names' short-dated (7–16 DTE) slightly-OTM calls, hold ~1 trading week,
  exit — matching the reel's exact "in a week" mechanics
- **Window:** 2024-09-03 → 2026-06-08, **93 weekly rebalances, 242 trades** (87% contract-data hit rate)
- **Data:** Alpaca option daily bars (inception ~Feb 2024) + IEX stock bars
- **Nulls:** random-K pick, bottom-K (anti-signal); graded at 0 / 5 / 10% round-trip spread
- **Repro:** `/tmp/uw_kernel_backtest.py` + `/tmp/uw_boot.py` (ephemeral; rebuild from this note)

## Result

| spread | strat | n | win% | avg ret | median | trade-Sharpe |
|---|---|---|---|---|---|---|
| 0% | **TOP-K (signal)** | 242 | 31.8% | **+5.8%** | **−55.9%** | 0.26 |
| 0% | RANDOM-K (null) | 248 | 32.7% | +22.4% | −46.1% | 0.90 |
| 0% | BOTTOM-K (anti-signal) | 242 | 35.5% | +22.3% | −48.3% | 0.84 |
| 5% | TOP-K | 242 | 31.4% | +0.6% | −58.0% | 0.03 |
| 5% | RANDOM-K | 248 | 32.3% | +16.4% | −48.7% | 0.69 |
| 10% | TOP-K | 242 | 30.2% | **−4.3%** | −60.1% | **−0.22** |

## Verdict: **DEAD** (no edge; mildly anti-predictive; negative net of cost)

Three independent reasons the kernel is not an edge:

1. **Loses to random.** TOP-K (+5.8% gross) underperforms both RANDOM-K and the *anti-signal*
   BOTTOM-K (+22% each) at zero cost. The momentum+uvol score is, if anything, mildly
   anti-predictive for short-dated calls — it buys names that already ran (peak-IV lottery tickets).
2. **All P&L is 3 lottery tickets.** Bootstrap 95% CI on TOP-K mean = **[−12.8%, +26.1%]** —
   straddles zero. **166% of total P&L came from 3 of 242 trades** (top winners +1081%, +631%, +604%).
   Strip those 3 → average trade is **−3.9%**. Median trade is **−56%**.
3. **Negative net of realistic spread.** At 5% round-trip the edge is gone (+0.6%); at 10% it's
   −4.3% with negative trade-Sharpe.

This is the exact shape of the reel: **one SMCI-style moonshot funds the highlight; the ~68% that
expire worthless never make the video.** Survivorship, not signal. The IG commenter who said
"financial data is non-IID, AI models overfit this" was correct.

## Why the legitimate leg wasn't tested

GEX — the only factor with a real mechanistic thesis (dealer hedging is a mechanical force;
see `options-microstructure-regimes` skill) — is **not reconstructable on our data**: Alpaca serves
greeks + OI only as-of-now, never as a historical time series. Reconstructing GEX historically needs
a paid EOD option-chain archive or the Unusual Whales API. UW is **token-gated, $50/wk, no free tier,
hard-exit on missing key** (auth test in the methodology note) — so the GEX leg cannot be backtested
for free, only forward-paper-tested behind the paywall.

## Disposition

- **No build.** The reconstructable kernel is a measured loser; do not wire short-dated call-buying.
- **Catalog so it doesn't get re-litigated.** This joins the bake-off canon of "popular internet
  options primitive that dies under honest grading" (cf. the FPT / ICT-FVG / open-reversion negatives
  in `strategy-backtest-optimization`'s reference bank).
- **Only live thread:** forward-only paper GEX+flow ranking via the UW MCP server, gated on the
  $50/wk token, GEX as a silence-bias gate dimension (not a fire signal). Staged, not activated.
