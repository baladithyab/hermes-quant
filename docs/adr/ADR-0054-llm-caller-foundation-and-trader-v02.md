# ADR-0054: LLM-Caller Foundation & TraderNode v0.2

**Status:** Accepted  
**Date:** 2026-05-27  
**Author:** ARIA (deep-work-loop on hermes-quant; v0.2-4 task)  
**Implements:** Wave 3 LLM-call infrastructure + TraderNode v0.2 feature flag  
**Depends on:** ADR-0044 (Trader stage), ADR-0031 (silence-by-default / governance plane)  
**Defers:** RiskCommittee v0.2 wiring, Reflector v0.2 wiring (same injection point — wire when ready)

---

## 1. Context

ADR-0044 shipped TraderNode v0.1 (deterministic) and structured-output helpers
(`bind_structured`, `invoke_structured_or_freetext`) but left a gap: **there was no
actual HTTP call path**. `structured_output.py` constructs the kwargs/params dicts but
delegates the real call to the caller. Every future LLM-driven component (TraderNode v0.2,
RiskCommittee v0.2, Reflector v0.2) would need to re-implement:

1. The HTTP POST to OpenRouter with `Authorization: Bearer <key>`.
2. The OAI-compatible response extraction (choices[0].message.content).
3. Structured-output parsing with graceful fallback.
4. Audit-log recording for full provenance.
5. The silence-by-default error contract (ADR-0031): **never crash the pipeline on LLM error**.

Rather than duplicating this logic three times, this ADR introduces `LLMCaller` as a
single composable unit that all three v0.2 paths will use.

---

## 2. Decision

### D1. `hermes_quant/agents/llm_caller.py` — the `LLMCaller` class

A single reusable class encapsulating:

- **Transport**: `httpx.Client` (already in the project dependency graph; see §6).
- **Auth**: reads `OPENROUTER_API_KEY` env var at call time (never at import time).
- **Structured output**: delegates to `bind_structured()` + `_parse_response()` +
  `_parse_freetext_json()` from `structured_output.py`.
- **Audit logging**: every call records 8 fields to the governance audit log (§4).
- **Silence-by-default**: `.call()` never raises; errors return `(None, {"error": ...})`.

```python
class LLMCaller:
    def __init__(self, *, model_id="openai/gpt-4.1-mini",
                 api_key=None, base_url="https://openrouter.ai/api/v1",
                 timeout=30.0, audit_kind="llm_call"):
        ...

    def available(self) -> bool:
        """True iff OPENROUTER_API_KEY (or constructor key) is present.
        Cheap: env-var check only, no network round-trip."""
        ...

    def call(self, system_prompt, user_prompt, *, schema=None
             ) -> tuple[BaseModel | str | None, dict]:
        """Call the LLM; return (parsed_obj, raw_response_dict).
        Never raises. Returns (None, {"error": ...}) on any failure."""
        ...
```

### D2. `TraderNodeLLM` — v0.2 (in `hermes_quant/agents/trader.py`)

A separate class (does not replace `TraderNode` v0.1) that composes with `LLMCaller`:

```python
class TraderNodeLLM:
    def __init__(self, *, llm_caller=None, atr_multiplier=2.0):
        ...
    def __call__(self, research_plan_dict, advisor_signal_dict) -> TraderProposal:
        ...
```

**Fallback chain** (silence-by-default per ADR-0031):

| Condition | Path fired | Audit field `path` |
|---|---|---|
| `HERMES_QUANT_TRADER_LLM=0` (default) | v0.1 deterministic | `v01_deterministic` |
| Flag=1, `llm_caller is None` | v0.1 deterministic | `v01_deterministic` |
| Flag=1, `available()==False` | v0.1 deterministic | `v02_llm_fallback_to_v01` |
| Flag=1, LLM call raises | v0.1 deterministic | `v02_llm_fallback_to_v01` |
| Flag=1, LLM returns None/invalid | v0.1 deterministic | `v02_llm_fallback_to_v01` |
| Flag=1, LLM returns valid `TraderProposal` | **v0.2 LLM** | `v02_llm_succeeded` |

The caller side is **unchanged** — it receives a `TraderProposal` regardless of path.

