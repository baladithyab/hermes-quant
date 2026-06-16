"""tests/eval/test_promotion_orchestrator.py — Tests for PromotionOrchestrator (ADR-0052)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

from hermes_quant.eval.promotion_gate import PromotionDecision, PromotionGate
from hermes_quant.eval.promotion_orchestrator import (
    PromotionLog,
    PromotionOrchestrator,
    PromotionRecord,
    _summarise_result,
)
from hermes_quant.eval.stockbench import STOCKBENCHResult
from hermes_quant.research.hypothesis import AppendOnlyViolation


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------

_WINDOW_START = date(2025, 6, 1)
_WINDOW_END = date(2025, 8, 31)
_UNIVERSE = ["AAPL", "MSFT"]


def _make_passing_result(**overrides) -> STOCKBENCHResult:
    """Return a STOCKBENCHResult that passes all gate criteria."""
    defaults = dict(
        universe=list(_UNIVERSE),
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        benchmark="SPY",
        cumulative_return=0.12,
        max_drawdown=-0.05,
        sortino=1.2,
        n_decisions=20,
        decisions_per_day_avg=0.22,
        vs_buyhold_alpha=0.03,
        contamination_guard_fired=False,
        metadata={"buyhold_cumulative_return": 0.09},
    )
    defaults.update(overrides)
    return STOCKBENCHResult(**defaults)


def _make_failing_result() -> STOCKBENCHResult:
    """Return a result that fails alpha + sortino criteria."""
    return _make_passing_result(
        vs_buyhold_alpha=-0.01,
        sortino=0.2,
        max_drawdown=-0.05,
    )


class _StubHarness:
    """Stub harness that returns a pre-configured STOCKBENCHResult."""

    def __init__(self, result: STOCKBENCHResult) -> None:
        self._result = result
        self.call_count = 0

    def run(self, strategy, universe, window_start, window_end, **kwargs) -> STOCKBENCHResult:
        self.call_count += 1
        return self._result


class _StubGate:
    """Stub gate that returns a pre-configured PromotionDecision."""

    def __init__(self, decision: PromotionDecision) -> None:
        self._decision = decision
        self.call_count = 0

    def check(self, result: STOCKBENCHResult) -> PromotionDecision:
        self.call_count += 1
        return self._decision


class _AlwaysLong:
    def decide(self, ticker, as_of, price_history):
        return 1.0


# ---------------------------------------------------------------------------
# 1. PromotionRecord round-trip through PromotionLog
# ---------------------------------------------------------------------------


def test_promotion_record_round_trip(tmp_path: Path) -> None:
    """PromotionRecord serialises → persists → deserialises without data loss."""
    log = PromotionLog(path=tmp_path / "promo.jsonl")
    result = _make_passing_result()
    gate = PromotionGate()
    decision = gate.check(result)

    record = PromotionRecord.from_result_and_decision(
        result=result,
        decision=decision,
        strategy_name="buyhold",
        hypothesis_id="hyp_test_001",
    )
    record_id = log.record(record)

    recovered = log.read(record_id)
    assert recovered is not None
    assert recovered.record_id == record.record_id
    assert recovered.strategy_name == "buyhold"
    assert recovered.hypothesis_id == "hyp_test_001"
    assert recovered.window_start == _WINDOW_START
    assert recovered.window_end == _WINDOW_END
    assert recovered.decision["promote"] is True
    assert recovered.schema_version == 1


# ---------------------------------------------------------------------------
# 2. Append-only enforcement — truncate raises
# ---------------------------------------------------------------------------


def test_promotion_log_truncate_raises(tmp_path: Path) -> None:
    """truncate() must raise AppendOnlyViolation."""
    log = PromotionLog(path=tmp_path / "promo.jsonl")
    with pytest.raises(AppendOnlyViolation, match="append-only"):
        log.truncate()


# ---------------------------------------------------------------------------
# 3. Append-only enforcement — update raises
# ---------------------------------------------------------------------------


def test_promotion_log_update_raises(tmp_path: Path) -> None:
    """update() must raise AppendOnlyViolation."""
    log = PromotionLog(path=tmp_path / "promo.jsonl")
    with pytest.raises(AppendOnlyViolation, match="append-only"):
        log.update(something="val")


# ---------------------------------------------------------------------------
# 4. PromotionOrchestrator.run() with stubs produces deterministic record
# ---------------------------------------------------------------------------


def test_orchestrator_run_deterministic(tmp_path: Path) -> None:
    """PromotionOrchestrator.run() with stub harness + gate returns a PromotionRecord."""
    result = _make_passing_result()
    decision = PromotionDecision(
        promote=True,
        reasons=[],
        suggested_action="Paper-trade for 6 months.",
    )
    stub_harness = _StubHarness(result)
    stub_gate = _StubGate(decision)
    log = PromotionLog(path=tmp_path / "promo.jsonl")

    orch = PromotionOrchestrator(gate=stub_gate, log=log, harness=stub_harness)
    record = orch.run(
        strategy=_AlwaysLong(),
        universe=list(_UNIVERSE),
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        auto_record=True,
    )

    assert isinstance(record, PromotionRecord)
    assert record.decision["promote"] is True
    assert stub_harness.call_count == 1
    assert stub_gate.call_count == 1
    # Persisted
    assert log.read(record.record_id) is not None


# ---------------------------------------------------------------------------
# 5. auto_record=False → no JSONL row written
# ---------------------------------------------------------------------------


def test_orchestrator_auto_record_false(tmp_path: Path) -> None:
    """When auto_record=False, no row is appended to the log."""
    result = _make_passing_result()
    decision = PromotionDecision(promote=True, reasons=[], suggested_action="OK")
    log = PromotionLog(path=tmp_path / "promo.jsonl")
    orch = PromotionOrchestrator(
        gate=_StubGate(decision),
        log=log,
        harness=_StubHarness(result),
    )

    record = orch.run(
        strategy=_AlwaysLong(),
        universe=list(_UNIVERSE),
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        auto_record=False,
    )

    assert record is not None
    assert log.read(record.record_id) is None  # not persisted


# ---------------------------------------------------------------------------
# 6. hypothesis_id passes through to record
# ---------------------------------------------------------------------------


def test_hypothesis_id_pass_through(tmp_path: Path) -> None:
    """hypothesis_id supplied to .run() appears in the PromotionRecord."""
    result = _make_passing_result()
    decision = PromotionDecision(promote=True, reasons=[], suggested_action="OK")
    log = PromotionLog(path=tmp_path / "promo.jsonl")
    orch = PromotionOrchestrator(
        gate=_StubGate(decision),
        log=log,
        harness=_StubHarness(result),
    )

    record = orch.run(
        strategy=_AlwaysLong(),
        universe=list(_UNIVERSE),
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        hypothesis_id="hyp_xyz_42",
        auto_record=False,
    )

    assert record.hypothesis_id == "hyp_xyz_42"


# ---------------------------------------------------------------------------
# 7. Failing result → promote=False, reasons populated
# ---------------------------------------------------------------------------


def test_orchestrator_failing_strategy(tmp_path: Path) -> None:
    """A failing STOCKBENCHResult propagates promote=False + reasons."""
    result = _make_failing_result()
    log = PromotionLog(path=tmp_path / "promo.jsonl")
    orch = PromotionOrchestrator(log=log, harness=_StubHarness(result))

    record = orch.run(
        strategy=_AlwaysLong(),
        universe=list(_UNIVERSE),
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        auto_record=True,
    )

    assert record.decision["promote"] is False
    assert len(record.decision["reasons"]) >= 1


# ---------------------------------------------------------------------------
# 8. Multiple records can be appended and read back (read_all)
# ---------------------------------------------------------------------------


def test_promotion_log_read_all(tmp_path: Path) -> None:
    """PromotionLog.read_all() returns all records in insertion order."""
    log = PromotionLog(path=tmp_path / "promo.jsonl")
    gate = PromotionGate()

    ids = []
    for i in range(3):
        result = _make_passing_result()
        decision = gate.check(result)
        record = PromotionRecord.from_result_and_decision(
            result=result,
            decision=decision,
            strategy_name=f"strat_{i}",
        )
        ids.append(log.record(record))

    all_records = log.read_all()
    assert len(all_records) == 3
    for i, r in enumerate(all_records):
        assert r.strategy_name == f"strat_{i}"


# ---------------------------------------------------------------------------
# 9. PromotionRecord extra fields forbidden (extra="forbid")
# ---------------------------------------------------------------------------


def test_promotion_record_extra_forbid() -> None:
    """PromotionRecord rejects unknown fields."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="extra"):
        PromotionRecord(
            strategy_name="test",
            window_start=_WINDOW_START,
            window_end=_WINDOW_END,
            decision={"promote": True, "reasons": [], "suggested_action": "OK"},
            unknown_field="should_fail",
        )


