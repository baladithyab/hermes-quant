"""tests/eval/test_promotion_gate.py — PromotionGate decision-support tests.

Coverage:
    - Promotes when all criteria pass (strong synthetic result)
    - Does NOT promote on negative alpha (beats buy-and-hold criterion fails)
    - Does NOT promote on low Sortino (< 0.5)
    - Does NOT promote on excessive drawdown (< -0.20)
    - Does NOT promote when contamination_guard_fired=True
    - Multiple failing criteria enumerated in reasons list
    - reasons is empty when promote=True
    - suggested_action is non-empty string in all cases
    - Custom thresholds accepted
    - PromotionDecision is frozen / hashable
    - Zero-alpha exactly at threshold does NOT promote (> not ≥)
    - Inf Sortino promotes (unlimited upside)
    - NaN Sortino does NOT promote
"""

from __future__ import annotations

import math
from datetime import date
from unittest.mock import MagicMock

import numpy as np
import pytest

from hermes_quant.eval.promotion_gate import PromotionGate, PromotionDecision
from hermes_quant.eval.stockbench import STOCKBENCHResult

# ---------------------------------------------------------------------------
# Synthetic result factories
# ---------------------------------------------------------------------------


def _make_result(**overrides) -> STOCKBENCHResult:
    """Build a STOCKBENCHResult with strong defaults, then apply overrides."""
    defaults = dict(
        universe=["AAPL", "MSFT", "NVDA"],
        window_start=date(2025, 6, 1),
        window_end=date(2025, 8, 30),
        benchmark="SPY",
        cumulative_return=0.12,
        max_drawdown=-0.08,
        sortino=1.2,
        n_decisions=45,
        decisions_per_day_avg=0.5,
        vs_buyhold_alpha=0.05,
        contamination_guard_fired=False,
    )
    defaults.update(overrides)
    return STOCKBENCHResult(**defaults)


# ---------------------------------------------------------------------------
# Promote cases
# ---------------------------------------------------------------------------


class TestPromotionGatePromote:
    def test_all_criteria_pass_promotes(self):
        gate = PromotionGate()
        result = _make_result(
            vs_buyhold_alpha=0.05,
            sortino=1.2,
            max_drawdown=-0.08,
            contamination_guard_fired=False,
        )
        decision = gate.check(result)
        assert decision.promote is True
        assert decision.reasons == []
        assert len(decision.suggested_action) > 0

    def test_inf_sortino_promotes(self):
        """Infinite Sortino (no downside) should still promote."""
        gate = PromotionGate()
        decision = gate.check(_make_result(sortino=float("inf")))
        assert decision.promote is True

    def test_custom_thresholds_honoured(self):
        gate = PromotionGate(
            alpha_threshold=-0.05,
            sortino_threshold=0.1,
            max_drawdown_floor=-0.50,
        )
        result = _make_result(
            vs_buyhold_alpha=-0.02,  # > -0.05 → passes custom
            sortino=0.2,             # > 0.1 → passes custom
            max_drawdown=-0.30,      # > -0.50 → passes custom
        )
        decision = gate.check(result)
        assert decision.promote is True


# ---------------------------------------------------------------------------
# Reject cases
# ---------------------------------------------------------------------------


class TestPromotionGateReject:
    def test_negative_alpha_fails(self):
        gate = PromotionGate()
        decision = gate.check(_make_result(vs_buyhold_alpha=-0.01))
        assert decision.promote is False
        assert any("vs_buyhold_alpha" in r for r in decision.reasons)

    def test_exactly_zero_alpha_fails(self):
        """alpha=0.0 is NOT > 0 → must reject."""
        gate = PromotionGate()
        decision = gate.check(_make_result(vs_buyhold_alpha=0.0))
        assert decision.promote is False

    def test_low_sortino_fails(self):
        gate = PromotionGate()
        decision = gate.check(_make_result(sortino=0.3))
        assert decision.promote is False
        assert any("sortino" in r for r in decision.reasons)

    def test_excessive_drawdown_fails(self):
        gate = PromotionGate()
        decision = gate.check(_make_result(max_drawdown=-0.25))
        assert decision.promote is False
        assert any("max_drawdown" in r or "drawdown" in r.lower() for r in decision.reasons)

    def test_contamination_guard_fired_fails(self):
        gate = PromotionGate()
        decision = gate.check(_make_result(contamination_guard_fired=True))
        assert decision.promote is False
        assert any("contamination" in r.lower() for r in decision.reasons)

    def test_nan_sortino_fails(self):
        gate = PromotionGate()
        decision = gate.check(_make_result(sortino=float("nan")))
        assert decision.promote is False

    def test_uniform_loss_strategy_does_not_promote(self):
        """A net-losing strategy with uniform-magnitude stop-loss days must NOT
        promote.

        Regression for the fail-open Sortino defect: `std(neg)` about the
        losers' own mean collapsed to ~0 for uniform-magnitude losses, producing
        a spurious +inf Sortino (the BEST possible) that cleared this gate even
        though the strategy is a net loser. The RMS-about-MAR=0 downside fix
        makes the Sortino correctly negative so the gate rejects.
        """
        from hermes_quant.eval.stockbench import _compute_sortino

        # Constant -2% stop-loss days dominate small up days → net loser.
        rets = np.array(
            [0.01, -0.02, 0.01, -0.02, 0.01, -0.02, 0.01, -0.02, 0.005, -0.02]
        )
        sortino = _compute_sortino(rets)
        assert math.isfinite(sortino) and sortino < 0.0

        gate = PromotionGate()
        # Feed the computed Sortino through the gate (alpha/dd kept passing so
        # ONLY the Sortino criterion can fail → isolates the regression).
        decision = gate.check(_make_result(sortino=sortino))
        assert decision.promote is False
        assert any("sortino" in r for r in decision.reasons)

    def test_nan_alpha_fails(self):
        gate = PromotionGate()
        decision = gate.check(_make_result(vs_buyhold_alpha=float("nan")))
        assert decision.promote is False

    def test_multiple_failures_enumerated_in_reasons(self):
        gate = PromotionGate()
        result = _make_result(
            vs_buyhold_alpha=-0.05,
            sortino=0.1,
            max_drawdown=-0.30,
        )
        decision = gate.check(result)
        assert decision.promote is False
        # All three criteria failed → should have 3 reasons
        assert len(decision.reasons) == 3

    def test_suggested_action_non_empty_on_reject(self):
        gate = PromotionGate()
        decision = gate.check(_make_result(vs_buyhold_alpha=-0.01))
        assert len(decision.suggested_action) > 0

    def test_suggested_action_varies_by_failure_count(self):
        gate = PromotionGate()
        d_single = gate.check(_make_result(vs_buyhold_alpha=-0.01))
        d_multi = gate.check(_make_result(
            vs_buyhold_alpha=-0.01, sortino=0.1, max_drawdown=-0.30
        ))
        # Single-failure and multi-failure messages should differ
        assert d_single.suggested_action != d_multi.suggested_action


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class TestPromotionDecisionModel:
    def test_decision_is_frozen(self):
        d = PromotionDecision(promote=True, reasons=[], suggested_action="ok")
        with pytest.raises((TypeError, AttributeError)):
            d.promote = False  # type: ignore[misc]

    def test_reasons_list_preserved(self):
        reasons = ["reason_a", "reason_b"]
        d = PromotionDecision(promote=False, reasons=reasons, suggested_action="fix it")
        assert d.reasons == reasons
