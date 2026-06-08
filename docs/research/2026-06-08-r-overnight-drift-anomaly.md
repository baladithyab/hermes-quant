# The Overnight Return Anomaly (2024–2026): Empirical State & Tradeability

> Research note grounding ADR-00XX (OvernightDriftAnalyst). Compiled 2026-06-08
> via Tavily + Exa across primary sources. For a quant repo's perception layer.

**Bottom line:** The anomaly is real, persistent, and recently re-confirmed — but
the headline returns are *gross/pre-cost*, and the cross-sectional long-short
version is **not** harvestable net of costs by a daily open/close round-trip. A
low-frequency interday system that simply **holds through the close captures the
premium structurally for ~free**; it should not try to round-trip the open/close.

## 1. Is the long-only overnight-vs-intraday split still present post-2020?
Yes. Haghani, Ragulin & Dewey ("Night Moves," *JOIM* 22(2):68–80, 2024): over ~30
years, $1 in SPY held open→close grew to ~$1.20 (below T-bills) while held
close→open grew to ~$17.27. In **22 of 24 countries** the open→close index return
since 1990 was negative even before costs. NY Fed (Boyarchenko, Larsen & Whelan,
"The Overnight Drift," *RFS* 36(9), 2023): overnight return positive in **20 of 23
years since 1998**, significant in 17. Elm's Mar-2025 update confirms persistence.
No decay/reversal in the basic time-series split. (Reversal exists only in China —
T+1 settlement.)

## 2. Cross-sectional long-short: Sharpe & survival net of costs
HRD's long-short (long high trailing overnight-minus-intraday, short converse):
**~38% p.a. gross, ex-costs, unlevered**, ~4% max DD → implied gross Sharpe ~**10×
long-short momentum**. The authors state it is explanatory, *"not to propose a
potentially profitable"* strategy.

**Costs destroy it.** Trades the full book at open AND close daily, so **1 bp/side
≈ −5% p.a.**, plus borrow. Corroboration:
- **STOXX (2024):** EURO STOXX 50 overnight 12.9% vs intraday −4.3%, but at **1
  bp/trade the overnight strategy falls to 7.3% p.a. — below buy-and-hold.**
- **AlphaArchitect:** buy-open/sell-close goes **+717% gross → −32% net**.
- **NY Fed (S&P futures):** 2–3pm long gross Sharpe **1.1 → −0.5** after spreads.

**Verdict:** gross Sharpe spectacular; **net-of-cost ≈ zero-to-negative** for the
daily round-trip.

## 3. Which cohorts strongest?
Concentrated in **retail-excitement / "meme" names** — high-retail-attention,
ARKK, Bitcoin ETFs, retail-oriented ETFs; weak/absent in dull large-caps. Effect
rises with retail ownership, ETF packaging, small/mid-cap illiquidity, high
idiosyncratic vol — **the same cohort that is most expensive to trade.** The
+138,000,000% MU figure is a gross, compounded, single-stock illustration, NOT a
deployable return.

## 4. Leading explanations
- **Retail buy-at-open / meme demand** (HRD 2024) — explains the cohort pattern.
- **Inventory/overnight risk premium** (NY Fed 2023) — MMs carrying overnight
  inventory at 50–100× lower volume demand compensation; not easily arbitraged.
- **ETF liquidity-provision mechanics** (Lachance 2021, *JFM*).
- **Tug-of-war** (Lou, Polk & Skouras 2019, *JFE*) — retail(open) vs institutions
  (close) → persistent overnight↔intraday reversal; basis of the cross-section.
- **"Toxic factor"** (Knuteson) — contested; Aaron Brown calls it a conspiracy
  theory. No settled science.

## 5. Harvestable by a hold-through-close interday system? — THE KEY REFRAME
- **Hold-through-close — YES, structurally, for free.** Any interday system that
  stays invested across the close already earns the overnight drift as part of
  total return, at zero incremental cost. Design stance for an
  `OvernightDriftAnalyst`: **avoid systematically selling into the close and
  rebuying at the open; prefer executing unavoidable rebalances near the CLOSE,
  not the open** (the open is where you pay the inflated price). Trade-timing is
  the real lever (NY Fed concurs).
- **Round-trip open/close — NO.** 2× daily full-book turnover; killed by
  spreads/borrow (§2). Simulate it to *show* the gross/net gap; never treat it as
  deployable alpha.

**Guardrails:** (a) always report gross AND net (≥1 bp/side ≈ −5%/yr on the L/S);
(b) model open-auction spreads >> close; the strongest-signal cohort is the most
costly; (c) treat cross-sectional L/S as a *diagnostic*, not a book; (d) capacity
tiny, price-impact dominates; (e) decay risk — a known, award-winning, retail-driven
effect; the *exploitable* edge is fragile even though the *passive* split persists.

## Implication for hermes-quant design
The analyst should be a **conviction modulator on hold-through-close daily
positions**, NOT a dedicated open/close round-trip sleeve. It measures each name's
trailing overnight-vs-intraday spread and nudges the existing daily long thesis
(a name that earns its return overnight is a *better* hold-through-close candidate;
flat-by-close would forfeit it). Zero added turnover. The dedicated L/S sleeve is a
"reopen only if a fat net edge ever appears" deferral.

## Sources
- HRD (2024) *JOIM* 22(2) — elmwealth.com/night-moves-overnight-drift ; /night-shift
- Boyarchenko, Larsen & Whelan (2023) *RFS* — newyorkfed.org SR917
- Bogousslavsky (2021); Lou, Polk & Skouras (2019) *JFE*; Lachance (2015, 2021)
- Cost-skeptic: alphaarchitect.com/trading-costs-wipe-out-the-overnight-return-anomaly ; stoxx.com (2024)
- Knuteson "toxic factor" debate: Bloomberg / Aaron Brown (Mar 2025)
