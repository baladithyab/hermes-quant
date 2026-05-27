"""tests/backtest/test_stub_llm.py — StubLLMCommittee determinism tests (Wave 6a / ADR-0045).

Coverage:
- Deterministic: same inputs → identical outputs across N calls
- Direction → recommendation mapping
- Confidence clamping
- llm_caller interface
- Research plan schema
"""

from __future__ import annotations

import pytest

from hermes_quant.backtest.stub_llm import StubLLMCommittee


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stub():
    return StubLLMCommittee()


# ---------------------------------------------------------------------------
# Determinism tests (the core contract)
# ---------------------------------------------------------------------------


class TestStubDeterminism:
    """Bit-identical outputs given same inputs across multiple calls."""

    def test_same_direction_same_plan(self, stub):
        """Calling twice with identical args returns identical dicts."""
        plan1 = stub.research_plan(direction=1, confidence=0.75, symbol="AAPL")
        plan2 = stub.research_plan(direction=1, confidence=0.75, symbol="AAPL")
        assert plan1 == plan2

    def test_deterministic_across_instances(self):
        """Two separate StubLLMCommittee instances agree on all outputs."""
        s1 = StubLLMCommittee()
        s2 = StubLLMCommittee()
        for direction in (-1, 0, 1):
            p1 = s1.research_plan(direction=direction, confidence=0.5, symbol="TEST")
            p2 = s2.research_plan(direction=direction, confidence=0.5, symbol="TEST")
            assert p1 == p2, f"Mismatch for direction={direction}"

    def test_deterministic_100_calls(self, stub):
        """100 repeated calls with same args return identical results."""
        first = stub.research_plan(direction=1, confidence=0.6, symbol="SPY")
        for _ in range(99):
            result = stub.research_plan(direction=1, confidence=0.6, symbol="SPY")
            assert result == first

    def test_llm_caller_interface_deterministic(self, stub):
        """llm_caller (system_prompt, user_prompt) -> str is deterministic."""
        r1 = stub("system: trade SPY", "user: what do you think?")
        r2 = stub("system: trade SPY", "user: what do you think?")
        assert r1 == r2

    def test_different_symbols_same_direction_same_plan_structure(self, stub):
        """Symbol variation does not change recommendation/confidence."""
        plan_aapl = stub.research_plan(direction=1, confidence=0.8, symbol="AAPL")
        plan_goog = stub.research_plan(direction=1, confidence=0.8, symbol="GOOG")
        assert plan_aapl["recommendation"] == plan_goog["recommendation"]
        assert plan_aapl["confidence"] == plan_goog["confidence"]


# ---------------------------------------------------------------------------
# Direction → recommendation mapping
# ---------------------------------------------------------------------------


class TestDirectionMapping:
    """Correct direction to recommendation mapping."""

    def test_positive_direction_is_buy(self, stub):
        plan = stub.research_plan(direction=1, confidence=0.7, symbol="X")
        assert plan["recommendation"] == "Buy"

    def test_zero_direction_is_hold(self, stub):
        plan = stub.research_plan(direction=0, confidence=0.5, symbol="X")
        assert plan["recommendation"] == "Hold"

    def test_negative_direction_is_sell(self, stub):
        plan = stub.research_plan(direction=-1, confidence=0.7, symbol="X")
        assert plan["recommendation"] == "Sell"

    def test_large_positive_direction_clamped_to_buy(self, stub):
        """direction=100 should still map to Buy."""
        plan = stub.research_plan(direction=100, confidence=0.5, symbol="X")
        assert plan["recommendation"] == "Buy"

    def test_large_negative_direction_clamped_to_sell(self, stub):
        plan = stub.research_plan(direction=-99, confidence=0.5, symbol="X")
        assert plan["recommendation"] == "Sell"


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestResearchPlanSchema:
    """Output dict has all required keys."""

    REQUIRED_KEYS = {
        "recommendation",
        "confidence",
        "rationale",
        "strategic_actions",
        "horizon_emphasis",
        "signal_provenance",
    }

    def test_all_required_keys_present(self, stub):
        plan = stub.research_plan(direction=1, confidence=0.6, symbol="TEST")
        for key in self.REQUIRED_KEYS:
            assert key in plan, f"Missing key: {key}"

    def test_confidence_clamped_above_1(self, stub):
        plan = stub.research_plan(direction=1, confidence=2.0, symbol="X")
        assert plan["confidence"] <= 1.0

    def test_confidence_clamped_below_0(self, stub):
        plan = stub.research_plan(direction=1, confidence=-0.5, symbol="X")
        assert plan["confidence"] >= 0.0

    def test_rationale_is_string(self, stub):
        plan = stub.research_plan(direction=0, confidence=0.5, symbol="X")
        assert isinstance(plan["rationale"], str)
        assert len(plan["rationale"]) > 0

    def test_signal_provenance_contains_source(self, stub):
        plan = stub.research_plan(direction=1, confidence=0.5, symbol="Y")
        prov = plan["signal_provenance"]
        assert prov["source"] == "StubLLMCommittee"
        assert prov["symbol"] == "Y"

    def test_llm_caller_returns_string(self, stub):
        result = stub("any system", "any user")
        assert isinstance(result, str)
        assert len(result) > 0
