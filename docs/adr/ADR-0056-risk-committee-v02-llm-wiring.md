# ADR-0056: RiskCommittee v0.2 — LLM Wiring

**Status:** Accepted  
**Date:** 2026-05-27  
**Author:** ARIA (deep-work-loop on hermes-quant; v0.3-2 task)  
**Implements:** Wave 3 RiskCommittee LLM-call wiring  
**Depends on:** ADR-0043 (RiskCommittee v0.1 design), ADR-0054 (LLMCaller foundation), ADR-0031 (silence-by-default)  
**Parallels:** ADR-0054 §D2 (TraderNodeLLM v0.2 pattern)

---

## 1. Context

ADR-0043 shipped RiskCommittee v0.1 with three deterministic rule-based personas
(Aggressive, Conservative, Neutral) and explicitly deferred the LLM path behind a
`llm_caller: Callable | None = None` injection point.

ADR-0054 shipped `LLMCaller` — a reusable silence-by-default HTTP + audit wrapper —
and applied the v0.2 wiring pattern to `TraderNodeLLM`. The canonical v0.2 wiring
pattern is:

1. Feature flag `HERMES_QUANT_<COMPONENT>_LLM=1` (default OFF).
2. Caller availability check (`LLMCaller.available()`).
3. Attempt structured LLM call with Pydantic schema.
4. On any failure → fall back to v0.1 deterministic silently.
5. Audit every call with `kind=<component>_llm_call`.

This ADR documents how that pattern is applied to RiskCommittee with one key
extension: **per-persona partial fallback** (instead of whole-debate fallback).

---

## 2. Decision

### D1. Feature flag

`HERMES_QUANT_RISK_COMMITTEE_LLM=1` enables the v0.2 LLM path. Default is `0` (OFF).
The flag is checked in `RiskCommittee._should_use_llm()` which gates the entire
`_debate_with_llm()` dispatch. Three conditions must ALL be true to enable the LLM path:

```
(a) self._llm_caller is not None
(b) self._llm_caller.available() returns True  (env-var key presence check)
(c) os.environ["HERMES_QUANT_RISK_COMMITTEE_LLM"] == "1"
```

Any condition failing → `_debate_deterministic()` is called (v0.1).

### D2. Per-persona partial fallback (key difference from TraderNodeLLM)

TraderNodeLLM falls back for the **whole proposal** on LLM failure.
RiskCommittee has three independent persona turns per round. A failure in one
persona's LLM call must NOT abort the other two personas' LLM turns.

Each persona's turn is handled by `_invoke_persona_llm()` which returns
`(turn, path_label)`. Failure modes within this method:

| Failure | Outcome |
|---------|---------|
| `LLM_PROMPT_TEMPLATE.format()` raises | That persona: `v02_llm_fallback_to_v01` |
| `llm_caller.call()` raises | That persona: `v02_llm_fallback_to_v01` |
| `call()` returns `(None, raw)` | That persona: `v02_llm_fallback_to_v01` |
| Pydantic rebuild fails | That persona: `v02_llm_fallback_to_v01` |
| `isinstance(obj, RiskCommitteeTurn)` succeeds | `v02_llm_succeeded` |

The outer `_debate_with_llm()` loop continues to the next persona regardless.

**Example mixed-path audit log** (Conservative fails, others succeed):
```json
{"persona": "aggressive",   "path": "v02_llm_succeeded"}
{"persona": "conservative", "path": "v02_llm_fallback_to_v01"}
{"persona": "neutral",      "path": "v02_llm_succeeded"}
```

### D3. Structured output schema: `RiskCommitteeTurn`

Each persona's LLM call requests a `RiskCommitteeTurn` Pydantic model via
`LLMCaller.call(system_prompt, user_prompt, schema=RiskCommitteeTurn)`.

The schema fields are:
- `persona`: str — canonical name (enforced post-LLM regardless of what LLM emits)
- `turn_index`: int — enforced post-LLM
- `critique_text`: str (1–2048 chars) — conversational debate text
- `evidence_ids`: list[str] — evidence tags
- `risk_assessment`: `Literal["amplify", "silence", "neutral"]`
- `confidence`: float [0.0, 1.0]

After a successful LLM call, `persona` and `turn_index` are **overwritten** to their
canonical values regardless of what the LLM returned, preventing prompt-injection
persona spoofing.

### D4. CV5 anti-amplify invariant — preserved outside LLM scope

The CV5 invariant (silence_multiplier starts at 1.0 and ONLY DECREASES) is
**structurally enforced in `_debate_with_llm()`**, NOT by the LLM.

The LLM-returned `risk_assessment` field is read as:
- `"silence"` → `silence_multiplier *= 0.5`
- `"amplify"` or `"neutral"` → multiplier UNCHANGED

The LLM cannot raise the multiplier above 1.0 because:
1. The multiplier starts at 1.0.
2. Only `"silence"` votes mutate it (multiply by 0.5, which decreases it).
3. The final `max(0.0, min(1.0, silence_multiplier))` clamp is applied after
   the debate loop regardless of v0.1 or v0.2 path.

There is no code path through which an LLM response can increase silence_multiplier.

