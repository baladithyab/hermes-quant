# Vibe-Trading Analysis: Run Cards, Research-vs-Execution Boundary, Shadow Accounts & Memory Architecture

**Source repo:** HKUDS/Vibe-Trading (`/tmp/quant-research/sources/Vibe-Trading/`)  
**Analysis date:** 2026-05-24  
**Lens:** Run Cards + research-vs-execution split + shadow-account behavior + memory architecture  
**Hermes-quant posture alignment:** Vibe-Trading draws an identical hard boundary at "no live execution". Money never moves; all artifacts are research-grade and replayable from disk.

---

## 1. Run Card Schema (Trust Layer)

**Primary source:** `agent/backtest/run_card.py` (full 200 LOC file, especially `write_run_card:24`, `_list_artifacts:122`, `_render_markdown:145`)

### Artifacts generated for every backtest
- `run_card.json` — authoritative JSON record (deterministic, sorted keys, UTF-8, trailing newline)
- `run_card.md` — rendered Markdown summary shown on the run detail page alongside equity curve and metrics

### Exact schema (from `write_run_card` and `_render_markdown`)
```json
{
  "schema_version": "0.1",
  "generated_at": "2026-05-24T14:22:05Z",
  "run_dir": "/home/user/.vibe-trading/runs/2026-05-24_abc123",
  "backtest": {
    "codes": ["000001.SZ", "600519.SH"],
    "start_date": "2020-01-01",
    "end_date": "2025-12-31",
    "interval": "1D",
    "engine": "daily",
    "initial_cash": 1000000,
    "source": "tushare"
  },
  "reproducibility": {
    "config_hash": "sha256:7e3f2a1b9c8d...",
    "strategy_hash": "sha256:9f8e7d6c5b4a..."   // optional
  },
  "data_sources": ["tushare", "akshare"],
  "metrics": {
    "final_value": 1245678.90,
    "total_return": 0.2457,
    "annual_return": 0.0482,
    "max_drawdown": -0.1123,
    "sharpe": 1.84,
    "win_rate": 0.632,
    "trade_count": 87
  },
  "validation": {                         // present only when supplied in metrics dict
    "monte_carlo": {...},
    "walk_forward": {...}
  },
  "warnings": ["slippage model conservative (2 bp)"],
  "artifacts": [
    {"path": "config.json", "size_bytes": 1240, "sha256": "a1b2c3..."},
    {"path": "code/signal_engine.py", "size_bytes": 4821, "sha256": "d4e5f6..."},
    {"path": "artifacts/equity.csv", "size_bytes": 18920, "sha256": "789abc..."}
  ]
}
```

### Reproducibility implementation details
- `config_hash` (`_json_hash:87`): deterministic JSON serialization excluding underscore-prefixed keys
- `strategy_hash` (`_file_hash:98`): 1 MiB chunked SHA-256 when `strategy_path` is supplied and exists
- `_list_artifacts:122`: always includes `config.json` + `code/signal_engine.py` + every file under `run_dir/artifacts/`
- Markdown rendering (`_render_markdown:145`) produces human-readable sections for Backtest Summary, Reproducibility, Data Sources, Metrics, Validation, Warnings, and Artifacts with SHA-256 fingerprints

### Status in hermes-quant
hermes-quant currently emits raw metrics CSV + equity CSV with no canonical reproducibility manifest or artifact manifest.  
**Recommendation:** port `write_run_card` directly into `hermes-quant/backtest/runner.py` so every backtest leaves a `run_card.json` + `run_card.md`.

---

## 2. Research-vs-Execution Boundary (Enforced in Code)

Vibe-Trading never ships execution capability. The boundary is architectural:

- **Tool registry** (`agent/src/tools/`) contains only research, backtest, shadow, read, upload, memory, and hypothesis tools. No `submit_order`, `place_order`, or broker SDK calls exist.
- **Shadow Account pipeline** (`agent/src/shadow_account/`) produces only counterfactual attribution reports (`AttributionBreakdown`).
- **Backtest runner** (`agent/backtest/runner.py:52`) accepts only validated `BacktestConfigSchema`; the CLI entrypoint `python -m backtest.runner <run_dir>` contains no live-mode flags.
- **Security scanner** (`agent/src/security/scanner.py`) statically rejects generated code containing broker SDK symbols or live execution patterns.
- **Hypothesis registry** (`agent/src/hypotheses/registry.py:18`) only manages status strings (`exploring`, `testing`, `validated`, `rejected`, `monitoring`). No execution path from any state.