# ---------------------------------------------------------------------------
# 10. record_id auto-generated and starts with "prom_"
# ---------------------------------------------------------------------------


def test_record_id_auto_generated() -> None:
    """Auto-generated record_id starts with 'prom_' and is 13 chars."""
    result = _make_passing_result()
    decision = PromotionGate().check(result)
    record = PromotionRecord.from_result_and_decision(
        result=result,
        decision=decision,
        strategy_name="buyhold",
    )
    assert record.record_id.startswith("prom_")
    # "prom_" + 8 hex chars = 13 characters
    assert len(record.record_id) == 13


# ---------------------------------------------------------------------------
# 11. PromotionLog.read_for_strategy filters correctly
# ---------------------------------------------------------------------------


def test_promotion_log_read_for_strategy(tmp_path: Path) -> None:
    """read_for_strategy() returns only records for the named strategy."""
    log = PromotionLog(path=tmp_path / "promo.jsonl")
    gate = PromotionGate()
    result = _make_passing_result()
    decision = gate.check(result)

    for name in ["strat_a", "strat_b", "strat_a"]:
        log.record(
            PromotionRecord.from_result_and_decision(
                result=result, decision=decision, strategy_name=name
            )
        )

    a_records = log.read_for_strategy("strat_a")
    b_records = log.read_for_strategy("strat_b")
    assert len(a_records) == 2
    assert len(b_records) == 1


