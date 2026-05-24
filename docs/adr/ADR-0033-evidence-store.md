# ADR-0033: Evidence Store + Three-Timestamp Invariant

**Status:** Proposed
**Date:** 2026-05-24
**Wave:** B.5 (parallel with ADR-0031 Governance plane, ADR-0032 Trading flow contract)
**Cost:** $0 (local storage, no LLM calls; 50GB local disk cap)

## Context

Phase 3 cross-family review (R1 Gemini-3.1-Pro on TradingAgents, R4 DeepSeek-V4-Pro on FutureSim, R5 Codex on TradingAgents state flow, R6 on moon-dev) surfaced two convergent gaps in hermes-quant's current analyst architecture:

**CV1 — Missing evidence linkage from claims to source data.** TradingAgents' analysts emit free-text markdown blobs that downstream agents read but cannot audit; structured output appears only at the three decision roles. R5 confirmed there is no audit chain from final decision back to the underlying bars/news/filings. R4's solution was a 22-field `EvidenceRecord` Pydantic model with `evidence_ids: tuple[UUID, ...]` linking analyst views back to underlying evidence. This is the single highest-leverage change in the whole synthesis.

**CV2 — The `available_at` invariant is universal, not OHLCV-specific.** R4 formalized FutureSim's chronological-replay mechanism into a three-timestamp model (`published_at`, `ingested_at`, `available_at`) that every evidence kind must carry. R6 documented moon-dev's zero look-ahead-prevention — agents pull live data with no `as_of` clamp anywhere. hermes-quant currently enforces `available_at` for OHLCV bars only (already enforced by ADR-0028 patched D5/D7 for option chains). The invariant is non-negotiable for any analyst that consumes news, filings, or social posts; CI must be extended to assert that no analyst at backtest tick T returns any evidence with `available_at > T`.

This ADR formalizes the Evidence Store as the per-row provenance layer that links every analyst claim to the evidence it consumed, and operationalizes both the audit trail and the FutureSim chronological-replay invariant.

## Decision

### D1 — EvidenceRecord schema

Every piece of evidence consumed by an analyst is materialized as a Pydantic `EvidenceRecord`:

```python
class EvidenceRecord(BaseModel):
    id: UUID                    # stable hash of (kind, source, payload)
    kind: Literal['bar', 'news', 'filing', 'social',
                  'option_chain', 'earnings_call', 'macro_print']
    symbol: str | None          # None for macro
    source: str                 # 'yfinance', 'alpaca', 'edgar',
                                # 'reddit/r/wsb', 'sec_8k', etc.

    # Three-timestamp invariant — ALL three required:
    published_at: datetime      # when the SOURCE published it
    ingested_at: datetime       # when WE pulled it (system clock at fetch)
    available_at: datetime      # max(published_at, ingest_lag_floor);
                                # when downstream may USE it.

    payload_ref: str            # path or URI to the actual data
    payload_hash: str           # SHA-256 of payload bytes (tamper detect)
    schema_version: int = 1
    supersedes: UUID | None = None  # for append-only update chain
```

Per-kind subtypes (`BarEvidence`, `NewsEvidence`, `FilingEvidence`, …) extend the base with kind-specific structured fields (e.g. OHLCV columns for `BarEvidence`, headline + body for `NewsEvidence`, accession_number for `FilingEvidence`). The base record is sufficient for the audit chain; subtypes are for analyst convenience.

### D2 — Three-timestamp invariant + per-kind ingest_lag_floor

Every record carries `published_at`, `ingested_at`, `available_at` and Pydantic rejects any record missing one of the three. The `available_at` field is computed as `max(published_at, published_at + ingest_lag_floor[kind])`; an analyst at backtest tick T may consume a record only if `record.available_at <= T`.

| Kind            | ingest_lag_floor | Rationale                                     |
| --------------- | ---------------- | --------------------------------------------- |
| `bar`           | 60s              | Fill latency; minute bars settle one tick out |
| `news`          | provider-specific (e.g. 30s Bloomberg, 5min RSS) | Delivery lag from source to our ingest |
| `filing`        | 0s               | SEC EDGAR is immediate at publication         |
| `social`        | 0s               | But rate-limit deduped (no flood replay)      |
| `option_chain`  | per ADR-0028 D5  | NBBO snapshot lag                             |
| `earnings_call` | 0s after transcript publish | Audio is real-time; transcript is the evidence |
| `macro_print`   | 0s               | Release timestamp is the published_at         |

