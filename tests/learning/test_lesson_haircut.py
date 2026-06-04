"""57f6 — bounded, asof-honest, lesson-driven confidence haircut.

A recent same-symbol same-direction LOSS lesson applies a documented, bounded
multiplicative haircut to a decision's confidence. The function:

  - matches only lessons on the SAME ticker AND SAME direction;
  - is asof-HONEST: a lesson whose outcome became observable at or after the
    decision asof is ignored (using it would be lookahead);
  - is BOUNDED: the total haircut can never drive confidence below a documented
    floor fraction, no matter how many losses pile up;
  - DE-DUPLICATES by lesson id so the same lesson can't be counted twice;
  - is a strict NO-OP (returns the input unchanged) when no lesson matches.

Pure-Python, offline, deterministic.
"""

from __future__ import annotations

import pandas as pd

from hermes_quant.learning.lesson_haircut import LossLesson, apply_lesson_haircut


def _lesson(lesson_id: str, ticker: str, direction: int, observable: str) -> LossLesson:
    return LossLesson(
        lesson_id=lesson_id,
        ticker=ticker,
        direction=direction,
        tau_observable=pd.Timestamp(observable, tz="UTC"),
        alpha_return=-0.05,
    )


DECISION = pd.Timestamp("2026-06-01", tz="UTC")
PER_LESSON = 0.15   # each matching loss shaves 15%
FLOOR = 0.5         # never cut below 50% of the original confidence


def test_no_lessons_is_noop():
    out = apply_lesson_haircut(
        confidence=0.80,
        ticker="AAPL",
        direction=1,
        decision_asof=DECISION,
        lessons=[],
        per_lesson_haircut=PER_LESSON,
        floor_fraction=FLOOR,
    )
    assert out == 0.80


def test_matching_loss_applies_bounded_haircut():
    lessons = [_lesson("l1", "AAPL", 1, "2026-05-20")]
    out = apply_lesson_haircut(
        confidence=0.80,
        ticker="AAPL",
        direction=1,
        decision_asof=DECISION,
        lessons=lessons,
        per_lesson_haircut=PER_LESSON,
        floor_fraction=FLOOR,
    )
    # One matching loss: 0.80 * (1 - 0.15) = 0.68.
    assert out == 0.80 * (1.0 - PER_LESSON)
    assert out < 0.80


def test_wrong_ticker_or_direction_is_noop():
    lessons = [
        _lesson("l1", "MSFT", 1, "2026-05-20"),   # wrong ticker
        _lesson("l2", "AAPL", -1, "2026-05-20"),  # wrong direction
    ]
    out = apply_lesson_haircut(
        confidence=0.80, ticker="AAPL", direction=1, decision_asof=DECISION,
        lessons=lessons, per_lesson_haircut=PER_LESSON, floor_fraction=FLOOR,
    )
    assert out == 0.80


def test_future_lesson_excluded_no_lookahead():
    """A loss whose outcome becomes observable at/after the decision asof must
    not haircut the decision — that would be learning from the future."""
    lessons = [
        _lesson("future-at", "AAPL", 1, "2026-06-01"),   # exactly at asof → excluded
        _lesson("future-after", "AAPL", 1, "2026-06-10"),  # after asof → excluded
    ]
    out = apply_lesson_haircut(
        confidence=0.80, ticker="AAPL", direction=1, decision_asof=DECISION,
        lessons=lessons, per_lesson_haircut=PER_LESSON, floor_fraction=FLOOR,
    )
    assert out == 0.80


def test_haircut_is_bounded_by_floor():
    """Many matching losses cannot drive confidence below the floor fraction."""
    lessons = [_lesson(f"l{i}", "AAPL", 1, "2026-05-01") for i in range(20)]
    out = apply_lesson_haircut(
        confidence=0.80, ticker="AAPL", direction=1, decision_asof=DECISION,
        lessons=lessons, per_lesson_haircut=PER_LESSON, floor_fraction=FLOOR,
    )
    assert out == 0.80 * FLOOR  # clamped at the floor, not driven toward 0


def test_duplicate_lesson_ids_counted_once():
    """The same lesson id appearing twice must not double-count the haircut."""
    dup = _lesson("same", "AAPL", 1, "2026-05-20")
    out = apply_lesson_haircut(
        confidence=0.80, ticker="AAPL", direction=1, decision_asof=DECISION,
        lessons=[dup, dup], per_lesson_haircut=PER_LESSON, floor_fraction=FLOOR,
    )
    assert out == 0.80 * (1.0 - PER_LESSON)  # one haircut, not two


def test_case_insensitive_ticker_match():
    lessons = [_lesson("l1", "aapl", 1, "2026-05-20")]
    out = apply_lesson_haircut(
        confidence=0.80, ticker="AAPL", direction=1, decision_asof=DECISION,
        lessons=lessons, per_lesson_haircut=PER_LESSON, floor_fraction=FLOOR,
    )
    assert out == 0.80 * (1.0 - PER_LESSON)


def test_deterministic():
    lessons = [_lesson("l1", "AAPL", 1, "2026-05-20")]
    a = apply_lesson_haircut(0.8, "AAPL", 1, DECISION, lessons, PER_LESSON, FLOOR)
    b = apply_lesson_haircut(0.8, "AAPL", 1, DECISION, lessons, PER_LESSON, FLOOR)
    assert a == b
