"""tests/eval/test_reflector_faithfulness.py — B41-c reflector faithfulness gate.

ADVISORY-PLANE / EVAL-ONLY gate (ADR-4665 §7.3). These tests prove the gate's
three faithfulness checks over GOLDEN FIXTURES — no live LLM, no network, pinned
inputs, fully deterministic. The Reflector is the LLM stage closest to
default-ON (write-only, off the decision path); this gate produces the
pass/fail verdict a human reads before flipping HERMES_QUANT_REFLECTOR_LLM.

Required scenarios (task DELIVERABLES §2):
  (a) faithful reflection grounded in logged facts        → PASS
  (b) reflection inventing a number not in the record     → grounding FAILS
  (c) reflection leaking post-trade info into a
      decision-feeding field                              → leakage FAILS
  (d) unstable lesson_category across identical inputs    → stability FAILS
  (e) determinism: same fixture twice → identical verdict
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from hermes_quant.eval.reflector_faithfulness import (
    JudgeVerdict,
    ReflectorFaithfulnessGate,
    golden_judge,
    recompute_trade_facts,
)

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "reflector_faithfulness" / "golden.json"


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(_FIXTURE.read_text())


@pytest.fixture
def trade_record(golden: dict) -> dict:
    return golden["trade_record"]


@pytest.fixture
def judge(golden: dict):
    """A deterministic golden judge replaying RECORDED verdicts — no live LLM."""
    return golden_judge(golden["judge_verdicts"])


# ---------------------------------------------------------------------------
# Ground-truth recomputation (the gate trusts the trade record, not the reflection)
# ---------------------------------------------------------------------------


def test_recompute_trade_facts_matches_fixture_math(golden: dict, trade_record: dict) -> None:
    """Facts are recomputed from the trade record, independent of the reflection."""
    facts = recompute_trade_facts(trade_record)
    expected = golden["_derived_ground_truth"]
    assert facts.raw_return == pytest.approx(expected["raw_return"])
    assert facts.alpha_return == pytest.approx(expected["alpha_return"])
    assert facts.holding_days == expected["holding_days"]
    assert facts.tau_floor.isoformat() == expected["tau_floor"]


# ---------------------------------------------------------------------------
# (a) faithful reflection grounded in logged facts → PASS
# ---------------------------------------------------------------------------


def test_a_faithful_reflection_passes(golden: dict, trade_record: dict, judge) -> None:
    gate = ReflectorFaithfulnessGate()
    verdict = gate.evaluate_one(golden["faithful_reflection"], trade_record, judge=judge)
    assert verdict.passed is True, verdict.reasons
    assert verdict.judge_used is True
    # every individual check passed
    assert all(c.passed for c in verdict.checks), [c for c in verdict.checks if not c.passed]


def test_a_grounding_check_names_traced_facts(golden: dict, trade_record: dict, judge) -> None:
    gate = ReflectorFaithfulnessGate()
    verdict = gate.evaluate_one(golden["faithful_reflection"], trade_record, judge=judge)
    grounding = next(c for c in verdict.checks if c.name == "grounding")
    assert grounding.passed
    # all numerical claims in the faithful text were traced to the record
    assert grounding.detail["n_ungrounded"] == 0
    assert grounding.detail["n_claims"] >= 4


# ---------------------------------------------------------------------------
# (b) reflection inventing a number not in the record → grounding FAILS
# ---------------------------------------------------------------------------


def test_b_invented_number_fails_grounding(golden: dict, trade_record: dict, judge) -> None:
    gate = ReflectorFaithfulnessGate()
    verdict = gate.evaluate_one(golden["invented_number_reflection"], trade_record, judge=judge)
    assert verdict.passed is False
    grounding = next(c for c in verdict.checks if c.name == "grounding")
    assert grounding.passed is False
    # the fabricated "22%" data-center figure is the untraceable claim
    assert any("22" in u for u in grounding.detail["ungrounded_claims"]), grounding.detail


def test_b_invented_number_fails_even_without_judge(golden: dict, trade_record: dict) -> None:
    """The deterministic number-tracer alone (no judge) catches the invented figure."""
    gate = ReflectorFaithfulnessGate()
    verdict = gate.evaluate_one(golden["invented_number_reflection"], trade_record, judge=None)
    assert verdict.judge_used is False
    grounding = next(c for c in verdict.checks if c.name == "grounding")
    assert grounding.passed is False


# ---------------------------------------------------------------------------
# (c) leaking post-trade info into a decision-feeding field → leakage FAILS
# ---------------------------------------------------------------------------


def test_c_future_date_in_text_fails_leakage(golden: dict, trade_record: dict, judge) -> None:
    """A future-dated event reference in reflection_text (a decision-feeding field)
    is post-trade knowledge that would leak past tau_observable."""
    gate = ReflectorFaithfulnessGate()
    verdict = gate.evaluate_one(golden["future_date_reflection"], trade_record, judge=judge)
    assert verdict.passed is False
    leakage = next(c for c in verdict.checks if c.name == "no_leakage")
    assert leakage.passed is False
    assert any("2026-05-15" in r for r in leakage.reasons), leakage.reasons


def test_c_understated_tau_fails_leakage(golden: dict, trade_record: dict, judge) -> None:
    """tau_observable below the deterministic floor would admit the reflection to a
    future decision too early — the core no-look-ahead failure."""
    gate = ReflectorFaithfulnessGate()
    verdict = gate.evaluate_one(golden["understated_tau_reflection"], trade_record, judge=judge)
    assert verdict.passed is False
    leakage = next(c for c in verdict.checks if c.name == "no_leakage")
    assert leakage.passed is False
    assert leakage.detail["tau_below_floor"] is True


def test_c_leakage_is_purely_deterministic_no_judge(golden: dict, trade_record: dict) -> None:
    """Leakage (#2) is the core no-look-ahead contract — it must NOT depend on an LLM."""
    gate = ReflectorFaithfulnessGate()
    v_future = gate.evaluate_one(golden["future_date_reflection"], trade_record, judge=None)
    v_undertau = gate.evaluate_one(golden["understated_tau_reflection"], trade_record, judge=None)
    assert next(c for c in v_future.checks if c.name == "no_leakage").passed is False
    assert next(c for c in v_undertau.checks if c.name == "no_leakage").passed is False


# ---------------------------------------------------------------------------
# (c') ABSENT / None tau_observable → leakage FAILS (fail-closed)
#
# None is the literal initial persisted value of tau_observable (a reflection
# persisted before its tau was computed, or via a torn/partial write, carries
# None — tests/memory/test_decisions.py asserts the initial row value is None).
# The PRODUCTION retriever fail-CLOSES on None (excludes the reflection from
# retrieval, retriever.py:366-367). This eval gate exists to CERTIFY the same
# no-look-ahead rule, so it must ALSO treat an absent/None tau as a hard
# leakage failure — never silently PASS it (which it did when None was routed
# through _parse_dt → datetime.now(UTC), always after the deterministic floor).
# Same _parse_dt(None)->now() fail-open family as ar29/ar33/ar35, at this site.
# ---------------------------------------------------------------------------


def test_cp_none_tau_fails_leakage(golden: dict, trade_record: dict, judge) -> None:
    """tau_observable=None (the most-dishonest case: NO observability stamp) must
    fail the no-leakage check, not silently pass. Mirrors the retriever's
    fail-closed exclusion of None (retriever.py:366-367)."""
    gate = ReflectorFaithfulnessGate()
    none_tau = dict(golden["faithful_reflection"])
    none_tau["tau_observable"] = None
    verdict = gate.evaluate_one(none_tau, trade_record, judge=judge)
    leakage = next(c for c in verdict.checks if c.name == "no_leakage")
    assert leakage.passed is False, leakage.detail
    assert verdict.passed is False
    assert any("absent" in r.lower() or "none" in r.lower() for r in leakage.reasons), leakage.reasons


def test_cp_missing_tau_key_fails_leakage(golden: dict, trade_record: dict, judge) -> None:
    """A MISSING tau_observable key (.get -> None) is identical to None: fail-closed."""
    gate = ReflectorFaithfulnessGate()
    missing_tau = dict(golden["faithful_reflection"])
    missing_tau.pop("tau_observable", None)
    verdict = gate.evaluate_one(missing_tau, trade_record, judge=judge)
    leakage = next(c for c in verdict.checks if c.name == "no_leakage")
    assert leakage.passed is False, leakage.detail
    assert verdict.passed is False


def test_cp_blank_tau_fails_leakage(golden: dict, trade_record: dict, judge) -> None:
    """A blank/whitespace tau_observable string is also an absent stamp: fail-closed
    (do not let an empty string slip into _parse_dt and raise / coerce)."""
    gate = ReflectorFaithfulnessGate()
    blank_tau = dict(golden["faithful_reflection"])
    blank_tau["tau_observable"] = "   "
    verdict = gate.evaluate_one(blank_tau, trade_record, judge=judge)
    leakage = next(c for c in verdict.checks if c.name == "no_leakage")
    assert leakage.passed is False, leakage.detail
    assert verdict.passed is False


def test_cp_none_tau_fails_without_judge(golden: dict, trade_record: dict) -> None:
    """The absent-tau failure is purely deterministic — no LLM judge needed."""
    gate = ReflectorFaithfulnessGate()
    none_tau = dict(golden["faithful_reflection"])
    none_tau["tau_observable"] = None
    verdict = gate.evaluate_one(none_tau, trade_record, judge=None)
    leakage = next(c for c in verdict.checks if c.name == "no_leakage")
    assert leakage.passed is False


def test_cp_none_tau_fails_leakage_in_batch(golden: dict, trade_record: dict, judge) -> None:
    """evaluate_batch shares the same _check_no_leakage — an absent tau must fail
    there too (the holed check is line 455's per-reflection leakage call)."""
    gate = ReflectorFaithfulnessGate()
    none_tau = dict(golden["faithful_reflection"])
    none_tau["tau_observable"] = None
    records = {none_tau["decision_id"]: trade_record}
    verdict = gate.evaluate_batch([none_tau], records, judge=judge)
    leakage = next(c for c in verdict.checks if c.name == "no_leakage")
    assert leakage.passed is False, leakage.detail
    assert verdict.passed is False


# ---------------------------------------------------------------------------
# (d) unstable lesson_category across identical inputs → stability FAILS
# ---------------------------------------------------------------------------


def test_d_unstable_lesson_category_fails(golden: dict, trade_record: dict, judge) -> None:
    gate = ReflectorFaithfulnessGate()
    pair = golden["stability_pair"]
    records = {r["decision_id"]: trade_record for r in pair}
    verdict = gate.evaluate_batch(pair, records, judge=judge)
    assert verdict.passed is False
    stability = next(c for c in verdict.checks if c.name == "lesson_stability")
    assert stability.passed is False
    # both drifting categories are surfaced
    assert "correct_call_too_early" in str(stability.detail)
    assert "unknown" in str(stability.detail)


def test_d_consistent_categories_pass_stability(golden: dict, trade_record: dict, judge) -> None:
    """Same trade-class, same category twice → stable."""
    gate = ReflectorFaithfulnessGate()
    a = dict(golden["stability_pair"][0])
    b = dict(golden["stability_pair"][1])
    b["lesson_category"] = a["lesson_category"]  # force agreement
    records = {a["decision_id"]: trade_record}
    verdict = gate.evaluate_batch([a, b], records, judge=judge)
    stability = next(c for c in verdict.checks if c.name == "lesson_stability")
    assert stability.passed is True, stability.reasons


# ---------------------------------------------------------------------------
# (e) determinism: same fixture twice → identical verdict
# ---------------------------------------------------------------------------


def test_e_determinism_identical_verdict_twice(golden: dict, trade_record: dict, judge) -> None:
    gate = ReflectorFaithfulnessGate()
    v1 = gate.evaluate_one(golden["faithful_reflection"], trade_record, judge=judge)
    v2 = gate.evaluate_one(golden["faithful_reflection"], trade_record, judge=judge)
    assert _verdict_signature(v1) == _verdict_signature(v2)


def test_e_determinism_failing_verdict_twice(golden: dict, trade_record: dict, judge) -> None:
    gate = ReflectorFaithfulnessGate()
    v1 = gate.evaluate_one(golden["invented_number_reflection"], trade_record, judge=judge)
    v2 = gate.evaluate_one(golden["invented_number_reflection"], trade_record, judge=judge)
    assert _verdict_signature(v1) == _verdict_signature(v2)
    assert v1.passed is False


def _verdict_signature(v) -> str:
    return json.dumps(
        {
            "passed": v.passed,
            "judge_used": v.judge_used,
            "checks": [
                {"name": c.name, "passed": c.passed, "reasons": c.reasons, "detail": c.detail}
                for c in v.checks
            ],
            "reasons": v.reasons,
        },
        sort_keys=True,
        default=str,
    )


# ---------------------------------------------------------------------------
# Judge seam: golden replay, no live LLM
# ---------------------------------------------------------------------------


def test_golden_judge_replays_recorded_verdict(golden: dict) -> None:
    j = golden_judge(golden["judge_verdicts"])
    v = j(golden["faithful_reflection"], recompute_trade_facts(golden["trade_record"]))
    assert isinstance(v, JudgeVerdict)
    assert v.grounded is True
    assert v.lesson_category == "correct_call_too_early"


def test_judge_grounded_false_can_fail_grounding(golden: dict, trade_record: dict, judge) -> None:
    """When the judge flags an invented claim ungrounded, grounding fails even if the
    deterministic tracer were lenient — the judge is the qualitative layer for #1."""
    gate = ReflectorFaithfulnessGate()
    verdict = gate.evaluate_one(golden["invented_number_reflection"], trade_record, judge=judge)
    grounding = next(c for c in verdict.checks if c.name == "grounding")
    assert grounding.detail["judge_grounded"] is False


# ---------------------------------------------------------------------------
# Advisory-plane / eval-only — the safety-frame regression guard
# ---------------------------------------------------------------------------

_FORBIDDEN_IMPORT_SUBSTRINGS = (
    "risk.gate",
    "risk_gate",
    "governance.kill_switch",
    "kill_switch",
    "react.live",
    "react.paper",
    "sizing",
    "portfolio_state",
)


def _collect_imports(module_path: Path) -> set[str]:
    tree = ast.parse(module_path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names.add(mod)
            for alias in node.names:
                names.add(f"{mod}.{alias.name}")
    return names


def test_advisory_plane_never_touches_gate_or_ladder() -> None:
    """The module imports NOTHING from the risk-gate / kill-switch / sizing-ladder /
    money-write paths, and references no position-sizing write symbol. Eval-only."""
    import hermes_quant.eval.reflector_faithfulness as mod

    src_path = Path(mod.__file__)
    imports = _collect_imports(src_path)
    for imp in imports:
        low = imp.lower()
        for forbidden in _FORBIDDEN_IMPORT_SUBSTRINGS:
            assert forbidden not in low, (
                f"reflector_faithfulness imports a forbidden module: {imp!r} matched {forbidden!r}"
            )

    text = src_path.read_text()
    for sizing_symbol in (
        "target_position_pct", "fill_size_pct", "RiskGate", "KillSwitch",
        "place_order", "submit_order",
    ):
        assert sizing_symbol not in text, (
            f"reflector_faithfulness references a sizing / execution symbol: {sizing_symbol!r}"
        )


def test_no_network_in_test_path(golden: dict, trade_record: dict, judge) -> None:
    """Sanity: the gate evaluates with a golden judge and never constructs an LLMCaller
    or performs I/O beyond the in-memory inputs (httpx is never imported by the gate)."""
    import hermes_quant.eval.reflector_faithfulness as mod

    assert "httpx" not in Path(mod.__file__).read_text()
    gate = ReflectorFaithfulnessGate()
    # Must not raise / must not hit network.
    gate.evaluate_one(golden["faithful_reflection"], trade_record, judge=judge)


def test_reuses_real_grounding_extractor(golden: dict, trade_record: dict) -> None:
    """The grounding check reuses the regression-tested ClaimVerifier extraction
    primitive (extract_numerical_claims), not a private re-implementation."""
    import hermes_quant.eval.reflector_faithfulness as mod

    text = Path(mod.__file__).read_text()
    assert "extract_numerical_claims" in text
    assert "from hermes_quant.grounding" in text


def test_reuses_real_tau_observable_floor() -> None:
    """The tau-floor invariant reuses the Reflector's deterministic _compute_tau_observable,
    so the gate's floor can never drift from the production guard."""
    import hermes_quant.eval.reflector_faithfulness as mod

    text = Path(mod.__file__).read_text()
    assert "_compute_tau_observable" in text
    assert "from hermes_quant.memory.reflector" in text