A causal sanity check (`available_at >= published_at`) is enforced at construction time; violation raises `EvidenceCausalityError`.

### D3 — Storage layout

Evidence Store lives at `~/.hermes/quant/evidence_store/`:

- **Parquet partitions** at `evidence_store/year=YYYY/month=MM/kind=<kind>/part-NNNN.parquet`. One row per record. Columns are the EvidenceRecord fields plus kind-specific subtype fields.
- **WAL-mode SQLite index** at `evidence_store/evidence_index.db` mapping `id → (parquet_partition_path, row_offset)`. Enables O(1) lookup by UUID for audit walkback.
- **Append-only.** Updates never overwrite an existing record; the corrected record is written with a new `id` and `supersedes: <old_uuid>`. Attempting to overwrite an existing partition raises `EvidenceStoreImmutable`.
- **Payload separation.** `payload_ref` may point to an external blob (e.g. `~/.hermes/quant/evidence_store/blobs/<hash>.json` for full-text news) when the payload is too large to embed in the parquet row.

### D4 — AnalystView amendment to ADR-0002

Single field added to `AnalystView` (and `AggregatedSignal`):

```python
class AnalystView(BaseModel):
    # ... existing fields per ADR-0002 ...
    evidence_ids: tuple[UUID, ...] = ()    # default empty for backward-compat
```

**Migration phases:**

- **Phase 1 (this ADR lands):** field accepted but optional. Existing analysts continue to work with `evidence_ids=()`.
- **Phase 2 (after Wave C connectors land):** CI gate (`tests/test_analyst_evidence_required.py`) fails any analyst that emits a non-empty signal with empty `evidence_ids`. A non-trivial claim must cite at least one `EvidenceRecord`.

### D5 — CI gate: no-lookahead generalized to all evidence kinds

`tests/test_no_lookahead.py::test_no_evidence_with_available_at_gt_asof` is added. For every analyst at backtest tick T, the test asserts:

```python
for record_id in view.evidence_ids:
    record = evidence_store.get(record_id)
    assert record.available_at <= T, EvidenceLookaheadError(record, T)
```

This is equivalent to ADR-0028 patched D5/D7's lookahead drop counter, but generalized to all evidence kinds. The OHLCV-only check in `AGENTS.md` "No look-ahead bias" is superseded by this generalized gate; the existing OHLCV gate becomes a special case (`kind='bar'`).

### D6 — Bidirectional audit chain

Given any final `TradeIntent`, an operator can walk backward:

```
TradeIntent
  → AggregatedSignal (via aggregated_signal_id)
    → AnalystView[]   (via component_views field on AggregatedSignal)
      → evidence_ids: tuple[UUID, ...]
        → EvidenceRecord[] (via evidence_index.db lookup)
          → payload (via payload_ref)
```

Every step is inspectable via the CLI:

```
hermes quant audit <intent_id>           # full walkback, JSON-printable
hermes quant audit <intent_id> --tree    # human-readable tree
hermes quant audit-evidence <evidence_id> # show record + supersedes chain
```

This audit chain is the data substrate that ADR-0031's governance plane consumes when it emits `evidence_referenced` events to the audit log.

### D7 — Storage budgets and retention

The evidence store is bounded: **50GB local disk cap.** Retention rules:

- **Bars:** full 1m resolution retained for 90 days. Older than 90 days, resampled to 1h and the 1m partition is removed. (1h footprint is ~1.5% of 1m for the same span.)
- **News, filings:** full-text retained 365 days. Older than 365 days, header-only (title + url + published_at + summary ≤ 200 chars); full body purged.
- **Social:** rate-limit-deduped at ingest; raw retained 30 days then header-only.
- **Option chains:** per ADR-0028 retention.

When the store reaches 50GB, retention compaction runs first; if still ≥ 50GB, new writes block with `EvidenceStoreFull` and the user is prompted to either raise the cap, run `hermes quant evidence prune --aggressive`, or relocate the store to external disk via `HERMES_QUANT_EVIDENCE_DIR`.