### D3. Feature flag — `HERMES_QUANT_TRADER_LLM`

```
HERMES_QUANT_TRADER_LLM=0   # default OFF — v0.1 always fires
HERMES_QUANT_TRADER_LLM=1   # enable v0.2 LLM path with v0.1 fallback
```

- **Default is OFF** (safe for CI, cold-start, missing API key).
- No config file introduced — follows the existing env-var convention established
  across the codebase.
- The flag is read at call time (not import time) so tests can toggle it via
  `monkeypatch.setenv()` without module reloads.

### D4. Audit-log integration

Every `LLMCaller.call()` invocation appends one event with kind `llm_call`
(or the `audit_kind` constructor argument) and 8 mandatory fields:

| Field | Description |
|---|---|
| `model_id` | Full model identifier, e.g. `openai/gpt-4.1-mini` |
| `prompt_hash` | SHA-256 of (system\_prompt + user\_prompt) — enables dedup |
| `raw_response` | Truncated raw OAI response dict (≤4096 chars serialized) |
| `parsed_dump` | `model_dump()` of the parsed Pydantic object, or `None` |
| `latency_ms` | End-to-end call latency in milliseconds |
| `error` | Error string, or `None` on success |
| `audit_kind` | The `audit_kind` constructor argument |
| `timestamp` | UTC ISO datetime of the call |

`TraderNodeLLM._record_path()` additionally appends a `trader_llm_call` event
with `path`, `reason`, `action`, `size_fraction`, `confidence`, and `warning_message`.

**Extension kind contract**: `llm_call` and `trader_llm_call` are not in the 8-value
`EventKind` Literal in `governance/audit_log.py` (which covers the core governance
kinds). These are **extension kinds** written via a raw JSON append path that bypasses
the strict Literal gate. This is intentional: the LLM-call audit is operational
provenance, not a governance event. A future ADR may promote them to first-class kinds
with a `schema_version` bump if cross-query tooling requires it.

### D5. Silence-by-default contract (from ADR-0031)

Every error path in `LLMCaller.call()` and `TraderNodeLLM.__call__()` follows:

1. Logs a `WARNING` with the error message.
2. Returns a `TraderProposal` (from v0.1) or `(None, {"error": ...})`.
3. **Never raises**.
4. Records the failure in the audit log for post-hoc investigation.

This is the same contract as `TraderNode._fallback()` and `invoke_structured_or_freetext`.

### D6. Deferred wiring — RiskCommittee v0.2, Reflector v0.2

Both deferred components will compose with `LLMCaller` in exactly the same way:

```python
# RiskCommittee v0.2 (deferred):
class RiskCommitteeV2:
    def __init__(self, *, llm_caller: LLMCaller | None = None, ...):
        self._llm_caller = llm_caller
        ...

# Reflector v0.2 (deferred):
class ReflectorV2:
    def __init__(self, *, llm_caller: LLMCaller | None = None, ...):
        self._llm_caller = llm_caller
        ...
```

The injection point is identical. The only wiring change needed is:
1. Instantiate a `LLMCaller(audit_kind="risk_committee_llm_call")` (or `"reflector_llm_call"`).
2. Pass it to the constructor.
3. Add a feature flag (`HERMES_QUANT_RISK_COMMITTEE_LLM`, `HERMES_QUANT_REFLECTOR_LLM`).
4. Implement the same fallback chain as `TraderNodeLLM`.

---

## 3. Consequences

**Positive:**
- Single composable LLM-call unit reduces duplication across three v0.2 components.
- Every LLM call is recorded in the audit log: full prompt provenance via `prompt_hash`,
  latency, raw response, parsed result. Fully reproducible.
- `available()` is a cheap gate — no latency added to the hot path when the flag is OFF.
- Feature flag default OFF guarantees CI green without an API key.
- v0.1 `TraderNode` is untouched — backward compatibility preserved.

**Neutral:**
- `llm_caller.py` imports `structured_output._detect_provider`, `_parse_response`,
  `_parse_freetext_json` (private helpers). These are stable within the module.
  If they are ever renamed, update both files.
