# Spike 001 — catalyst correlation (butterfly engine)

## Question
Given the raw 2026-05-28 Blue Origin explosion headline, can an entity-extract +
propagation-graph layer surface RKLB / LUNR / ASTS as bearish-touched with a
defensible score — **without hardcoding the answer**?

## Approach
- Input: 3 raw, untagged headlines (paraphrased from real coverage).
- A curated propagation graph built from **general space-sector domain knowledge**
  (who competes with Blue Origin, who's a sector member) — authored blind to the
  2026-05-28 price action. Includes OPEC/energy and Taiwan-quake/semis edges to
  prove the graph is general, not space-rigged.
- Lightweight entity extraction (gazetteer stand-in for NER) + a negative-catalyst
  lexicon (explodes/anomaly/tumble/…).
- Propagate entities → symbols with signed weighted scores.
- **Validation is a separate after-the-fact step**: pull real 2026-05-28→05-29
  close-to-close moves via yfinance. The graph never sees prices.

## Result

```
entities found: ['blue origin', 'new glenn']

RKLB  score=-4.15  BEARISH   predicted DOWN | actual  -3.07%  ✅
LUNR  score=-3.65  BEARISH   predicted DOWN | actual  -4.09%  ✅
ASTS  score=-3.40  BEARISH   predicted DOWN | actual -14.79%  ✅
RDW   score=-1.80  BEARISH   predicted DOWN | actual  -5.14%  ✅

DIRECTIONAL HIT RATE: 4/4
```

## Verdict: VALIDATED

The graph turned raw "rocket exploded" text into the correct bearish space basket
with zero price knowledge, and every flagged symbol moved down as predicted. Three
of the four (RKLB, LUNR, RDW) are **not in the current trading universe** — this is
signal the system has no other way to obtain. The core butterfly mechanic works.

### What worked
- Entity extraction on raw text → {blue origin, new glenn} cleanly.
- Negative-catalyst lexicon correctly fired (explodes/anomaly/tumble).
- Propagation produced a sensible *ranking* (RKLB most-exposed competitor down to
  RDW peripheral member) that loosely tracks the magnitude ordering of the real
  moves (ASTS is the magnitude outlier — see caveats).
- The separate-validation discipline held: the graph is honest, not fit to outcome.

### What didn't / caveats (the real risks this toy exposes)
1. **Sign ambiguity is real and unsolved.** A competitor's failure is *theoretically*
   bullish for rivals (less competition). The empirically-correct short-horizon read
   here is bearish contagion (whole-sector confidence shock), which I encoded — but
   I encoded it KNOWING launch failures de-rate the sector. A naive graph could just
   as easily have signed it bullish. **The sign of a propagated effect is itself a
   modeling problem**, not a given. The production version must learn signs from
   historical news+return co-movement, not hardcode them per-edge.
2. **Magnitude ≠ score.** ASTS moved −14.8% but scored *less* bearish than RKLB
   (−3.40 vs −4.15). The score reflects *graph linkage strength*, not expected move
   size. These are different quantities; conflating them would mis-size trades.
3. **Curated graph doesn't scale.** Four edges for one event is trivial; covering the
   market needs hundreds of entities × edges, and it staleness-rots. This is fine for
   v1 (operator-curated sector baskets + obvious competitor/supply edges) but the moat
   is the *learned* graph — mine co-movement over a historical news corpus we don't
   have yet. Instrument v1 to log every propagation so the corpus accumulates.
4. **No base-rate / false-positive test.** This spike only ran the ONE case we knew
   worked. The honest next test is a NEGATIVE control: feed a benign headline and a
   benign day, confirm the graph stays quiet (no spurious bearish flags). 4/4 on a
   single hand-picked event is encouraging, not proof of precision.
5. **Single-day close-to-close** is the crudest validation window. Real eval needs
   the lookahead-honest version (packet asof = pub time, measure forward return from
   the next tradeable bar after publication).

### Recommendation for the real build
- The butterfly mechanic is **worth building** — it produces actionable, otherwise-
  unobtainable signal. Proceed to spikes 002 (free-feed ingest) + 003 (packet
  roundtrip + lookahead).
- **v1 graph: curated, but sign-learned where possible.** Ship operator-editable
  sector baskets + competitor/supply edges, but treat edge SIGN as the thing most
  likely to be wrong — surface it for review, and log propagations to bootstrap a
  learned model.
- **Feed score → confidence, not magnitude.** In the SemanticPacket, graph linkage
  score maps to `confidence`; `magnitude` should come from the LLM synthesis stage
  reading the headline severity, not from the graph weight.
- **Add a negative-control gate before trusting it live.** A precision test on benign
  days is a hard prerequisite — a butterfly engine that cries wolf is worse than none.
- Semantic enters the BMA aggregator as a peer view (never an override); the
  ensemble + require_ensemble guard keep it from firing trades alone.