### D5. LLM prompt templates: `LLM_PROMPT_TEMPLATE` per persona

Each persona class gains a `LLM_PROMPT_TEMPLATE` class attribute (alongside the
existing `SYSTEM_PROMPT_TEMPLATE`). The template:

1. Contains the **verbatim** `"Output conversationally as if you are speaking
   without any special formatting"` preamble (TauricResearch v0.2.5 anti-pattern
   fix, gap #2 — forces real debate text rather than bullet-point hiding).
2. Instructs the LLM to return a single JSON object conforming to `RiskCommitteeTurn`.
3. Includes explicit CV5 rules in the prompt text:
   - `AggressivePersona`: notes amplify is audit-only.
   - `ConservativePersona`: defines when silence is appropriate.
   - `NeutralPersona`: explicitly forbids `"amplify"` — Neutral never amplifies.
4. Accepts format placeholders: `{ticker}`, `{turn_index}`, `{proposal_json}`,
   `{plan_json}`, `{prior_turns_json}`.

Template format errors fall back to v0.1 silently (partial fallback per D2).

### D6. Model tier and cost discipline (ADR-0037)

The `LLMCaller` injected into `RiskCommittee` uses the **haiku-equivalent** tier
(`openai/gpt-4.1-mini` default). This preserves the `~$0.012/decision` budget
established in ADR-0043 and is consistent with ADR-0037 cost discipline.

Cost breakdown per debate:
- 3 turns × ~4,000 tokens system prompt × haiku rate = well within $0.012 envelope
- Total 3-turn debate ≈ $0.003–$0.005 (haiku tier, 1 round)

### D7. Audit-log integration

Every turn on the v0.2 path emits one audit event with
`kind="risk_committee_llm_call"` (an extension kind per ADR-0054 §4):

```json
{
  "kind": "risk_committee_llm_call",
  "source": "hermes_quant.agents.risk_committee.committee",
  "payload": {
    "proposal_id": "<str>",
    "persona": "aggressive|conservative|neutral",
    "turn_index": 0,
    "path": "v02_llm_succeeded|v02_llm_fallback_to_v01",
    "risk_assessment": "amplify|silence|neutral",
    "confidence": 0.7,
    "silence_multiplier_after": 1.0
  }
}
```

The `_audit_append` function in `committee.py` is a module-level thin wrapper
(delegates to `llm_caller._audit_append`) so tests can patch it directly at
`hermes_quant.agents.risk_committee.committee._audit_append`.

---

## 3. Rejected alternatives

### R1. Whole-debate fallback (TraderNodeLLM style)

Rejected because a 3-persona debate benefits from **maximum LLM coverage**:
if Conservative's LLM call fails but Aggressive and Neutral succeed, those two
turns still provide real debate signal. Aborting the whole debate on one failure
would waste 2/3 of the LLM work already done.

### R2. Synchronous per-turn flag check

Rejected — a per-turn flag check would allow the LLM path to be toggled mid-debate.
The flag is checked once in `_should_use_llm()` and the whole debate runs on one
path. This ensures per-debate consistency.

### R3. Separate `RiskCommitteeLLM` class (parallel to `TraderNodeLLM`)

Considered but rejected — RiskCommittee already has a clean `llm_caller` injection
point and the `_debate_with_llm` / `_debate_deterministic` split preserves v0.1
without a second class. The TraderNodeLLM split was necessary because TraderNode
and TraderNodeLLM differ in their `__call__` signatures; RiskCommittee's `debate()`
API is identical across both paths.

---

## 4. Consequences

### Positive
- RiskCommittee personas can now reason about trades using real LLM judgment.
- Partial fallback maximizes LLM contribution per debate turn.
- CV5 invariant is structurally guaranteed — no amount of prompt engineering
  can bypass it.
- 38 v0.1 tests are unaffected; new tests cover all paths (14 tests).
- Audit trail now shows which path each persona turn took.

### Negative / Accepted trade-offs
- 3 LLM calls per debate round (vs 0 for v0.1); latency increases ~3–5×.
- haiku-tier structured output occasionally returns `None`; partial fallback
  handles this gracefully but may reduce debate quality for that persona.

---

## 5. Files modified

| File | Change |
|------|--------|
| `hermes_quant/agents/risk_committee/personas.py` | Added `LLM_PROMPT_TEMPLATE` to `AggressivePersona`, `ConservativePersona`, `NeutralPersona` and `RiskPersona` base; updated module docstring |
| `hermes_quant/agents/risk_committee/committee.py` | Added `_debate_with_llm()`, `_invoke_persona_llm()`, `_v01_turn()`, `_should_use_llm()`, `_audit_append()` module wrapper; refactored `debate()` to dispatch; updated module docstring; added `_LLM_FLAG_ENV_VAR`, `_RISK_COMMITTEE_AUDIT_KIND` constants |
| `tests/agents/test_risk_committee_llm_v02.py` | New — 14 tests covering all v0.2 paths, partial fallback, CV5 invariants, audit log |
| `docs/adr/ADR-0056-risk-committee-v02-llm-wiring.md` | This document |
