# ADR-0060 — Fallback Probe for Silence-by-Default Verification

- **Status:** Accepted
- **Date:** 2026-05-27
- **Supersedes:** none
- **Superseded-By:** none
- **Related:** ADR-0031 (silence-by-default), ADR-0054 (LLMCaller foundation), ADR-0056 (RiskCommittee v0.2), ADR-0057 (Reflector v0.2), ADR-0058 (HMM regime classifier)

## Context

Four production surfaces have v0.2 LLM-driven paths feature-flagged behind environment variables (`HERMES_QUANT_TRADER_LLM`, `HERMES_QUANT_RISK_COMMITTEE_LLM`, `HERMES_QUANT_REFLECTOR_LLM`, `HERMES_QUANT_REGIME_HMM`). Each surface promises **silence-by-default**: when the LLM fails (network timeout, 429 rate-limit, 500 server error, malformed JSON, schema-invalid output, empty response), the surface must transparently fall back to its deterministic v0.1 path with no observable behavioural difference.

Existing unit tests verify this fallback **with mocked LLMCallers**. The mocks are by definition aware of how the surface will exercise them, and may not faithfully reproduce all the edge cases of a real provider failure (mid-stream connection reset, partial JSON, oversized response). Before activating any v0.2 LLM flag in production paper-trading, we need a **synthetic failure-injection probe** that exercises every surface against every documented failure mode and confirms that:

1. The surface returns a valid output (no exception escapes).
2. That output **matches** the v0.1 deterministic baseline within tolerance.

This decouples production rollout from "the unit tests pass" — many unit tests pass while a refactor silently breaks the fallback (e.g. someone adds an `assert obj is not None` after the LLM call without preserving the fallback path).

## Decision

Implement `hermes_quant.observability.fallback_probe` exposing:

- `FAILURE_MODES` — the canonical list of injection modes: `happy_path`, `timeout`, `rate_limit`, `server_error`, `malformed_json`, `schema_invalid`, `empty`.
- `SURFACES` — the canonical list of LLM-wired surfaces: `trader`, `risk_committee`, `reflector`, `regime_hmm`.
- `StubLLMCaller(failure_mode)` — duck-typed in-process LLMCaller stand-in. Implements `.available()` and `.call(system_prompt, user_prompt, *, schema=None)`. Per-mode behaviour:
  - `happy_path` → returns `(parsed, raw)` synthetic valid output for that surface.
  - `timeout` → raises `TimeoutError`.
  - `rate_limit` → raises `RateLimitError` (HTTP 429-shaped).
  - `server_error` → raises `ServerError` (HTTP 500-shaped).
  - `malformed_json` → returns `(None, raw_text)` where `raw_text` is non-parseable.
  - `schema_invalid` → returns `(None, raw_text)` where JSON parses but does not match schema.
  - `empty` → returns `(None, "")`.
- 4 probe functions (`probe_trader_node`, `probe_risk_committee`, `probe_reflector`, `probe_regime_hmm`) that synthesize a minimal input fixture, monkeypatch the LLMCaller injection point, run the surface end-to-end, and compare the output against a v0.1 reference computation.
- `run_fallback_probe(surfaces, failure_modes, dry_run=True)` runs the cross-product matrix.
- CLI: `scripts/quant-fallback-probe.py --surface ... --failure-mode ... --format ...`. Exit code is **1** if any probe produced invalid output, **0** otherwise. This is consumed by the rollout playbook (ADR-0062) as a hard pre-flight gate.

The probe NEVER makes a real network call. All stubs are pure in-process Python. Integration with real OpenRouter is an explicit non-goal of this ADR; chaos-monkey-in-prod is rejected as too risky for the paper-trading boundary.

## Consequences

### Positive
- Refactoring confidence: any change to a v0.2 surface that breaks silence-by-default is caught by a single CLI invocation, no need to run the full test suite.
- Production gate: the rollout playbook (ADR-0062) wires `quant-fallback-probe --surface all --failure-mode all` as a hard pre-flight check. Operator cannot accidentally activate v0.2 LLM in production without verification.
- Future-proof: when a 5th LLM-wired surface lands (e.g. AlphaBench MCP), it adds one entry to `SURFACES` and one `probe_*` function. The matrix grows automatically.
- Independent verification surface: the probe is a separate module from the v0.2 surfaces themselves, so reviewers can audit the fallback contract without reading every surface's internals.

### Negative
- Stub maintenance: the `StubLLMCaller` must mirror the real `LLMCaller` interface. If the real LLMCaller adds a method, the stub must too. Mitigation: duck-typing keeps the stub minimal; `.available()` and `.call()` is the entire surface area today.
- Synthetic failure modes ≠ real-world: a real provider can fail in ways we don't enumerate (truncated SSE stream, gateway timeout mid-tokens). The probe is necessary but not sufficient.
- HMM mismatch: the HMM doesn't speak to a per-call LLM — it loads a model file. The HMM probe maps LLM failure modes to model-load failure modes (`timeout` → corrupt file load, `server_error` → missing file, `schema_invalid` → wrong tensor shape) to keep the matrix consistent. Documented in module docstring.

### Neutral
- One new module + one new CLI + one new test file. ~1.5KLoC delta. No dependency on any v0.2 surface internals beyond their public LLMCaller injection point.

## Alternatives Considered

1. **Chaos-monkey-in-production** — periodically inject a real failure into the live paper-trading system and observe whether the fallback held. Rejected: while paper-trading carries no real capital risk, intentional failure injection during a live decision corrupts the audit log with synthetic events that pollute future Reflector training.

2. **Load test** — run the v0.2 surfaces under high concurrency / rate-limit pressure with a real provider. Rejected: this is a separate concern (capacity) from silence-by-default (correctness under failure). Will be addressed by a separate `quant-load-test` tool when needed.

3. **Per-surface inline assertions** — add `assert fallback_held()` calls at the end of each v0.2 path. Rejected: bloats production code with test scaffolding; assertions can be silently disabled with `-O`; doesn't compose into a single pre-flight gate.

4. **Property-based testing** (Hypothesis) — generate random failure modes and assert fallback always produces a valid output. Rejected for now: the matrix of 7 modes × 4 surfaces is small enough to enumerate exhaustively, and exhaustive is more reproducible than property-based for a binary "did the fallback hold" question. Hypothesis is a good candidate for **input** fuzzing of the surfaces themselves, but that's a different concern.

## References

- ADR-0031 — Silence-by-default error handling (the contract this probe verifies)
- ADR-0054 — LLMCaller foundation (the abstraction the stub mimics)
- `hermes_quant/observability/fallback_probe.py` — implementation
- `scripts/quant-fallback-probe.py` — CLI
- `tests/observability/test_fallback_probe.py` — 43 tests
- `docs/operations/ROLLOUT.md` — production playbook that consumes this probe
