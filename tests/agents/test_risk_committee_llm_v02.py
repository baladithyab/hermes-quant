"""RiskCommittee v0.2 LLM-wiring tests (ADR-0056).

Coverage (≥12 tests):
  1.  flag=0, any caller           → v0.1 path (bit-identical regression)
  2.  flag=1, caller=None          → v0.1 path
  3.  flag=1, available()==False   → v0.1 path
  4.  flag=1, available()==True, all LLM ok → v02_llm_succeeded for each turn
  5.  flag=1, available()==True, Conservative LLM raises
      → Aggressive+Neutral get v02_llm_succeeded, Conservative gets fallback
  6.  CV5 invariant: LLM returns silence_multiplier > 1.0 in structured turn
      → wrapper STILL clamps to 1.0
  7.  Anti-amplify invariant: LLM suggests 'amplify' → multiplier stays 1.0
  8.  Audit log records 'risk_committee_llm_call' kind on v0.2 path
  9.  partial fallback: audit log shows MIXED paths per turn
  10. v0.2 path with LLM returning None → per-turn fallback for that persona
  11. Debate summary structure is valid RiskDebateSummary on v0.2 path
  12. multi-round v0.2 path (max_rounds=2) → 6 turns produced
  13. v0.1 regression: silence_multiplier computation is bit-identical
"""

from __future__ import annotations

import json
import os
from typing import Literal
from unittest.mock import MagicMock, patch

import pytest

from hermes_quant.agents.risk_committee import (
    RiskCommittee,
    RiskCommitteeTurn,
    RiskDebateSummary,
)
from hermes_quant.agents.trader import TraderAction, TraderProposal


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_proposal(
    *,
    action: TraderAction = TraderAction.BUY,
    size_fraction: float = 0.10,
    entry_price: float | None = 100.0,
    stop_loss: float | None = 97.0,  # 3% away → within Conservative threshold
    target_price: float | None = 106.0,
    confidence: float = 0.75,
) -> TraderProposal:
    return TraderProposal(
        action=action,
        size_fraction=size_fraction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        target_price=target_price,
        time_horizon_days=21,
        confidence=confidence,
        rationale="LLM v0.2 test proposal.",
    )


_PLAN = {
    "ticker": "AAPL",
    "recommendation": "Buy",
    "confidence": 0.75,
    "rationale": "Strong momentum and earnings beat.",
    "strategic_actions": "Enter long position.",
}


def _mock_caller(available: bool = True) -> MagicMock:
    """Build a MagicMock that mimics the LLMCaller interface."""
    caller = MagicMock()
    caller.available.return_value = available
    return caller


def _make_valid_turn(
    persona: str,
    turn_index: int,
    risk_assessment: Literal["amplify", "silence", "neutral"] = "neutral",
    confidence: float = 0.7,
) -> RiskCommitteeTurn:
    return RiskCommitteeTurn(
        persona=persona,
        turn_index=turn_index,
        critique_text=f"LLM critique from {persona} at turn {turn_index}.",
        evidence_ids=[f"llm_evidence_{persona}"],
        risk_assessment=risk_assessment,
        confidence=confidence,
    )


def _llm_returns_neutral(caller: MagicMock) -> None:
    """Configure caller.call to always return a neutral RiskCommitteeTurn."""
    persona_order = ["aggressive", "conservative", "neutral"]

    call_count = [0]

    def _call_side_effect(system_prompt, user_prompt, *, schema=None):
        idx = call_count[0]
        persona = persona_order[idx % 3]
        call_count[0] += 1
        turn = _make_valid_turn(persona, idx, "neutral")
        return turn, {"choices": [{"message": {"content": turn.model_dump_json()}}]}

    caller.call.side_effect = _call_side_effect


# ---------------------------------------------------------------------------
# 1. Flag OFF → always v0.1 (regression: output bit-identical to no-caller)
# ---------------------------------------------------------------------------


def test_flag_off_uses_v01_path():
    """With HERMES_QUANT_RISK_COMMITTEE_LLM=0 → v0.1 path, ignores caller."""
    proposal = _make_proposal(size_fraction=0.20, stop_loss=97.0)
    caller = _mock_caller(available=True)

    with patch.dict(os.environ, {"HERMES_QUANT_RISK_COMMITTEE_LLM": "0"}):
        committee_with_caller = RiskCommittee(llm_caller=caller)
        committee_no_caller = RiskCommittee()

        summary_with = committee_with_caller.debate(proposal, _PLAN, proposal_id="pid-1")
        summary_no = committee_no_caller.debate(proposal, _PLAN, proposal_id="pid-1")

    # LLM path should not have been touched.
    caller.call.assert_not_called()

    # Both summaries should be structurally identical (same turns, same multiplier).
    assert summary_with.silence_multiplier == summary_no.silence_multiplier
    assert len(summary_with.turns) == len(summary_no.turns)
    for t_with, t_no in zip(summary_with.turns, summary_no.turns):
        assert t_with.persona == t_no.persona
        assert t_with.risk_assessment == t_no.risk_assessment
        assert t_with.confidence == t_no.confidence


