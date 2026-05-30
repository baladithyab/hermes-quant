# Wave C — Observability + UX Gaps (implementation-ready plan)

> Date: 2026-05-30. Author: deep-work loop (architect pass).
> Scope: 6 backlog items — **G3, G10, G11, G15, B11, B38**.
> Posture (AGENTS.md, non-negotiable): money-software. Silence-by-default.
> The deterministic risk gate (ADR-0004) is the FINAL authority; nothing here
> touches the gate, sizing ladder, or order path. **Every item is additive /
> new-module / read-only observability or a default-OFF wire.** No item widens
> the discrete action space, mutates the gate, or makes the LLM an authority.
>
> All times UTC. `asof` = publication/decision time. No look-ahead.

---

## Posture summary per item

| ID | Item | Surface | Money-path risk | Flag |
|----|------|---------|-----------------|------|
| G3  | Markdown render over `decisions.jsonl` | read-only renderer | none (read-only) | n/a (renderer is pure) |
| G10 | Per-schema `render_X()` helpers | pure formatters | none | n/a |
| G11 | `bind_structured`/`invoke_structured_or_freetext` consolidation | refactor + tests | none (LLM is evidence only) | existing `_TRADER_LLM` etc. |
| G15 | same-ticker-rich vs cross-ticker-lean retriever split | render verbosity | none (read-only, behind `MEMORY_INJECT`) | `HERMES_QUANT_MEMORY_INJECT` |
| B11 | Calibrator drift detection | weekly cron + alert | low (read-only; never mutates a live calibrator unattended) | `HERMES_QUANT_CALIBRATOR_AUTO_REFIT` |
| B38 | IC dedup gate at factor ingestion | wire existing gate into `AlphaZoo.register` | low (research-only registry) | `HERMES_QUANT_IC_DEDUP_AT_INGEST` |

**Important pre-existing-state note (verified by code read, not docs):**
- **G11 is ~90% already shipped.** `hermes_quant/agents/structured_output.py`
  (ADR-0044) already exports `bind_structured` and
  `invoke_structured_or_freetext`. The real remaining gap is (a) **no test file
  exists** for `structured_output.py`, and (b) `LLMCaller.call`
  (`agents/llm_caller.py:210-243`) **reimplements** the parse/fallback logic
  inline instead of delegating to `invoke_structured_or_freetext` — two parse
  paths that can drift. Wave C closes the drift + adds the missing tests.
- **G15's BM25 split already exists** (`retriever.get_past_context` returns
  `same_ticker` / `cross_ticker` / `cross_sector` buckets). The remaining gap is
  that `format_context_block` renders all three buckets **identically verbose**.
  "Rich vs lean" is a *rendering* asymmetry, not a retrieval one.
- **B38's gate already exists** (`factors/ic_dedup.py:ICDedupGate`) but is **not
  called by `AlphaZoo.register`** — that method runs only the AST-purity and
  lookahead gates (`factors/alpha_zoo.py:270-271`). Wave C wires it in,
  default-OFF.

Run all tests with:
`~/.hermes/hermes-agent/venv/bin/python3 -m pytest tests/ -q`

---

## G3 — Markdown render layer over `decisions.jsonl`

**Goal.** A pure function that materializes the append-only decision log
(`~/.hermes/quant/memory/decisions.jsonl`) into operator-readable markdown,
folding the `decision`/`resolution` event chain into one block per decision.
Read-only; no writes; no mutation of the log.

### Files to touch

| File | Action |
|------|--------|
| `hermes_quant/memory/decisions_render.py` | **NEW** — the renderer |
| `hermes_quant/memory/__init__.py` | export `render_decisions_md`, `render_decision_block` |
| `tests/memory/test_decisions_render.py` | **NEW** — unit tests |

### Existing surface to build on (verified)
- `hermes_quant/memory/decisions.py`:
  - `DecisionLog.read_all()` → `Iterator[dict]` (decisions + resolutions)
  - `DecisionLog.read_pending()` → decision rows with no resolution
  - `DecisionLog.read_resolved()` → `(decision_row, resolution_row)` pairs
  - decision row keys: `decision_id, asof_decision, ticker, asset_class, rating,
    direction, confidence, target_position_pct, thesis_summary,
    thesis_evidence_ids, signal_provenance, research_plan_text, trader_proposal,
    risk_debate_summary, state, resolution`
  - resolution row keys: `decision_id, reflection_id, asof_resolution`
