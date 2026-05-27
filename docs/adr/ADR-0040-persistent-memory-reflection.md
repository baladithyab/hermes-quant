# ADR-0040: Persistent Memory & Deferred Reflection Layer

**Status:** Proposed
**Date:** 2026-05-27
**Related:** ADR-0033 (Evidence Store), ADR-0034 (Run Cards), ADR-0037 (LLM Committee)
**Author:** ARIA (deep-work-loop on hermes-quant; research synthesis)

## Context

hermes-quant has zero cross-run learning today. Each committee session starts cold. Past decisions are not stored, not reflected upon, and not injected into future PM prompts.

The 2026-05-27 research scatter identified this as the **single highest-leverage gap** vs. the reference projects:

- **TauricResearch/TradingAgents** has `~/.tradingagents/memory/trading_memory.md` (append-only markdown), pending → resolved lifecycle, alpha-vs-benchmark in reflection prompt, 5-same-ticker + 3-cross-ticker injection.
- **Mai0313/TradingAgents** has BM25-indexed episodic memory per analyst role with analogy-applicability check; structured `Reflector` rubric (1–5 score per factor + outcome quality + lesson category enum).
- **HKUDS/Vibe-Trading** has FTS5 persistent memory at `~/.vibe-trading/memory/`, auto-injected into system prompt every run.
- **virattt/ai-hedge-fund** has per-agent memory of past decisions injected as context.

The Agentic Trading survey (arxiv:2605.19337) identifies the **Oracle Fallacy in episodic memory** — retrieving a past episode whose narrative embeds future-knowledge — as a named, documented failure mode. The fix is to require a `τ_observable` field (real wall-clock timestamp at which outcome became knowable) on every memory entry and block retrieval of episodes where outcome-narrative timestamp postdates decision timestamp.

## Decision

Build a 3-layer memory system at `~/.hermes/quant/memory/`:

### Layer 1: Decision Log (canonical, append-only)

Path: `~/.hermes/quant/memory/decisions.jsonl`

One row per committee decision (whether approved, rejected, or silenced). Schema:

```json
{
  "schema_version": 1,
  "decision_id": "dec_20260527T165047_MRNA_ed1e81",
  "asof_decision": "2026-05-27T16:50:47Z",
  "tau_observable": null,              // filled in by reflector when outcome resolves
  "ticker": "MRNA",
  "asset_class": "equity",
  "rating": "Underweight",             // 5-tier from research_manager
  "direction": -1,
  "confidence": 1.0,
  "target_position_pct": -0.2,
  "thesis_summary": "...",             // one-paragraph summary from PM
  "thesis_evidence_ids": ["ev_..."],   // ADR-0033 evidence store IDs
  "signal_provenance": {...},          // from ADR-0039
  "research_plan_text": "...",         // from research_manager debate output
  "trader_proposal": {...},            // from TraderNode (when ADR-0042 lands)
  "risk_debate_summary": "...",        // from 3-way risk committee (when ADR-0041 lands)
  "state": "pending",                  // pending | resolved | invalidated
  "resolution": null                   // filled when resolved (see Layer 2)
}
```

**Append-only.** A `state: pending` → `state: resolved` transition is recorded as a SEPARATE event, not by mutating the row. The "current state" view is materialized by replaying the log; the canonical record is the event chain.

This is the same pattern as audit_log.jsonl (ADR-0031). Same enforcement: `truncate()`, `update()`, `seek-and-write` all raise `AppendOnlyViolation`.

### Layer 2: Reflection Log (deferred, append-only)

Path: `~/.hermes/quant/memory/reflections.jsonl`

When a position closes (PaperReactor settlement OR a stop-loss hit OR a time-horizon expiry), the daemon:

1. Fetches benchmark price series (SPY for US equities, currency-pair index for FX, BTC/ETH for crypto, defined per-asset-class in `benchmark_map`).
2. Computes `raw_return`, `alpha_return = stock_return - benchmark_return`, `holding_days`, `outcome_quality` (a 1–5 ordinal: -2σ → 1, -1σ → 2, ±0.5σ → 3, +1σ → 4, +2σ → 5 against historical alpha distribution for that ticker).
3. Invokes the **quick-tier LLM** (per ADR-0037 two-tier split) with this prompt template:

```
You are reflecting on a closed trade. The decision log entry follows.
Write exactly 2-4 sentences of plain prose (no bullets, no headers, no markdown).

Cover in order:
1. Was the directional call correct? (cite the alpha figure)
2. Which part of the investment thesis held or failed?
3. One concrete lesson to apply to the next similar analysis.

Be specific and terse. Your output will be stored verbatim in a decision log
and re-read by future analysts, so every word must earn its place.

DECISION:
{decision_log_entry}

OUTCOME:
- raw_return: {raw_return:+.2%}
- alpha_return: {alpha_return:+.2%}
- benchmark: {benchmark}
- holding_days: {holding_days}
- outcome_quality: {outcome_quality}/5
```