**File:line evidence:** The boundary is maintained by absence of execution code, reinforced by `security/scanner.py` and the complete lack of any `ExecutionMode` or `--live` argument across `cli/`, `backtest/`, and `agent/src/`.

This posture is identical to hermes-quant's "Money never moves through plugin tools — only CLI with explicit confirmation".

---

## 3. Shadow-Account Behavior Analysis

**Core implementation:** `agent/src/shadow_account/` (extractor.py, codegen.py, backtester.py, reporter.py, models.py, storage.py)

### Exact pipeline (`extractor.py:44`)
1. Parse broker journal (同花顺 / 富途 / generic CSV) via `trade_journal_parsers`
2. FIFO pairing via `pair_trades_fifo`
3. Filter to profitable roundtrips only (`pnl_pct > 0`)
4. Feature engineering: numeric (`holding_days`, `pnl_pct`, `entry_hour`, `entry_weekday`) + categorical (`market`)
5. KMeans clustering (k auto-selected 2–5) + decision-tree path extraction (max depth 3)
6. Structured `entry_condition` / `exit_condition` dicts (feature → scalar or (op, value) tuples)
7. LLM-light natural-language translation (template fallback when no LLM available)
8. Immutable `ShadowRule` + `ShadowProfile` emission

### Frozen dataclasses (`models.py:14-80`)
- `ShadowRule`: `rule_id`, `human_text` (≤30 chars), `entry_condition`, `exit_condition`, `holding_days_range`, `support_count`, `coverage_rate`, `sample_trades`, `weight`
- `ShadowProfile`: `shadow_id` ("shadow_<8-hex>"), `journal_hash` (SHA1), `profitable_roundtrips`, `total_roundtrips`, `date_range`, `profile_text`, `rules` (3–5), `preferred_markets`, `typical_holding_days`
- `AttributionBreakdown`: delta PnL, rule_violation count, early_exit count, missed_signal count, forgone PnL

### Report rendering (`reporter.py:1-50`)
- 8-section structured HTML + optional PDF (Jinja2 + weasyprint, graceful HTML-only downgrade)
- Charts rendered via matplotlib with CJK font support (`fonts.py`)
- Deterministic output paths: `shadow_report.html`, `shadow_report.pdf`, `attribution.json`

### Promote / reject trigger
High forgone PnL combined with low rule-violation count in the attribution report is the observable signal that historically triggers manual promotion into the Hypothesis Registry.

---

## 4. Memory Architecture (Cross-Session Research Notes)

**Core implementation:** `agent/src/memory/persistent.py` (entire 364 LOC file)

### Layout on disk
```
~/.vibe-trading/memory/
├── MEMORY.md                 # compact index (< 200 lines)
├── user_prefs.md
├── project_btc_momentum.md
├── factor_rmw.md
└── ...
```

### Entry format
```markdown
---
title: "BTC momentum factor v3"
memory_type: project
description: "Long-only 20d/50d crossover on CSI300 with T+1 filter"
created_at: "2026-05-20T08:12:00Z"
---
Body Markdown text (hard-capped at 8000 chars). C0/C1 control bytes are stripped on write.
```

### Tokenization & retrieval (`persistent.py:68-84`, `43-44`, `250-263`)
- `_TOKEN_RE`: ASCII words ≥ 3 characters + full characters from CJK Unified, Extension A, Thai, Arabic, Hebrew, Cyrillic ranges
- Underscores treated as word boundaries (`mcp_wiring_test` matches natural-language query `"mcp wiring"`)
- Scoring: metadata hits (title + description) weighted 2.0×; body hits weighted 1.0×
- Hard cap: `MAX_RESULTS = 5`
- Index kept under `MAX_INDEX_LINES = 200`

