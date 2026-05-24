# R4: FutureSim Evidence-Store Schema for hermes-quant

> **Operationalizing FutureSim's chronological-replay invariant as an evidence-store schema spanning bars, news, filings, social, and account-state evidence.**

---

## 1. FutureSim mechanism — how the paper prevents lookahead

FutureSim (Goel et al., arXiv 2605.15188) evaluates language-model agents on a *forecasting* task over a simulated 90-day period (January–March 2026). The environment is a **chronological replay** of the real world: agents must predict discrete future events (who wins an election, what rate the Fed sets, etc.) while the information available to them evolves day-by-day, exactly as it did in reality.

**The simulation loop is minimal by design** — the environment exposes only two actions:

1. `submit_forecast(question_id, outcomes)` — register or update a probability distribution over self-generated possible outcomes for a question.
2. `next_day()` — advance the simulation by one calendar day. When called, the task-state CSV is updated (resolved questions receive their ground-truth answer) and the news corpus rolls forward to include articles published on or before the new date.

**Lookahead prevention is structural, not optional.** The news corpus is an offline, deduplicated snapshot of Common Crawl News (7.36M articles from 141 sources). Articles live in folders organized by date. The agent's sandbox grants **read access only to folders up to the current simulation date** — no `curl`, no web search, no external APIs. Even the internal `search_news(query, from_date, to_date)` hybrid semantic/keyword tool constrains `to_date ≤ current_day`. The sandbox details in Appendix B.3 make this airtight: future-dated folders are literally not present in the file tree the agent can traverse.