Reflection-log row schema:

```json
{
  "schema_version": 1,
  "reflection_id": "ref_20260612T140000_MRNA_ed1e81",
  "decision_id": "dec_20260527T165047_MRNA_ed1e81",
  "asof_resolution": "2026-06-12T14:00:00Z",
  "tau_observable": "2026-06-12T20:00:00Z",   // wall-clock when outcome became knowable (post-close adj-data publication)
  "ticker": "MRNA",
  "raw_return": 0.0421,
  "alpha_return": 0.0119,
  "benchmark": "SPY",
  "holding_days": 16,
  "outcome_quality": 4,
  "reflection_text": "Direction call (-1 short) was wrong; +1.2% alpha against the bear thesis. Earnings beat the conservative consensus by 8%, invalidating the 'pipeline-attrition' core thesis. Lesson: when biotech short thesis rests on pipeline-attrition odds, require concurrent insider-selling signal before sizing past 0.10.",
  "lesson_category": "thesis_invalidation_at_earnings",   // enum from Mai0313 rubric
  "reflector_model": "anthropic/claude-haiku-4.6",
  "reflector_prompt_hash": "sha256:..."
}
```

`lesson_category` is an open enum stored in `references/lesson-categories.md`. New categories require a small PR. Existing categories include: `thesis_invalidation_at_earnings`, `regime_shift_invalidation`, `position_sized_too_small`, `position_sized_too_large`, `correct_call_too_early`, `correct_call_too_late`, `noise_trade_no_lesson`.

### Layer 3: Memory Retriever (read-only, BM25 + recency)

Module: `hermes_quant/memory/retriever.py`

API:

```python
def get_past_context(
    ticker: str,
    asof: datetime,
    *,
    k_same_ticker: int = 5,
    k_cross_ticker: int = 3,
    k_cross_sector: int = 2,
    only_resolved: bool = True,
) -> PastContext:
    ...
```

`PastContext` carries:

- `same_ticker: list[ResolvedDecision]` — most recent k resolved decisions for this ticker, oldest first. Each is a tight `(asof, rating, raw_return, alpha_return, holding_days, lesson)` tuple.
- `cross_ticker: list[ResolvedDecision]` — top-k by **BM25 similarity** of `thesis_summary` against the new committee's anticipated thesis. Excludes the ticker itself.
- `cross_sector: list[ResolvedDecision]` — top-k from the same GICS sector (or asset-class group), filtered by recency.
- `aggregate_stats: AggregateStats` — over last N days for this ticker: hit rate, avg alpha, avg holding days, current open positions count.

**Oracle Fallacy guard (canonical):**

```python
def get_past_context(... asof: datetime ...):
    # Hard rule: tau_observable < asof. Reject any reflection whose outcome
    # became knowable AT OR AFTER the decision time we're advising on.
    candidates = [r for r in reflections
                  if r.tau_observable is not None
                  and r.tau_observable < asof]
```

A unit test asserts that injecting a reflection with `tau_observable >= asof` into the retriever's index causes it to be excluded. **This test MUST exist** (regression-resistant — the Oracle Fallacy is the most named failure mode for memory-augmented financial agents).

### Injection point

The PM prompt template is amended to include a `Lessons from prior decisions and outcomes:` block (max 2KB). Format mirrors TauricResearch:

```
[YYYY-MM-DD | TICKER | RATING | +RAW% | +ALPHA% | DAYSd]
{reflection_text}
```

Same-ticker block first, then cross-ticker, then cross-sector. Empty block is rendered as `(none)`.

The bull, bear, aggressive-risk, conservative-risk, neutral-risk debaters DO NOT see the reflection block — they are deliberately ignorant of past outcomes to keep their personas clean. Only the research_manager (judge) and portfolio_manager (final authority) see it.

## Storage choice rationale

| Option | Pros | Cons | Pick |
|---|---|---|---|
| JSONL append-only + in-memory BM25 | Same model as audit_log, easy to inspect, no DB drift | BM25 rebuild on every reflection ingestion | ✅ |
| SQLite with FTS5 | Fast retrieval, query language | Schema migration is a real cost, breaks the "memory is markdown / JSONL" simplicity vibe | ❌ |
| Markdown append-only (TauricResearch) | Human-readable | No structured query; cross-ticker similarity is grep-only | ❌ |
| Vector DB (chromadb/lance) | Best semantic recall | Embedding cost, dependency surface, opaque storage | ❌ for v0.2; reconsider for v0.3 |

