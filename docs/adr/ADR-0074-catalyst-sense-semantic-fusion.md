# ADR-0074: Catalyst Sense — semantic-numerical fusion via parallel catalyst detection

**Status:** Accepted (2026-05-29), implemented
**Date:** 2026-05-29
**Wave:** E (signal-surface expansion)
**Supersedes:** nothing
**Amends:** [ADR-0018](ADR-0018-analyst-loadout.md) (adds SemanticAnalyst to the loadout), [ADR-0073](ADR-0073-event-catalyst-awareness.md) (this is its Phase 2, expanded with a fusion + butterfly-correlation design)
**Cites:** `hermes_quant/semantic.py` (SemanticPacket + validate_semantic_packet), `hermes_quant/analysts/semantic.py` (HermesSemanticAnalyst), `hermes_quant/aggregators/bma.py` (fusion point), [ADR-0004](ADR-0004-deterministic-risk-gate.md), [ADR-0068](ADR-0068-decision-time-vs-bar-time-honesty.md) (decision-time honesty — applies to headlines)
**Spike evidence:** `spikes/001-catalyst-correlation`, `spikes/002-freefeed-ingest`, `spikes/003-packet-roundtrip` (all VALIDATED 2026-05-29)

---

## Context

The system is blind to event/catalyst information (ADR-0073). The operator's vision (2026-05-29): a catalyst-detection pipeline running **parallel to the universe scan**, pulling news/social feeds (worldmonitor.app style), feeding **probabilistic semantic** signal alongside the existing **deterministic numerical** analysis — *especially world events, because the butterfly effect is real* (a Florida rocket explosion moves space stocks; a Taiwan quake moves semis; an OPEC headline moves energy).

Three spikes de-risked the idea before committing to a build:

- **Spike 001 (butterfly correlation) — VALIDATED, 4/4.** Raw untagged Blue Origin headlines → entity-extract → curated propagation graph → surfaced RKLB/LUNR/ASTS/RDW as bearish-touched, with **zero price knowledge**. All four moved down as predicted (ASTS −14.8%). 3 of 4 aren't in the trading universe — otherwise-unobtainable signal.
- **Spike 002 (free-feed ingest) — VALIDATED.** Google News RSS, stdlib-only, zero paid API: 364 deduped items across 4 queries, avg 0.77s latency, 83 Blue Origin items. **The no-X-API bet holds** — GN RSS covers social-surfaced stories.
- **Spike 003 (packet roundtrip + lookahead) — VALIDATED, all pass.** The real `validate_semantic_packet` correctly rejects pre-publication leaks (`future_packet`), accepts post-publication, expires stale packets, and is tamper-evident. **The hardest fidelity problem is already solved on the consumer side.**

Key finding: the consumer side (SemanticPacket, lookahead gate, SemanticAnalyst) is **already built and correct**. The gap is purely the **producer** (an ingester that writes packets) + the **butterfly correlation layer** (entity → sector → symbol).

---

## Decision

Build **Catalyst Sense**: a standalone pipeline running parallel to the universe scan, emitting `SemanticPacket`s the existing `HermesSemanticAnalyst` consumes. Five stages, fusing into the BMA aggregator.

```
[1 INGEST] → [2 CLASSIFY] → [3 CORRELATE] → [4 SYNTHESIZE] → [5 EMIT]
GN RSS +      keyword →      entity→sector    per-symbol LLM   SemanticPacket
curated RSS   local NER →    →symbol          stance+conf+mag  (asof=PUB TIME)
              LLM(survivors) (butterfly)      +citations       → packet store
                                                               → MarketContext.extras
                                                                      │
       DETERMINISTIC NUMERICAL ──► BMA AGGREGATOR ◄── PROBABILISTIC SEMANTIC
       (ClassicalTA+Micro+Kronos)       │            (SemanticAnalyst)
                                         ▼
                          risk gate (0004) + caps (0071) + open-guard (0072)
```

### D74.1 Fusion happens IN the BMA aggregator — semantic is a peer view, never an override

Semantic enters as **one more `AnalystView`** alongside the numerical analysts. The BMA aggregator weights it by track-record-calibrated reliability. Consequences (all desirable):
- A high-confidence semantic stance that **disagrees** with the numerical analysts **reduces** aggregate confidence rather than overriding — the system gets appropriately uncertain when "chart says up, news says down."
- The `require_ensemble` / `n_distinct_analysts >= 2` degeneracy guard (2026-05-26 BMA fix) still applies: **semantic alone cannot fire a trade** — it needs a numerical corroborator, or it's a single view and gets silenced.
- Semantic reliability is **learned**: if the news analyst is noise, BMA down-weights it over time. Probabilistic signal earns its weight; it isn't granted it.

This is the cleanest "deterministic + probabilistic" fusion — let the Bayesian model-averaging layer do exactly what it's designed for.

### D74.2 The butterfly correlation layer (stage 3) — curated-first, sign-aware, logged-for-learning

Three sub-layers (spike 001 validated the mechanic):
1. **Entity extraction** — NER over headline/body → {companies, sectors, commodities, countries, people}.
2. **Propagation graph** — signed, weighted edges encoding how a catalyst on an entity propagates to symbols: `BlueOrigin --competitor--> {RKLB,LUNR,ASTS}`, `OPEC --commodity--> {XOM,CVX}`, `TaiwanQuake --supply_chain--> {TSM,NVDA}`. **v1 is operator-curated YAML** (sector baskets + competitor/supply edges). Every propagation is logged so a **learned** graph (co-movement mining over historical news+returns) can replace it later — that's the moat.
3. **Symbol resolution + onboarding** — map propagated entities to tradable symbols; if a touched symbol isn't in the liquidity universe (LUNR/RKLB), **onboard via the catalyst path** (ADR-0073 Phase 1).