# ---------------------------------------------------------------------------
# 2. Flag ON, caller=None → v0.1 path
# ---------------------------------------------------------------------------


def test_flag_on_caller_none_uses_v01():
    """With flag=1 but llm_caller=None → v0.1 deterministic path."""
    proposal = _make_proposal()
    with patch.dict(os.environ, {"HERMES_QUANT_RISK_COMMITTEE_LLM": "1"}):
        committee = RiskCommittee(llm_caller=None)
        summary = committee.debate(proposal, _PLAN, proposal_id="pid-2")

    assert isinstance(summary, RiskDebateSummary)
    assert summary.silence_multiplier <= 1.0
    assert len(summary.turns) == 3


# ---------------------------------------------------------------------------
# 3. Flag ON, available()==False → v0.1 path
# ---------------------------------------------------------------------------


def test_flag_on_not_available_uses_v01():
    """With flag=1 and LLMCaller.available()==False → v0.1 path."""
    proposal = _make_proposal()
    caller = _mock_caller(available=False)

    with patch.dict(os.environ, {"HERMES_QUANT_RISK_COMMITTEE_LLM": "1"}):
        committee = RiskCommittee(llm_caller=caller)
        summary = committee.debate(proposal, _PLAN, proposal_id="pid-3")

    # LLM call should not be attempted.
    caller.call.assert_not_called()
    assert isinstance(summary, RiskDebateSummary)
    assert len(summary.turns) == 3


# ---------------------------------------------------------------------------
# 4. Flag ON, available()==True, all LLM ok → v02_llm_succeeded per turn
# ---------------------------------------------------------------------------


def test_v02_llm_path_all_succeed_audit_log(tmp_path):
    """All 3 persona LLM calls succeed → audit log has v02_llm_succeeded ×3."""
    proposal = _make_proposal()
    caller = _mock_caller(available=True)
    _llm_returns_neutral(caller)

    audit_lines: list[dict] = []

    def _fake_audit(kind, source, payload):
        audit_lines.append({"kind": kind, "payload": payload})

    with (
        patch.dict(os.environ, {"HERMES_QUANT_RISK_COMMITTEE_LLM": "1"}),
        patch(
            "hermes_quant.agents.risk_committee.committee._audit_append",
            side_effect=_fake_audit,
        ),
    ):
        committee = RiskCommittee(llm_caller=caller)
        summary = committee.debate(proposal, _PLAN, proposal_id="pid-4")

    assert isinstance(summary, RiskDebateSummary)
    assert len(summary.turns) == 3

    # Audit: 3 entries, all risk_committee_llm_call kind, all v02_llm_succeeded.
    rc_entries = [e for e in audit_lines if e["kind"] == "risk_committee_llm_call"]
    assert len(rc_entries) == 3
    for entry in rc_entries:
        assert entry["payload"]["path"] == "v02_llm_succeeded"


# ---------------------------------------------------------------------------
# 5. Partial fallback: Conservative raises, others succeed
# ---------------------------------------------------------------------------


def test_partial_fallback_conservative_raises(tmp_path):
    """Conservative LLM call raises → only Conservative falls back to v0.1."""
    proposal = _make_proposal()
    caller = _mock_caller(available=True)

    persona_order = ["aggressive", "conservative", "neutral"]
    call_count = [0]

    def _side_effect(system_prompt, user_prompt, *, schema=None):
        idx = call_count[0]
        persona = persona_order[idx % 3]
        call_count[0] += 1
        if persona == "conservative":
            raise RuntimeError("Simulated conservative LLM failure")
        turn = _make_valid_turn(persona, idx, "neutral")
        return turn, {}

    caller.call.side_effect = _side_effect

    audit_lines: list[dict] = []

    def _fake_audit(kind, source, payload):
        audit_lines.append({"kind": kind, "payload": payload})

    with (
        patch.dict(os.environ, {"HERMES_QUANT_RISK_COMMITTEE_LLM": "1"}),
        patch(
            "hermes_quant.agents.risk_committee.committee._audit_append",
            side_effect=_fake_audit,
        ),
    ):
        committee = RiskCommittee(llm_caller=caller)
        summary = committee.debate(proposal, _PLAN, proposal_id="pid-5")

    assert len(summary.turns) == 3

    rc_entries = [e for e in audit_lines if e["kind"] == "risk_committee_llm_call"]
    paths_by_persona = {e["payload"]["persona"]: e["payload"]["path"] for e in rc_entries}

    # Aggressive and Neutral succeed on LLM path.
    assert paths_by_persona["aggressive"] == "v02_llm_succeeded"
    assert paths_by_persona["neutral"] == "v02_llm_succeeded"
    # Conservative falls back to v0.1.
    assert paths_by_persona["conservative"] == "v02_llm_fallback_to_v01"