**The task-state CSV** stores each question's background, resolution criteria, resolution date, and the agent's most recent forecast. When `next_day()` advances past a resolution date, the ground-truth outcome appears in the CSV — the agent can *learn from resolved questions* but cannot *preview* unresolved ones. This creates a clean **information arrival curve**: every piece of evidence has a wall-clock timestamp (the article's publication date) and the agent's access to it is gated by the simulation clock.

**Key design property for trading:** FutureSim achieves reproducibility through determinism. The same agent harness, model, seed, and corpus produce the same sequence of forecasts. The environment dynamics are derived *from real-world timestamps*, not from a human-designed simulator — so the complexity and unpredictability of real societal evolution are preserved while remaining replayable.

---

## 2. The `available_at` invariant — formal definition

The auxiliary framework document (doc_1a46a8507598) explicitly cites FutureSim's chronological-replay principle as the invariant hermes-quant backtests require:

> *"Every news article, filing, Reddit/X post, price tick, order book update, and account state update needs an `available_at` timestamp so you can replay the world without look-ahead bias."*

### The three-timestamp model

Every evidence item in hermes-quant MUST carry three timestamps:

| Field | Meaning | Set by | Example |
|-------|---------|--------|---------|
| `published_at` | When the source claims the event occurred / was published | External source metadata | A 10-K filing header says "Filed: 2026-03-15 08:03:00 ET" |
| `ingested_at` | When our ingestion pipeline first observed and stored the item | Pipeline clock (NTP-synced, UTC) | Our CCNews scraper wrote the article record at 2026-03-15 08:07:22Z |
| `available_at` | When the evidence becomes **permissible for backtest consumption** | MAX(`ingested_at`, `published_at + latency_budget`) or explicit operator timestamp | 2026-03-15 08:07:22Z (if no extra budget) |

### Why the difference matters for backtests

**`published_at` alone is insufficient.** If a filing hits EDGAR at 08:03 but our pipeline's polling cycle only picks it up at 08:07, a backtest that allows decisions at 08:03:01 based on that filing commits lookahead bias — the real system would not have had it.

**`ingested_at` alone is insufficient.** It captures *our* pipeline latency but ignores the case where a source backdates content. An article might have `published_at = 2026-01-01` but only appear in CCNews on 2026-01-03. `ingested_at` would be 2026-01-03, which is correct — but if we only store `ingested_at`, we lose the provenance chain to the source's claimed publication time.

**`available_at` is the contract field.** It is the LATTER of `ingested_at` and `published_at + latency_budget`. The `latency_budget` is a per-source, per-kind constant (e.g., 0s for direct exchange tick data, 300s for EDGAR filings, 3600s for CCNews articles) that models the maximum acceptable processing delay. If a source provides an explicit "embargo lifts at" timestamp, `available_at` uses that directly. The invariant is:

> **No analyst query at backtest tick `T` may return any evidence record with `available_at > T`.**

This collapses to the already-enforced `as_of` invariant for bars (`timestamp <= as_of`), but extends it across heterogeneous evidence types.

### Formalization (Pydantic-style)

```python
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

EvidenceKind = Literal[
    "bar", "quote", "orderbook_snapshot", "orderbook_delta",
    "news", "filing", "social_post", "earnings",
    "account_state", "macro_indicator",
]

class EvidenceTimestamp:
    """Three-timestamp model for every evidence record."""
    published_at: datetime   # UTC, from source metadata
    ingested_at: datetime    # UTC, pipeline clock
    available_at: datetime   # UTC, the LATER of ingested_at and published_at+latency

    @classmethod
    def compute_available_at(
        cls, published_at: datetime, ingested_at: datetime,
        *, latency_budget_s: float = 0.0,
    ) -> datetime:
        """Compute the available_at timestamp.
        
        available_at = max(ingested_at, published_at + timedelta(seconds=latency_budget))
        
        If an explicit embargo timestamp is provided, use that instead.
        """
        from datetime import timedelta
        candidate = published_at + timedelta(seconds=latency_budget)
        return max(ingested_at, candidate)
```

---

## 3. Evidence store schema — Pydantic model

```python
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class EvidenceKind(StrEnum):
    """Granular evidence type for query filtering."""
    BAR = "bar"
    QUOTE = "quote"
    ORDERBOOK_SNAPSHOT = "orderbook_snapshot"
    ORDERBOOK_DELTA = "orderbook_delta"
    NEWS = "news"
    FILING = "filing"               # SEC, K-1, 8-K, 10-Q, 10-K, etc.
    SOCIAL_POST = "social_post"      # X / Reddit / Discord / Telegram
    EARNINGS = "earnings"            # earnings call transcript or report
    ACCOUNT_STATE = "account_state"  # broker positions, margin, fills
    MACRO_INDICATOR = "macro_indicator"  # CPI, NFP, FOMC minutes, etc.

    @property
    def default_latency_budget_s(self) -> float:
        """Per-kind default processing latency budget in seconds."""
        return {
            EvidenceKind.BAR: 0.0,
            EvidenceKind.QUOTE: 0.0,
            EvidenceKind.ORDERBOOK_SNAPSHOT: 0.0,
            EvidenceKind.ORDERBOOK_DELTA: 0.0,
            EvidenceKind.NEWS: 3600.0,         # 1 hour for CCNews pipeline
            EvidenceKind.FILING: 300.0,         # 5 minutes for EDGAR polling
            EvidenceKind.SOCIAL_POST: 600.0,    # 10 minutes for social scrapers
            EvidenceKind.EARNINGS: 3600.0,      # 1 hour for transcript processing
            EvidenceKind.ACCOUNT_STATE: 0.0,    # direct broker API
            EvidenceKind.MACRO_INDICATOR: 300.0,# 5 minutes
        }[self]


class ExtractedFeatures(BaseModel):
    """Provenance-tracked feature extraction metadata."""
    pipeline: str                    # e.g., "sentiment_v2.1", "entity_linker_v1.0"
    pipeline_version: str            # semver
    extracted_at: datetime           # UTC — when features were computed
    features: dict[str, Any] = Field(default_factory=dict)
    # e.g., {"sentiment": 0.72, "entities": ["AAPL", "NFLX"], "event_type": "earnings_beat"}
    embedding: list[float] | None = None  # optional vector embedding (Qwen3-8B, 4096-d)
    embedding_model: str | None = None    # e.g., "Qwen3-Embedding-8B"


class EvidenceRecord(BaseModel):
    """Canonical evidence-store record for hermes-quant.

    Every piece of information the perception layer produces is stored as
    an EvidenceRecord. Analysts and backtests query the store with an
    `as_of` timestamp; records with `available_at > as_of` are invisible.
    """
    # ── Identity ──────────────────────────────────────────────
    evidence_id: UUID = Field(default_factory=uuid4)
    kind: EvidenceKind
    source_uri: str
    # e.g., "https://www.sec.gov/Archives/edgar/data/320193/0000320193-26-000012.txt"
    # or "ccnews://al-jazeera/2026-03-15/article-42"

    # ── Temporal ──────────────────────────────────────────────
    published_at: datetime           # UTC, from source claims
    ingested_at: datetime            # UTC, pipeline ingestion clock
    available_at: datetime           # UTC, contract field — see §2
    # Backtests MUST filter: WHERE available_at <= tick_time

    # ── Content ───────────────────────────────────────────────
    content_hash: str                # sha256 of raw content bytes
    content_length_bytes: int        # for token-budget estimation
    mime_type: str = "text/plain"   # text/html, application/pdf, etc.
    raw_content: str | None = None   # nullable — stored in blob tier for large items
    # For bars: raw_content is None; the bar data lives in the parquet store
    # with a foreign key (bar_id) in extracted_features

    # ── Classification ────────────────────────────────────────
    asset_tags: list[str] = Field(default_factory=list)
    # e.g., ["AAPL", "SPX", "BTC/USDT"] — which assets this evidence relates to
    sector_tags: list[str] = Field(default_factory=list)
    # e.g., ["technology", "consumer_electronics"]
    geo_tags: list[str] = Field(default_factory=list)
    # e.g., ["US", "CN"]

    # ── Provenance ────────────────────────────────────────────
    source_name: str                 # e.g., "ccnews", "edgar", "x_api", "binance_ws"
    source_credibility: float = 1.0  # [0, 1] — operator-assigned or ML-estimated
    pipeline_run_id: str | None = None  # for debugging/replay

    # ── Extracted features ────────────────────────────────────
    extracted_features: ExtractedFeatures | None = None
    # May contain multiple feature-sets from different pipeline versions;
    # the analyst selects which to consume by pipeline name.

    # ── Optional: replayed-at for backtest determinism ────────
    replayed_at: datetime | None = None
    # Set by the backtest harness when replaying a historical evidence record.
    # If set, the analyst sees `replayed_at` as the effective `available_at`
    # to prevent wall-clock leakage.

    # ── Relations ─────────────────────────────────────────────
    parent_evidence_id: UUID | None = None
    # For chain-of-evidence: an earnings transcript might reference the original
    # SEC filing as its parent.
    related_evidence_ids: list[UUID] = Field(default_factory=list)
    # For graph traversal: a news article citing a social post.

    class Config:
        use_enum_values = True
        json_encoders = {datetime: lambda v: v.isoformat()}
```

### Why each field exists

| Field | Justification |
|-------|--------------|
| `evidence_id` (UUID) | Universal foreign key — cited by `TradeIntent.evidence_ids`, analyst metadata, and audit logs |
| `kind` (enum) | Enables per-kind index partitioning, latency budgets, and query filtering |
| `source_uri` | Immutable pointer to the raw artifact for replay verification |
| `published_at` | Source-claimed timestamp — needed for time-series alignment and dispute resolution |
| `ingested_at` | Pipeline clock — needed to distinguish "was published at 08:01, ingested at 08:05" from "was published at 08:05, ingested at 08:05" |
| `available_at` | **The contract field.** Backtest queries filter on this. The latency budget is per-kind. |
| `content_hash` | Reproducibility: same evidence record produces same signal. Used by CI invariant. |
| `extracted_features` | Decouples raw evidence from analyst-consumable features. Multiple pipeline versions can coexist. |
| `asset_tags` / `sector_tags` / `geo_tags` | Enables efficient "give me all news about AAPL" queries without full-text scan |
| `parent_evidence_id` / `related_evidence_ids` | Enables graph queries: "show me the evidence chain that led to this claim" |

---

## 4. How analysts consume the evidence store

### Query pattern at backtest tick `T`

```python
# Analyst at tick T asks: "what evidence was available by T?"
records: list[EvidenceRecord] = evidence_store.query(
    asset_tags=["AAPL"],
    kinds=[EvidenceKind.NEWS, EvidenceKind.FILING, EvidenceKind.EARNINGS],
    available_before=T,          # WHERE available_at <= T
    min_published_after=T - timedelta(days=7),  # recency window
    limit=50,
)
```

The **invariant** is enforced at the query layer: `WHERE available_at <= :tick_time`. This is a single indexed column predicate that any backend (Parquet, DuckDB, SQLite, Iceberg) can efficiently scan.

### Index strategy

- **Primary index:** `(asset_tags, available_at)` — compound B-tree covering the dominant query pattern.
- **Secondary index:** `(kind, available_at)` — for kind-specific analysts (e.g., "news-only").
- **Full-text index:** on `raw_content` for keyword search (FTS5 in SQLite, or inverted index in DuckDB).
- **Vector index:** if embeddings are stored, use DuckDB's `ARRAY` type with brute-force cosine similarity for <10K records per query window; migrate to pgvector/Qdrant only when >100K concurrent records is common.

### Cache strategy

- **Per-backtest-run cache:** The backtest harness pre-loads all evidence with `available_at` in `[T_start, T_end]` into an in-memory dict keyed by `(asset_tag, tick_day)`. Memory cost: ~10K records × 2KB ≈ 20MB for a 90-day single-asset run.
- **Hot evidence cache:** A Redis sorted set `evidence_available:{asset}` with `available_at` as score. Analysts call `ZRANGEBYSCORE` with `max=T`. TTL = 24h for news, 7d for filings.
- **Embedding token budget:** For news analysts using LLM summarization, cap raw content at 4096 tokens per evidence record (~3K words). The `content_length_bytes` field allows pre-filtering before the expensive LLM call.

### Reproducibility guarantee

The evidence store is **append-only and immutable**. Once an `EvidenceRecord` is written (with its `content_hash`), it is never updated — new features (e.g., a better sentiment model) are written as new `ExtractedFeatures` rows with a different `pipeline_version`. This means replaying a backtest with the same evidence store UUIDs produces byte-identical analyst views — the same guarantee that FutureSim provides for its news corpus.

---

## 5. Linkage to TradeIntent — audit trail from evidence to order

Per the auxiliary framework document, the decision layer emits a `TradeIntent` (not an order):

```json
{
  "strategy_id": "btc_5m_polymarket_latency_v3",
  "instrument": "POLYMARKET:BTC-5M:UP",
  "intent": "OPEN_LONG",
  "confidence": 0.63,
  "evidence_ids": [
    "550e8400-e29b-41d4-a716-446655440001",
    "550e8400-e29b-41d4-a716-446655440002"
  ]
}
```

### How hermes-quant maps this to its existing protocol

The `AnalystView` dataclass in `hermes_quant/protocol.py` currently has no `evidence_ids` field. We add it:

```python
@dataclass(frozen=True)
class AnalystView:
    # ... existing fields ...
    evidence_ids: tuple[UUID, ...] = ()  # NEW — immutable, empty by default
```

The `AggregatedSignal` propagates them:

```python
@dataclass(frozen=True)
class AggregatedSignal:
    # ... existing fields ...
    evidence_ids: tuple[UUID, ...] = ()  # union of component evidence_ids
```

### Audit trail

1. **At decision time:** The aggregator unions `evidence_ids` from all contributing `AnalystView`s.
2. **At signal bus write:** `signals.jsonl` records include the full evidence tuple.
3. **At settlement:** The settlement loop (ADR-0010) writes `RealizedOutcome` with the same `evidence_ids`, enabling per-evidence-chain performance attribution.
4. **At audit query:** `hermes quant audit --trace evidence_ids=<uuid>` traverses: Evidence → AnalystView → AggregatedSignal → Action → Fill → RealizedOutcome. This is a linear provenance chain, fully replayable from disk.

The **CI invariant** ensures no analyst can cite evidence it shouldn't have seen: if an `AnalystView.evidence_ids` tuple contains a UUID whose `available_at > MarketContext.asof`, the CI gate fails.

---

## 6. Storage backends — recommendation with tradeoffs

hermes-quant currently uses **Parquet for bars** and **JSONL for the signal bus**. For the evidence store, the requirements are:

| Requirement | Constraint |
|-------------|-----------|
| Append-only, immutable records | No updates after write |
| Filter by `(asset_tags, available_at <= T)` | Compound index needed |
| Full-text search on `raw_content` | Optional but valuable for news analysts |
| Vector similarity (optional) | Embedding search for semantic retrieval |
| Single-machine operation | No distributed consensus for v0 |
| Reproducibility | Byte-identical reads between runs |
| ≤ 100K records per asset per year | Modest scale |

### Recommendation: DuckDB (primary) + JSONL (fallback archive)

**DuckDB** is the strongest fit for v0–v1:

| Criterion | DuckDB | SQLite+JSONB | Iceberg/S3 |
|-----------|--------|-------------|------------|
| Embedded, zero-config | ✅ Yes | ✅ Yes | ❌ Needs Spark/Trino |
| Columnar scans on `available_at` | ✅ Native | ⚠️ Row-oriented | ✅ Best-in-class |
| Parquet interoperability | ✅ Reads/writes directly | ❌ Separate library | ✅ Native |
| Full-text search | ✅ FTS extension | ✅ FTS5 (built-in) | ❌ Separate service |
| Vector search | ✅ `ARRAY` + `list_cosine_similarity` | ⚠️ Extension needed | ❌ Separate service |
| Single-file portability | ✅ `.duckdb` file | ✅ `.sqlite` file | ❌ Directory of files |
| Python ergonomics | ✅ DuckDB Python API | ✅ sqlite3 built-in | ⚠️ PyIceberg |
| Hermes-quant alignment | ✅ Same toolchain (Python data) | ✅ | ❌ Overkill for v0 |

**Tradeoff:** DuckDB's write concurrency is limited (single-writer). For hermes-quant's single-daemon architecture with append-only patterns, this is acceptable. If the evidence store grows beyond ~10M records, migrating to Iceberg on S3/MinIO becomes worthwhile — but that's a v0.4+ concern.

**JSONL fallback:** Every evidence record is also written to `~/.hermes/quant/evidence/evidence.jsonl` (append-only, line-buffered, fsynced). This is the **replay-from-disk** contract surface: the JSONL file is the source of truth; DuckDB is a derived index that can be rebuilt from JSONL.

### Schema (DuckDB DDL sketch)

```sql
CREATE TABLE evidence (
    evidence_id UUID PRIMARY KEY,
    kind VARCHAR NOT NULL,
    source_uri VARCHAR NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    content_length_bytes INTEGER NOT NULL,
    mime_type VARCHAR DEFAULT 'text/plain',
    raw_content VARCHAR,
    asset_tags VARCHAR[],
    sector_tags VARCHAR[],
    geo_tags VARCHAR[],
    source_name VARCHAR NOT NULL,
    source_credibility FLOAT DEFAULT 1.0,
    pipeline_run_id VARCHAR,
    extracted_features JSON,   -- ExtractedFeatures serialized
    replayed_at TIMESTAMPTZ,
    parent_evidence_id UUID,
    related_evidence_ids UUID[],
);

CREATE INDEX idx_evidence_asset_available
    ON evidence (asset_tags, available_at);

CREATE INDEX idx_evidence_kind_available
    ON evidence (kind, available_at);
```

---

## 7. Migration path — minimum viable evidence store for ONE news-driven analyst

Today hermes-quant has **0%** of this. Bars live in Parquet files with `timestamp` columns and `as_of` filtering — that's the only `available_at`-like discipline. News, filings, social, and multi-modal evidence have no schema, no store, no CI gate. ADR-0022 (Hermes Semantic Perception) defines a *packet* schema (`schema_version`, `asset`, `asof`, `stance`, `confidence`, `packet_hash`) but has no persistent evidence store backing it.

### Phase 1: MVP — ONE table, ONE analyst (1-2 days of work)

**Goal:** Get a news-driven analyst producing `AnalystView`s backed by real `evidence_ids` that survive CI.

**Step 1 — Create the minimal table:**

```sql
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,          -- UUID as text
    kind TEXT NOT NULL DEFAULT 'news',     -- single value for MVP
    source_uri TEXT NOT NULL,
    published_at TEXT NOT NULL,            -- ISO 8601 UTC
    ingested_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,            -- sha256 hex
    asset_tags TEXT NOT NULL DEFAULT '[]', -- JSON array
    raw_content TEXT,
    extracted_features TEXT,               -- JSON blob
    UNIQUE(content_hash)                   -- dedup guard
);

CREATE INDEX IF NOT EXISTS idx_evidence_available
    ON evidence(available_at);
```

**Step 2 — Write a CLI command to ingest news:**

```bash
hermes quant evidence ingest-news \
  --source ccnews \
  --articles-dir ~/.hermes/quant/news/2026-03-15/ \
  --latency-budget 3600  # 1 hour
```

This reads article files, computes `content_hash`, sets `available_at = max(ingested_at, published_at + 3600s)`, and inserts rows.

**Step 3 — Write a `NewsAnalyst` that queries the evidence store:**

```python
class NewsAnalyst:
    def analyze(self, ctx: MarketContext) -> AnalystView | None:
        # Query evidence available by ctx.asof
        records = evidence_store.query_latest(
            asset_tags=[ctx.asset],
            available_before=ctx.asof,
            limit=20,
        )
        if not records:
            return None  # silence-by-default
        
        # Produce a view; include evidence_ids
        return AnalystView(
            analyst="news_v0.1",
            direction=self._extract_direction(records),
            confidence=self._calibrate(records),
            evidence_ids=tuple(r.evidence_id for r in records),
            ...
        )
```

**Step 4 — Wire the CI invariant (see §8).**

**Step 5 — Add `evidence_ids` to `AnalystView` and `AggregatedSignal` in `protocol.py` (backward-compat: empty tuple default).**

### Phase 2: Extend to filings + social (v0.4)

Add `EvidenceKind.FILING` and `EvidenceKind.SOCIAL_POST` rows. The table schema is already generic; only ingestion adapters change.

### Phase 3: Multi-modal + embeddings (v0.5+)

Add `embedding` column, migrate to DuckDB, add vector search.

---

## 8. CI invariant — extending `test_no_lookahead.py`

The existing CI gate (`tests/test_no_lookahead.py`) tests two things:

1. **Analyst determinism under `as_of`** — same context, same output regardless of future data.
2. **Shuffle-timestamps test** — analyst's score is distinguishable from chance when timestamps are shuffled.

Neither tests that an analyst *reads evidence with `available_at > tick_time`*. This is the new invariant.

### The test

```python
def test_analyst_never_reads_future_evidence():
    """No analyst may consume evidence with available_at > ctx.asof.
    
    This is the FutureSim invariant: at simulation day D, the agent
    can only see articles published on or before day D. Translated to
    hermes-quant: at tick T, the analyst's evidence_ids must all have
    available_at <= T.
    """
    from datetime import datetime, timedelta, timezone
    from hermes_quant.analysts.news import NewsAnalyst  # new analyst
    
    # Create evidence with a MIX of available_at timestamps
    now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    
    past_evidence = [
        _make_evidence(available_at=now - timedelta(hours=2)),   # valid
        _make_evidence(available_at=now - timedelta(hours=1)),   # valid
    ]
    future_evidence = [
        _make_evidence(available_at=now + timedelta(hours=1)),   # LEAKED
        _make_evidence(available_at=now + timedelta(days=1)),    # LEAKED
    ]
    
    # Seed the evidence store with ALL records
    store = InMemoryEvidenceStore(past_evidence + future_evidence)
    
    # Query with as_of = now — should only return past_evidence
    ctx = MarketContext(
        asset="AAPL", timeframe="1d", asset_class="equity",
        exchange=None, bars=_make_fixture_bars(asof=now),
        last_close=100.0, last_volume=1e6, asof=now,
    )
    
    analyst = NewsAnalyst(evidence_store=store)
    view = analyst.analyze(ctx)
    
    if view is None:
        return  # abstained — silence is valid
    
    # CRITICAL: every evidence_id must correspond to available_at <= ctx.asof
    for eid in view.evidence_ids:
        record = store.get(eid)
        assert record is not None, f"evidence_id {eid} not found in store"
        assert record.available_at <= ctx.asof, (
            f"LOOKAHEAD VIOLATION: analyst {analyst.name} cited evidence "
            f"{eid} with available_at={record.available_at} > tick_time={ctx.asof}. "
            f"This is the FutureSim chronological-replay invariant failure. "
            f"Release blocked."
        )
```

### Integration with existing CI

Add to `tests/test_no_lookahead.py`:

```python
# New test function
def test_evidence_store_lookahead_gate():
    """Invariant: NO evidence with available_at > tick_time reaches any analyst."""
    ...

# Parametrized across ALL analysts (existing pattern)
@pytest.mark.parametrize("analyst_factory", [
    lambda: ClassicalTAAnalyst(),
    lambda: MicrostructureLite(),
    lambda: NewsAnalyst(evidence_store=fixture_store()),   # NEW
])
def test_shuffle_timestamps_invariant_via_evaluation_module(analyst_factory):
    # ... existing implementation ...
```

### Enforcement level

This is a **release blocker** per ADR-0006: any analyst that fails the evidence-store lookahead test blocks the next version tag, same as the existing shuffle-timestamps gate.

---

## Summary

| Deliverable | Status |
|-------------|--------|
| FutureSim mechanism explained | §1 — daily cadence, sandboxed news corpus, `next_day()` advancement, offline corpus prevents web leakage |
| `available_at` invariant formalized | §2 — three-timestamp model (`published_at`, `ingested_at`, `available_at`) with per-kind latency budgets |
| Evidence store schema (Pydantic) | §3 — `EvidenceRecord` with 20+ fields, `EvidenceKind` enum, `ExtractedFeatures` provenance |
| Analyst consumption pattern | §4 — `WHERE available_at <= T` query, compound index, in-memory pre-load cache, token budget for LLM summarization |
| TradeIntent linkage | §5 — `evidence_ids` tuple on `AnalystView` → `AggregatedSignal` → signal bus → settlement → audit |
| Storage backend recommendation | §6 — DuckDB primary (columnar, embedded, Parquet-interop) + JSONL fallback archival source-of-truth |
| Migration path | §7 — Phase 1 MVP (1 table, 1 news analyst, 1-2 days); Phase 2 filings+social; Phase 3 embeddings |
| CI invariant | §8 — `test_analyst_never_reads_future_evidence()` — release blocker, parametrized across all analysts |

---

*Provenance: FutureSim (Goel et al., arXiv 2605.15188), hermes-quant ADRs 0005/0019/0020/0022, auxiliary framework doc_1a46a8507598, `tests/test_no_lookahead.py`, `hermes_quant/protocol.py`.*