The JSONL+BM25 path matches our existing append-only-discipline (audit_log, executions, signals, proposals). When BM25 retrieval becomes the bottleneck (probably never on hermes-quant scale) we can layer FTS5 on top; the JSONL stays canonical.

## Lifecycle

```
PaperReactor.execute()
        │
        ▼
   decision_id created  ──►  decisions.jsonl (state=pending, tau_observable=null)
                                                       │
        position opens, calibrator updates, time passes
                                                       │
   PaperReactor.settle()  ─►  outcome computed
                                                       │
                                                       ▼
                                  Reflector  ──► reflections.jsonl
                                                       │
                                                       ▼
                                       decisions.jsonl appends
                                       resolution event linking
                                       decision_id ↔ reflection_id
```

The "settle" event is fired by:

- **Long position close**: explicit close in PaperReactor, OR `tau_observable >= asof_decision + time_horizon` from TraderProposal.
- **Short position close**: same.
- **Time-horizon expiry without close**: synthetic close at last known price, flagged `forced_resolution: true`.

## Performance & cost

- **Reflector LLM cost** (haiku-class): ~$0.0006 per reflection. At ~50 closes/month → $0.03/month. Negligible.
- **Memory storage**: ~1KB per decision + ~500B per reflection. 1000 decisions ≈ 1.5MB. Negligible.
- **BM25 index rebuild**: O(N) on ingestion, N small. <100ms even at 10K decisions.

## Anti-patterns rejected

- ❌ **Mutable memory rows** (e.g., updating `decisions.jsonl` in place to flip pending → resolved). Rejected: append-only discipline. Resolution is a separate event.
- ❌ **Cross-ticker similarity by exact-match on category alone.** Rejected: brittle, misses the soft narrative-similarity signal that's the whole point of episodic memory. BM25 over `thesis_summary` is the canonical match function.
- ❌ **Injecting reflection block into bull/bear/risk debaters.** Rejected: their job is fresh-eyes adversarial reasoning. Memory is the judge's tool, not the debater's.
- ❌ **Storing reflection text in the decision_log row directly.** Rejected: violates append-only at the row level (you'd need to mutate the decision row to attach the reflection). Two logs, joined by `decision_id`, keeps both append-only.
- ❌ **Self-graded reflection** (PM model evaluates its own outcome). Rejected: confounds reflection signal with confirmation bias. Use the haiku-tier model that did NOT make the original decision.

## Compliance with the Oracle Fallacy guard

Required test (in `tests/memory/test_retriever_oracle_fallacy.py`):

```python
def test_retriever_excludes_reflections_with_tau_observable_at_or_after_asof():
    """Oracle Fallacy guard: a reflection whose outcome became knowable AT or
    AFTER the decision-asof MUST NOT be retrievable for that decision.

    This is the canonical regression test for the memory-augmented
    financial-agent failure mode named in arxiv:2605.19337 §4.2.
    """
    asof = datetime(2026, 6, 1, tzinfo=UTC)
    # Inject a reflection whose tau_observable is exactly asof
    inject_reflection(tau_observable=asof, ticker="AAPL", ...)
    # And one that's exactly 1 second before
    inject_reflection(tau_observable=asof - timedelta(seconds=1), ticker="AAPL", ...)
    ctx = get_past_context(ticker="AAPL", asof=asof)
    # Only the strictly-prior one is visible
    assert len(ctx.same_ticker) == 1
    assert ctx.same_ticker[0].tau_observable < asof
```

## Migration / deployment

Phase 1 (Wave 6.2): Land Layer 1 (decision_log) only, populated by PaperReactor on every settle. No reflection, no retriever. Pure observability.

Phase 2 (Wave 6.3): Land Layer 2 (reflector) gated on env var `HERMES_QUANT_REFLECTION=1`. Default OFF. Reflections accumulate to disk but are not yet injected.

Phase 3 (Wave 6.4): Land Layer 3 (retriever) gated on env var `HERMES_QUANT_MEMORY_INJECT=1`. Default OFF. PM prompt is amended to include the block; debaters unchanged.

Phase 4 (after 30 days of accumulated reflections): Flip both env-var defaults to ON. Run a 4-week A/B comparison via `quant-playbook-tick-daily`'s `dry_run` mode: half of universe scans get memory injection, half don't, and we compare outcome-quality distribution.

## Cross-references

- ADR-0033 (Evidence Store) — `thesis_evidence_ids` reuses evidence IDs.
- ADR-0034 (Run Cards) — reflection_id can be referenced from run-card artifacts.
- ADR-0037 (LLM Committee) — quick-tier LLM (haiku) is the reflector.
- ADR-0039 (Signal Provenance) — `signal_provenance` is copied into decision_log row at decision time.