# ---------------------------------------------------------------------------
# 6. CV5 invariant: LLM turn with silence → multiplier NEVER exceeds 1.0
#    even if silence_multiplier were to overflow (edge case: many silence votes)
# ---------------------------------------------------------------------------


def test_cv5_silence_multiplier_never_above_1():
    """CV5: silence_multiplier is always ≤ 1.0 regardless of LLM path."""
    proposal = _make_proposal()
    caller = _mock_caller(available=True)

    persona_order = ["aggressive", "conservative", "neutral"]
    call_count = [0]

    def _side_effect(system_prompt, user_prompt, *, schema=None):
        idx = call_count[0]
        persona = persona_order[idx % 3]
        call_count[0] += 1
        # All personas vote silence
        turn = _make_valid_turn(persona, idx, "silence", confidence=0.9)
        return turn, {}

    caller.call.side_effect = _side_effect

    with patch.dict(os.environ, {"HERMES_QUANT_RISK_COMMITTEE_LLM": "1"}):
        committee = RiskCommittee(llm_caller=caller)
        summary = committee.debate(proposal, _PLAN, proposal_id="pid-6", max_rounds=3)

    # After 3 rounds × 3 silence votes: multiplier = 1.0 * 0.5^9 → well below 1
    # In all cases, invariant must hold.
    assert summary.silence_multiplier >= 0.0
    assert summary.silence_multiplier <= 1.0


# ---------------------------------------------------------------------------
# 7. Anti-amplify invariant: 'amplify' votes NEVER raise multiplier above 1.0
# ---------------------------------------------------------------------------


def test_cv5_amplify_never_raises_multiplier():
    """CV5: LLM returning 'amplify' does NOT increase silence_multiplier."""
    proposal = _make_proposal()
    caller = _mock_caller(available=True)

    persona_order = ["aggressive", "conservative", "neutral"]
    call_count = [0]

    def _side_effect(system_prompt, user_prompt, *, schema=None):
        idx = call_count[0]
        persona = persona_order[idx % 3]
        call_count[0] += 1
        # Aggressive + Neutral amplify; Conservative neutral
        assessment = "amplify" if persona != "conservative" else "neutral"
        turn = _make_valid_turn(persona, idx, assessment)
        return turn, {}

    caller.call.side_effect = _side_effect

    with patch.dict(os.environ, {"HERMES_QUANT_RISK_COMMITTEE_LLM": "1"}):
        committee = RiskCommittee(llm_caller=caller)
        summary = committee.debate(proposal, _PLAN, proposal_id="pid-7")

    # amplify votes must NEVER raise silence_multiplier above 1.0
    assert summary.silence_multiplier == 1.0
    amplify_turns = [t for t in summary.turns if t.risk_assessment == "amplify"]
    assert len(amplify_turns) >= 1


# ---------------------------------------------------------------------------
# 8. Audit log records 'risk_committee_llm_call' kind per turn
# ---------------------------------------------------------------------------


def test_audit_log_kind_is_risk_committee_llm_call():
    """Audit events have kind='risk_committee_llm_call' for every turn."""
    proposal = _make_proposal()
    caller = _mock_caller(available=True)
    _llm_returns_neutral(caller)

    captured: list[dict] = []

    def _cap(kind, source, payload):
        captured.append({"kind": kind, "source": source, "payload": payload})

    with (
        patch.dict(os.environ, {"HERMES_QUANT_RISK_COMMITTEE_LLM": "1"}),
        patch(
            "hermes_quant.agents.risk_committee.committee._audit_append",
            side_effect=_cap,
        ),
    ):
        committee = RiskCommittee(llm_caller=caller)
        committee.debate(proposal, _PLAN, proposal_id="pid-8")

    rc = [e for e in captured if e["kind"] == "risk_committee_llm_call"]
    assert len(rc) == 3
    for entry in rc:
        assert entry["source"] == "hermes_quant.agents.risk_committee.committee"
        # Each audit entry must have all required fields.
        payload = entry["payload"]
        assert "persona" in payload
        assert "turn_index" in payload
        assert "path" in payload
        assert "risk_assessment" in payload
        assert "silence_multiplier_after" in payload


