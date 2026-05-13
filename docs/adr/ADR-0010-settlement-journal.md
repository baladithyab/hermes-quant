# ADR-0010: Settlement journal (markdown sidecar)

**Status**: Accepted
**Date**: 2026-05-13
**Target**: v0.1.2

## Context

v0.1.1 shipped the sidecar daemon → JSONL signal bus → freqtrade strategy loop (ADR-0001, ADR-0008). The bus is the wire-format truth: every decision the daemon ever makes is appended to `~/.hermes/quant/signals.jsonl` and mirrored into `~/.hermes/quant/state.db::signals`. That is sufficient for execution and post-hoc analysis.

What it is NOT sufficient for is **operator UX during a live run**. `tail -f signals.jsonl | jq` is debuggable but not legible. An operator wants to scroll a flat file and see, in narrative form: "at 23:05 UTC the daemon went long BTC/USDT at 10% NAV because BMA confidence 0.72; the position closed 4h later at +1.4% raw / +0.9% alpha vs benchmark; deterministic reflector tagged the thesis as held, magnitude error 0.6×." That is a journal, not a bus.

The settlement journal is that journal. It is a markdown file written alongside the JSONL, derived from the same Pydantic objects, intended for human reading.

A second concern is forward-looking. ADR-0011 will graduate the v0.2 RL aggregator slot; ADR-0012 will introduce an `LLMAnalyst` protocol. LLM analysts in particular benefit from a recency-tail of "what did we decide last time and how did it work out" injected into their context. We need the on-disk format that will eventually feed that retrieval to be stable BEFORE the consumers exist, so the retrieval surface is a contract rather than a refactor target. v0.1.2 ships the journal and the retrieval helper; consumers arrive in v0.3.0.

The Phase-4 cross-family review (`docs/research/04-tradingagents-comparison.md` round 2) called out two patterns from `TauricResearch/TradingAgents` worth porting:

- **Pattern #10**: per-symbol recency tail with cross-asset reflections (`get_recent_lessons(symbol, n_same, n_cross)`).
- **Pattern #17**: Pydantic-only writer — markdown is a render derivative of a typed object, never authored as a raw string.

Notably, the same comparison flagged a pattern we are explicitly **not** porting: `FinancialSituationMemory` (a ChromaDB-backed vector store of past decisions). TradingAgents shipped it, then removed it in their v2 refactor in favor of a plain recency tail. Our v0.1.2 starts where they ended: no embeddings, no vector store, no BM25 — flat append-only markdown with deterministic retrieval.

## Decision

hermes-quant v0.1.2 adds a settlement journal: a markdown file at `~/.hermes/quant/journal.md` (overridable via `HERMES_QUANT_JOURNAL_PATH`), append-only, two-phase, written exclusively through a Pydantic writer.

1. **Path**: `~/.hermes/quant/journal.md`. XDG-style location matching the rest of the daemon's state. `HERMES_QUANT_JOURNAL_PATH` environment variable overrides per the same pattern as `HERMES_QUANT_BUS_PATH` (round-2 pattern #13).

2. **Append-only with HTML-comment delimiters**. Each entry ends with the literal line `<!-- ENTRY_END -->`. The delimiter is an HTML comment rather than a markdown header (`---`, `##`) so that an entry's narrative body — which may contain analyst-authored markdown headings, horizontal rules, or YAML — cannot accidentally collide with the record separator.

3. **Two-phase entries**. Every decision produces a Phase-A pending entry at decision time; the corresponding Phase-B resolution patches that entry at settlement time. Pending entries are tagged `[pending]` in their first-line summary; resolution rewrites that tag to a realized return summary.

4. **Atomic-rename writes**. The writer materializes the full file (header + entries + new entry/patch) into `journal.md.tmp`, calls `fsync(2)`, then `rename(2)` to `journal.md`. Same crash-safety pattern the daemon already uses for `state.json` (ADR-0001 §Implementation notes).

5. **No embeddings, no vector store, no BM25**. Retrieval is flat tail-N over the parsed entry list. Cited divergence from TradingAgents (see Provenance).

6. **Optional rotation**. `journal_max_entries` config (default: unlimited) caps the file size by dropping the oldest **resolved** entries. Pending entries are never rotated out — a Phase-A entry whose Phase-B has not arrived stays in the file regardless of rotation policy, because losing it means losing accountability for an open thesis.