**Edge SIGN is the highest-risk modeling choice** (spike 001 caveat #1): a competitor's failure is theoretically bullish for rivals, but a catastrophic safety event is bearish sector-contagion. v1 hardcodes the sign per-edge but MUST surface it for operator review and log it for the learned model. Do not treat propagation signs as ground truth.

### D74.3 Score → confidence, severity → magnitude (do not conflate)

Spike 001 caveat #2: graph linkage score ≠ expected move size (ASTS moved −14.8% but scored less bearish than RKLB). Therefore in the emitted packet:
- **`confidence`** ← butterfly graph linkage score (how sure we are the symbol is touched).
- **`magnitude`** ← LLM synthesis stage's read of headline *severity* (how big the move).
These are different quantities; conflating them mis-sizes trades.

### D74.4 Producer fidelity — asof = publication time, always

Spike 003: the producer MUST set packet `asof` = the headline's publication timestamp (Google News `pubDate`), never wall-clock-now. This is the single rule that keeps backtests honest — the lookahead gate enforces the rest. `max_age_minutes` must be tuned **per horizon**, not a global 24h constant.

### D74.5 Ingest — free-feed backbone, stdlib, no paid API for v1

Spike 002: query-driven Google News RSS + a curated direct-RSS set (reuse worldmonitor's feed catalog as a seed — feed URLs are facts; reimplement the fetcher, do NOT vendor AGPL code into the private repo). Stdlib `urllib` + `xml.etree`, no feedparser. Per-query timeout + cache + concurrent fetch + graceful per-feed degradation. **X/Twitter direct is deferred** — GN RSS covers v1; revisit `xurl` only if fast-breaking social-first coverage proves to lag.

### D74.6 Classify — cheap-to-expensive cascade

Mirror worldmonitor: (1) keyword/regex severity+category match (instant, free), (2) local NER/sentiment (small model, free), (3) LLM only on survivors (local-first, OpenRouter mid-tier for per-symbol stance synthesis where quality matters). Cache aggressively.

### D74.7 Negative-control gate before live (hard prerequisite)

Spike 001 caveat #4: 4/4 on one hand-picked event is encouraging, not proof of precision. Before Catalyst Sense influences any live decision, it MUST pass a **negative-control eval**: benign headlines on benign days produce no spurious bearish/bullish flags, AND a forward-return backtest (lookahead-honest, from the next tradeable bar after publication) shows the semantic stance has positive information value. **A butterfly engine that cries wolf is worse than none.** Default OFF (`HERMES_QUANT_SEMANTIC_ENABLED=0`) until this passes.

### D74.8 Cadence

Ingester runs on a 30–60 min in-market cron parallel to the autonomous-tick (catalysts are intraday; daily is too coarse to "react to the explosion"). Packets timestamped at true pub-time so the lookahead gate handles freshness regardless of when the ingester runs.

---

## Consequences

**Positive:**
- The system gains catalyst awareness: sees movers (onboarding), understands why (semantic stance), reacts intraday — all proven feasible by spikes.
- Reuses fully-built, correct scaffolding (packet schema, lookahead gate, analyst, BMA fusion point). The gaps are producers + a graph, not new core abstractions.
- Free-tier ingest, no paid API, sub-second latency.
- Fusion via BMA means semantic can't do harm alone — it's bounded by the ensemble + every existing risk control.

**Negative / risks:**
- **Edge-sign modeling (D74.2)** is genuinely hard and v1-hardcoded; wrong signs produce confident-wrong stances. Mitigated by operator review + the require_ensemble guard + the negative-control gate.
- **Precision unproven at scale** — one validated case ≠ a precise detector. D74.7 is the gate, not optional.
- Curated graph is labor + staleness; the learned version needs a corpus we accumulate over time.
- LLM cost/latency in the classify+synthesize stages; mitigated by the cheap-first cascade + caching.
- GN pubDate lags the wire slightly (conservative for lookahead, but caps our speed-to-signal).

**Out of scope:** real-time/streaming execution; direct X API; structured world-event feeds (USGS/ACLED/GDELT — ADR-0073 Phase 3); the learned propagation graph (future ADR once corpus exists).

---

## Rollout

1. **Negative-control eval harness FIRST** (D74.7) — build the precision/forward-return test before the ingester, so we have the gate ready.
2. **Ingester + classify cascade** (D74.5, D74.6) behind a feature flag, writing to a packet store.
3. **Butterfly graph v1** (D74.2) — curated YAML, sign-reviewed, propagation-logged.
4. **Synthesize + emit** (D74.3, D74.4) — packets with honest asof.
5. **Wire HermesSemanticAnalyst** into `_build_default_analysts()` behind `HERMES_QUANT_SEMANTIC_ENABLED=1` (no-ops to neutral without packets; safe to enable pre-coverage).
6. **Gate live influence on D74.7 passing** — replay the Blue Origin case lookahead-honestly + a benign-day negative control; only then let semantic views carry weight in live decisions.
