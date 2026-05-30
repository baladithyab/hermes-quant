# ADR-0076: Social-arbitrage integration — consumer-trend entity class, sized fusion, and a profitability-verification loop

**Status:** Accepted
**Date:** 2026-05-30
**Wave:** E (signal-surface expansion)
**Supersedes:** nothing
**Amends:** [ADR-0074](ADR-0074-catalyst-sense-semantic-fusion.md) (adds a consumer-trend entity class, a confidence-sizing mechanism, social producers, and a live profitability loop to Catalyst Sense)
**Cites:** `hermes_quant/catalyst/propagation.py` (`_BUILTIN_GRAPH`, `log_propagations`), `hermes_quant/catalyst/classify.py` (consumer-trend lexicon), `hermes_quant/catalyst/synthesize.py` (`CONSUMER_TREND_CONFIDENCE_HAIRCUT`), `hermes_quant/catalyst/social.py` (Reddit + Google Trends producers), `hermes_quant/catalyst/profitability.py` (edge-verification loop), [ADR-0004](ADR-0004-risk-gate.md), [ADR-0075](ADR-0075-catalyst-driven-universe-onboarding.md) (the coverage gap this surfaces)
**Eval evidence:** `ops/scripts/quant-catalyst-socialarb-{labels,eval}.py` — D74.7 gate PASS at exactly 0.60 hit-rate on n=5 (knife-edge).

---

## Context

The operator's prompt: *"integrate social arbitrage into our trading system so that we properly size but also benefit from the profitability of the strategy ... in addition to the other facets that we analyze."* The reference case is Chris Camillo's audited ~77%/yr "social arbitrage" — spotting consumer/cultural trends (Crocs, Celsius, the pandemic bike shortage) before they hit financials.

Social arbitrage decomposes into three capabilities: **detect a narrative**, **map it to a ticker**, **size and express the bet**. The first two are exactly Catalyst Sense (ADR-0074) — its butterfly graph already maps entities → symbols. The missing pieces are a **consumer-trend entity class** (brands/products, not just macro events), a **principled way to size a signal whose evidence is weak**, and a way to **verify the strategy actually pays** rather than assuming it.

A measure-first Phase-0 eval ran the 5 documented Camillo cases (Celsius/Crocs/Dorel/Tapestry/Newell) with **real yfinance forward returns** through the existing `catalyst.eval.eval_gate`:

| Axis | Result |
|---|---|
| Negative control | PASS — 0 spurious packets |
| Edge-sign consistency | PASS — 6/6 |
| Directional precision | **PASS at exactly 0.60** (3/5; **TPR −25% and NWL −35% were false positives**) |

**This is a knife-edge pass on n=5.** The research framed all 5 as wins; disciplined entry dates + fixed forward windows show 2 would have lost. The honest reading: enough evidence to *integrate the plumbing and give the signal a capped seat at the table*, **not** enough to grant it full weight. Every decision below follows from that constraint.

---

## Decision

Integrate consumer-trend social-arbitrage into the live Catalyst Sense pipeline as a **deliberately weak, profitability-gated** peer signal. Four decisions extend ADR-0074.

### D76.1 Consumer-trend entity class — brands/products → the public maker

A new edge class in the propagation graph: `celsius energy → CELH`, `crocs → CROX`, `dorel bicycle → DIIBF`, `coach handbag → TPR`, `elmer glue → NWL`, relation tag `brand_self`. `effect_sign = -1` (per ADR-0074 D74.2 semantics): a **negative** brand event (recall/scandal) is bearish for the maker, and because a positive catalyst flips the sign, a **viral/positive trend is bullish** — the social-arb thesis. Promoted into `_BUILTIN_GRAPH`/`_BUILTIN_ALIASES` (the live source of truth — there is no deployed YAML, so seed-YAML-only edges were dead). **ENTITY aliases only, never person-aliases** (the person-alias-contamination lesson from the 8-sector expansion: a founder spans sectors and creates false correlations).

Consumer-trend catalyst vocabulary was added to `classify.py` (`viral`, `craze`, `sells out`, `frenzy`, `shortage`, `skyrocket`, ...). Without it the social signal *itself* is invisible — the base lexicon only fired on incidental price-verbs.

### D76.2 "Properly size" = a confidence haircut keyed on the weak-eval relation

This is the core sizing decision. A packet whose propagation contributions are **all** `brand_self` has its confidence multiplied by `CONSUMER_TREND_CONFIDENCE_HAIRCUT = 0.5` at synthesis time. Live-verified: "Celsius goes viral" → CELH at conf **0.45** (0.90 × 0.5), while a Blue Origin space-sector packet keeps conf **0.97** (haircut 1.0). The weak-eval social signal enters BMA as a deliberately weak **peer view**.

