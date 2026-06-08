# Designing a Small, Defined-Risk Long-Horizon Convex-Bet Sleeve (LEAPS)

> Research note grounding the deferred-decision ADR for a speculative-convexity
> sleeve. Compiled 2026-06-08 via Tavily + Exa. Operator is risk-averse on
> decay-to-zero instruments → strict sizing discipline required.

**Scope:** A *small* sleeve expressing high-upside speculative theses (pre-revenue
rare-earth, clean-tech, clinical-stage biotech micro-caps) as long-dated calls
(LEAPS, 12–24mo) instead of stock — capping downside at premium while keeping
convex upside.

**Headline (skeptical) finding:** For genuinely high-IV, sub-$1B micro-caps, **a
small stock position usually BEATS a LEAPS** — three compounding problems: (1)
expensive premium / vega crush, (2) wide-to-untradeable bid-ask spreads, (3) thin
or nonexistent long-dated open interest. LEAPS earn their place on *liquid*
high-conviction names, not on the illiquid lottery tickets influencers tout.

## 1. Sizing convex / lottery-like bets
Full Kelly is the wrong tool for positive-skew bets you can't reliably edge-estimate
(can produce >50% DD). Practitioners run **fractional Kelly (¼–⅓)**; at 30% of Kelly
the odds of an 80% DD drop ~1-in-5 → ~1-in-213 while keeping ~51% of growth. With
real risk-of-ruin, optimal fraction collapses further.

**Spitznagel/Universa convexity logic transfers:** payoff *shape* > average return;
extreme convexity means a **tiny allocation moves the needle**. Universa sizes tail
insurance ~3% of NAV because a ~10x payoff at 3% adds ~30%, and ≥10% *hurts* ("the
dose differentiates poison from remedy"). Mirror for *upside* convexity: keep each
bet small, expect most to expire worthless, rely on a few multibaggers.

**Practical rule:** size by **premium-at-risk** (LEAPS → zero), not notional.
Risk-per-name **~0.25–0.5% of NAV**.

## 2. LEAPS mechanics: theta, vega, ITM vs OTM, rolling
LEAPS behave like **leveraged stock**, not gamma trades. ~18mo call bleeds
~$0.01–0.04/day theta vs $0.15–0.50 for ATM weekly; decay stays gentle until final
~90–120 days, then accelerates.
- **Deep ITM (0.75–0.85 delta):** mostly intrinsic → low theta, **low vega**, stable
  delta, stock-like tracking; less leverage. "Goldilocks" equity-replacement zone;
  keep extrinsic <10–15% of premium.
- **OTM (sub-0.30 delta):** cheapest, most convex, but mostly extrinsic → painful
  18mo theta, dominant vega, delta collapses on pullbacks. "Pure speculation… a
  low-probability bet."

A *convex* thesis wants more OTM — exactly where vega/theta hurt most. **Roll at
~4–6 months to expiry** (before the theta cliff), up-and-out into 0.70–0.85 delta.

## 3. The skeptical core: high-IV names — LEAPS or small stock?
**Buy options when IV is LOW; sell when high.** High-IV premiums are inflated and
mean-revert → buying a LEAPS at elevated IV means overpaying for extrinsic value
that compresses (IV crush eats 30–40% of expected gain). Speculative micro-caps live
at *structurally* high IV — there is no "wait for IV<40" window; you're perpetually
the overpaying buyer with large vega drag.

**Verdict:** For high-IV illiquid micro-caps, **a small stock position generally
beats a LEAPS.** The small stake already has bounded downside (lose only what you
put in), never expires, no theta, no IV crush, trivial bid-ask. The LEAPS's only
structural edge — capped downside via leverage — is redundant when the stock stake
is already sized as total-loss-tolerable. **LEAPS earn their keep on liquid,
high-conviction names with normalized IV.**

## 4. Liquidity reality for micro-cap LEAPS
Often **not tradeable.** LEAPS are the least-liquid chain corner (few trade 2yr out;
MMs widen spreads — hard to hedge far-dated). Sub-$1B micro-caps compound this: OTM
long-dated strikes routinely show **zero OI and spreads >100% of mid** (real example:
bid $0.11 / ask $2.57, OI 0). Screens: avoid if spread >10–15% of price, OI <100 at
your strike, underlying <500K shares/day. Many micro-caps **list no LEAPS at all.**
Round-trip spread can exceed the entire convex edge.

## 5. Portfolio-level caps
- **Aggregate speculative-convexity sleeve: ≤3–5% of NAV** (Universa ~3% logic).
- **Per name: ~0.25–0.5% premium-at-risk; hard cap ~1%.**
- **Diversify ≥8–15 independent theses.**
- **Treat 100% loss per position as base case;** total sleeve loss must not impair
  the core book.

## Bottom line / ADR posture
LEAPS convexity is real **on liquid underlyings at normalized IV.** On the high-IV,
illiquid micro-caps influencers tout, a **small total-loss-tolerable STOCK position
usually dominates** (same capped practical downside, no theta/IV-crush/untradeable
spreads). → The deferred ADR should frame the sleeve as: **small fractional-Kelly
defined-risk bets, instrument chosen per-name (stock for high-IV illiquid; LEAPS
only when liquid + IV normalized + OI/spread screens pass), ≤3–5% NAV aggregate,
reopen-conditioned on the existing PMCC shadow tracker producing a measurable edge.**

## Sources
- Kelly/fractional: Wikipedia Kelly criterion; nickyoder.com/kelly-criterion; Gehm (1983)
- Universa/Spitznagel: Safe Haven series (notion.moontowermeta.com)
- LEAPS mechanics: theoptionpremium.com deep-ITM delta playbook; TradeAlgo; Ainvest
- IV crush: Investopedia implied-volatility; Ainvest LEAPS guide
- Liquidity: TradingBlock options-liquidity; WheelMetrics bid-ask spreads
