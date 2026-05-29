# Catalyst Sense — design proposal + spike plan

**Date:** 2026-05-29
**Status:** Design / pre-spike (feeds ADR-0073 Phase 2 + a new ADR-0074 for the fusion layer)
**Author:** ARIA + Codeseys
**Context doc:** `~/wiki/projects/hermes-quant.md` (catalyst-blindness section), ADR-0073 (event/catalyst awareness)

---

## The vision (Codeseys, 2026-05-29)

> A catalyst-detection pipeline running **in parallel with the universe scan**, pulling X/Twitter, Google News, social + news feeds (worldmonitor.app style), feeding **probabilistic semantic** signal alongside the existing **deterministic numerical** analysis. Fuse both so agents are better informed — *especially* by world events, because the butterfly effect is real: a rocket explosion in Florida moves space stocks; a Taiwan earthquake moves semis; an OPEC headline moves energy.

The thesis: **deterministic numerical (what the price did) + probabilistic semantic (what the world did, and why) > either alone.** The numerical layer is precise but blind to causes; the semantic layer is noisy but sees catalysts before price confirms. Fused, they cover each other's failure modes.

---

## What already exists (the good news)

The hermes-quant codebase already has the **consumer side** of this fully built and correct:

- `hermes_quant/semantic.py` — `SemanticPacket` dataclass (asset, asof, horizon, stance ∈ {bullish/bearish/neutral}, confidence, magnitude, summary, sources, hash). **Immutable, replayable, content-hashed.**
- `validate_semantic_packet()` — **the lookahead gate already works**: rejects `future_packet` (`packet_asof > ctx_asof`) and `stale_packet` (age > max_age_minutes). This is the single hardest fidelity problem in news-trading and it's *already solved* on the consumer side.
- `hermes_quant/analysts/semantic.py` — `SemanticAnalyst` consumes packets from `MarketContext.extras`, emits a normal `AnalystView` into the BMA aggregator. Has a grounding/citation hook. **Just not in the default loadout.**
- `hermes_quant/grounding/current_clear.py` — purge node for stale tool-call messages between analyst stages.

**The entire gap is: (1) a producer that writes packets, and (2) an entity→sector correlation layer that decides WHICH symbols a world event touches.** The "wire SemanticAnalyst into the loadout" step is trivial (one env-gated line, ADR-0064 pattern).

## worldmonitor.app as blueprint (koala73/worldmonitor, AGPL-3.0, 41k★)

Studied via deepwiki. Its architecture maps almost 1:1 onto what we need. **License note: AGPL-3.0 — we REUSE the data-source catalog and reimplement the patterns; we do NOT vendor its code into our (private) repo without taking on AGPL obligations.** Feed lists (URLs) are facts, not copyrightable expression; the collection/classification *code* is AGPL.

What it does that we want:
| worldmonitor component | What it is | Our analog |
|---|---|---|
| 435 curated RSS feeds + Google News RSS searches | Free-tier news backbone, no paid API | Catalyst Sense ingester feed list |
| Google News RSS `gn('site:apnews.com')`, `gn('(OpenAI OR ...) when:2d')` | Query-driven topical pulls; indirectly surfaces social stories | Per-sector + per-watchlist-symbol GN queries |
| Multi-tier fetch (direct → relay → server-aggregate, per-feed timeout + cache) | Resilient collection | Our ingester's fetch layer (cron, not browser) |
| 3-stage classifier: ~120 keyword severity-map → browser NER/sentiment → LLM (Groq Llama-3.1-8B @ temp 0, cached) | Cheap→expensive cascade; LLM overrides keyword if higher confidence | Our classify cascade: regex/keyword → small local model → LLM only on survivors |
| **Focal Point Detector** — entity extraction (countries/companies/orgs) → signal aggregation → cross-reference → score & rank "main characters" | **The butterfly-effect engine** | Our entity→sector→symbol correlation layer (THE hard, novel part) |
| Non-RSS sources: USGS, NASA EONET/FIRMS, ACLED, GDELT, OpenSky, AISstream, Polymarket, FRED | World-event layers beyond headlines | Phase-3 world-event feeds (earthquakes→semis, conflict→energy/defense) |