- The extension-kind audit write bypasses the `EventKind` Literal gate. This is a
  deliberate trade-off: operational audit vs. governance event taxonomy.

**Negative / trade-offs:**
- `httpx` is a dependency. It is already in the project's `pyproject.toml` / `requirements.txt`.
  If it were not, `urllib.request` would be the fallback (no third-party deps needed).
  The `LLMCaller._http_post()` method is the only httpx touch-point — swapping to
  `urllib.request` is a 20-line change if needed.
- `available()` only checks env-var presence, not network reachability. A misconfigured
  API key (wrong value) is only detected at first `.call()` (returns 401 + falls back).
  This is intentional: a network probe on every startup would add latency.

---

## 4. Alternatives Considered

**Alt A: Extend `invoke_structured_or_freetext` with HTTP transport.**  
Rejected: `structured_output.py` intentionally imports no LLM SDK and has no HTTP logic
(per its own module docstring). Mixing concerns would make it harder to test.

**Alt B: Use the OpenAI Python SDK directly.**  
Rejected: introduces a third-party dependency for a use-case that only needs a single
REST POST. `httpx` (already present) is sufficient. Using the OpenAI SDK would also
break the provider-agnostic routing in `bind_structured`.

**Alt C: One feature flag `HERMES_QUANT_LLM_ENABLED` for all v0.2 paths.**  
Rejected: different components will be promoted to LLM-driven at different times.
Per-component flags (`HERMES_QUANT_TRADER_LLM`, `HERMES_QUANT_RISK_COMMITTEE_LLM`,
`HERMES_QUANT_REFLECTOR_LLM`) allow surgical enablement in production.

---

## 5. Test Coverage

`tests/agents/test_llm_caller.py` (15 tests):
- `available()` with/without env var, with constructor key
- `.call()` with no key → `(None, {"error": ...})` + audit event
- Mock httpx: success path (valid JSON), timeout, 401, 500, malformed JSON, free-text
- All audit events contain the 8 required fields
- `_sha256_hash` determinism
- `_safe_truncate` boundaries
- `audit_kind` customisation

`tests/agents/test_trader_llm_v02.py` (10 tests):
- Flag OFF → v0.1, bit-identical output; LLM mock never called
- Flag ON, `available()==False` → fallback; audit `v02_llm_fallback_to_v01`
- Flag ON, valid proposal → v0.2 succeeded; audit `v02_llm_succeeded`
- Flag ON, LLM raises → fallback; audit `v02_llm_fallback_to_v01`
- Flag ON, LLM returns None → fallback; `llm_parse_failed` reason
- `llm_caller=None` → always v0.1
- v0.1 `TraderNode` unchanged regardless of flag
- `_trader_llm_enabled()` helper
- `TraderNodeLLM` never raises

---

## 6. Dependencies

| Dependency | Already present? | Notes |
|---|---|---|
| `httpx` | ✅ yes | Used in `LLMCaller._http_post()`. Version: 0.28.x. |
| `pydantic` v2 | ✅ yes | `BaseModel`, `ValidationError`. |
| `structured_output.py` | ✅ yes (ADR-0044) | `bind_structured`, `_parse_response`, `_parse_freetext_json`. |
| `governance/audit_log.py` | ✅ yes (ADR-0031) | `AUDIT_LOG_PATH`, `CURRENT_SCHEMA_VERSION`, `_write_lock`. |

---

## 7. Open Questions (deferred)

1. **Rate-limit handling**: `LLMCaller._http_post()` does not retry on 429. If
   retry-with-backoff is needed, wrap in a `tenacity` retry or add a `max_retries`
   constructor arg. Deferred until production load is observed.

2. **Token budget**: No `max_tokens` is set by default. Callers can pass it via
   `extra_kwargs` in the `call()` signature. Deferred until prompt lengths are profiled.

3. **Anthropic tool-call response extraction**: The `_http_post` path only extracts
   `choices[0].message.content`. Anthropic tool-calls land in
   `choices[0].message.tool_calls[0].function.arguments`. This is handled by
   `_parse_freetext_json` as a fallback. A dedicated extraction path for Anthropic
   is deferred until Anthropic models are tested via OpenRouter.