- Follow the existing renderer convention in `journal/render.py` (pure function
  of a typed object, machine-readable meta + human narrative). Reuse the
  direction-arrow idiom `{1:"↑", -1:"↓", 0:"→"}`.

### Signatures

```python
# hermes_quant/memory/decisions_render.py
from __future__ import annotations
from pathlib import Path
from typing import Any
from hermes_quant.memory.decisions import DecisionLog

DECISIONS_RENDER_HEADER = "# hermes-quant decision log\n\n..."  # static preamble

def render_decision_block(
    decision_row: dict[str, Any],
    resolution_row: dict[str, Any] | None = None,
) -> str:
    """Render ONE decision (+ optional resolution) to a markdown block.

    Heading:  `## <TICKER> <arrow> <rating> [pending|resolved]`
    Meta:     decision_id, asof_decision, asset_class, direction, confidence,
              target_position_pct, state, (resolution: reflection_id,
              asof_resolution when present).
    Narrative: thesis_summary; risk_debate_summary if present;
               evidence_ids as a comma list.
    Pure; never raises on missing optional keys (uses .get with defaults).
    """

def render_decisions_md(
    *,
    path: Path | None = None,
    log: DecisionLog | None = None,
    limit: int | None = None,          # most-recent N decisions (by asof_decision)
    state_filter: str | None = None,   # "pending" | "resolved" | None=all
) -> str:
    """Materialize the whole decisions.jsonl into markdown.

    Folds the event chain: each decision_id appears once, annotated with its
    resolution if one exists. Ordered newest-first. `path`/`log` are mutually
    exclusive test seams; default reads DecisionLog() at the canonical path.
    Empty log → header + "_(no decisions recorded)_".
    """
```

### Acceptance criteria (pytest-verifiable)
`tests/memory/test_decisions_render.py`:
1. `test_empty_log_renders_placeholder` — empty `DecisionLog(tmp)` →
   `render_decisions_md(log=...)` contains `"no decisions recorded"` and does
   NOT raise.
2. `test_pending_decision_block_has_arrow_and_meta` — record one pending
   decision (`direction=1, rating="Buy"`); block contains `"## "`, `"↑"`,
   `"Buy"`, the `decision_id`, and `target_position_pct`.
3. `test_resolved_decision_folds_resolution` — record decision then
   `record_resolution(dec_id, "refl_x")`; rendered block for that `dec_id`
   shows `reflection_id: refl_x` and is tagged resolved (exactly one block for
   the id, not two).
4. `test_state_filter_pending_only` — two decisions, one resolved; with
   `state_filter="pending"` only the unresolved `decision_id` appears.
5. `test_limit_returns_most_recent` — 3 decisions with increasing
   `asof_decision`; `limit=1` returns only the newest `ticker`.
6. `test_render_is_pure_no_writes` — capture file mtime + byte-size of the
   jsonl before/after rendering; assert unchanged (read-only invariant).
7. `test_missing_optional_keys_tolerated` — hand-write a minimal decision row
   missing `risk_debate_summary`/`trader_proposal`; renderer returns a block
   without raising.

---

## G10 — Per-schema `render_X(schema_obj) -> str` markdown helpers

**Goal.** Replace ad-hoc f-string formatting of the LLM-stage Pydantic objects
with one canonical pure renderer per schema, mirroring `journal/render.py`'s
"markdown is a function of the typed object" discipline (ADR-0010 §8).

### Files to touch

| File | Action |
|------|--------|
| `hermes_quant/agents/schema_render.py` | **NEW** — the `render_*` helpers |
| `tests/agents/test_schema_render.py` | **NEW** — unit tests |

(Keep the helpers in a single module so the operator/brief layer imports one
place. Do NOT scatter `render_*` into each schema module — that re-introduces
the ad-hoc drift this item targets.)

### Render targets (verified schemas)
| Schema | Module | Key fields to render |
|--------|--------|----------------------|
| `TraderProposal` | `agents/trader.py:122` | action, size_fraction, entry_price, stop_loss, target_price, time_horizon_days, confidence, rationale, warning_message |
| `ResearchPlan` | `agents/research_debate/schemas.py:81` | recommendation (PortfolioRating), confidence, rationale, strategic_actions, horizon_emphasis |
| `RiskDebateSummary` | `agents/risk_committee/committee.py:103` | trader_proposal_id, silence_multiplier, final_recommendation, n_rounds, terminated_reason, per-turn (persona/risk_assessment/confidence) |
| `PortfolioDecision` | `aggregators/llm_committee.py:115` | action, size_multiplier, confidence, vetoed, veto_source, rationale |

### Signatures

```python
# hermes_quant/agents/schema_render.py
from __future__ import annotations
from hermes_quant.agents.trader import TraderProposal
from hermes_quant.agents.research_debate.schemas import ResearchPlan
from hermes_quant.agents.risk_committee.committee import RiskDebateSummary
from hermes_quant.aggregators.llm_committee import PortfolioDecision

def render_trader_proposal(p: TraderProposal) -> str: ...
def render_research_plan(p: ResearchPlan) -> str: ...
def render_risk_debate_summary(s: RiskDebateSummary) -> str: ...
def render_portfolio_decision(d: PortfolioDecision) -> str: ...

# Dispatch convenience used by the brief layer:
def render_schema(obj) -> str:
    """Dispatch on isinstance to the matching render_* helper.
    Raises TypeError for an unregistered schema type (fail-loud here is
    correct: a brief that silently drops a stage's output is worse than a
    crash in the offline render path)."""
```

Each `render_*` must be **pure**, deterministic, and round-trip-safe in the
sense that every load-bearing field appears verbatim. Use a markdown bullet
block; reuse the `{1:"↑",-1:"↓",0:"→"}` arrow only where a signed direction
exists. Sizing/confidence floats render with explicit precision
(`:+.2%` / `:.2f`) so the operator sees the discrete-ladder value exactly.

### Acceptance criteria (pytest-verifiable)
`tests/agents/test_schema_render.py`:
1. `test_render_trader_proposal_contains_size_and_action` — build a
   `TraderProposal(action=BUY, size_fraction=0.10, confidence=0.7, rationale=...)`;
   output contains `"BUY"`, `"10.00%"` (or `0.10`), `"0.70"`.
2. `test_render_trader_proposal_surfaces_warning` — when
   `warning_message="fallback"` is set, it appears in the output.
3. `test_render_research_plan_shows_rating` — `recommendation=PortfolioRating.SELL`
   renders `"SELL"` and the confidence.
4. `test_render_risk_summary_lists_turns_and_multiplier` — a summary with 2
   turns renders `silence_multiplier`, both `persona` strings, and
   `final_recommendation`.
5. `test_render_portfolio_decision_veto` — `vetoed=True, veto_source="risk"`
   surfaces `"vetoed"` and `"risk"`.
6. `test_render_schema_dispatch_each_type` — `render_schema(obj)` returns the
   same string as the direct `render_*` for all four schema types.
7. `test_render_schema_unknown_type_raises` — `render_schema(object())` raises
   `TypeError`.
8. `test_all_renderers_pure` — calling each renderer twice on the same object
   returns byte-identical strings (determinism).

---

## G11 — `bind_structured` / `invoke_structured_or_freetext` consolidation + tests

**Goal.** Close the *drift* gap, not re-build the helper. The provider-aware
helpers already exist (`agents/structured_output.py`, ADR-0044). Wave C:
(1) add the missing test file; (2) make `LLMCaller.call(schema=...)` delegate
its parse/fallback to `invoke_structured_or_freetext`'s parse helpers so there
is ONE structured-output code path, not two.

### Files to touch

| File | Action |
|------|--------|
| `tests/agents/test_structured_output.py` | **NEW** — the missing test file |
| `hermes_quant/agents/llm_caller.py` | refactor `call()` parse block to reuse a single shared parser (no behavior change on the happy path) |

### Current duplication (verified)
- `structured_output.py` has `_parse_response` + `_parse_freetext_json`
  (structured-first, then fenced/embedded JSON fallback).
- `llm_caller.py:232-243` *imports those same private helpers* and re-sequences
  them inline. The risk: a future fix to the parse ladder in one place silently
  diverges from the other. Consolidate by exposing a single public
  `parse_structured_or_freetext(raw_text, raw_response, schema, model_id)` in
  `structured_output.py` and calling it from both `invoke_structured_or_freetext`
  and `LLMCaller.call`.

### Signature (new public, additive)

```python
# hermes_quant/agents/structured_output.py  (additive)
def parse_structured_or_freetext(
    raw_text: str,
    raw_response: dict[str, Any],
    schema: Type[T],
    model_id: str,
) -> Optional[T]:
    """The single canonical parse ladder: provider-native parse →
    free-text JSON fallback. Returns the validated schema instance or None.
    `invoke_structured_or_freetext` and `LLMCaller.call` BOTH route through
    this so the two structured-output entry points cannot drift."""
```

`LLMCaller.call` keeps its public signature and silence-by-default contract
(never raises; returns `(None, raw)` on failure). Only the internal parse block
changes to call `parse_structured_or_freetext`.

### Acceptance criteria (pytest-verifiable)
`tests/agents/test_structured_output.py` (no network — `client` is a mock/dict):
1. `test_detect_provider_routing` — `_detect_provider` returns
   `openai`/`google`/`anthropic`/`unknown` for `openai/gpt-4o`,
   `google/gemini-2.0-flash`, `anthropic/claude-3-5-haiku`, `xai/grok-3`
   (→openai), and a bare `"mystery-model"` (→unknown).
2. `test_bind_structured_openai_shape` — returns a dict with
   `response_format.type == "json_schema"` and the schema name.
3. `test_bind_structured_anthropic_tool_choice_any` — returns `tools` +
   `tool_choice == {"type":"any"}`.
4. `test_bind_structured_google_response_schema` — returns `response_schema` +
   `response_mime_type == "application/json"`.
5. `test_bind_structured_unknown_empty` — unknown provider → `{}`.
6. `test_invoke_happy_path_callable_client` — a callable client returning a
   valid JSON string for `TraderProposal` yields `(obj, raw)` with
   `obj.action` correct.
7. `test_invoke_freetext_fallback_fenced` — client returns a ```json fenced```
   block; helper still parses to the schema.
8. `test_invoke_validation_failure_returns_none` — client returns malformed
   JSON; helper returns `(None, raw)` and does NOT raise.
9. `test_invoke_client_exception_graceful` — client raises; helper returns
   `(None, {"error": ...})`.
10. `test_parse_helper_shared_by_caller_and_invoke` — feed the same malformed-
    then-fenced raw text through `parse_structured_or_freetext` and assert it
    matches what `invoke_structured_or_freetext` produces (single-path proof).

`tests/agents/test_llm_caller.py` (extend or new):
11. `test_caller_uses_shared_parser` — monkeypatch `_http_post` to return a
    fenced-JSON body; `LLMCaller(...).call(..., schema=TraderProposal)`
    returns a parsed `TraderProposal` via the consolidated path.
12. `test_caller_no_api_key_silent` — with `OPENROUTER_API_KEY` unset,
    `call(...)` returns `(None, {"error": "no_api_key..."})` and never raises.

---

## G15 — same-ticker-rich vs cross-ticker-lean retriever render split

**Goal.** Differentiate render verbosity: same-ticker history gets the **full**
reflection narrative + return/holding facts; cross-ticker and cross-sector
analogs get a **lean** one-line summary (date|ticker|rating|alpha). This keeps
the most-relevant (same-ticker) context rich while bounding token spend on
weaker analogs. Retrieval split already exists; this is a render change.

### Files to touch

| File | Action |
|------|--------|
| `hermes_quant/memory/retriever.py` | add `format_context_block_split(...)`; keep `format_context_block` unchanged for back-compat |
| `tests/memory/test_retriever.py` | extend with split-render tests (or `tests/memory/test_retriever_split.py` NEW) |

### Existing surface (verified, `retriever.py`)
- `get_past_context(...) -> PastContext` already returns `.same_ticker`,
  `.cross_ticker`, `.cross_sector` (each `list[ResolvedDecision]`) + `.aggregate_stats`.
- `ResolvedDecision` fields: `reflection_id, decision_id, asof, tau_observable,
  ticker, rating, raw_return, alpha_return, holding_days, lesson,
  lesson_category, outcome_quality`.
- Current `format_context_block` renders ALL three buckets with the same two
  lines (fact line + full `lesson`). The Oracle-Fallacy guard
  (`tau_observable < asof`) and BM25 split are upstream and unchanged.

### Signature (additive)

```python
# hermes_quant/memory/retriever.py  (additive — do NOT change format_context_block)
def format_context_block_split(
    ctx: PastContext,
    *,
    max_chars: int = 2048,
    rich_lesson_chars: int = 400,   # same-ticker lessons truncated to this
    lean: bool = True,              # cross-* buckets get the one-line summary
) -> str:
    """Render PastContext with asymmetric verbosity.

    same_ticker  → RICH:  fact line + full lesson (truncated to rich_lesson_chars)
                          + lesson_category + outcome_quality.
    cross_ticker → LEAN:  single line `[date|TICKER|RATING|+alpha%]` (no lesson).
    cross_sector → LEAN:  same one-line form.
    Section order unchanged (same → cross-ticker → cross-sector). Empty → "(none)".
    Total output clipped to max_chars (same trailing-"..." rule as the original)."""
```

### Acceptance criteria (pytest-verifiable)
`tests/memory/test_retriever_split.py`:
1. `test_same_ticker_is_rich` — a `PastContext` with one same-ticker
   `ResolvedDecision` whose `lesson="LESSON_MARKER ..."`; split render contains
   `LESSON_MARKER` and the `lesson_category`.
2. `test_cross_ticker_is_lean` — a cross-ticker `ResolvedDecision` whose
   `lesson="SHOULD_NOT_APPEAR"`; split render does NOT contain
   `SHOULD_NOT_APPEAR` but DOES contain that row's `ticker` and alpha.
3. `test_rich_lesson_truncated` — same-ticker `lesson` of 1000 chars with
   `rich_lesson_chars=400` → the rendered lesson is ≤ ~403 chars (incl. "...").
4. `test_section_order_and_headers` — with all three buckets populated, the
   same-ticker header index < cross-ticker header index < cross-sector header
   index in the output string.
5. `test_empty_context_none` — empty `PastContext` → `"(none)"`.
6. `test_max_chars_clip` — `max_chars=50` → `len(out) <= 50`.
7. `test_original_format_unchanged` — `format_context_block` output is byte-
   identical to the pre-change golden for a fixed `PastContext` (back-compat
   guard; the existing `llm_committee.py:310` caller is untouched).

> Wiring note (out of scope for this PR, leave a TODO): switching
> `llm_committee.py:310` from `format_context_block` to the split variant is a
> behavior change to a gated path (`HERMES_QUANT_MEMORY_INJECT`). Do it in a
> follow-up once the render is test-locked; this PR ships the renderer + tests
> only.

---

## B11 — Calibrator drift detection (weekly auto-refit + >5% raw→calibrated alert)

**Goal.** Detect when the live `IsotonicCalibrator` has drifted from realized
outcomes and (a) emit an alert when the mean |raw → calibrated| gap exceeds a
threshold (default 5%), and (b) optionally auto-refit weekly. **Default-OFF
auto-refit** — detection/alert is always safe (read-only); the refit that
swaps the live calibrator pickle is gated behind a flag because it changes what
the risk gate sees.

### Files to touch

| File | Action |
|------|--------|
| `hermes_quant/training/calibrator_drift.py` | **NEW** — drift metric + decision |
| `ops/scripts/quant-calibrator-drift.py` | **NEW** — weekly cron wrapper |
| `tests/unit/test_calibrator_drift.py` | **NEW** — unit tests |

### Existing surface (verified)
- `hermes_quant/calibrators.py`: `IsotonicCalibrator` (`.calibrate(raw)`,
  `.fit(raw, correct)`, `.status()`, `.is_calibrated`, `.n_samples`),
  `ColdStartCalibrator`, `IdentityCalibrator`.
- `hermes_quant/training/bootstrap_calibrator.py`: `bootstrap_calibrator(...)`
  returns `{n_samples, fitted, output_path, symbols_processed, analyst_breakdown}`
  and atomic-pickles to `DEFAULT_CALIBRATOR_PATH =
  ~/.hermes/quant/calibrators/isotonic.pkl`. **Reuse `_atomic_pickle` and the
  walk-forward pair collection — do not duplicate the Alpaca fetch loop.**
- `calibrators.py` module docstring already declares the intended drift metric:
  "comparing fitted calibrator's E[direction_correct | calibrated] against the
  calibrated probability — surfaced in quant_doctor." Implement exactly that.
- Alert sink: reuse the audit-log append pattern
  (`agents/llm_caller.py:_audit_append`) writing `kind="calibrator_drift"`, OR
  write a one-line JSONL to `~/.hermes/quant/calibrators/drift-log.jsonl`. Use
  the JSONL drift-log (simpler, append-only, testable with tmp_path).

### Drift metric (deterministic, no network in the metric itself)
Given paired `(raw_scores, direction_correct)` over a recent window:
- For each sample compute `calibrated_i = calibrator.calibrate(raw_i)`.
- `realized = mean(direction_correct)` (the empirical hit rate).
- `predicted = mean(calibrated_i)` (what the calibrator claims).
- `drift = abs(predicted - realized)`.
- Drift exceeds threshold ⇒ `should_alert = drift > threshold` (default 0.05).
- This is the population-level ECE-style gap the docstring already prescribes;
  bucketed-ECE is a stretch goal, NOT required for acceptance.

### Signatures

```python
# hermes_quant/training/calibrator_drift.py
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

DRIFT_LOG_PATH = Path.home() / ".hermes" / "quant" / "calibrators" / "drift-log.jsonl"
_DEFAULT_DRIFT_THRESHOLD = 0.05  # 5% raw→calibrated vs realized

@dataclass(frozen=True)
class DriftResult:
    drift: float                 # abs(predicted_mean - realized_mean)
    predicted_mean: float
    realized_mean: float
    n_samples: int
    threshold: float
    should_alert: bool
    refit_recommended: bool      # should_alert and n_samples >= min_refit_samples
    reason: str

def compute_drift(
    calibrator: Any,                       # any obj with .calibrate(float)->float
    raw_scores: Sequence[float],
    direction_correct: Sequence[bool],
    *,
    threshold: float = _DEFAULT_DRIFT_THRESHOLD,
    min_refit_samples: int = 200,          # mirrors IsotonicCalibrator.n_min_samples
) -> DriftResult:
    """Pure metric. n_samples==0 → drift=0.0, should_alert=False,
    reason='no_samples' (silence-by-default: no data ⇒ no alarm)."""

def append_drift_log(result: DriftResult, *, path: Path | None = None) -> None:
    """Atomic append one JSON row (schema_version=1, asof=UTC now, all
    DriftResult fields). Never raises (logs at WARNING on failure)."""

def run_drift_check(
    *,
    calibrator_path: Path | None = None,    # default DEFAULT_CALIBRATOR_PATH
    pairs: tuple[Sequence[float], Sequence[bool]] | None = None,  # test seam
    auto_refit: bool = False,               # gated by env in the cron
    threshold: float = _DEFAULT_DRIFT_THRESHOLD,
    refit_kwargs: dict[str, Any] | None = None,
) -> DriftResult:
    """Load the live calibrator, compute drift over `pairs`, append the drift
    log. If auto_refit AND refit_recommended: call bootstrap_calibrator(**
    refit_kwargs) to re-fit + atomic-replace the pickle, and record the refit
    in the drift log. When auto_refit is False, NEVER touches the live pickle."""
```

### Cron wrapper (`ops/scripts/quant-calibrator-drift.py`)
- Weekly schedule (suggest `0 7 * * 1` — Monday 07:00 UTC).
- Reads recent `(raw, correct)` pairs by replaying the bootstrap walk over the
  configured universe (reuse `bootstrap_calibrator._walk_bars_for_symbol`).
- `auto_refit = os.environ.get("HERMES_QUANT_CALIBRATOR_AUTO_REFIT") == "1"`
  (DEFAULT-OFF). When off, the cron only alerts.
- Read-only against Alpaca, paper=True, silence-by-default (any exception →
  log + exit 0; never crash the cron).

### Acceptance criteria (pytest-verifiable)
`tests/unit/test_calibrator_drift.py` (no network — use `IdentityCalibrator`
and synthetic pairs; for refit path pass `pairs=` + monkeypatch
`bootstrap_calibrator`):
1. `test_no_samples_no_alert` — `compute_drift(IdentityCalibrator(), [], [])`
   → `drift==0.0`, `should_alert is False`, `reason=="no_samples"`.
2. `test_well_calibrated_no_alert` — `IdentityCalibrator`, raw all `0.6`,
   `direction_correct` 60% True ⇒ `predicted≈0.6`, `realized≈0.6`,
   `drift<0.05`, `should_alert is False`.
3. `test_drift_above_threshold_alerts` — raw all `0.9`, realized hit-rate
   `0.5` ⇒ `drift≈0.4 > 0.05`, `should_alert is True`.
4. `test_refit_recommended_requires_min_samples` — drift high but
   `n_samples < min_refit_samples` ⇒ `refit_recommended is False`.
5. `test_custom_threshold_honored` — same data, `threshold=0.5` ⇒
   `should_alert is False`.
6. `test_append_drift_log_writes_one_row` — `append_drift_log(result,
   path=tmp)` ⇒ file has exactly 1 JSON line with `schema_version`, `drift`,
   `should_alert`, `asof`.
7. `test_run_drift_check_no_refit_when_flag_off` — `auto_refit=False`,
   monkeypatch `bootstrap_calibrator` to a sentinel that asserts-not-called;
   `run_drift_check(pairs=(highdrift), auto_refit=False)` does NOT call it and
   the live pickle path is untouched.
8. `test_run_drift_check_refit_when_recommended` — `auto_refit=True` + high
   drift + `n_samples>=200`; monkeypatched `bootstrap_calibrator` IS called
   once and the drift log records `refit=true`.
9. `test_compute_drift_pure_deterministic` — two identical calls return equal
   `DriftResult`.

---

## B38 — IC dedup gate at factor ingestion (ICmax ≥ 0.99 → discard)

**Goal.** Wire the existing `ICDedupGate` into the factor-ingestion path so a
near-duplicate factor (max |correlation| ≥ threshold, default 0.99) is rejected
at `AlphaZoo.register`, not silently bloating the library (F4 "Correlation Red
Sea"). **Default-OFF** behind `HERMES_QUANT_IC_DEDUP_AT_INGEST=1` so existing
register behavior is bit-identical until the operator opts in — and because the
gate needs a *returns series* per factor, which the current `register(factor)`
signature does not carry.

### Files to touch

| File | Action |
|------|--------|
| `hermes_quant/factors/alpha_zoo.py` | add optional IC-dedup hook to `register`; new `RedundantFactorError` |
| `tests/factors/test_alpha_zoo_ic_dedup.py` | **NEW** — wiring tests |
| (no change to `factors/ic_dedup.py` — reused as-is) | — |

### Existing surface (verified)
- `factors/alpha_zoo.py:253 register(self, factor: AlphaFactor) -> str` runs
  `_run_purity_gate` then `_run_lookahead_gate`, then appends. **No IC gate.**
- `factors/ic_dedup.py:ICDedupGate.check(new_factor_returns, existing_library=,
  threshold=) -> ICDedupResult(passes, max_corr, correlated_with, reason)` and
  `.register(name, returns)`. Threshold default from
  `HERMES_QUANT_IC_DEDUP_THRESHOLD` (already wired, 0.99).
- `factors/ic_metrics.py:factor_correlation(a, b)` is the Pearson kernel
  `ICDedupGate` uses.

### Design — keep `register` backward-compatible
Add an OPTIONAL `factor_returns` arg + an injected/owned `ICDedupGate`. The IC
gate only runs when (a) the env flag is on, (b) `factor_returns` is supplied.
Otherwise behavior is unchanged (silence-by-default for the new path; never
breaks the existing call-sites that pass no returns).

```python
# hermes_quant/factors/alpha_zoo.py  (additive)
class RedundantFactorError(RuntimeError):
    """Raised when IC-dedup rejects a factor as a near-duplicate."""
    def __init__(self, factor_id: str, result: "ICDedupResult") -> None: ...

class AlphaZoo:
    def __init__(self, base_dir=..., *, ic_dedup_gate: "ICDedupGate | None" = None) -> None:
        # Lazily own an ICDedupGate (its own threshold env) when none injected.
        ...

    def register(
        self,
        factor: AlphaFactor,
        *,
        factor_returns: "np.ndarray | None" = None,
    ) -> str:
        """...existing purity + lookahead gates...
        IC-dedup gate (NEW, default-OFF):
          if HERMES_QUANT_IC_DEDUP_AT_INGEST == "1" and factor_returns is not None:
              result = self._ic_gate.check(factor_returns)
              if not result.passes:
                  raise RedundantFactorError(factor.factor_id, result)
              # on pass: register the returns into the gate's library AFTER the
              # JSONL append succeeds, so a rejected factor never pollutes either
              # store and the gate library tracks the persisted library.
        """
```

Env flag read at call time (not import time) so tests can monkeypatch
`os.environ` per-test.

### Acceptance criteria (pytest-verifiable)
`tests/factors/test_alpha_zoo_ic_dedup.py` (use `HERMES_QUANT_ALPHA_ZOO_DIR` →
tmp via monkeypatch, and a clean-source factor like
`source_code='bars["close"] - bars["open"]'`):
1. `test_flag_off_no_dedup` — flag unset, register two factors with identical
   `factor_returns`; both register successfully (back-compat: no IC gate).
2. `test_flag_off_no_returns_unchanged` — flag set but `factor_returns=None`;
   register succeeds (gate is a no-op without a returns series).
3. `test_flag_on_rejects_near_duplicate` — flag `=1`; register factor A with a
   returns array, then register factor B whose returns are A + 1e-9 noise with
   the same flag ⇒ raises `RedundantFactorError`; `err.result.max_corr >= 0.99`;
   `err.result.correlated_with` is A's name.
4. `test_flag_on_accepts_orthogonal` — flag `=1`; A then an independent random
   B both register; library length == 2.
5. `test_rejected_factor_not_persisted` — after a `RedundantFactorError`, the
   `alpha_zoo.jsonl` line count is unchanged (gate runs BEFORE append) and the
   rejected `factor_id` is not in `list_all()`.
6. `test_threshold_env_respected` — with `HERMES_QUANT_IC_DEDUP_THRESHOLD=0.5`
   and flag on, a moderately-correlated B (corr≈0.6) is rejected.
7. `test_purity_gate_still_runs_first` — a factor with forbidden source still
   raises `PurityViolation` regardless of the IC flag (gate ordering preserved).
8. `test_injected_gate_used` — pass `AlphaZoo(ic_dedup_gate=my_gate)`; after a
   successful flagged register, `my_gate` library contains the factor name
   (proves the injected gate, not a private one, is used).

---

## Cross-cutting verification

After implementing all six:

```bash
~/.hermes/hermes-agent/venv/bin/python3 -m pytest \
  tests/memory/test_decisions_render.py \
  tests/agents/test_schema_render.py \
  tests/agents/test_structured_output.py \
  tests/agents/test_llm_caller.py \
  tests/memory/test_retriever_split.py \
  tests/unit/test_calibrator_drift.py \
  tests/factors/test_alpha_zoo_ic_dedup.py -q

# Full suite + lint + types (no regressions)
~/.hermes/hermes-agent/venv/bin/python3 -m pytest tests/ -q
~/.hermes/hermes-agent/venv/bin/python3 -m ruff check hermes_quant/ tests/
~/.hermes/hermes-agent/venv/bin/python3 -m mypy hermes_quant/
```

Invariants that MUST still hold (regression guards):
- `format_context_block` output unchanged (G15 back-compat test).
- `AlphaZoo.register(factor)` (no returns, flag off) unchanged (B38 test 1–2).
- `LLMCaller.call` never raises and is silence-by-default (G11 test 12).
- No new write to `decisions.jsonl` from the renderer (G3 test 6).
- `tests/test_no_lookahead.py` still passes (none of these touch analysts).

## Suggested PR slicing (independent, parallelizable)
- **PR-C1:** G3 + G10 (pure renderers; zero coupling) — smallest, lowest risk.
- **PR-C2:** G11 consolidation + tests (touches `llm_caller.py`; gated path).
- **PR-C3:** G15 split renderer + tests (memory; gated path).
- **PR-C4:** B11 calibrator drift module + cron + tests.
- **PR-C5:** B38 IC-dedup wire + tests.

Each ships with its acceptance tests green; PR-C1/C3/C4/C5 are independent and
can run in parallel. PR-C2 is independent too but touches a shared file
(`llm_caller.py`) so sequence it after PR-C1 to avoid a trivial merge.

## ADR note
None of these six require a new ADR: G3/G10/G15 are observability/render polish
under the existing ADR-0010/ADR-0042 contracts; G11 completes ADR-0044; B11
implements the drift detection already prescribed in ADR-0009 §P0-2's calibrator
docstring; B38 wires the gate prescribed by ADR-0050 / the F4 anti-pattern.
If the operator wants the B11 weekly auto-refit cron flipped ON by default, that
flip (not the build) warrants a one-paragraph ADR amendment (next free number:
ADR-0079) — but Wave C ships it DEFAULT-OFF, so no ADR is blocking.
```
