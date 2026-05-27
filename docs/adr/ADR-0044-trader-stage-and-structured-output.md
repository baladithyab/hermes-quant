# ADR-0044: Trader Stage & Structured Output (Wave 2)

**Status:** Accepted  
**Date:** 2026-05-27  
**Author:** ARIA (deep-work-loop on hermes-quant; based on TauricResearch/TradingAgents + Mai0313 fork research)  
**Implements:** Wave 2 — Decision contract + structured output (PROJECT-ROADMAP-2026-05-27.md)

---

## Context

The 2026-05-27 audit of `~/.hermes/quant/governance/audit_log.jsonl` found that
**every approval in the trailing 30-day window carries `target_position_pct=-0.20`** —
the literal surface signature of the default-sizing failure mode. When
`aggregated_signal.confidence` or `aggregated_signal.direction` is None (e.g. BMA
degeneracy or stale data), the risk gate silently fell back to a -0.20 position.

Two compounding root causes:

1. **No Trader intermediary stage.** The pipeline went directly from the BMA aggregator
   to the risk gate, skipping the step that should translate a 5-tier rating into a
   *concrete* entry price + stop-loss + position size. Without this step, `stop_loss`
   and `entry_price` were structurally impossible to audit.

2. **No provider-aware structured output.** Structured output for LLM-driven agents
   (future Wave 2 LLM trader + Wave 3 risk committee) required a binding helper that
   routes correctly to OpenAI json_schema / Gemini response_schema / Anthropic tool-use,
   with a graceful-fallback contract for refusals.

This ADR formalizes both additions.

---

## Decision

### 1. `TraderProposal` — Pydantic v2 schema (`hermes_quant/agents/trader.py`)

Every approval that passes the risk gate **MUST** carry a `TraderProposal` embedded at
`advisor_result['trader_proposal']`. The `TraderProposal` schema makes the following
fields mandatory (never None silently):

| Field | Type | Notes |
|---|---|---|
| `action` | `TraderAction` (BUY/HOLD/SELL) | str Enum — JSON-serializes without custom encoder |
| `size_fraction` | `float [0.0, 1.0]` | Fraction of available capital; unsigned (sign is in `action`) |
| `entry_price` | `Optional[float > 0]` | Current close or best estimate; None only if price data unavailable |
| `stop_loss` | `Optional[float > 0]` | Hard stop; BUY: stop < entry; SELL: stop > entry |
| `target_price` | `Optional[float > 0]` | 1R profit target; None if unavailable |
| `time_horizon_days` | `Optional[int [1, 365]]` | Holding period estimate |
| `confidence` | `float [0.0, 1.0]` | From ResearchPlan; never fabricated |
| `rationale` | `str [1, 2048]` | 2–4 sentences anchored in research plan |
| `warning_message` | `Optional[str]` | Non-None if graceful fallback was triggered |

Cross-field constraint: if both `entry_price` and `stop_loss` are present, stops must be
on the **losing side** — enforced by `model_validator(mode='after')`.

### 2. `TraderNode` v0.1 — deterministic mapping (`hermes_quant/agents/trader.py`)

TraderNode v0.1 is **fully deterministic** (no LLM calls). It maps a 5-tier research
recommendation to a `TraderProposal` using:

**Sizing ladder (v0.1)**

| Rating | size_fraction | action | horizon_days |
|---|---|---|---|
| Buy | 0.20 | BUY | 30 |
| Overweight | 0.10 | BUY | 21 |
| Hold | 0.00 | HOLD | 14 |
| Underweight | 0.10 | SELL | 21 |
| Sell | 0.20 | SELL | 30 |

**Stop placement (v0.1)**

```
ATR_abs = atr_relative × last_close       # atr_relative from microstructure analyst
stop = entry ± (2.0 × ATR_abs)            # BUY: entry − 2ATR, SELL: entry + 2ATR
target = entry ∓ (2.0 × ATR_abs)         # symmetric 1R target
```

`atr_relative` is fetched from `advisor_signal['metadata']['atr_relative']` — already
computed by `MicrostructureAnalyst.atr_relative()`.

**Graceful fallback**: if `research_plan.recommendation` is missing or invalid, or if
`research_plan.confidence` is None, TraderNode returns a conservative default:

```python
action=HOLD, size_fraction=0.05, confidence=0.50,
warning_message="TraderNode graceful fallback — <reason>"
```

This makes it **impossible** for `pos=None` to reach the risk gate silently.

### 3. `TraderNode` v0.2 — LLM-driven (deferred to Wave 3)

