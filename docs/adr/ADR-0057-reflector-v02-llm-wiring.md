# ADR-0057: Reflector v0.2 — LLM-Wired Structured Reflection

**Status:** Accepted  
**Date:** 2026-05-27  
**Related:** ADR-0042 (Persistent Memory & Reflection Layer), ADR-0054 (LLM-Caller Foundation & TraderNode v0.2)  
**Author:** ARIA (deep-work-loop on hermes-quant)

---

## Context

ADR-0042 (Wave 4) defined the three-layer memory system and specified that the Reflector (Layer 2) should use the **quick-tier LLM** (haiku-class, ADR-0037) to write `reflection_text` and classify `lesson_category`. The v0.1 implementation shipped with a deterministic stub-text formatter (no API calls, safe in CI) as a deliberate placeholder. The `llm_caller` injection point was already wired into `Reflector.__init__` but was only consumed as a plain `Callable[[str], str]`.

This ADR documents the v0.2 upgrade that:

1. Wires the `LLMCaller` structured-output path (ADR-0054) into the Reflector.
2. Returns a richer `reflection_text` and a LLM-classified `lesson_category` via a Pydantic I/O boundary schema (`ReflectionLLMOutput`).
3. Adds the **self-grade refusal invariant** as a load-bearing guard.
4. Preserves the **Oracle Fallacy guard** identically from v0.1.
5. Feature-flags the v0.2 path behind `HERMES_QUANT_REFLECTOR_LLM=1` (default OFF), matching ADR-0054 discipline.

---

## Decision

### 1. Structured I/O boundary: `ReflectionLLMOutput` Pydantic model

The `Reflection` dataclass is the persistence schema (append-only JSONL). The `ReflectionLLMOutput` Pydantic model is the **LLM I/O boundary only** — used to bind structured output and validate the response. It exposes exactly two fields:

```python
class ReflectionLLMOutput(BaseModel):
    reflection_text: str   # 2-4 sentence prose, no markdown
    lesson_category: str   # one of LessonCategory enum values
```

`tau_observable` is **intentionally absent** from `ReflectionLLMOutput`. This is the Oracle Fallacy guard: the LLM cannot influence the timestamp at which outcome knowledge is locked (see §5).

**Rationale for Pydantic wrapper approach** (vs. bypass): The `LLMCaller.call(schema=...)` interface from ADR-0054 expects a Pydantic `BaseModel` subclass for structured-output binding. The `Reflection` dataclass is the canonical persistence type but is not Pydantic. Rather than introducing Pydantic inheritance into `Reflection`, we define a thin `ReflectionLLMOutput` schema at the I/O boundary and convert its fields into the existing `Reflection` dataclass. This keeps the persistence layer free of LLM dependencies.

### 2. Path selection logic

`Reflector._reflect_with_llm(...)` is the v0.2 entry point. It returns `(reflection_text, lesson_category, prompt_hash)` on success or `None` to fall through to v0.1. Three gates must all be open:

| Gate | Condition | Fallback path_kind |
|------|-----------|--------------------|
| (a) Duck-typed LLMCaller | `_is_llm_caller_instance(self._llm_caller)` | (no audit; plain v0.1) |
| (b) Feature flag | `HERMES_QUANT_REFLECTOR_LLM=1` | `v01_stub_text` (reason: `feature_flag_off`) |
| (c) API key present | `self._llm_caller.available() == True` | `v01_stub_text` (reason: `llm_caller_not_available`) |

After all three gates: check the **self-grade refusal invariant** (see §4). On LLM call failure or `None` return: `v02_llm_fallback_to_v01`.

### 3. Prompting strategy

**System prompt** frames the model as a post-trade reviewer:

> "You are a post-trade reviewer with deep expertise in quantitative portfolio analysis. The decision below was made by a portfolio manager and has now resolved with the outcome shown. Your task: write exactly 2-4 sentences of plain prose covering whether the directional call was correct (cite alpha), which part of the thesis held or failed, and one concrete lesson. Classify into exactly one lesson category from the canonical enum. Return only the structured JSON — no surrounding commentary."

The system prompt enumerates all valid `LessonCategory` values to constrain the model without requiring an `enum` JSON schema validator (which some providers reject on strict schemas).

**User prompt** contains:
- A `DECISION` block: all decision dict fields except `schema_version` and `kind` (non-informative), serialised as JSON.
- An `OUTCOME` block: `raw_return`, `alpha_return`, `benchmark`, `holding_days`, `outcome_quality`.

**Prompt hash**: `"llm-v02:" + sha256(system+user)[:12]` — stored in `reflector_prompt_hash` for auditability. Distinguishable from v0.1 stub hash (`"stub:..."`) and v0.1 plain-Callable hash (`"llm:..."`).

### 4. Self-grade refusal invariant (ADR-0042 §anti-patterns)

> **Invariant**: The model that made the PM decision MUST NOT evaluate its own outcome.

**Implementation:**

```python
pm_model = decision.get("llm_committee_model_id", None)
reflector_model = getattr(self._llm_caller, "model_id", None)
if pm_model and reflector_model and pm_model == reflector_model:
    logger.warning("SELF-GRADE REFUSED — ...")
    _audit_reflector_call("v02_self_grade_refused", {...})
    return None  # fall through to v0.1 stub
```

**Why this matters:** A PM model grading its own trade introduces confirmation bias into the episodic memory layer. The Reflector sees the decision narrative, the alpha outcome, and has significant latitude in `lesson_category` assignment. If the same model writes both the original thesis and the reflection, it will systematically over-weight narrative consistency and under-weight genuine causal analysis.

