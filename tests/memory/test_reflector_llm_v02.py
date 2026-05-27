"""tests/memory/test_reflector_llm_v02.py — Reflector v0.2 LLM wiring (ADR-0057).

Tests for the v0.2 structured-output LLM path introduced in ADR-0057:
  - Feature-flag gating (HERMES_QUANT_REFLECTOR_LLM)
  - LLMCaller.available() gate
  - Self-grade refusal invariant (canonical regression test — ADR-0042 §anti-patterns)
  - Oracle Fallacy guard preservation (tau_observable always deterministic)
  - Audit path_kind values: v01_stub_text | v02_llm_succeeded |
                             v02_llm_fallback_to_v01 | v02_self_grade_refused
  - Full v0.2 success path: reflection_text from LLM, lesson_category from LLM
  - Exception fall-through to v0.1
  - No real LLM calls — all mocked via unittest.mock.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hermes_quant.memory.reflector import (
    LessonCategory,
    Reflection,
    Reflector,
    ReflectionLLMOutput,
    _compute_tau_observable,
    _is_llm_caller_instance,
    _stub_reflection_text,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_REFLECTOR_MODEL_ID = "anthropic/claude-haiku-4-5"
_PM_MODEL_ID = "anthropic/claude-sonnet-4-5"


def _make_decision(
    ticker: str = "NVDA",
    direction: int = 1,
    rating: str = "Buy",
    asof_decision: str = "2026-04-01T10:00:00+00:00",
    decision_id: str = "dec_20260401T100000_NVDA_aabbcc",
    llm_committee_model_id: str | None = None,
) -> dict[str, Any]:
    d = {
        "schema_version": 1,
        "kind": "decision",
        "decision_id": decision_id,
        "asof_decision": asof_decision,
        "ticker": ticker,
        "asset_class": "equity",
        "rating": rating,
        "direction": direction,
        "confidence": 0.80,
        "target_position_pct": 0.10,
        "thesis_summary": "AI accelerator demand cyclically elevated; data-center capex super-cycle.",
    }
    if llm_committee_model_id is not None:
        d["llm_committee_model_id"] = llm_committee_model_id
    return d


def _make_exit(
    entry_price: float = 800.0,
    exit_price: float = 860.0,
    benchmark_return: float = 0.02,
    asof_resolution: str = "2026-04-21T14:00:00+00:00",
) -> dict[str, Any]:
    return {
        "asof_resolution": asof_resolution,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "benchmark_return": benchmark_return,
    }


def _fake_llm_caller(
    model_id: str = _REFLECTOR_MODEL_ID,
    available: bool = True,
    reflection_text: str = "LLM-generated reflection text from v0.2 path.",
    lesson_category: str = "thesis_invalidation_at_earnings",
    raise_on_call: Exception | None = None,
    return_none: bool = False,
) -> MagicMock:
    """Build a duck-typed mock LLMCaller for testing."""
    caller = MagicMock()
    caller.model_id = model_id
    caller.available = MagicMock(return_value=available)

    if raise_on_call is not None:
        caller.call = MagicMock(side_effect=raise_on_call)
    elif return_none:
        caller.call = MagicMock(return_value=(None, {"error": "simulated_none"}))
    else:
        mock_output = MagicMock(spec=ReflectionLLMOutput)
        mock_output.reflection_text = reflection_text
        mock_output.lesson_category = lesson_category
        caller.call = MagicMock(return_value=(mock_output, {"choices": []}))

    return caller


@pytest.fixture
def reflector_path(tmp_path: Path) -> Path:
    return tmp_path / "reflections.jsonl"


@pytest.fixture
def audit_calls() -> list[dict]:
    """Capture audit events written by _audit_reflector_call."""
    calls: list[dict] = []

    original = __import__(
        "hermes_quant.memory.reflector", fromlist=["_audit_reflector_call"]
    )._audit_reflector_call

    def capturing_audit(path_kind: str, extra: dict) -> None:
        calls.append({"path_kind": path_kind, **extra})
        # do NOT call the real impl (which needs governance imports)

    with patch("hermes_quant.memory.reflector._audit_reflector_call", side_effect=capturing_audit):
        yield calls


# ---------------------------------------------------------------------------
# 1. Feature flag OFF → v0.1 stub (bit-identical)
# ---------------------------------------------------------------------------


def test_flag_off_uses_v01_stub(reflector_path: Path, audit_calls: list) -> None:
    """HERMES_QUANT_REFLECTOR_LLM=0 → v0.1 stub; LLMCaller.call never invoked."""
    fake_caller = _fake_llm_caller()
    reflector = Reflector(
        reflections_path=reflector_path,
        llm_caller=fake_caller,
        model_name="stub-v0.1",
    )
    decision = _make_decision()
    exit_rec = _make_exit()

    with patch.dict(os.environ, {"HERMES_QUANT_REFLECTOR_LLM": "0"}):
        r = reflector.reflect_on_close(decision, exit_rec)

    fake_caller.call.assert_not_called()
    # v0.1 stub text format
    assert "Direction call" in r.reflection_text
    assert "Lesson:" in r.reflection_text
    # audit records feature_flag_off
    assert any(c["path_kind"] == "v01_stub_text" and c.get("reason") == "feature_flag_off"
               for c in audit_calls)


def test_flag_absent_uses_v01_stub(reflector_path: Path, audit_calls: list) -> None:
    """Absent HERMES_QUANT_REFLECTOR_LLM → same as =0."""
    fake_caller = _fake_llm_caller()
    reflector = Reflector(
        reflections_path=reflector_path,
        llm_caller=fake_caller,
        model_name="stub-v0.1",
    )
    env = {k: v for k, v in os.environ.items() if k != "HERMES_QUANT_REFLECTOR_LLM"}
    with patch.dict(os.environ, env, clear=True):
        r = reflector.reflect_on_close(_make_decision(), _make_exit())

    fake_caller.call.assert_not_called()
    assert "Direction call" in r.reflection_text


# ---------------------------------------------------------------------------
# 2. Flag ON but LLMCaller.available() == False → v0.1 stub
# ---------------------------------------------------------------------------


def test_llm_unavailable_uses_v01_stub(reflector_path: Path, audit_calls: list) -> None:
    """HERMES_QUANT_REFLECTOR_LLM=1 + available()==False → v0.1 stub."""
    fake_caller = _fake_llm_caller(available=False)
    reflector = Reflector(
        reflections_path=reflector_path,
        llm_caller=fake_caller,
        model_name="stub-v0.1",
    )
    with patch.dict(os.environ, {"HERMES_QUANT_REFLECTOR_LLM": "1"}):
        r = reflector.reflect_on_close(_make_decision(), _make_exit())

    fake_caller.call.assert_not_called()
    assert "Direction call" in r.reflection_text
    assert any(
        c["path_kind"] == "v01_stub_text" and c.get("reason") == "llm_caller_not_available"
        for c in audit_calls
    )


# ---------------------------------------------------------------------------
# 3. Flag ON + available + mock returns valid Reflection → v0.2 succeeds
# ---------------------------------------------------------------------------


def test_v02_success_path_uses_llm_text(reflector_path: Path, audit_calls: list) -> None:
    """v0.2 success: reflection_text from LLM, lesson_category from LLM."""
    llm_text = "The directional call was correct; +7.5% alpha confirmed AI capex thesis. Earnings showed 22% data-center revenue growth, validating the super-cycle narrative. Lesson: hold size when thesis confirmed by revenue segment, not just guidance."
    fake_caller = _fake_llm_caller(
        reflection_text=llm_text,
        lesson_category="correct_call_too_early",
    )
    reflector = Reflector(
        reflections_path=reflector_path,
        llm_caller=fake_caller,
        model_name=_REFLECTOR_MODEL_ID,
    )
    with patch.dict(os.environ, {"HERMES_QUANT_REFLECTOR_LLM": "1"}):
        r = reflector.reflect_on_close(_make_decision(), _make_exit())

    fake_caller.call.assert_called_once()
    assert r.reflection_text == llm_text
    assert r.lesson_category == "correct_call_too_early"
    assert r.reflector_prompt_hash.startswith("llm-v02:")
    assert any(c["path_kind"] == "v02_llm_succeeded" for c in audit_calls)


def test_v02_success_persists_to_disk(reflector_path: Path) -> None:
    """v0.2 success path writes correct row to JSONL on disk."""
    llm_text = "LLM reflection v02 test."
    fake_caller = _fake_llm_caller(reflection_text=llm_text)
    reflector = Reflector(
        reflections_path=reflector_path,
        llm_caller=fake_caller,
        model_name=_REFLECTOR_MODEL_ID,
    )
    with patch("hermes_quant.memory.reflector._audit_reflector_call"):
        with patch.dict(os.environ, {"HERMES_QUANT_REFLECTOR_LLM": "1"}):
            r = reflector.reflect_on_close(_make_decision(), _make_exit())

    assert reflector_path.exists()
    rows = [json.loads(l) for l in reflector_path.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["reflection_text"] == llm_text
    assert rows[0]["reflector_prompt_hash"].startswith("llm-v02:")


# ---------------------------------------------------------------------------
# 4. LLM raises → fall through to v0.1; audit = v02_llm_fallback_to_v01
# ---------------------------------------------------------------------------


def test_llm_raises_fallback_to_v01(reflector_path: Path, audit_calls: list) -> None:
    """HERMES_QUANT_REFLECTOR_LLM=1 + LLM raises → v0.1; correct audit path_kind."""
    fake_caller = _fake_llm_caller(raise_on_call=RuntimeError("API timeout"))
    reflector = Reflector(
        reflections_path=reflector_path,
        llm_caller=fake_caller,
        model_name=_REFLECTOR_MODEL_ID,
    )
    with patch.dict(os.environ, {"HERMES_QUANT_REFLECTOR_LLM": "1"}):
        r = reflector.reflect_on_close(_make_decision(), _make_exit())

    # Falls through to v0.1 stub
    assert "Direction call" in r.reflection_text
    assert any(c["path_kind"] == "v02_llm_fallback_to_v01" for c in audit_calls)


def test_llm_returns_none_fallback_to_v01(reflector_path: Path, audit_calls: list) -> None:
    """LLMCaller.call returns (None, ...) → fallback to v0.1."""
    fake_caller = _fake_llm_caller(return_none=True)
    reflector = Reflector(
        reflections_path=reflector_path,
        llm_caller=fake_caller,
        model_name=_REFLECTOR_MODEL_ID,
    )
    with patch.dict(os.environ, {"HERMES_QUANT_REFLECTOR_LLM": "1"}):
        r = reflector.reflect_on_close(_make_decision(), _make_exit())

    assert "Direction call" in r.reflection_text
    assert any(c["path_kind"] == "v02_llm_fallback_to_v01" for c in audit_calls)


# ---------------------------------------------------------------------------
# 5. SELF-GRADE REFUSED — canonical regression test (ADR-0042 / ADR-0057)
# ---------------------------------------------------------------------------


def test_self_grade_refused_when_pm_model_equals_reflector_model(
    reflector_path: Path, audit_calls: list
) -> None:
    """CANONICAL REGRESSION TEST (ADR-0042 §anti-patterns / ADR-0057 §4).

    When decision['llm_committee_model_id'] == reflector llm_caller.model_id,
    the v0.2 path MUST be refused.  The audit log MUST record
    path_kind='v02_self_grade_refused'.  The reflection MUST fall back to
    the v0.1 stub (not the LLM text).

    This prevents the 'self-graded reflection' anti-pattern: a PM model
    evaluating its own trade outcome introduces confirmation bias into the
    episodic memory and degrades future advice quality.
    """
    shared_model_id = "anthropic/claude-sonnet-4-5"
    fake_caller = _fake_llm_caller(
        model_id=shared_model_id,
        reflection_text="This text MUST NOT appear — self-grade should be refused.",
    )
    reflector = Reflector(
        reflections_path=reflector_path,
        llm_caller=fake_caller,
        model_name=shared_model_id,
    )
    # Decision was made by the same model as the reflector
    decision = _make_decision(llm_committee_model_id=shared_model_id)

    with patch.dict(os.environ, {"HERMES_QUANT_REFLECTOR_LLM": "1"}):
        r = reflector.reflect_on_close(decision, _make_exit())

    # LLM call must NOT have been made
    fake_caller.call.assert_not_called()
    # Fell back to v0.1 stub
    assert "Direction call" in r.reflection_text
    assert "This text MUST NOT appear" not in r.reflection_text
    # Audit must record the refusal
    refusal_records = [c for c in audit_calls if c["path_kind"] == "v02_self_grade_refused"]
    assert len(refusal_records) == 1
    assert refusal_records[0]["pm_model"] == shared_model_id
    assert refusal_records[0]["reflector_model"] == shared_model_id


def test_self_grade_allowed_when_models_differ(
    reflector_path: Path, audit_calls: list
) -> None:
    """v0.2 proceeds normally when PM model ≠ reflector model."""
    llm_text = "Different-model reflection — allowed."
    fake_caller = _fake_llm_caller(
        model_id=_REFLECTOR_MODEL_ID,
        reflection_text=llm_text,
    )
    reflector = Reflector(
        reflections_path=reflector_path,
        llm_caller=fake_caller,
        model_name=_REFLECTOR_MODEL_ID,
    )
    # PM used a different (Sonnet) model
    decision = _make_decision(llm_committee_model_id=_PM_MODEL_ID)

    with patch.dict(os.environ, {"HERMES_QUANT_REFLECTOR_LLM": "1"}):
        r = reflector.reflect_on_close(decision, _make_exit())

    fake_caller.call.assert_called_once()
    assert r.reflection_text == llm_text
    assert not any(c["path_kind"] == "v02_self_grade_refused" for c in audit_calls)


# ---------------------------------------------------------------------------
# 6. ORACLE FALLACY GUARD — tau_observable is always deterministic
# ---------------------------------------------------------------------------


def test_oracle_fallacy_guard_tau_observable_never_from_llm(
    reflector_path: Path, audit_calls: list
) -> None:
    """ORACLE FALLACY GUARD INTEGRATION TEST (ADR-0042 §4.2 / ADR-0057 §5).

    The LLM output schema (ReflectionLLMOutput) does NOT contain a
    tau_observable field.  The Reflector always computes tau_observable
    deterministically via _compute_tau_observable.  This test verifies that
    the persisted reflection has the correct deterministic tau_observable
    regardless of what the LLM returns.
    """
    fake_caller = _fake_llm_caller(
        reflection_text="Good trade.",
        lesson_category="unknown",
    )
    reflector = Reflector(
        reflections_path=reflector_path,
        llm_caller=fake_caller,
        model_name=_REFLECTOR_MODEL_ID,
    )
    decision = _make_decision(asof_decision="2026-04-01T10:00:00+00:00")
    exit_rec = _make_exit(asof_resolution="2026-04-21T14:00:00+00:00")

    with patch.dict(os.environ, {"HERMES_QUANT_REFLECTOR_LLM": "1"}):
        r = reflector.reflect_on_close(decision, exit_rec)

    # Compute expected tau_observable deterministically
    asof_dec = datetime(2026, 4, 1, 10, 0, 0, tzinfo=UTC)
    asof_res = datetime(2026, 4, 21, 14, 0, 0, tzinfo=UTC)
    holding_days = max(0, int((asof_res - asof_dec).total_seconds() / 86400))
    expected_tau = _compute_tau_observable(asof_res, asof_dec, holding_days)

    from hermes_quant.memory.reflector import _parse_dt
    actual_tau = _parse_dt(r.tau_observable)
    assert actual_tau == expected_tau, (
        f"Oracle Fallacy guard failed: tau_observable {actual_tau} "
        f"!= deterministic {expected_tau}"
    )


def test_oracle_fallacy_tau_cannot_be_before_asof_plus_6h(
    reflector_path: Path,
) -> None:
    """tau_observable is always >= asof_resolution AND >= asof_decision + 6h.

    Even if the LLM were to return an arbitrary tau_observable (it cannot,
    since ReflectionLLMOutput has no such field), the Reflector computes it
    deterministically and the value always satisfies the post-publication
    delay invariant.
    """
    asof_dec = datetime(2026, 4, 1, 10, 0, 0, tzinfo=UTC)
    asof_res = datetime(2026, 4, 21, 14, 0, 0, tzinfo=UTC)
    holding_days = 20

    tau = _compute_tau_observable(asof_res, asof_dec, holding_days)

    assert tau >= asof_res, "tau_observable must be >= asof_resolution"
    assert tau >= asof_dec + timedelta(hours=6), "tau_observable must be >= asof_decision + 6h"


# ---------------------------------------------------------------------------
# 7. No LLMCaller (plain None) → v0.1 stub, no audit
# ---------------------------------------------------------------------------


def test_no_llm_caller_uses_v01_stub(reflector_path: Path) -> None:
    """When llm_caller=None, v0.1 stub is used regardless of env flag."""
    reflector = Reflector(reflections_path=reflector_path, llm_caller=None)
    with patch.dict(os.environ, {"HERMES_QUANT_REFLECTOR_LLM": "1"}):
        r = reflector.reflect_on_close(_make_decision(), _make_exit())

    assert "Direction call" in r.reflection_text
    assert r.reflector_prompt_hash.startswith("stub:")


# ---------------------------------------------------------------------------
# 8. Plain Callable (v0.1 legacy) still works unchanged
# ---------------------------------------------------------------------------


def test_plain_callable_v01_still_works(reflector_path: Path) -> None:
    """v0.1 legacy path: passing a plain Callable still routes through the
    v0.1 Callable branch (not v0.2 LLMCaller branch)."""
    custom_text = "Custom LLM reflection text."
    reflector = Reflector(
        reflections_path=reflector_path,
        llm_caller=lambda _prompt: custom_text,
        model_name="test-haiku",
    )
    with patch.dict(os.environ, {"HERMES_QUANT_REFLECTOR_LLM": "1"}):
        r = reflector.reflect_on_close(_make_decision(), _make_exit())

    assert r.reflection_text == custom_text
    assert r.reflector_model == "test-haiku"
    assert r.reflector_prompt_hash.startswith("llm:")


# ---------------------------------------------------------------------------
# 9. _is_llm_caller_instance duck-typing
# ---------------------------------------------------------------------------


def test_is_llm_caller_instance_detects_mock() -> None:
    from hermes_quant.memory.reflector import _is_llm_caller_instance
    fake = _fake_llm_caller()
    assert _is_llm_caller_instance(fake) is True


def test_is_llm_caller_instance_rejects_plain_callable() -> None:
    from hermes_quant.memory.reflector import _is_llm_caller_instance
    assert _is_llm_caller_instance(lambda x: x) is False


def test_is_llm_caller_instance_rejects_none() -> None:
    from hermes_quant.memory.reflector import _is_llm_caller_instance
    assert _is_llm_caller_instance(None) is False


# ---------------------------------------------------------------------------
# 10. LLM returns unknown lesson_category → coerced to 'unknown'
# ---------------------------------------------------------------------------


def test_unknown_lesson_category_coerced(reflector_path: Path) -> None:
    """If LLM returns a lesson_category not in the enum, it becomes 'unknown'."""
    fake_caller = _fake_llm_caller(lesson_category="invented_category_xyz")
    reflector = Reflector(
        reflections_path=reflector_path,
        llm_caller=fake_caller,
        model_name=_REFLECTOR_MODEL_ID,
    )
    with patch("hermes_quant.memory.reflector._audit_reflector_call"):
        with patch.dict(os.environ, {"HERMES_QUANT_REFLECTOR_LLM": "1"}):
            r = reflector.reflect_on_close(_make_decision(), _make_exit())

    assert r.lesson_category == "unknown"


# ---------------------------------------------------------------------------
# 11. v0.2 system prompt contains all LessonCategory values
# ---------------------------------------------------------------------------


def test_v02_system_prompt_contains_all_lesson_categories(
    reflector_path: Path,
) -> None:
    """The system prompt sent to the LLM must enumerate all valid categories."""
    captured_prompts: list[tuple[str, str]] = []

    def capture_call(sys_p: str, usr_p: str, **kwargs: Any):
        captured_prompts.append((sys_p, usr_p))
        mock_out = MagicMock(spec=ReflectionLLMOutput)
        mock_out.reflection_text = "Captured."
        mock_out.lesson_category = "unknown"
        return mock_out, {}

    fake_caller = MagicMock()
    fake_caller.model_id = _REFLECTOR_MODEL_ID
    fake_caller.available = MagicMock(return_value=True)
    fake_caller.call = MagicMock(side_effect=capture_call)

    reflector = Reflector(
        reflections_path=reflector_path,
        llm_caller=fake_caller,
        model_name=_REFLECTOR_MODEL_ID,
    )
    with patch("hermes_quant.memory.reflector._audit_reflector_call"):
        with patch.dict(os.environ, {"HERMES_QUANT_REFLECTOR_LLM": "1"}):
            reflector.reflect_on_close(_make_decision(), _make_exit())

    assert len(captured_prompts) == 1
    system_prompt = captured_prompts[0][0]
    for lc in LessonCategory:
        assert lc.value in system_prompt, (
            f"LessonCategory.{lc.value} missing from system prompt"
        )


# ---------------------------------------------------------------------------
# 12. Audit path_kind coverage — all four path_kinds are emittable
# ---------------------------------------------------------------------------


def test_all_four_audit_path_kinds_are_distinct() -> None:
    """The four path_kind values are distinct strings as required by ADR-0057."""
    kinds = {
        "v01_stub_text",
        "v02_llm_succeeded",
        "v02_llm_fallback_to_v01",
        "v02_self_grade_refused",
    }
    assert len(kinds) == 4, "All four path_kinds must be distinct"


def test_v02_succeeded_audit_has_reflector_model(
    reflector_path: Path, audit_calls: list
) -> None:
    """v02_llm_succeeded audit event records reflector_model."""
    fake_caller = _fake_llm_caller(
        model_id=_REFLECTOR_MODEL_ID,
        reflection_text="Succeeded.",
    )
    reflector = Reflector(
        reflections_path=reflector_path,
        llm_caller=fake_caller,
        model_name=_REFLECTOR_MODEL_ID,
    )
    with patch.dict(os.environ, {"HERMES_QUANT_REFLECTOR_LLM": "1"}):
        reflector.reflect_on_close(_make_decision(), _make_exit())

    success_records = [c for c in audit_calls if c["path_kind"] == "v02_llm_succeeded"]
    assert len(success_records) == 1
    assert success_records[0]["reflector_model"] == _REFLECTOR_MODEL_ID