7. **Cross-asset retrieval helper**. `get_recent_lessons(symbol, n_same=5, n_cross=3)` returns the last `n_same` full entries (decision + reflection) for the queried symbol, plus the last `n_cross` reflection-only entries from any other symbol. Adapted from TradingAgents pattern #10. Intended for v0.3.0 LLM-analyst context injection (ADR-0012).

8. **Pydantic-only writer**. The only public mutator is `journal.append_pending(entry: SettlementEntry)` and `journal.resolve(entry_id, ...)`. Neither accepts raw strings. Markdown rendering is a private function of `SettlementEntry`. This locks in "Pydantic = source of truth, markdown = render derivative" before LLM analysts arrive (round-2 pattern #17). Operators who hand-edit the file will have their edits silently overwritten on the next atomic rename; this is a feature, not a bug.

9. **Observability only — never a decision input**. The daemon does not read `journal.md` at decision time. The signal bus (`signals.jsonl`) and `state.db` are the daemon's only inputs. Reading the journal back into the decision loop would create a feedback path that violates ADR-0001's reproducibility constraint (a backtest replay would need the journal as an input artifact, which it is not). The forward direction — journal → LLM-analyst RAG context — is in scope for v0.3.0 under ADR-0012; that consumer is an analyst, not the daemon's gate.

10. **Deterministic reflector for v0.1.2**. The Phase-B reflection field is computed by a deterministic rule: thesis-held iff `direction == sign(raw_return) and raw_return > 0`; magnitude-error = `|raw_return - expected_return| / max(|expected_return|, 1e-6)`. LLM-driven reflection (Reflector agent that reads the per-analyst components and produces prose) is deferred to v0.3.0.

## Rationale

- **Why markdown, not JSON/SQLite?** JSON and SQLite already exist (`signals.jsonl`, `state.db`). Adding a third structured store of the same data buys nothing. The journal exists because a human cannot read JSONL during a live run; a human can scroll markdown. The format is chosen to maximize legibility for that single use case.

- **Why two-phase?** A decision and its outcome are separated by minutes-to-hours of holding period. Writing the entry once at settlement (single-phase) hides the open thesis from the operator during the window when it matters most ("what is this daemon currently long?"). Writing only the decision (no Phase B) loses the realized return — the most important field for human review. Two-phase + the `[pending]` tag is the minimum that supports both reads.

- **Why HTML-comment delimiters and not `---`?** Markdown rendering pipelines treat `---` as a horizontal rule or YAML frontmatter delimiter. Per-analyst components in the entry body may legitimately want to use `---` for narrative separation. `<!-- ENTRY_END -->` parses as an HTML comment in any markdown renderer (it is invisible) and cannot collide with any markdown header level.

- **Why Pydantic-only?** Once `SettlementEntry` is a typed model, schema migrations are mechanical (`model_validate_json` failure → known migration path). Once it is a string template, schema migrations are textual archaeology. v0.3.0 will add LLM-authored fields (analyst prose, reflection narrative); doing that on top of a typed model is straightforward, on top of a string buffer it is not.

- **Why no vector store?** TradingAgents' published artifact (`agents/utils/memory_log.py` in `TauricResearch/TradingAgents`) initially used `FinancialSituationMemory` backed by ChromaDB to retrieve "similar past situations" for the analyst LLMs. Their v2 refactor removed ChromaDB and replaced it with a plain recency tail because (a) embedding similarity over short numeric narratives produced near-uniform scores and (b) the operational cost of a vector index for a sub-1000-row corpus exceeded its lift. v0.1.2 starts at their endpoint. If a future ADR finds the recency tail insufficient, an embedding index can be added as a derived artifact alongside the journal; it does not need to live in the journal itself.

- **Why protect pending entries from rotation?** A Phase-A entry without a Phase-B is an open accounting question. Rotating it out before resolution silently drops a position from the operator's view. The rotation policy applies only to resolved entries.

## Schema

```python
# hermes_quant/journal/models.py
from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field

class AnalystComponent(BaseModel):
    analyst: str
    direction: Literal[-1, 0, 1]
    confidence: float = Field(ge=0.0, le=1.0)
    weight: float = Field(ge=0.0, le=1.0)

class SettlementEntry(BaseModel):
    # ── Phase A: required at decision time ──────────────────────────
    entry_id: str                       # mirrors signal id from ADR-0008
    asof_decision: datetime             # UTC, decision timestamp
    symbol: str                         # e.g. "BTC/USDT"
    asset_class: Literal["crypto", "equity", "fx", "futures"]
    direction: Literal[-1, 0, 1]
    confidence: float = Field(ge=0.0, le=1.0)
    target_position_pct: float          # signed, post-gate (ADR-0004)
    decision_price: float
    benchmark_symbol: str               # e.g. "BTC/USDT" for crypto, "SPY" for US eq
    per_analyst_components: list[AnalystComponent]
    reason: str                         # human-readable, mirrors signal.reason

    # ── Phase B: None until settlement, required at resolve() ───────
    asof_settlement: Optional[datetime] = None
    exit_price: Optional[float] = None
    raw_return: Optional[float] = None        # signed log return, net of fees/slip
    alpha_return: Optional[float] = None      # raw_return − benchmark_return over hold
    hold_minutes: Optional[int] = None
    reflection: Optional["Reflection"] = None

    # ── Invariants ──────────────────────────────────────────────────
    # Phase A: all Phase-A fields non-None, all Phase-B fields None.
    # Phase B: all fields non-None.
    # Mixed states are rejected by `journal.resolve()` validation.

class Reflection(BaseModel):
    thesis_held: bool                   # direction matched & raw_return > 0
    magnitude_error: float              # |actual − expected| / max(|expected|, 1e-6)
    rule_version: str = "deterministic-v1"  # bumped when rule changes; v0.3.0 = "llm-v1"
```

Markdown rendering is a private function `_render(entry: SettlementEntry) -> str`. Operators read the markdown; the daemon and tests read `SettlementEntry`. The two are kept consistent by `tests/unit/test_journal_roundtrip.py` (parse rendered markdown back into `SettlementEntry`, assert equality).

## Lifecycle (pending → resolved two-phase)

**Phase A — decision time** (called from `daemon/tick_loop.py`):

1. `tick_loop` runs analyst pool, aggregator, risk gate per ADR-0002/3/4.
2. Gate emits an `Action` (non-flat). The signal is appended to `signals.jsonl` per ADR-0008.
3. `tick_loop` constructs a `SettlementEntry` with all Phase-A fields populated and Phase-B fields `None`. `entry_id` equals the signal id from step 2 (1:1 with the bus record).
4. `journal.append_pending(entry)`:
   - load the existing journal (parse on `<!-- ENTRY_END -->` delimiters)
   - render the new entry with `[pending]` in its summary line
   - write `journal.md.tmp`, `fsync`, `rename` to `journal.md`
5. Tick loop returns. The entry now sits in the journal with `[pending]` until settlement.

**Phase B — settlement time** (called from `daemon/settlement_loop.py`):

1. `settlement_loop` joins exit fills (from freqtrade's `executions.jsonl` back-channel per ADR-0009 P0-3) against open journal entries by `entry_id`.
2. On a matched exit fill, the loop computes:
   - `raw_return` = log(exit_price / decision_price) − fees − slippage (per ADR-0009 P1-12 defaults)
   - `benchmark_return` = log over the same hold window for `benchmark_symbol`
   - `alpha_return` = `raw_return − benchmark_return`
   - `hold_minutes` = `(asof_settlement − asof_decision).total_seconds() / 60`
3. Apply the deterministic reflection rule → `Reflection(thesis_held, magnitude_error, rule_version="deterministic-v1")`.
4. `journal.resolve(entry_id, asof_settlement, exit_price, raw_return, alpha_return, hold_minutes, reflection)`:
   - load the journal, locate the entry by `entry_id` (raise if missing or already resolved)
   - patch the in-memory `SettlementEntry`, re-render
   - write `journal.md.tmp`, `fsync`, `rename` to `journal.md`
5. The entry's summary line now reads `[+1.42% raw / +0.91% alpha / 247m]` instead of `[pending]`.

```
tick_loop                            settlement_loop
    │                                       │
    │ gate.gate() ──► Action                │
    │                                       │
    │ signals.jsonl  ◄─────── ADR-0008 ─────│
    │                                       │
    │ journal.append_pending(entry) ─► .tmp │
    │                              fsync    │
    │                              rename   │
    │                                       │
    ▼                                       │
 (entry sits as [pending])                  │
                                            │
                        exit fill arrives   │
                                            │
                                            │ compute raw_return, alpha_return,
                                            │         hold_minutes, reflection
                                            │
                                            │ journal.resolve(entry_id, ...)
                                            │     ─► .tmp, fsync, rename
                                            ▼
                                    (entry patched; [pending] gone)
```

## What this is NOT

- **NOT a transactional database.** Multi-row consistency, indexes, and queryability live in SQLite (`state.db`). Halt state, future portfolio reconstruction, fill reconciliation — all SQLite. The journal is flat append-only markdown with one global lock (the atomic rename). Do not push relational concerns into it.

- **NOT a wire format.** `signals.jsonl` is the wire-format truth (ADR-0008). Consumers (freqtrade today, NautilusTrader for v0.2 equities) read JSONL, never the journal. Schema-versioning concerns live on the JSONL contract.

- **NOT consumed by the daemon for decisions.** The daemon's gate has no read path into `journal.md`. A read path would create a feedback loop: today's decision conditions on yesterday's journal entry, journal entry conditions on today's decision, replay-from-bars no longer reproduces because the journal has accumulated state outside the bar stream. Reproducibility (ADR-0001) forbids this. The journal → LLM-analyst direction (ADR-0012, v0.3.0) is allowed because the LLM analyst is itself a deterministic function of `(MarketContext, retrieved_lessons)` and the lessons are versioned with the bus.

- **NOT a vector store.** No embeddings, no ChromaDB, no FAISS, no BM25. `TauricResearch/TradingAgents` shipped a `FinancialSituationMemory` backed by ChromaDB and removed it in their v2; we start at their endpoint. If retrieval ever needs to grow beyond `get_recent_lessons`, the upgrade path is a derived index next to the journal, not inside it.

- **NOT human-edited.** The atomic-rename pattern overwrites the file on every Phase-A and Phase-B call. Manual edits between writes will be silently lost. Operators who want to annotate a trade should attach notes via `state.db` or an external tool; the journal is render-only output.

## Cross-cuts

- **ADR-0001** (sidecar / reproducibility): the journal must not become a daemon decision input. Backtest replay-from-bars must reproduce identical journal output given identical bus output, which holds because the journal is a pure function of the bus + exit-fills back-channel.

- **ADR-0008** (signal bus is the wire format): `entry_id` equals the bus signal id. Every Phase-A entry has exactly one bus record; every Phase-B resolution has exactly one matching exit-fill record on `executions.jsonl` (ADR-0009 P0-3). The journal is the readable join of those two streams.

- **ADR-0009 P0-3** (`executions.jsonl` back-channel, broker reconciliation): settlement loop reads exit fills from this back-channel; the journal cannot be implemented without it.

- **ADR-0012** (planned, v0.3.0, `LLMAnalyst` protocol — forward reference): will consume `get_recent_lessons(symbol, n_same, n_cross)` output as RAG-style context injection. The retrieval surface is fixed by this ADR so ADR-0012 has a stable contract to build against.

## Provenance

- `TauricResearch/TradingAgents` — `agents/utils/memory_log.py` (`TradingMemoryLog`): two-phase pending/resolved markdown journal pattern, per-symbol recency retrieval. Their v2 refactor removed `FinancialSituationMemory` (ChromaDB vector store) in favor of plain recency tail; v0.1.2 adopts the endpoint, not the journey.

- `docs/research/04-tradingagents-comparison.md` round 2:
  - Pattern #10 — `get_recent_lessons(symbol, n_same, n_cross)`: per-symbol full + cross-symbol reflection-only retrieval. Adopted as the journal's only retrieval helper.
  - Pattern #13 — `HERMES_QUANT_*_PATH` env override convention. Adopted for `HERMES_QUANT_JOURNAL_PATH`.
  - Pattern #17 — Pydantic-only writer; markdown is a render derivative. Adopted as the only public mutation surface for the journal.

- ADR-0001 §Implementation notes — atomic-rename pattern for `state.json`. Reused unchanged for `journal.md`.