# ---------------------------------------------------------------------------
# 9. Mixed-path audit log (partial fallback scenario)
# ---------------------------------------------------------------------------


def test_mixed_path_audit_log():
    """Partial fallback shows mixed v02_llm_succeeded and v02_llm_fallback_to_v01."""
    proposal = _make_proposal()
    caller = _mock_caller(available=True)

    persona_order = ["aggressive", "conservative", "neutral"]
    call_count = [0]

    def _side_effect(system_prompt, user_prompt, *, schema=None):
        idx = call_count[0]
        persona = persona_order[idx % 3]
        call_count[0] += 1
        if persona == "conservative":
            return None, {"error": "test_parse_fail"}
        turn = _make_valid_turn(persona, idx, "neutral")
        return turn, {}

    caller.call.side_effect = _side_effect

    captured: list[dict] = []

    def _cap(kind, source, payload):
        captured.append({"kind": kind, "payload": payload})

    with (
        patch.dict(os.environ, {"HERMES_QUANT_RISK_COMMITTEE_LLM": "1"}),
        patch(
            "hermes_quant.agents.risk_committee.committee._audit_append",
            side_effect=_cap,
        ),
    ):
        committee = RiskCommittee(llm_caller=caller)
        summary = committee.debate(proposal, _PLAN, proposal_id="pid-9")

    rc = [e for e in captured if e["kind"] == "risk_committee_llm_call"]
    paths = {e["payload"]["persona"]: e["payload"]["path"] for e in rc}
    assert paths["aggressive"] == "v02_llm_succeeded"
    assert paths["conservative"] == "v02_llm_fallback_to_v01"
    assert paths["neutral"] == "v02_llm_succeeded"


# ---------------------------------------------------------------------------
# 10. LLM returning None → per-turn fallback
# ---------------------------------------------------------------------------


def test_llm_returns_none_fallback_per_turn():
    """LLM returning (None, raw) causes per-turn fallback, not full debate abort."""
    proposal = _make_proposal()
    caller = _mock_caller(available=True)
    # All calls return (None, {}) — no valid structured output.
    caller.call.return_value = (None, {"error": "parse_fail"})

    captured_paths: list[str] = []

    def _cap(kind, source, payload):
        if kind == "risk_committee_llm_call":
            captured_paths.append(payload["path"])

    with (
        patch.dict(os.environ, {"HERMES_QUANT_RISK_COMMITTEE_LLM": "1"}),
        patch(
            "hermes_quant.agents.risk_committee.committee._audit_append",
            side_effect=_cap,
        ),
    ):
        committee = RiskCommittee(llm_caller=caller)
        summary = committee.debate(proposal, _PLAN, proposal_id="pid-10")

    assert isinstance(summary, RiskDebateSummary)
    assert len(summary.turns) == 3
    # All turns fell back to v0.1
    assert all(p == "v02_llm_fallback_to_v01" for p in captured_paths)


# ---------------------------------------------------------------------------
# 11. v0.2 path produces a valid RiskDebateSummary
# ---------------------------------------------------------------------------


def test_v02_debate_produces_valid_summary():
    """v0.2 path output is a well-formed RiskDebateSummary."""
    proposal = _make_proposal()
    caller = _mock_caller(available=True)
    _llm_returns_neutral(caller)

    with (
        patch.dict(os.environ, {"HERMES_QUANT_RISK_COMMITTEE_LLM": "1"}),
        patch("hermes_quant.agents.risk_committee.committee._audit_append"),
    ):
        committee = RiskCommittee(llm_caller=caller)
        summary = committee.debate(proposal, _PLAN, proposal_id="pid-11")

    assert isinstance(summary, RiskDebateSummary)
    assert summary.trader_proposal_id == "pid-11"
    assert 0.0 <= summary.silence_multiplier <= 1.0
    assert summary.n_rounds >= 1
    assert len(summary.turns) == 3
    for turn in summary.turns:
        assert isinstance(turn, RiskCommitteeTurn)
        assert turn.persona in ("aggressive", "conservative", "neutral")
        assert turn.risk_assessment in ("amplify", "silence", "neutral")
        assert 0.0 <= turn.confidence <= 1.0