# ---------------------------------------------------------------------------
# 12. _summarise_result caps keys at ≤ 20
# ---------------------------------------------------------------------------


def test_summarise_result_max_keys() -> None:
    """_summarise_result produces ≤ 20 keys."""
    result = _make_passing_result()
    summary = _summarise_result(result)
    assert len(summary) <= 20


# ---------------------------------------------------------------------------
# 13. Contamination guard flag propagates into record summary
# ---------------------------------------------------------------------------


def test_contamination_guard_flag_in_summary() -> None:
    """When contamination_guard_fired=True, summary reflects it."""
    result = _make_passing_result(contamination_guard_fired=True)
    summary = _summarise_result(result)
    assert summary["contamination_guard_fired"] is True


# ---------------------------------------------------------------------------
# 13b. Non-finite Sortino serialises as JSON null (not `Infinity`/`NaN` tokens)
# ---------------------------------------------------------------------------


def test_summary_inf_sortino_serialises_as_null() -> None:
    """A legitimate +inf Sortino (no-downside) → null in strict JSON.

    `json.dumps` would otherwise emit the non-standard `Infinity` token, which
    strict JSON readers (and `json.loads(..., parse_constant=...)` consumers of
    promotion_decisions.jsonl) reject.
    """
    import json

    result = _make_passing_result(sortino=float("inf"))
    summary = _summarise_result(result)
    assert summary["sortino"] is None
    # Round-trips through STRICT json (no non-standard tokens permitted).
    line = json.dumps(summary, default=str, allow_nan=False)
    assert json.loads(line)["sortino"] is None


def test_summary_nan_sortino_serialises_as_null() -> None:
    """A NaN Sortino (malformed / empty window) → null in strict JSON."""
    import json

    result = _make_passing_result(sortino=float("nan"))
    summary = _summarise_result(result)
    assert summary["sortino"] is None
    line = json.dumps(summary, default=str, allow_nan=False)
    assert json.loads(line)["sortino"] is None


def test_summary_finite_sortino_preserved() -> None:
    """A finite Sortino is rounded and preserved (not nulled)."""
    result = _make_passing_result(sortino=1.234567)
    summary = _summarise_result(result)
    assert summary["sortino"] == 1.234567


# ---------------------------------------------------------------------------
# 14. PromotionOrchestrator defaults: gate, log, harness are created lazily
# ---------------------------------------------------------------------------


def test_orchestrator_defaults_created() -> None:
    """PromotionOrchestrator with no args instantiates default components."""
    from hermes_quant.eval.promotion_orchestrator import PromotionOrchestrator, PromotionLog
    from hermes_quant.eval.promotion_gate import PromotionGate
    from hermes_quant.eval.stockbench import STOCKBENCHHarness

    orch = PromotionOrchestrator()
    assert isinstance(orch.gate, PromotionGate)
    assert isinstance(orch.log, PromotionLog)
    assert isinstance(orch.harness, STOCKBENCHHarness)
