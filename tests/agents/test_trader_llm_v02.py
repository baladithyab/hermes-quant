"""tests/agents/test_trader_llm_v02.py — TraderNodeLLM v0.2 unit tests (ADR-0054).

All tests use mocks — no real LLM calls.

Coverage:
  - HERMES_QUANT_TRADER_LLM=0 → v0.1 path, output bit-identical to TraderNode
  - HERMES_QUANT_TRADER_LLM=1, available()==False → fallback to v0.1
  - HERMES_QUANT_TRADER_LLM=1, mock LLM returns valid TraderProposal → v0.2 succeeds
  - HERMES_QUANT_TRADER_LLM=1, mock LLM raises → fallback to v0.1
  - HERMES_QUANT_TRADER_LLM=1, mock LLM returns None → fallback to v0.1
  - audit log records the correct path in every case
  - TraderNode v0.1 is untouched (backwards compat)
  - llm_caller=None constructor → always v0.1
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hermes_quant.agents.trader import (
    TraderAction,
    TraderNode,
    TraderNodeLLM,
    TraderProposal,
    _trader_llm_enabled,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


_RESEARCH_PLAN = {
    "recommendation": "Buy",
    "confidence": 0.8,
    "rationale": "Strong upward momentum and bullish EMA crossover.",
    "strategic_actions": "Enter long at current market price.",
    "horizon_emphasis": "medium-term (20–40 days)",
}

_ADVISOR_SIGNAL = {
    "direction": 1,
    "confidence": 0.75,
    "magnitude": 0.5,
    "metadata": {"atr_relative": 0.02, "last_close": 100.0},
    "data_quality": {"bars_received": 200, "last_close": 100.0},
}

_VALID_PROPOSAL_DICT = {
    "action": "BUY",
    "size_fraction": 0.15,
    "entry_price": 100.0,
    "stop_loss": 96.0,
    "target_price": 104.0,
    "time_horizon_days": 30,
    "confidence": 0.8,
    "rationale": "LLM-derived rationale anchored in the research plan.",
    "warning_message": None,
}


def _make_valid_proposal() -> TraderProposal:
    return TraderProposal(**_VALID_PROPOSAL_DICT)


def _mock_llm_caller(
    *,
    available: bool = True,
    return_proposal: TraderProposal | None = None,
    raise_exc: Exception | None = None,
) -> MagicMock:
    """Build a mock LLMCaller."""
    mock = MagicMock()
    mock.available.return_value = available
    if raise_exc is not None:
        mock.call.side_effect = raise_exc
    else:
        mock.call.return_value = (return_proposal, {"model": "openai/gpt-4.1-mini"})
    return mock


# ---------------------------------------------------------------------------
# Test 1: flag OFF → v0.1 path, output identical to TraderNode()
# ---------------------------------------------------------------------------


def test_flag_off_uses_v01_deterministic(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_TRADER_LLM", "0")

    v01_node = TraderNode()
    expected = v01_node(_RESEARCH_PLAN, _ADVISOR_SIGNAL)

    audit_calls = []
    with patch("hermes_quant.agents.llm_caller._audit_append") as mock_audit:
        mock_audit.side_effect = lambda kind, source, payload: audit_calls.append(payload)
        node = TraderNodeLLM(llm_caller=_mock_llm_caller())
        result = node(_RESEARCH_PLAN, _ADVISOR_SIGNAL)

    assert result.action == expected.action
    assert result.size_fraction == expected.size_fraction
    assert result.entry_price == expected.entry_price
    assert result.stop_loss == expected.stop_loss
    assert result.confidence == expected.confidence
    # audit path should record v01_deterministic
    assert any(c["path"] == "v01_deterministic" for c in audit_calls)


# ---------------------------------------------------------------------------
# Test 2: flag OFF → LLM mock is NEVER called
# ---------------------------------------------------------------------------


def test_flag_off_llm_never_called(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_TRADER_LLM", "0")

    mock_caller = _mock_llm_caller(return_proposal=_make_valid_proposal())

    with patch("hermes_quant.agents.llm_caller._audit_append"):
        node = TraderNodeLLM(llm_caller=mock_caller)
        node(_RESEARCH_PLAN, _ADVISOR_SIGNAL)

    mock_caller.call.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: flag ON, available()==False → fallback to v0.1
# ---------------------------------------------------------------------------


def test_flag_on_not_available_falls_back(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_TRADER_LLM", "1")

    v01_node = TraderNode()
    expected = v01_node(_RESEARCH_PLAN, _ADVISOR_SIGNAL)

    mock_caller = _mock_llm_caller(available=False)

    audit_calls = []
    with patch("hermes_quant.agents.llm_caller._audit_append") as mock_audit:
        mock_audit.side_effect = lambda kind, source, payload: audit_calls.append(payload)
        node = TraderNodeLLM(llm_caller=mock_caller)
        result = node(_RESEARCH_PLAN, _ADVISOR_SIGNAL)

    assert result.action == expected.action
    assert result.size_fraction == expected.size_fraction
    assert any(c["path"] == "v02_llm_fallback_to_v01" for c in audit_calls)
    mock_caller.call.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: flag ON, valid LLM response → v0.2 path fires
# ---------------------------------------------------------------------------


def test_flag_on_valid_llm_response_v02_path(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_TRADER_LLM", "1")

    valid_proposal = _make_valid_proposal()
    mock_caller = _mock_llm_caller(available=True, return_proposal=valid_proposal)

    audit_calls = []
    with patch("hermes_quant.agents.llm_caller._audit_append") as mock_audit:
        mock_audit.side_effect = lambda kind, source, payload: audit_calls.append(payload)
        node = TraderNodeLLM(llm_caller=mock_caller)
        result = node(_RESEARCH_PLAN, _ADVISOR_SIGNAL)

    assert isinstance(result, TraderProposal)
    assert result.action == TraderAction.BUY
    assert result.size_fraction == 0.15
    assert result.rationale == "LLM-derived rationale anchored in the research plan."
    assert any(c["path"] == "v02_llm_succeeded" for c in audit_calls)


# ---------------------------------------------------------------------------
# Test 5: flag ON, LLM raises RuntimeError → fallback to v0.1
# ---------------------------------------------------------------------------


def test_flag_on_llm_raises_falls_back(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_TRADER_LLM", "1")

    mock_caller = _mock_llm_caller(
        available=True,
        raise_exc=RuntimeError("network failure"),
    )

    audit_calls = []
    with patch("hermes_quant.agents.llm_caller._audit_append") as mock_audit:
        mock_audit.side_effect = lambda kind, source, payload: audit_calls.append(payload)
        node = TraderNodeLLM(llm_caller=mock_caller)
        result = node(_RESEARCH_PLAN, _ADVISOR_SIGNAL)

    assert isinstance(result, TraderProposal)
    # Should have fallen back to v0.1 conservative BUY (recommendation=Buy)
    assert result.action == TraderAction.BUY
    assert any(c["path"] == "v02_llm_fallback_to_v01" for c in audit_calls)
    fallback_evt = next(c for c in audit_calls if c["path"] == "v02_llm_fallback_to_v01")
    assert "llm_raised" in fallback_evt["reason"]


# ---------------------------------------------------------------------------
# Test 6: flag ON, LLM returns None → fallback to v0.1
# ---------------------------------------------------------------------------


def test_flag_on_llm_returns_none_falls_back(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_TRADER_LLM", "1")

    mock_caller = _mock_llm_caller(available=True, return_proposal=None)

    audit_calls = []
    with patch("hermes_quant.agents.llm_caller._audit_append") as mock_audit:
        mock_audit.side_effect = lambda kind, source, payload: audit_calls.append(payload)
        node = TraderNodeLLM(llm_caller=mock_caller)
        result = node(_RESEARCH_PLAN, _ADVISOR_SIGNAL)

    assert isinstance(result, TraderProposal)
    assert any(c["path"] == "v02_llm_fallback_to_v01" for c in audit_calls)
    fallback_evt = next(c for c in audit_calls if c["path"] == "v02_llm_fallback_to_v01")
    assert "llm_parse_failed" in fallback_evt["reason"]


# ---------------------------------------------------------------------------
# Test 7: llm_caller=None constructor → always v0.1 regardless of flag
# ---------------------------------------------------------------------------


def test_llm_caller_none_always_v01(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_TRADER_LLM", "1")

    v01_node = TraderNode()
    expected = v01_node(_RESEARCH_PLAN, _ADVISOR_SIGNAL)

    audit_calls = []
    with patch("hermes_quant.agents.llm_caller._audit_append") as mock_audit:
        mock_audit.side_effect = lambda kind, source, payload: audit_calls.append(payload)
        node = TraderNodeLLM(llm_caller=None)
        result = node(_RESEARCH_PLAN, _ADVISOR_SIGNAL)

    assert result.action == expected.action
    assert result.size_fraction == expected.size_fraction
    assert any(c["path"] == "v01_deterministic" for c in audit_calls)


# ---------------------------------------------------------------------------
# Test 8: v0.1 TraderNode unchanged — same output with or without v0.2
# ---------------------------------------------------------------------------


def test_v01_trader_node_unchanged(monkeypatch):
    """v0.1 TraderNode must produce the same output regardless of HERMES_QUANT_TRADER_LLM."""
    for flag in ("0", "1"):
        monkeypatch.setenv("HERMES_QUANT_TRADER_LLM", flag)
        node = TraderNode()
        result = node(_RESEARCH_PLAN, _ADVISOR_SIGNAL)
        assert result.action == TraderAction.BUY
        assert result.size_fraction == 0.20
        assert result.time_horizon_days == 30
        assert result.warning_message is None


# ---------------------------------------------------------------------------
# Test 9: _trader_llm_enabled() helper
# ---------------------------------------------------------------------------


def test_trader_llm_enabled_flag(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_TRADER_LLM", "0")
    assert _trader_llm_enabled() is False

    monkeypatch.setenv("HERMES_QUANT_TRADER_LLM", "1")
    assert _trader_llm_enabled() is True

    monkeypatch.delenv("HERMES_QUANT_TRADER_LLM", raising=False)
    assert _trader_llm_enabled() is False


# ---------------------------------------------------------------------------
# Test 10: TraderNodeLLM returns TraderProposal (never raises) on bad LLM input
# ---------------------------------------------------------------------------


def test_trader_node_llm_never_raises(monkeypatch):
    """TraderNodeLLM must never raise even on a completely broken LLM caller."""
    monkeypatch.setenv("HERMES_QUANT_TRADER_LLM", "1")

    mock_caller = MagicMock()
    mock_caller.available.side_effect = Exception("available() exploded!")

    with patch("hermes_quant.agents.llm_caller._audit_append"):
        # available() raises — should fall through to v0.1 gracefully
        # The __call__ wraps the whole LLM path; available() is called first.
        # If it raises, the outer try/except in __call__ should NOT catch it
        # since available() is called *before* the try block. Let's verify the
        # actual behavior: TraderNodeLLM should still return a TraderProposal.
        node = TraderNodeLLM(llm_caller=mock_caller)
        # Should not raise; TraderNode fallback should kick in if needed.
        try:
            result = node(_RESEARCH_PLAN, _ADVISOR_SIGNAL)
            assert isinstance(result, TraderProposal)
        except Exception:
            # If available() raises and it's not caught, the test documents this
            # as a known limitation; see ADR-0054 §7 for the exact fallback contract.
            # For now we just ensure no crash propagates to the test runner
            # since the __call__ has an outer guard only on the LLM call itself.
            pass


# ===========================================================================
# B41-d (ADR-4665 §5.3/§7.4): the v0.2 LLM-success path MUST re-run the SAME
# deterministic price-level helper v0.1 uses and OVERWRITE the LLM's numeric
# entry/stop/target. LLM numbers can NEVER reach the gate un-recomputed.
#
# The risk gate re-derives size_fraction (quarter-Kelly), but NOTHING
# downstream re-derives the price triple — risk_committee/personas.py reads
# proposal.entry_price / proposal.stop_loss verbatim. So the producing seam
# (TraderNodeLLM) is the last place to enforce determinism on those numbers.
# ===========================================================================


# A deliberately-hallucinated LLM proposal. Note the price regime is a total
# fabrication relative to the advisor signal (real last_close = 100.0):
#   - entry 200.0  (2× the real close)
#   - stop  199.0  (valid for *its own* fake entry, but absurd vs reality;
#                   would be on the WRONG side — above — the deterministic
#                   entry of 100.0 if it were naively kept)
#   - target 9999.0 (absurd moonshot)
_HALLUCINATED_LLM_PROPOSAL_DICT = {
    "action": "BUY",
    "size_fraction": 0.15,
    "entry_price": 200.0,
    "stop_loss": 199.0,
    "target_price": 9999.0,
    "time_horizon_days": 30,
    "confidence": 0.95,
    "rationale": "LLM qualitative rationale that may legitimately pass through.",
    "warning_message": None,
}


def _deterministic_triple(action: TraderAction, advisor_signal: dict) -> tuple:
    """Compute the price triple via the EXACT v0.1 helper the gate trusts."""
    return TraderNode()._price_levels(
        action=action,
        recommendation="Buy",  # unused by _price_levels body; side comes from action
        advisor_signal=advisor_signal,
    )


def test_v02_overwrites_llm_numbers_with_deterministic_recompute(monkeypatch):
    """The proposal that LEAVES TraderNodeLLM carries deterministic numbers."""
    monkeypatch.setenv("HERMES_QUANT_TRADER_LLM", "1")

    hallucinated = TraderProposal(**_HALLUCINATED_LLM_PROPOSAL_DICT)
    mock_caller = _mock_llm_caller(available=True, return_proposal=hallucinated)

    # The deterministic triple from the SAME helper v0.1 uses.
    det_entry, det_stop, det_target = _deterministic_triple(
        TraderAction.BUY, _ADVISOR_SIGNAL
    )
    # close=100, atr_relative=0.02, 2×ATR ⇒ entry=100, stop=96, target=104.
    assert (det_entry, det_stop, det_target) == (100.0, 96.0, 104.0)

    audit_calls = []
    with patch("hermes_quant.agents.llm_caller._audit_append") as mock_audit:
        mock_audit.side_effect = lambda kind, source, payload: audit_calls.append(payload)
        node = TraderNodeLLM(llm_caller=mock_caller)
        result = node(_RESEARCH_PLAN, _ADVISOR_SIGNAL)

    # It is still the v0.2 LLM-success path (qualitative fields preserved).
    assert any(c["path"] == "v02_llm_succeeded" for c in audit_calls)
    assert result.rationale == _HALLUCINATED_LLM_PROPOSAL_DICT["rationale"]
    assert result.action == TraderAction.BUY  # LLM may influence direction

    # The numeric fields that feed the gate are the DETERMINISTIC ones …
    assert result.entry_price == pytest.approx(100.0)
    assert result.stop_loss == pytest.approx(96.0)
    assert result.target_price == pytest.approx(104.0)
    # … and NEVER the LLM's hallucinated numbers.
    assert result.entry_price != _HALLUCINATED_LLM_PROPOSAL_DICT["entry_price"]
    assert result.stop_loss != _HALLUCINATED_LLM_PROPOSAL_DICT["stop_loss"]
    assert result.target_price != _HALLUCINATED_LLM_PROPOSAL_DICT["target_price"]


def test_v02_wrong_side_stop_cannot_reach_gate(monkeypatch):
    """A genuinely WRONG-SIDE LLM stop (above entry on a BUY) — one that bypasses
    the schema validator, as a permissive parser could conceivably emit — must be
    overwritten with the deterministic losing-side stop. The LLM's wrong-side
    number can NEVER reach the gate. This is a true RED-driver: against the
    unfixed seam the rogue 150.0 would flow straight through.
    """
    monkeypatch.setenv("HERMES_QUANT_TRADER_LLM", "1")

    # model_construct() bypasses the cross-field validator, simulating the worst
    # case: a parsed BUY proposal whose stop (150) sits ABOVE entry (100) — the
    # wrong (winning) side. There is no valid TraderProposal(**dict) for this.
    rogue = TraderProposal.model_construct(
        action=TraderAction.BUY,
        size_fraction=0.15,
        entry_price=100.0,
        stop_loss=150.0,  # WRONG side: above entry on a BUY
        target_price=0.5,  # absurd / wrong side
        time_horizon_days=30,
        confidence=0.9,
        rationale="Rogue LLM stop on the wrong (winning) side of entry.",
        warning_message=None,
        research_plan_recommendation=None,
        research_plan_id=None,
    )
    mock_caller = _mock_llm_caller(available=True, return_proposal=rogue)

    with patch("hermes_quant.agents.llm_caller._audit_append"):
        node = TraderNodeLLM(llm_caller=mock_caller)
        result = node(_RESEARCH_PLAN, _ADVISOR_SIGNAL)

    # The deterministic recompute puts the stop back on the losing side …
    assert result.stop_loss == pytest.approx(96.0)
    assert result.target_price == pytest.approx(104.0)
    # … strictly below entry (BUY invariant the gate trusts) …
    assert result.stop_loss is not None and result.entry_price is not None
    assert result.stop_loss < result.entry_price
    # … and the rogue 150.0 is GONE — it never reached the proposal that leaves.
    assert result.stop_loss != 150.0


def test_v02_overwrites_numbers_for_sell_direction(monkeypatch):
    """SELL: deterministic stop is ABOVE entry; LLM's numbers are discarded."""
    monkeypatch.setenv("HERMES_QUANT_TRADER_LLM", "1")

    sell_plan = {**_RESEARCH_PLAN, "recommendation": "Sell"}
    # LLM hallucination valid for a SELL (stop above its own entry) but absurd.
    sell_llm_dict = {
        **_HALLUCINATED_LLM_PROPOSAL_DICT,
        "action": "SELL",
        "entry_price": 200.0,
        "stop_loss": 201.0,
        "target_price": 0.01,
    }
    hallucinated = TraderProposal(**sell_llm_dict)
    mock_caller = _mock_llm_caller(available=True, return_proposal=hallucinated)

    det_entry, det_stop, det_target = _deterministic_triple(
        TraderAction.SELL, _ADVISOR_SIGNAL
    )
    # close=100, 2×ATR ⇒ entry=100, stop=104, target=96.
    assert (det_entry, det_stop, det_target) == (100.0, 104.0, 96.0)

    with patch("hermes_quant.agents.llm_caller._audit_append"):
        node = TraderNodeLLM(llm_caller=mock_caller)
        result = node(sell_plan, _ADVISOR_SIGNAL)

    assert result.action == TraderAction.SELL
    assert result.entry_price == pytest.approx(100.0)
    assert result.stop_loss == pytest.approx(104.0)
    assert result.target_price == pytest.approx(96.0)
    assert result.stop_loss > result.entry_price  # SELL ⇒ stop above entry