**Key validation:** X/Twitter is NOT a direct source even for worldmonitor — they lean on Google News RSS which indirectly captures social-surfaced stories. This de-risks our biggest cost/ToS worry: **we likely do NOT need the paid X API for v1.** Google News RSS + curated RSS covers the catalyst-detection job. (We have `xurl` available if we later want direct X — but it's not the critical path.)

---

## Proposed architecture: "Catalyst Sense"

A standalone pipeline running parallel to the universe scan, emitting `SemanticPacket`s the existing analyst consumes. Five stages:

```
                    ┌─────────────────────────────────────────────┐
                    │  CATALYST SENSE  (parallel to universe scan) │
                    └─────────────────────────────────────────────┘

  [1 INGEST]            [2 CLASSIFY]         [3 CORRELATE]        [4 SYNTHESIZE]      [5 EMIT]
  RSS feeds        →    keyword cascade  →   entity→sector→   →   per-symbol LLM  →   SemanticPacket
  Google News RSS       → local NER           symbol mapping       stance+conf+mag     (asof = pub time!)
  (world-event APIs      → LLM (survivors)    (Focal Point)        + citations          → packet store
   in phase 3)          severity + category   = butterfly engine                        → MarketContext.extras

                                                                                            │
                                                                                            ▼
              DETERMINISTIC NUMERICAL                                          PROBABILISTIC SEMANTIC
              ClassicalTA + Microstructure + Kronos  ──►  BMA AGGREGATOR  ◄──  SemanticAnalyst(packet)
                                                              │
                                                              ▼
                                                     deterministic risk gate (ADR-0004)
                                                     + portfolio caps (ADR-0071)
                                                     + open-guard (ADR-0072)
```

### The fusion point is the BMA aggregator — and that's deliberate

We do NOT bolt semantic on as an override or a separate decision authority. It enters as **one more `AnalystView`** alongside the numerical analysts, and the BMA aggregator weighs it by its track-record-calibrated reliability. This means:
- A high-confidence semantic stance that disagrees with the numerical analysts **reduces** aggregate confidence (the system gets appropriately uncertain when "the chart says up but the news says down") rather than blindly following either.
- The `require_ensemble`/`n_distinct_analysts>=2` degeneracy guard (the 2026-05-26 BMA fix) still applies — semantic alone can't fire a trade; it needs a numerical corroborator or it's just one view.
- Semantic reliability is **learned** — if the news analyst is noise, BMA down-weights it over time. Probabilistic signal earns its weight; it isn't granted it.

This is the cleanest possible answer to "combine deterministic + probabilistic": let the existing Bayesian model-averaging layer do exactly what it's designed for, with semantic as a peer input.

### The butterfly engine (stage 3) is the hard, novel part

worldmonitor's Focal Point Detector correlates news→map-signals→countries. **Ours correlates news→sectors→symbols.** This is where the real IP is, and where the spike risk concentrates. Three sub-layers:
1. **Entity extraction** — NER on the headline/body → {companies, sectors, commodities, countries, people}.
2. **Propagation graph** — a curated + learnable edge set: `BlueOrigin --competitor--> {RKLB, LUNR, ASTS}`, `TaiwanEarthquake --supply_chain--> {TSM, NVDA, AMD}`, `OPEC --commodity--> {XOM, CVX, energy_sector}`. This is the butterfly-effect encoding. Start curated (a YAML the operator edits), graduate to learned (co-movement mining over historical news+returns).
3. **Symbol resolution + onboarding** — map the propagated entities to tradable symbols; if a touched symbol isn't in the liquidity universe (LUNR/RKLB), **onboard it via the catalyst path** (ADR-0073 Phase 1).

---

## Spike plan — de-risk before committing to the build

Three spikes, ordered by risk (the one most likely to kill the idea runs first). Throwaway code in `spikes/`.

| # | Spike | Validates (Given/When/Then) | Risk |
|---|-------|------------------------------|------|
| **001** | catalyst-correlation | Given the 2026-05-28 Blue Origin headline, when run through entity-extract + propagation-graph, then RKLB/LUNR/ASTS surface as bearish-touched with a defensible score — WITHOUT hardcoding the answer | **HIGH** — this is the novel part; if entity→symbol correlation is too noisy/too sparse to be useful, the whole idea is weaker |
| **002** | freefeed-ingest | Given Google News RSS + a curated RSS set, when polled for a sector/symbol query, then we get timestamped, deduped, parseable catalyst items at acceptable latency/coverage with zero paid API | **MEDIUM** — validates the no-X-API bet and the feed backbone |
| **003** | packet-roundtrip | Given a classified+correlated catalyst, when synthesized into a SemanticPacket with asof=pub-time and fed through SemanticAnalyst + validate_semantic_packet + a replay at an earlier decision_time, then the lookahead gate correctly REJECTS it (no leak) and ACCEPTS it post-publication | **MEDIUM** — proves the fidelity story end-to-end on real scaffolding (lower risk since the gate already exists, but must prove the producer respects it) |

**Spike 001 is the gate.** If the butterfly correlation can't turn "rocket exploded" into "short the space basket" with a defensible, non-cheating score on the real Blue Origin case, we rethink before building the ingester. Run it first.

---

## Open design decisions (need Codeseys input)

1. **Propagation graph: curated-first or learned-first?** Curated YAML is shippable in days and interpretable but labor-intensive and brittle. Learned (mine news+return co-movement) is the real moat but needs a historical news corpus we don't have yet. **My lean: curated-first for v1 (operator-editable sector baskets + competitor/supply-chain edges), instrument it to log every propagation so we accumulate the corpus to train the learned version.**
2. **LLM tier for synthesis.** worldmonitor uses Groq Llama-3.1-8B @ temp 0. We have OpenRouter rosters + local options. **My lean: local-first (cheap, private) for classification, OpenRouter mid-tier for the per-symbol stance synthesis where quality matters.**
3. **Cadence.** The ingester runs parallel to the universe scan (daily) for v1, but catalysts are intraday. **My lean: ingester on a 30–60 min in-market cron (parallel to autonomous-tick), packets timestamped at true pub-time so the lookahead gate handles freshness; daily is too coarse for "react to the explosion."**
4. **Scope of v1 world-events.** Headlines only (RSS/GN), or include the structured world-event APIs (USGS earthquakes, ACLED conflict, GDELT) in v1? **My lean: headlines-only v1; structured world-event feeds are Phase 3 — they're higher-value butterfly sources but add a lot of surface.**
5. **X/Twitter.** Skip for v1 (Google News RSS covers it indirectly), or wire `xurl` for direct high-signal accounts? **My lean: skip v1, revisit if spike 002 shows GN RSS misses fast-breaking social-first stories.**

---

## Recommendation

Run **spike 001 (catalyst-correlation) now** against the real Blue Origin case — it's the highest-risk, highest-information experiment and needs no infrastructure. If it validates, run 002 + 003, then I write **ADR-0074 (Catalyst Sense — semantic fusion)** as the production design with the spike findings baked in, and we build Phase 1 (ingester + curated propagation + packet emit + analyst activation) as a real PR. If 001 invalidates, we learn the butterfly correlation is harder than it looks and rethink the approach before spending build effort.