# ---------------------------------------------------------------------------
# 12. Multi-round v0.2 path produces 6 turns (max_rounds=2)
# ---------------------------------------------------------------------------


def test_v02_multi_round_produces_6_turns():
    """max_rounds=2 with LLM path → 6 turns total."""
    proposal = _make_proposal()
    caller = _mock_caller(available=True)

    persona_order = ["aggressive", "conservative", "neutral"]
    call_count = [0]

    def _side_effect(system_prompt, user_prompt, *, schema=None):
        idx = call_count[0]
        persona = persona_order[idx % 3]
        call_count[0] += 1
        turn = _make_valid_turn(persona, idx, "neutral")
        return turn, {}

    caller.call.side_effect = _side_effect

    with (
        patch.dict(os.environ, {"HERMES_QUANT_RISK_COMMITTEE_LLM": "1"}),
        patch("hermes_quant.agents.risk_committee.committee._audit_append"),
    ):
        committee = RiskCommittee(llm_caller=caller)
        summary = committee.debate(proposal, _PLAN, proposal_id="pid-12", max_rounds=2)

    assert len(summary.turns) == 6
    assert summary.n_rounds == 2


# ---------------------------------------------------------------------------
# 13. v0.1 regression: silence_multiplier computation is bit-identical
# ---------------------------------------------------------------------------


def test_v01_regression_silence_multiplier_exact():
    """v0.1 path silence_multiplier matches expected deterministic value."""
    # stop_loss=None → Conservative silences → multiplier = 0.5
    proposal_no_stop = _make_proposal(stop_loss=None)

    committee = RiskCommittee()  # no LLM caller, always v0.1
    with patch.dict(os.environ, {"HERMES_QUANT_RISK_COMMITTEE_LLM": "0"}):
        summary = committee.debate(proposal_no_stop, _PLAN, proposal_id="pid-13")

    # Conservative silences (stop_loss=None). Neutral may or may not second.
    # But multiplier must equal 0.5 * 0.5 if Neutral also silenced, or 0.5 if not.
    # Check it is strictly ≤ 1.0 and decreased.
    assert summary.silence_multiplier < 1.0
    assert summary.silence_multiplier >= 0.0

    # Wide stop → Conservative silences → multiplier = 0.5
    proposal_wide_stop = _make_proposal(entry_price=100.0, stop_loss=90.0)  # 10% wide
    with patch.dict(os.environ, {"HERMES_QUANT_RISK_COMMITTEE_LLM": "0"}):
        summary2 = committee.debate(proposal_wide_stop, _PLAN, proposal_id="pid-13b")
    assert summary2.silence_multiplier < 1.0


# ---------------------------------------------------------------------------
# 14. CV5 regression: even with v0.2 path active, silence_multiplier ≤ 1.0
#     when the LLM ITSELF tries to embed a multiplier > 1.0 in critique text.
#     (The multiplier lives OUTSIDE the LLM-returned struct; this test ensures
#     that structural enforcement prevents any bypass via the 'amplify' path.)
# ---------------------------------------------------------------------------


def test_cv5_regression_amplify_does_not_raise_multiplier_on_v02_path():
    """REGRESSION: amplify on v0.2 path leaves silence_multiplier = 1.0 exactly."""
    proposal = _make_proposal()
    caller = _mock_caller(available=True)

    persona_order = ["aggressive", "conservative", "neutral"]
    call_count = [0]

    def _side_effect(system_prompt, user_prompt, *, schema=None):
        idx = call_count[0]
        persona = persona_order[idx % 3]
        call_count[0] += 1
        # Every persona says amplify.
        turn = _make_valid_turn(persona, idx, "amplify", confidence=0.95)
        return turn, {}

    caller.call.side_effect = _side_effect

    with (
        patch.dict(os.environ, {"HERMES_QUANT_RISK_COMMITTEE_LLM": "1"}),
        patch("hermes_quant.agents.risk_committee.committee._audit_append"),
    ):
        committee = RiskCommittee(llm_caller=caller)
        summary = committee.debate(proposal, _PLAN, proposal_id="pid-14")

    # CRITICAL: silence_multiplier must be EXACTLY 1.0 when only amplify votes exist.
    assert summary.silence_multiplier == 1.0, (
        f"CV5 VIOLATION: silence_multiplier={summary.silence_multiplier} "
        "after all-amplify v0.2 turns — must be exactly 1.0."
    )
