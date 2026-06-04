"""tests/eval/test_promotion_orchestrator_oos.py — orchestrator threads the
out-of-sample fold-rate to the gate (seed 3767).

The orchestrator's job is to make the gate's new OOS requirement REACHABLE
end-to-end: a caller that has run walk_forward_replay supplies the genuine
positive_excess_fold_rate to ``run(oos_fold_rate=...)``; the orchestrator forwards
it to ``gate.check`` and records it on the PromotionRecord for audit. The change
is ADDITIVE — ``oos_fold_rate=None`` (the default) reproduces today's behavior.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from hermes_quant.eval.promotion_gate import PromotionGate
from hermes_quant.eval.promotion_orchestrator import (
    PromotionLog,
    PromotionOrchestrator,
)
from hermes_quant.eval.stockbench import STOCKBENCHResult

_WINDOW_START = date(2025, 6, 1)
_WINDOW_END = date(2025, 8, 31)
_UNIVERSE = ["AAPL", "MSFT"]


def _passing_result(**overrides) -> STOCKBENCHResult:
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


class _StubHarness:
    def __init__(self, result: STOCKBENCHResult) -> None:
        self._result = result

    def run(self, strategy, universe, window_start, window_end, **kwargs) -> STOCKBENCHResult:
        return self._result


class _AlwaysLong:
    def decide(self, ticker, as_of, price_history):
        return 1.0


def test_orchestrator_forwards_failing_oos_fold_rate_to_gate(tmp_path: Path) -> None:
    """Strong in-sample result + a failing OOS fold-rate -> the orchestrator's
    record shows promote=False with an OOS reason."""
    orch = PromotionOrchestrator(
        gate=PromotionGate(),  # default floor 0.60
        log=PromotionLog(path=tmp_path / "promo.jsonl"),
        harness=_StubHarness(_passing_result()),
    )
    record = orch.run(
        strategy=_AlwaysLong(),
        universe=list(_UNIVERSE),
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        oos_fold_rate=0.20,
        auto_record=False,
    )
    assert record.decision["promote"] is False
    assert any("oos" in r.lower() or "fold" in r.lower() for r in record.decision["reasons"])


def test_orchestrator_forwards_passing_oos_fold_rate_to_gate(tmp_path: Path) -> None:
    orch = PromotionOrchestrator(
        gate=PromotionGate(),
        log=PromotionLog(path=tmp_path / "promo.jsonl"),
        harness=_StubHarness(_passing_result()),
    )
    record = orch.run(
        strategy=_AlwaysLong(),
        universe=list(_UNIVERSE),
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        oos_fold_rate=0.80,
        auto_record=False,
    )
    assert record.decision["promote"] is True


def test_orchestrator_records_oos_fold_rate_for_audit(tmp_path: Path) -> None:
    """The fold-rate the decision was made on is captured on the record so the
    operator can audit WHY a promotion was held."""
    orch = PromotionOrchestrator(
        gate=PromotionGate(),
        log=PromotionLog(path=tmp_path / "promo.jsonl"),
        harness=_StubHarness(_passing_result()),
    )
    record = orch.run(
        strategy=_AlwaysLong(),
        universe=list(_UNIVERSE),
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        oos_fold_rate=0.20,
        auto_record=False,
    )
    assert record.oos_fold_rate == 0.20


def test_orchestrator_without_oos_is_byte_identical_to_today(tmp_path: Path) -> None:
    """Default path (no oos_fold_rate) promotes a strong in-sample result and
    records oos_fold_rate=None — no behavior change for existing callers."""
    orch = PromotionOrchestrator(
        gate=PromotionGate(),
        log=PromotionLog(path=tmp_path / "promo.jsonl"),
        harness=_StubHarness(_passing_result()),
    )
    record = orch.run(
        strategy=_AlwaysLong(),
        universe=list(_UNIVERSE),
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        auto_record=False,
    )
    assert record.decision["promote"] is True
    assert record.oos_fold_rate is None


def test_orchestrator_require_oos_gate_rejects_when_no_fold_rate(tmp_path: Path) -> None:
    """A require_oos gate with no fold-rate supplied REJECTS through the
    orchestrator (fail-closed end-to-end)."""
    orch = PromotionOrchestrator(
        gate=PromotionGate(require_oos=True),
        log=PromotionLog(path=tmp_path / "promo.jsonl"),
        harness=_StubHarness(_passing_result()),
    )
    record = orch.run(
        strategy=_AlwaysLong(),
        universe=list(_UNIVERSE),
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
        auto_record=False,
    )
    assert record.decision["promote"] is False
