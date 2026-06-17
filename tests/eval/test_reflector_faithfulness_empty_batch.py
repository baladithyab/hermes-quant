"""tests/eval/test_reflector_faithfulness_empty_batch.py — empty/degenerate-batch
fail-closed contract for the B41-c reflector faithfulness gate.

EMPTY-COLLECTION money-software defect family. ``ReflectorFaithfulnessGate``
produces the pass/fail verdict a HUMAN reads before flipping
``HERMES_QUANT_REFLECTOR_LLM`` default-ON. The aggregation folds per-reflection
results with ``all(...)`` — and ``all([]) == True`` in Python. So a batch with
ZERO reflections to evaluate (an empty ``reflections=[]`` list) folds to a clean
PASS verdict with no failing reasons. That is a fail-OPEN: a gate that certifies
faithfulness over an empty corpus has certified NOTHING. An empty batch is
insufficient data and must fail-CLOSED (a human must not read "PASS" off a
zero-reflection run).

These tests are self-contained (no golden fixture, no live LLM, no network):
they exercise only the aggregation polarity over an empty vs. a non-empty batch.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hermes_quant.eval.reflector_faithfulness import ReflectorFaithfulnessGate


def _faithful_reflection_and_record() -> tuple[dict, dict]:
    """A single faithful reflection + its trade record, with an HONEST tau.

    Built so the gate PASSES on a non-empty batch — this is the non-vacuity
    anchor: it proves the empty-batch assertion below is not trivially true
    because the gate fails everything.
    """
    asof_dec = datetime(2026, 1, 5, 14, 0, tzinfo=UTC)
    asof_res = datetime(2026, 1, 9, 14, 0, tzinfo=UTC)
    holding_days = 4
    # Deterministic tau floor is resolution + holding seconds + 6h; clear it well.
    tau = asof_res + timedelta(days=holding_days) + timedelta(hours=12)
    record = {
        "ticker": "AAA",
        "direction": 1,
        "entry_price": 100.0,
        "exit_price": 105.0,
        "benchmark_return": 0.01,
        "asof_decision": asof_dec.isoformat(),
        "asof_resolution": asof_res.isoformat(),
    }
    # raw_return = 0.05 -> cite only the grounded 5.0% magnitude.
    reflection = {
        "reflection_id": "r-1",
        "decision_id": "d-1",
        "reflection_text": "The position returned about 5.0% over the holding window.",
        "lesson_category": "trend_follow_win",
        "tau_observable": tau.isoformat(),
        "alpha_return": 0.04,
    }
    records = {"d-1": record}
    return reflection, records


def test_empty_batch_fails_closed() -> None:
    """A zero-reflection batch is insufficient data -> the verdict must NOT pass.

    RED before fix: ``all([])`` folds grounding + leakage to True, stability is
    vacuously True over an empty corpus, and the gate returns passed=True.
    """
    gate = ReflectorFaithfulnessGate()
    verdict = gate.evaluate_batch([], {})

    assert verdict.passed is False, (
        "empty reflection batch certified PASS — fail-OPEN: a faithfulness gate "
        "over ZERO reflections has certified nothing and must fail-closed"
    )
    # And it must SAY why (a silent fail-closed is as bad as a silent pass for the
    # human reading the verdict before flipping the flag).
    assert verdict.reasons, "empty-batch fail-closed verdict carried no reason"
    assert any(
        "empty" in r.lower() or "insufficient" in r.lower() or "no reflection" in r.lower()
        for r in verdict.reasons
    ), verdict.reasons


def test_non_empty_batch_still_passes_unchanged() -> None:
    """Non-vacuity: a healthy non-empty batch still PASSES.

    Guards against a fix that simply hard-fails every batch — the empty-batch
    assertion above must be meaningful, so a real reflection must still clear.
    """
    gate = ReflectorFaithfulnessGate()
    reflection, records = _faithful_reflection_and_record()
    verdict = gate.evaluate_batch([reflection], records)

    assert verdict.passed is True, verdict.reasons
    assert {c.name for c in verdict.checks} == {"grounding", "no_leakage", "lesson_stability"}