## Test plan

| # | Test | Asserts |
|---|------|---------|
| 1 | `test_evidence_record_three_timestamp_required` | Pydantic rejects record missing any of (published_at, ingested_at, available_at) |
| 2 | `test_evidence_record_available_at_geq_published_at` | record where available_at < published_at raises EvidenceCausalityError |
| 3 | `test_evidence_id_is_deterministic_hash` | same (kind, source, payload) → same UUID; payload diff → different UUID |
| 4 | `test_evidence_store_is_append_only` | overwrite existing partition raises EvidenceStoreImmutable |
| 5 | `test_supersedes_chain_walkable` | record A superseded by B superseded by C; `audit.history(C.id)` returns [C, B, A] |
| 6 | `test_analystview_evidence_ids_default_empty_tuple` | backward-compat with ADR-0002 unmodified analysts |
| 7 | `test_no_lookahead_evidence_ci_gate` | synthesize analyst at tick T returning evidence with available_at = T+1; CI gate fails with EvidenceLookaheadError |
| 8 | `test_audit_walkback_from_tradeintent` | given TradeIntent UUID, walk back to all EvidenceRecords; path complete, no broken link |
| 9 | `test_evidence_store_retention_compresses_old_bars` | bars older than 90 days resampled to 1h; 1m partition removed, 1h partition smaller |
| 10 | `test_storage_size_cap_blocks_writes_at_50gb` | at 49.9GB write succeeds; at 50.0GB write blocks with EvidenceStoreFull |

## Implementation map

```
hermes_quant/evidence/__init__.py            ~5 LOC
hermes_quant/evidence/schema.py              ~120 LOC  (EvidenceRecord + per-kind subtypes)
hermes_quant/evidence/store.py               ~250 LOC  (parquet read/write + WAL index)
hermes_quant/evidence/lookahead_gate.py      ~80 LOC   (CI test extension)
hermes_quant/evidence/audit.py               ~150 LOC  (walkback + CLI command)
hermes_quant/protocol.py                     ~+5 LOC   (add evidence_ids to AnalystView)
tests/evidence/test_*.py                     ~700 LOC across 10 named tests
```

**Estimate:** 1–2 weeks (depends on Wave C connectors landing to populate the store with non-bar evidence kinds).

## Consequences

**Positive:**

- Every analyst claim is auditable back to source bytes (R1, R5 gap closed).
- FutureSim chronological-replay invariant is enforced uniformly across all evidence kinds (R4, R6 gap closed).
- ADR-0028's per-kind lookahead gate generalizes to one CI test instead of one-per-kind.
- ADR-0031's governance plane has a concrete data substrate to reference (`evidence_referenced` audit events).
- ADR-0032's trading flows can scope evidence kinds at the screener level (e.g. "this flow consumes only `bar` and `filing` evidence") and the CI gate enforces that scoping.

**Negative / risks:**

- 50GB local cap may be insufficient for high-frequency multi-symbol research; mitigated by `HERMES_QUANT_EVIDENCE_DIR` external disk option and aggressive retention.
- Phase-2 migration requires every existing analyst to be updated to populate `evidence_ids`; the empty-tuple default keeps Phase 1 backward-compat but the eventual CI gate is a hard breaking change.
- Hashing-based deterministic UUIDs mean two semantically identical records from different sources collapse to the same `id`; this is a feature for dedup but requires `source` to be part of the hash input (it is, per D1).

## Cross-references

- **ADR-0002** — Analyst protocol; amended here to add `evidence_ids: tuple[UUID, ...]`.
- **ADR-0028 (patched D5/D7)** — option_chain replay lookahead drop; generalized here to all evidence kinds via the unified CI gate.
- **ADR-0031** — Governance plane; consumes the audit chain and emits `evidence_referenced` events.
- **ADR-0032** — Trading flow contract; flows scope evidence kinds via screener; CI gate enforces scoping.
- **AGENTS.md "No look-ahead bias"** — existing OHLCV-only CI gate; superseded by D5 generalized gate.
- **R4 reference:** FutureSim, arXiv 2605.15188 (chronological-replay foundation).