This composes with the existing controls — it does not replace them:
- BMA fusion (D74.1): semantic is a peer, never an override; disagreement *reduces* aggregate confidence.
- `require_ensemble` / `n_distinct_analysts >= 2`: **semantic alone cannot fire** — it needs a numerical corroborator.
- The haircut is an *additional* cap on top of the analyst's own `confidence_shrink`.

Live `recommend(CELH)` with a bullish consumer packet: semantic dir=+1 conf=0.25, Kronos disagreed, aggregate went dir=−1, `n_contributing=1` → **did not fire**. The weak signal correctly could not override price. That is the design working: *benefit from it, but don't let it run the show.*

The haircut is a single tunable constant, raised toward 1.0 **only** when D76.4 proves the class on live data. It is keyed on the contribution `relation`, so it survives graph edits.

### D76.3 Social producers — Reddit + Google Trends, mirroring the GN-RSS pattern

`hermes_quant/catalyst/social.py`: stdlib-only producers (Reddit public `.json` listings/search; Google Trends daily-trends JSON with the `)]}'` XSSI guard stripped), injectable fetcher, silence-by-default, emitting the same `CatalystItem` shape so they flow through classify → propagate → synthesize identically to news. `published_at` = post/trend observation time (the D74.4 fidelity anchor). The deployed ingester also gained four consumer GN-RSS queries (Celsius/Crocs/Coach/generic-viral), which already produce live consumer packets.

**Known limitation:** Reddit 403-blocks datacenter IPs; production needs a script-type OAuth app or residential egress. The producer silences correctly (0 items, no crash) — GN-RSS consumer queries carry v1 until OAuth is wired.

### D76.4 "Benefit from profitability" = VERIFY on live returns, don't assume

`hermes_quant/catalyst/profitability.py` + `ops/scripts/quant-catalyst-profitability.py`: joins the append-only propagation log (now persisted per-item with `asof` by the ingester — a gap fixed here) against realized yfinance forward returns, **grouped by relation class**, and emits a per-class verdict:
- `PROFITABLE` (n ≥ `MIN_SAMPLE`=20, hit-rate ≥ `MIN_HIT_RATE`=0.6, mean signed return > 0) → consider **raising** the D76.2 haircut toward 1.0.
- `UNPROFITABLE_CONSIDER_PRUNE` → keep the haircut low or prune the edges.
- `INSUFFICIENT_SAMPLE` → hold, accumulate more.

This is the loop that turns *"we think it has edge"* into *"the live data confirms it."* The `brand_self` verdict is the data-driven gate on whether consumer-trend earns more weight. The forward return is measured from the **next bar after** the propagation's `asof` (lookahead-honest, per D74.4); the graph never sees returns.

---

## Consequences

**Positive:**
- Social arbitrage is now a live *facet* of the analysis, fused with the numerical analysts through BMA, sized to its (weak) proven edge, with a loop that will tell us — from live returns — whether to trust it more or cut it.
- Reuses the entire Catalyst Sense pipeline; the only new abstractions are a relation-keyed haircut and a log-vs-returns join.
- The learned-graph corpus (ADR-0074 D74.2) now actually accumulates — the ingester persists every propagation with `asof`.

**Negative / risks:**
- **The eval is n=5 at exactly 0.60.** The integration deliberately does NOT raise trust; it gives the signal a capped seat and builds the machinery to earn more. The haircut stays 0.5 until D76.4 clears MIN_SAMPLE on live data.
- **Coverage gap (surfaced, not hidden):** 4/5 consumer targets (CELH/CROX/DIIBF/NWL) are NOT in the Alpaca tradeable universe — only TPR is. DIIBF (Dorel, OTC) never will be. Those packets are *perceived but un-actable* until catalyst-driven onboarding (ADR-0075) admits strong-catalyst names. Edges KEPT (knowledge), gap made visible by `coverage_against_universe`.
- Reddit producer is best-effort (403 on datacenter IPs) until OAuth.

**Out of scope:** raising the haircut (gated on D76.4); catalyst-driven universe onboarding (ADR-0075); the learned propagation graph; an LLM classify tier for consumer trends.

---

## Rollout

1. ✅ Consumer-trend edges + aliases + lexicon in the live graph (D76.1).
2. ✅ Confidence haircut at synthesis time, audit-tagged in packet metadata (D76.2).
3. ✅ Social producers + consumer GN-RSS queries in the deployed ingester; per-item propagation-log persistence (D76.3).
4. ✅ Profitability loop + ops runner (D76.4).
5. ⏳ Schedule the profitability cron (weekly, `no_agent`, silent until a class clears MIN_SAMPLE).
6. ⏳ Raise the haircut **only** when `brand_self` reaches `PROFITABLE` on accumulated live returns.
7. ⏳ ADR-0075 onboarding so consumer names become tradeable.

`HERMES_QUANT_SEMANTIC_ENABLED=1` is set; packets flow and are sized. The weak class earns more weight only by proving it on live returns.
