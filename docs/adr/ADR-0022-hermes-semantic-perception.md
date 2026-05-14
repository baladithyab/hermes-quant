# ADR-0022: Hermes semantic perception layer

- **Status:** Accepted
- **Date:** 2026-05-13
- **Related:** ADR-0002 analyst protocol, ADR-0012 news/LLM analyst, ADR-0021 PDR recipe runtime

## Context

The original charter centered the Perceive layer on quantitative analysts:
ClassicalTA, MicrostructureLite, Kronos, and later options/news modules. That is
necessary but incomplete. Hermes itself is a strong semantic analyzer: it can
read filings, research notes, news, operator memos, prior chat threads, social
summaries, and human-authored market theses. The platform should be able to use
that semantic context as another analyst voice.

The risk is that live LLM calls inside a trading tick would break three
hermes-quant invariants:

1. **Silence by default** — an unavailable LLM must not create a stale or
   hallucinated action.
2. **Hard rules over learned policy** — semantic analysis cannot bypass the
   deterministic risk gate.
3. **Reproducibility** — every signal must be replayable from disk. A hidden
   call to a changing model/provider is not replayable.

## Decision

Add a Hermes-native semantic perception contract based on **semantic packets**.
A semantic packet is a precomputed, signed/hashed input artifact written to disk
or injected into `MarketContext.extras`. A `HermesSemanticAnalyst` consumes those
packets and emits a normal `AnalystView`.

The analyst is deliberately **packet-driven**, not network-driven:

- It does not call Hermes, OpenRouter, or web APIs inside `analyze()`.
- It reads packet fields such as stance, confidence, horizon, summary, source
  refs, model id, and packet hash.
- It emits zero-confidence abstain when no fresh relevant packet exists.
- It includes packet provenance in `AnalystView.metadata` so the signal bus and
  backtests can replay exactly what the semantic analyst saw.

Hermes can still be the semantic engine, but it acts upstream of the trading
tick by producing packets through tools, cron jobs, research tasks, or explicit
operator actions.

## Packet schema

Minimum packet fields:

```json
{
  "schema_version": 1,
  "asset": "BTC/USDT",
  "asof": "2026-05-13T00:00:00Z",
  "horizon": "1h",
  "stance": "bullish | bearish | neutral",
  "confidence": 0.0,
  "magnitude": 0.0,
  "summary": "short human-readable thesis",
  "sources": [{"type": "url|note|session|filing", "ref": "..."}],
  "model": "hermes:<provider/model or human>",
  "packet_hash": "sha256..."
}
```

The hash is computed over canonical JSON excluding `packet_hash` itself. The
hash is stored in the emitted view metadata.

## Freshness and safety

The semantic analyst accepts only packets that:

- match the current asset,
- have an `asof` at or before `MarketContext.asof`,
- are not older than `max_age_minutes`,
- have a compatible horizon,
- have confidence within `[0, 1]` and finite magnitude,
- have a packet hash that matches the payload when present.

If any check fails, the analyst abstains with confidence `0.0`.

## Consequences

Positive:

- Hermes becomes a first-class semantic perception layer without compromising
  replayability.
- Semantic analysis is inspectable and auditable as a normal analyst component.
- Backtests can include semantic packets by replaying the packet archive.
- Future packet generators can be cron jobs, manual research tasks, or model
  committees without changing the analyst protocol.

Negative / deferred:

- The first implementation does not fetch news or call LLMs by itself.
- Packet generation workflows still need separate operators/cron jobs.
- Calibration for semantic confidence must be learned from outcomes over time;
  cold-start semantic confidence is shrinked like other analysts.

## Implementation notes

- Add `hermes_quant/semantic.py` for packet validation and hashing.
- Add `hermes_quant/analysts/semantic.py` implementing `HermesSemanticAnalyst`.
- Extend PDR recipes with optional `semantic` analyst entries.
- Keep tools read-only; any packet writer must be CLI/cron/operator gated.