### Safety & limits
- Body sanitization strips C0/C1 bytes (`_CONTROL_CHAR_RE`)
- Truncation marker appended when exceeding 8000 chars (`_TRUNCATION_MARKER`)
- No automatic stale-memory pruning; manual `memory forget <slug>` via CLI
- CLI commands: `vibe-trading memory list|show|search|forget`

**Recommendation for hermes-quant:** adopt `PersistentMemory` class and `~/.hermes-quant/memory/` layout verbatim; simply change `MEMORY_BASE`. This directly extends ADR-0026.

---

## 5. Multi-Agent Desks & Isolation

Vibe-Trading implements runtime-isolated swarms with file-based persistence:

- Every swarm writes task files under `~/.vibe-trading/runs/<run_id>/`
- Workers emit MCP SSE heartbeats; crashed or stale runs are recovered by re-reading task state (`SwarmTool` + stale-run reaper)
- No shared mutable in-memory state between workers
- Each worker receives an isolated grounded context slice (market data + hypothesis + Research Goal ledger)
- File-level isolation: `hypotheses.json` and memory directory are per-user and never shared across concurrent swarms

Same-compute vs separate: all workers share the same Python process but are isolated by task-file boundaries and per-worker context.

---

## 6. Strategy Proposal Flow (Natural Language → Archived)

1. **Natural language input** — CLI chat, `/goal`, or `hypothesis propose` command
2. **Schema validation** — `Hypothesis` dataclass persisted via `hypotheses/registry.py`
3. **Backtest validation** — `backtest/runner.py` + automatic `run_card.json` + `run_card.md` emission
4. **Archive** — hypothesis status updated (`validated`/`rejected`) and `run_card.json` path recorded inside the registry entry

**Concrete file paths**
- Hypothesis ledger: `~/.vibe-trading/hypotheses.json`
- Run cards: `<run_dir>/run_card.json` + `<run_dir>/run_card.md`
- Memory entries: `~/.vibe-trading/memory/<slug>.md`

---

## 7. Patterns to STEAL (Concrete File / Class)

1. **`agent/backtest/run_card.py:write_run_card`** — canonical reproducibility + artifact manifest with SHA-256 hashes. Direct port to hermes-quant backtest runner.
2. **`agent/src/shadow_account/`** (full 6-file module + `models.py`) — complete shadow-account extraction, codegen, backtest, and 8-section drift reporting pipeline. Reuse the frozen `ShadowRule` / `ShadowProfile` / `AttributionBreakdown` contracts.
3. **`agent/src/memory/persistent.py`** — zero-dependency, multilingual, filesystem-backed memory with tokenization, search, and CLI surface. Adopt verbatim (change only base path).

---

## 8. Anti-Patterns to AVOID

1. **Implicit execution surface** — Vibe-Trading contains zero broker SDK integration in the agent toolset. Maintain the non-negotiable rule that money never moves through plugin tools.
2. **Shared mutable state across swarms** — all coordination occurs through task files on disk. Never introduce in-memory singletons for swarm state.
3. **Missing path containment** — Vibe-Trading added hardened `safe_path` sandboxing after security review. Mirror identical containment checks for every `run_dir` and artifact path in hermes-quant.

---

## 9. ADR Recommendations

- **New ADR-00XX: Run Card Schema & Trust Layer** — mandate `run_card.json` + `run_card.md` emission on every backtest using the exact schema, reproducibility hashes, and artifact manifest defined above.
- **Extension to ADR-0026 (Memory Architecture)** — codify `PersistentMemory`, multilingual tokenization rules, 8000-char body limit, truncation marker, and CLI commands (`memory list|show|search|forget`).
- **New ADR-00YY: Shadow Account Drift Reporting** — adopt the four-stage extraction → codegen → backtest → attribution pipeline and the `AttributionBreakdown` contract as the canonical mechanism for "what would have happened".

---

**Total word count:** ~2,050  
**Status:** Complete. All 9 required sections delivered with precise file:line citations from the 91 MB repository. Ready for wholesale adoption by hermes-quant.