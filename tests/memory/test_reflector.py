"""tests/memory/test_reflector.py — Layer 2 reflector tests (ADR-0042)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hermes_quant.memory.reflector import (
    LessonCategory,
    Reflection,
    Reflector,
    _compute_outcome_quality,
    _compute_tau_observable,
    _stub_reflection_text,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def reflector(tmp_path: Path) -> Reflector:
    return Reflector(reflections_path=tmp_path / "reflections.jsonl")


def _make_decision(
    ticker: str = "MRNA",
    direction: int = -1,
    rating: str = "Underweight",
    asof_decision: str = "2026-05-27T16:50:47+00:00",
    decision_id: str | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "kind": "decision",
        "decision_id": decision_id or f"dec_20260527T165047_{ticker}_aabbcc",
        "asof_decision": asof_decision,
        "ticker": ticker,
        "asset_class": "equity",
        "rating": rating,
        "direction": direction,
        "confidence": 0.85,
        "target_position_pct": -0.10,
        "thesis_summary": "Pipeline attrition risk.",
    }


def _make_exit(
    entry_price: float = 100.0,
    exit_price: float = 95.0,
    benchmark_return: float = 0.02,
    asof_resolution: str = "2026-06-12T14:00:00+00:00",
) -> dict:
    return {
        "asof_resolution": asof_resolution,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "benchmark_return": benchmark_return,
    }


# ---------------------------------------------------------------------------
# Alpha computation
# ---------------------------------------------------------------------------


def test_alpha_computation_long_position(reflector: Reflector) -> None:
    decision = _make_decision(direction=1, rating="Buy")
    exit_rec = _make_exit(entry_price=100.0, exit_price=108.0, benchmark_return=0.03)

    reflection = reflector.reflect_on_close(decision, exit_rec)

    # raw_return = (108 - 100) / 100 = 0.08 (long)
    assert abs(reflection.raw_return - 0.08) < 1e-6
    # alpha = 0.08 - 0.03 = 0.05
    assert abs(reflection.alpha_return - 0.05) < 1e-6


def test_alpha_computation_short_position(reflector: Reflector) -> None:
    """Short direction: profit when price falls."""
    decision = _make_decision(direction=-1, rating="Underweight")
    # price fell 5% → short profits 5%
    exit_rec = _make_exit(entry_price=100.0, exit_price=95.0, benchmark_return=-0.02)

    reflection = reflector.reflect_on_close(decision, exit_rec)

    # raw: -(exit-entry)/entry = -(95-100)/100 = +0.05 for short
    assert abs(reflection.raw_return - 0.05) < 1e-6
    # alpha = 0.05 - (-0.02) = 0.07
    assert abs(reflection.alpha_return - 0.07) < 1e-6


def test_alpha_computation_negative_alpha(reflector: Reflector) -> None:
    """Bull call fails: negative alpha."""
    decision = _make_decision(direction=1, rating="Buy")
    exit_rec = _make_exit(entry_price=100.0, exit_price=99.0, benchmark_return=0.05)

    reflection = reflector.reflect_on_close(decision, exit_rec)

    assert reflection.raw_return < 0
    assert reflection.alpha_return < 0


# ---------------------------------------------------------------------------
# Outcome quality bucket assignment
# ---------------------------------------------------------------------------


def test_outcome_quality_very_bad() -> None:
    # -2σ zone: alpha <= mean - 2*std = 0 - 2*0.02 = -0.04
    assert _compute_outcome_quality(-0.05) == 1


def test_outcome_quality_bad() -> None:
    # -1σ < alpha <= -2σ: between -0.04 and -0.02
    assert _compute_outcome_quality(-0.025) == 2


def test_outcome_quality_neutral() -> None:
    # ±0.5σ: between -0.02 and +0.01
    assert _compute_outcome_quality(0.0) == 3


def test_outcome_quality_good() -> None:
    # +1σ zone: between 0.01 and 0.02
    assert _compute_outcome_quality(0.015) == 4


def test_outcome_quality_great() -> None:
    # > +1σ: > 0.02
    assert _compute_outcome_quality(0.05) == 5


def test_outcome_quality_full_range_covered() -> None:
    for alpha in [-0.10, -0.03, -0.01, 0.0, 0.015, 0.05, 0.10]:
        oq = _compute_outcome_quality(alpha)
        assert 1 <= oq <= 5


# ---------------------------------------------------------------------------
# tau_observable
# ---------------------------------------------------------------------------


def test_tau_observable_formula() -> None:
    asof_dec = datetime(2026, 5, 27, 16, 50, 47, tzinfo=UTC)
    asof_res = datetime(2026, 6, 12, 14, 0, 0, tzinfo=UTC)
    holding_days = 16

    tau = _compute_tau_observable(asof_res, asof_dec, holding_days)

    # natural = asof_dec + 16*86400 + 6*3600
    natural = asof_dec + timedelta(seconds=16 * 86400 + 6 * 3600)
    expected = max(asof_res, natural)
    assert tau == expected


def test_tau_observable_stored_in_reflection(reflector: Reflector) -> None:
    decision = _make_decision()
    exit_rec = _make_exit()

    reflection = reflector.reflect_on_close(decision, exit_rec)

    # tau_observable must be a non-None ISO string
    assert reflection.tau_observable is not None
    from datetime import timezone
    from hermes_quant.memory.reflector import _parse_dt
    tau = _parse_dt(reflection.tau_observable)
    assert tau is not None
    assert tau.tzinfo is not None


# ---------------------------------------------------------------------------
# Stub text formatting
# ---------------------------------------------------------------------------


def test_stub_text_correct_call() -> None:
    text = _stub_reflection_text(1, "Buy", 0.03, 4, LessonCategory.unknown)
    assert "correct" in text
    assert "+3.0%" in text
    assert "4/5" in text


def test_stub_text_wrong_call() -> None:
    text = _stub_reflection_text(1, "Buy", -0.02, 2, LessonCategory.thesis_invalidation_at_earnings)
    assert "wrong" in text
    assert "thesis_invalidation_at_earnings" in text


def test_stub_text_short_direction_label() -> None:
    text = _stub_reflection_text(-1, "Underweight", 0.01, 3, LessonCategory.noise_trade_no_lesson)
    assert "short" in text


def test_stub_text_short_correct_label() -> None:
    # reflect_on_close already direction-adjusts alpha_return (line 418-419:
    # `if direction < 0: raw_return = -raw_return`), so a positive alpha_return
    # means the trade made money regardless of direction. The stub text label
    # must therefore be direction-AGNOSTIC, matching _classify_lesson (line 195).
    # A *profitable* short (alpha_return > 0) must read "correct", not "wrong".
    text = _stub_reflection_text(-1, "Underweight", 0.05, 5, LessonCategory.unknown)
    assert "short" in text
    assert "correct" in text
    assert "wrong" not in text


def test_stub_text_short_wrong_label() -> None:
    # A *losing* short (alpha_return < 0) must read "wrong", not "correct".
    text = _stub_reflection_text(-1, "Underweight", -0.05, 1, LessonCategory.thesis_invalidation_at_earnings)
    assert "short" in text
    assert "wrong" in text
    # guard against the substring "wrong" being absent while "correct" sneaks in
    assert "was correct" not in text


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_reflection_persisted_to_disk(reflector: Reflector, tmp_path: Path) -> None:
    decision = _make_decision()
    exit_rec = _make_exit()

    reflector.reflect_on_close(decision, exit_rec)

    path = tmp_path / "reflections.jsonl"
    assert path.exists()
    lines = [l for l in path.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    import json
    row = json.loads(lines[0])
    assert row["schema_version"] == 1
    assert row["ticker"] == "MRNA"
    assert "tau_observable" in row
    assert "reflection_id" in row
    assert row["reflection_id"].startswith("ref_")


def test_reflection_has_all_required_fields(reflector: Reflector) -> None:
    decision = _make_decision()
    exit_rec = _make_exit()
    r = reflector.reflect_on_close(decision, exit_rec)

    assert isinstance(r, Reflection)
    assert r.schema_version == 1
    assert r.reflection_id
    assert r.decision_id
    assert r.asof_resolution
    assert r.tau_observable
    assert r.ticker == "MRNA"
    assert r.benchmark == "SPY"
    assert 1 <= r.outcome_quality <= 5
    assert r.lesson_category in {lc.value for lc in LessonCategory}
    assert r.reflector_model
    assert r.reflector_prompt_hash


def test_llm_caller_invoked_when_provided(tmp_path: Path) -> None:
    """When llm_caller is provided, reflection_text comes from it."""
    custom_text = "Custom LLM reflection text."
    reflector = Reflector(
        reflections_path=tmp_path / "reflections.jsonl",
        llm_caller=lambda _prompt: custom_text,
        model_name="test-haiku",
    )
    decision = _make_decision()
    exit_rec = _make_exit()
    r = reflector.reflect_on_close(decision, exit_rec)

    assert r.reflection_text == custom_text
    assert r.reflector_model == "test-haiku"
    assert r.reflector_prompt_hash.startswith("llm:")