**Canonical regression test** (in `tests/memory/test_reflector_llm_v02.py`):

```python
def test_self_grade_refused_when_pm_model_equals_reflector_model():
    shared_model_id = "anthropic/claude-sonnet-4-5"
    # decision was made by the same model as the reflector
    decision = _make_decision(llm_committee_model_id=shared_model_id)
    # ... reflector has same model_id ...
    # Assert: LLM call NOT made, audit has 'v02_self_grade_refused'
```

This test is designated **regression-resistant** per ADR-0042 §2.3. It MUST remain green on every merge to `main`.

### 5. Oracle Fallacy guard — preserved identically from v0.1

> **Invariant**: `tau_observable` is **always** computed by `_compute_tau_observable(asof_res, asof_dec, holding_days)`. The LLM CANNOT alter it.

`ReflectionLLMOutput` contains no `tau_observable` field. This is the structural Oracle Fallacy guard: even if a malformed LLM response tried to embed a crafted timestamp in free-text, the Reflector ignores it. The `tau_observable` on every persisted `Reflection` row is always:

```
tau_observable = max(asof_resolution, asof_decision + holding_days * 86400s + 6h)
```

The `+6h` reflects the typical adj-close data publication lag. Any reflection injected into future PM prompts via the retriever (Layer 3) must satisfy `tau_observable < asof_decision_future` (hard filter in `retriever.py`).

The canonical Oracle Fallacy regression test lives in `tests/memory/test_oracle_fallacy.py` (ADR-0042). The integration test in `tests/memory/test_reflector_llm_v02.py::test_oracle_fallacy_guard_tau_observable_never_from_llm` verifies that the v0.2 path produces the same deterministic `tau_observable` as the v0.1 path.

### 6. Audit trail: `reflector_llm_call` events

Every `_reflect_with_llm` invocation emits one `reflector_llm_call` audit event (kind registered as an extension kind, same pattern as `llm_call` in ADR-0054). The `path_kind` field distinguishes:

| `path_kind` | Meaning |
|-------------|---------|
| `v01_stub_text` | v0.2 gates closed; v0.1 stub used (reason field: `feature_flag_off` or `llm_caller_not_available`) |
| `v02_llm_succeeded` | v0.2 LLM call returned valid `ReflectionLLMOutput` |
| `v02_llm_fallback_to_v01` | v0.2 LLM call raised or returned `None`; fell back to v0.1 stub |
| `v02_self_grade_refused` | Self-grade refusal invariant triggered; v0.1 stub used |

When `llm_caller` is `None` or a plain `Callable` (not a `LLMCaller` instance), no audit event is emitted (the duck-typed gate fires before any audit write).

### 7. Cost discipline (ADR-0042 / ADR-0037)

Model tier: haiku-class (per ADR-0037 quick-tier split). Estimated cost: ~$0.0006 per reflection, matching the ADR-0042 budget of ≤$0.001/reflection. At 50 closes/month: ~$0.03/month. The prompt is intentionally terse: decision JSON + outcome block ≈ 400–600 input tokens; 2–4 sentence output ≈ 60–120 tokens.

### 8. Backward compatibility

The v0.1 plain-`Callable` path is fully preserved:

- `Reflector(llm_caller=lambda prompt: text)` → same behavior as pre-ADR-0057.
- `Reflector(llm_caller=None)` → deterministic v0.1 stub, bit-identical.
- All 13 existing `tests/memory/test_reflector.py` tests remain green.

The `_is_llm_caller_instance` duck-type check (`.call`, `.available`, `.model_id` attributes) distinguishes an `LLMCaller` from a plain `Callable` without a hard import of `LLMCaller`. This preserves the existing test in `test_reflector.py::test_llm_caller_invoked_when_provided` which passes a `lambda`.

---

## Consequences

**Positive:**
- `reflection_text` is now narrative-quality prose grounded in the alpha math, not a template string.
- `lesson_category` is LLM-classified from the decision narrative, not a heuristic lookup on `holding_days`.
- Self-grade refusal is structurally enforced, not policy.
- Oracle Fallacy guard is structural (field absence), not runtime assertion.
- Full audit trail for every reflection attempt.

**Negative / mitigations:**
- Adds ~400ms latency on position close (haiku tier is fast; acceptable for post-close async path).
- Requires `HERMES_QUANT_REFLECTOR_LLM=1` and `OPENROUTER_API_KEY` in production; default OFF means no cost in CI or dry-run mode.
- `ReflectionLLMOutput` Pydantic schema must be kept in sync with `LessonCategory` enum values if new categories are added. Lint check: the system prompt enumerates all categories at call time from the live enum, not from a hardcoded string.

---

## Cross-references

- ADR-0042 §anti-patterns: "Self-graded reflection (PM model evaluates its own outcome). Rejected."
- ADR-0042 §4.2 / arxiv:2605.19337 §4.2: Oracle Fallacy guard.
- ADR-0054 §2: "ReflectorV2 should compose with LLMCaller rather than rolling its own HTTP + audit path."
- ADR-0037: Quick-tier (haiku) LLM tier assignment.
- `tests/memory/test_oracle_fallacy.py`: canonical Oracle Fallacy regression test.
- `tests/memory/test_reflector_llm_v02.py::test_self_grade_refused_when_pm_model_equals_reflector_model`: canonical self-grade refusal regression test.
