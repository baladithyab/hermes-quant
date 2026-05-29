# Spike 003 — packet roundtrip + lookahead replay

## Question
Can we synthesize a catalyst into a `SemanticPacket` (asof = publication time),
feed it through the REAL `validate_semantic_packet` + `HermesSemanticAnalyst`, and
prove the lookahead gate rejects pre-publication leaks and accepts post-publication?

## Approach
Used the actual `hermes_quant.semantic` primitives (not a mock). Built a bearish
RKLB packet for the Blue Origin catalyst, asof = 2026-05-28T22:14 UTC (publication).
Replayed `validate_semantic_packet` at four decision_times + a tamper test, then
imported the real analyst.

## Result

```
TEST 1  pre-publication (same-day 20:00 UTC)  -> future_packet         ✅ PASS (reject)
TEST 2  post-publication (next session 13:30) -> ok                    ✅ PASS (accept)
TEST 3  stale (3 days later)                  -> stale_packet          ✅ PASS (reject)
TEST 4  tampered stance (no rehash)           -> packet_hash_mismatch  ✅ PASS (reject)
TEST 5  real HermesSemanticAnalyst import     -> wireable              ✅ PASS

RESULT: ALL PASS ✅
```

## Verdict: VALIDATED

The fidelity contract is proven end-to-end on real scaffolding. A packet timestamped
at publication time:
- **cannot leak** into a backtest bar that predates the headline (`future_packet`),
- **is usable** at the next decision after publication (within freshness),
- **goes stale** correctly past the window,
- **is tamper-evident** via content hash,
- and the real `HermesSemanticAnalyst` exists and is wireable into the BMA path.

This means the single hardest news-trading fidelity problem (lookahead) is already
solved on the consumer side — the producer just has to set `asof` honestly to the
publication time (which spike 002 confirmed GN RSS provides via pubDate).

### What worked
- All four gate behaviors are correct out of the box; no new fidelity code needed.
- Content-hash tamper detection works — packets are immutable & replayable.
- The class name is `HermesSemanticAnalyst` (not `SemanticAnalyst`) — note for wiring.

### What didn't / caveats
- This proves the GATE, not the SYNTHESIS QUALITY. Whether the stance/confidence/
  magnitude the LLM assigns are *good* is a separate eval (needs the negative-control
  + forward-return backtest from spike 001's caveats).
- `max_age_minutes=24*60` (24h) is a guess; the right freshness window is horizon-
  dependent (a 1d-horizon packet shouldn't drive a trade 20h later) and needs tuning.
- GN pubDate-as-asof means freshness is measured from Google's index time, slightly
  conservative — acceptable.

### Recommendation for the real build
- Producer MUST set `asof` = publication timestamp (GN pubDate), never wall-clock-now.
  This is the one rule that keeps backtests honest.
- Tune `max_age_minutes` per horizon, not a global constant.
- Wire `HermesSemanticAnalyst` into `_build_default_analysts()` behind
  `HERMES_QUANT_SEMANTIC_ENABLED=1` (ADR-0064 gate pattern); it no-ops to neutral
  when no packet present, so safe to enable before full coverage.