v0.2 will replace the deterministic ladder with a prompted `quick_model` call that:
- Receives the full `ResearchPlan` + 4 analyst report summaries
- Uses `bind_structured("openai/...", TraderProposal)` for native structured output
- Reasons about support/resistance levels, analyst-cited price targets, and entry timing
- Falls back to the v0.1 deterministic result if structured output is rejected

v0.2 is explicitly deferred. v0.1 ships in Wave 2 so the schema + audit trail contract
is in place before the LLM-driven logic arrives.

### 4. `bind_structured` + `invoke_structured_or_freetext` (`hermes_quant/agents/structured_output.py`)

Provider-aware helpers with the following routing table:

| model_id prefix | Structured output mechanism |
|---|---|
| `openai/*`, `xai/*` | `response_format={"type":"json_schema","json_schema":{...,"strict":True}}` |
| `google/*` | `response_schema={...}, response_mime_type="application/json"` |
| `anthropic/*` | `tools=[{"name":..., "input_schema":...}], tool_choice={"type":"any"}` |
| anything else | `{}` (free-text; caller does manual `json.loads()`) |

`invoke_structured_or_freetext(client, prompt, schema, model_id)` wraps the LLM call:
1. Tries provider-native structured output
2. On parse failure, tries `json.loads()` on raw text
3. Tries ```json ... ``` fenced-block extraction
4. On all failures: returns `(None, raw_response)` — **never raises**

Callers are expected to apply conservative defaults when `None` is returned.

### 5. Brief script hook (Wave 2)

The daily-interim brief script (`scripts/quant-daily-interim.py`) was updated to:

1. Call `TraderNode()(research_plan, advisor_signal)` for each actionable before
   persisting the Proposal.
2. Embed `trader_proposal` in `advisor_result` so the Proposal record (and thus the
   audit trail) carries the full struct.
3. Print per-actionable: `entry≈$X.XX | stop=$Y.YY | target=$Z.ZZ | horizon=Nd | size=X%`
4. Format `propose_id` with explicit `approve <proposal_id>` instruction.

The `approve <ticker>` shorthand continues to work as a convenience alias in the
Discord slash handler; `approve <proposal_id>` is the canonical form.

---

## Consequences

**Positive:**
- `pos=None` / `target_position_pct=None` failure mode is structurally eliminated.
  Every approval carries `stop_loss`, `entry_price`, `time_horizon_days` in its
  `advisor_result['trader_proposal']` field.
- The audit trail is now self-describing: an auditor reading `audit_log.jsonl` can
  reconstruct the sizing intent without re-running `recommend()`.
- Zero LLM cost increase for Wave 2 (v0.1 is deterministic).

**Neutral:**
- `hermes_quant/agents/` is a new package. No existing imports changed.
- `bind_structured` / `invoke_structured_or_freetext` are pure dict-construction
  utilities; they import no LLM SDK at module load time.

**Negative / trade-offs:**
- The v0.1 deterministic ladder ignores current volatility regime and support/resistance
  levels. Stop placement is a blunt 2×ATR rule. v0.2 must address this.
- `_extract_research_plan()` in the brief script reconstructs a research plan from
  `aggregated_signal.direction` when the LLM committee has not run. This is a lossy
  approximation; the real 5-tier recommendation is only available after the full
  committee run.

---

## Alternatives considered

**Alt A: Embed stop/entry directly in `AggregatedSignal`.**  
Rejected: `AggregatedSignal` is a deterministic aggregation schema; adding price-level
fields would conflate two concerns. The Trader stage is a separate semantic step.

**Alt B: Skip TraderNode entirely; derive stop from Kelly fraction.**  
Rejected: Kelly fraction is a position-size bound, not a price level. The audit trail
would still lack `stop_loss` and `entry_price`.

**Alt C: Ship v0.2 LLM Trader directly.**  
Rejected: LLM-driven Trader requires the full committee pipeline to have run (bull/bear
judge → research_plan). That is Wave 3. Shipping v0.1 first makes Wave 2 testable and
delivers the audit-trail benefit immediately.

---

## Test coverage

`tests/agents/test_trader.py` (80 tests):
- All `TraderProposal` field constraints
- Cross-field stop/entry validation for BUY, SELL, HOLD
- All 5-tier rating → size_fraction mappings
- Price-level derivation (entry, stop, target) with mock signal data
- Graceful fallback for missing/invalid inputs
- `bind_structured` provider routing for openai/xai/anthropic/google/unknown
- `invoke_structured_or_freetext` with mock clients: valid, invalid JSON, fenced JSON,
  Pydantic validation error, client exception, system message prepend