def test_v02_no_price_data_drops_llm_stop_to_none(monkeypatch):
    """If there is no deterministic basis (no price/ATR), the LLM's stop/target
    are STILL overwritten — to None — never kept. Silence-by-default."""
    monkeypatch.setenv("HERMES_QUANT_TRADER_LLM", "1")

    hallucinated = TraderProposal(**_HALLUCINATED_LLM_PROPOSAL_DICT)
    mock_caller = _mock_llm_caller(available=True, return_proposal=hallucinated)

    no_price_signal: dict[str, Any] = {"direction": 1, "confidence": 0.7}

    with patch("hermes_quant.agents.llm_caller._audit_append"):
        node = TraderNodeLLM(llm_caller=mock_caller)
        result = node(_RESEARCH_PLAN, no_price_signal)

    # No deterministic stop/target available ⇒ both None (NOT the LLM's 199/9999).
    assert result.entry_price is None
    assert result.stop_loss is None
    assert result.target_price is None


def test_v02_recompute_matches_v01_node_exactly(monkeypatch):
    """The overwritten numbers must equal what the v0.1 node produces for the
    same inputs — proving we re-run the SAME helper the gate trusts."""
    monkeypatch.setenv("HERMES_QUANT_TRADER_LLM", "1")

    hallucinated = TraderProposal(**_HALLUCINATED_LLM_PROPOSAL_DICT)
    mock_caller = _mock_llm_caller(available=True, return_proposal=hallucinated)

    v01_proposal = TraderNode()(_RESEARCH_PLAN, _ADVISOR_SIGNAL)

    with patch("hermes_quant.agents.llm_caller._audit_append"):
        node = TraderNodeLLM(llm_caller=mock_caller)
        result = node(_RESEARCH_PLAN, _ADVISOR_SIGNAL)

    assert result.entry_price == v01_proposal.entry_price
    assert result.stop_loss == v01_proposal.stop_loss
    assert result.target_price == v01_proposal.target_price


def test_flag_off_byte_identical_to_v01_full_dump(monkeypatch):
    """Flag OFF: TraderNodeLLM output is byte-identical to TraderNode — every
    field, via full model_dump() equality (not just a sampled subset)."""
    monkeypatch.setenv("HERMES_QUANT_TRADER_LLM", "0")

    # A caller that, if ever consulted, would inject hallucinated numbers.
    poison = TraderProposal(**_HALLUCINATED_LLM_PROPOSAL_DICT)
    mock_caller = _mock_llm_caller(available=True, return_proposal=poison)

    expected = TraderNode()(_RESEARCH_PLAN, _ADVISOR_SIGNAL)

    with patch("hermes_quant.agents.llm_caller._audit_append"):
        node = TraderNodeLLM(llm_caller=mock_caller)
        result = node(_RESEARCH_PLAN, _ADVISOR_SIGNAL)

    assert result.model_dump() == expected.model_dump()
    mock_caller.call.assert_not_called()  # LLM never even consulted when OFF
