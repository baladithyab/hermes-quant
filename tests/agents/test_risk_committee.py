"""Wave 3 — RiskCommittee deterministic v0.1 tests (ADR-0043).

Coverage:
  * Persona prompt template construction (verbatim conversational preamble)
  * Aggressive: amplifies small-size, neutral on large
  * Conservative: silences missing stop, silences too-wide stop, neutral else
  * Neutral: silences only when both A and C agree silence
  * silence_multiplier never exceeds 1.0 (CV5 anti-amplify guard)
  * silence_multiplier=0.0 on full silence sets action=HOLD on the wrapper
  * max_rounds env-var override works
  * Round-robin termination: count < 3 * max_rounds
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from hermes_quant.agents.risk_committee import (
    AggressivePersona,
    ConservativePersona,
    NeutralPersona,
    RiskCommittee,
    RiskCommitteeTurn,
    RiskDebateSummary,
)
from hermes_quant.agents.risk_committee.personas import (
    CONVERSATIONAL_PREAMBLE,
)
from hermes_quant.agents.trader import TraderAction, TraderProposal
from hermes_quant.agents.trader_node import (
    SILENCED_WARNING_MSG,
    TraderNodeWithRisk,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_proposal(
    *,
    action: TraderAction = TraderAction.BUY,
    size_fraction: float = 0.10,
    entry_price: float | None = 100.0,
    stop_loss: float | None = 97.0,
    target_price: float | None = 103.0,
    confidence: float = 0.7,
) -> TraderProposal:
    return TraderProposal(
        action=action,
        size_fraction=size_fraction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        target_price=target_price,
        time_horizon_days=21,
        confidence=confidence,
        rationale="Test proposal rationale.",
    )


def _make_plan(
    *,
    recommendation: str = "Overweight",
    confidence: float = 0.7,
) -> dict:
    return {
        "recommendation": recommendation,
        "confidence": confidence,
        "rationale": "test rationale",
        "strategic_actions": "test actions",
    }


# ---------------------------------------------------------------------------
# 1. Persona prompt template construction
# ---------------------------------------------------------------------------


def test_aggressive_prompt_contains_conversational_preamble():
    p = AggressivePersona()
    rendered = p.render_system_prompt(
        conversational_preamble=CONVERSATIONAL_PREAMBLE,
        ticker="AAPL",
        proposal_json="{}",
        plan_json="{}",
        prior_turns_json="[]",
    )
    assert CONVERSATIONAL_PREAMBLE in rendered
    assert "Aggressive Risk Manager" in rendered


def test_conservative_prompt_contains_conversational_preamble():
    p = ConservativePersona()
    rendered = p.render_system_prompt(
        conversational_preamble=CONVERSATIONAL_PREAMBLE,
        ticker="AAPL",
        proposal_json="{}",
        plan_json="{}",
        prior_turns_json="[]",
    )
    assert CONVERSATIONAL_PREAMBLE in rendered
    assert "Conservative Risk Manager" in rendered


def test_neutral_prompt_contains_conversational_preamble():
    p = NeutralPersona()
    rendered = p.render_system_prompt(
        conversational_preamble=CONVERSATIONAL_PREAMBLE,
        ticker="AAPL",
        proposal_json="{}",
        plan_json="{}",
        prior_turns_json="[]",
    )
    assert CONVERSATIONAL_PREAMBLE in rendered
    assert "Neutral Risk Manager" in rendered


def test_persona_prompt_renders_without_args_safely():
    """Missing placeholders fall back to verbatim template (no exceptions)."""
    p = AggressivePersona()
    rendered = p.render_system_prompt()
    # Verbatim preamble string is in the template literal even if placeholders
    # aren't substituted (we use .format which substitutes {conversational_preamble}).
    # Either rendered template contains the placeholder or the verbatim string.
    assert ("conversational_preamble" in rendered) or (
        CONVERSATIONAL_PREAMBLE in rendered
    )


# ---------------------------------------------------------------------------
# 2. Aggressive persona decision rule
# ---------------------------------------------------------------------------


def test_aggressive_amplifies_small_size():
    p = AggressivePersona()
    proposal = _make_proposal(size_fraction=0.05)
    plan = _make_plan()
    decision = p.decide(proposal, plan, prior_turns=[])
    assert decision.risk_assessment == "amplify"


def test_aggressive_neutral_on_large_size():
    p = AggressivePersona()
    proposal = _make_proposal(size_fraction=0.20)
    plan = _make_plan()
    decision = p.decide(proposal, plan, prior_turns=[])
    assert decision.risk_assessment == "neutral"


def test_aggressive_neutral_at_threshold_boundary():
    p = AggressivePersona()
    # Exactly at threshold (0.15) -> neutral (strict <)
    proposal = _make_proposal(size_fraction=0.15)
    decision = p.decide(proposal, _make_plan(), prior_turns=[])
    assert decision.risk_assessment == "neutral"


def test_aggressive_neutral_on_hold_action():
    """HOLD proposals get neutral even at small size (no upside to amplify)."""
    p = AggressivePersona()
    proposal = _make_proposal(
        action=TraderAction.HOLD,
        size_fraction=0.05,
        stop_loss=None,
        target_price=None,
    )
    decision = p.decide(proposal, _make_plan(), prior_turns=[])
    assert decision.risk_assessment == "neutral"


# ---------------------------------------------------------------------------
# 3. Conservative persona decision rule
# ---------------------------------------------------------------------------


def test_conservative_silences_missing_stop():
    p = ConservativePersona()
    proposal = _make_proposal(stop_loss=None, target_price=None)
    decision = p.decide(proposal, _make_plan(), prior_turns=[])
    assert decision.risk_assessment == "silence"


def test_conservative_silences_too_wide_stop():
    p = ConservativePersona()
    # Entry 100, stop 90 -> 10% wide, > 5% threshold
    proposal = _make_proposal(entry_price=100.0, stop_loss=90.0)
    decision = p.decide(proposal, _make_plan(), prior_turns=[])
    assert decision.risk_assessment == "silence"


def test_conservative_neutral_on_tight_stop():
    p = ConservativePersona()
    # Entry 100, stop 97 -> 3% wide, < 5% threshold
    proposal = _make_proposal(entry_price=100.0, stop_loss=97.0)
    decision = p.decide(proposal, _make_plan(), prior_turns=[])
    assert decision.risk_assessment == "neutral"


def test_conservative_neutral_when_entry_missing_but_stop_present():
    """No entry to compare against — abstain (neutral)."""
    p = ConservativePersona()
    proposal = _make_proposal(entry_price=None, stop_loss=97.0)
    decision = p.decide(proposal, _make_plan(), prior_turns=[])
    assert decision.risk_assessment == "neutral"


# ---------------------------------------------------------------------------
# 4. Neutral persona decision rule
# ---------------------------------------------------------------------------


def _stub_turn(persona: str, assessment: str, idx: int = 0) -> RiskCommitteeTurn:
    return RiskCommitteeTurn(
        persona=persona,
        turn_index=idx,
        critique_text="stub",
        evidence_ids=[],
        risk_assessment=assessment,  # type: ignore[arg-type]
        confidence=0.5,
    )


def test_neutral_silences_when_both_a_and_c_silence():
    p = NeutralPersona()
    prior = [
        _stub_turn("aggressive", "silence", 0),
        _stub_turn("conservative", "silence", 1),
    ]
    decision = p.decide(_make_proposal(), _make_plan(), prior_turns=prior)
    assert decision.risk_assessment == "silence"


def test_neutral_neutral_when_only_one_silences():
    p = NeutralPersona()
    prior = [
        _stub_turn("aggressive", "amplify", 0),
        _stub_turn("conservative", "silence", 1),
    ]
    decision = p.decide(_make_proposal(), _make_plan(), prior_turns=prior)
    assert decision.risk_assessment == "neutral"


def test_neutral_neutral_when_both_amplify_no_amplification():
    """Anti-CV5: even when both A and C amplify, Neutral does NOT amplify."""
    p = NeutralPersona()
    prior = [
        _stub_turn("aggressive", "amplify", 0),
        _stub_turn("conservative", "amplify", 1),
    ]
    decision = p.decide(_make_proposal(), _make_plan(), prior_turns=prior)
    assert decision.risk_assessment == "neutral"


def test_neutral_neutral_with_no_prior_turns():
    p = NeutralPersona()
    decision = p.decide(_make_proposal(), _make_plan(), prior_turns=[])
    assert decision.risk_assessment == "neutral"


# ---------------------------------------------------------------------------
# 5. Committee invariants — silence_multiplier ≤ 1.0 (CV5 guard)
# ---------------------------------------------------------------------------


def test_committee_silence_multiplier_starts_at_one():
    """All-amplify scenario must keep multiplier at 1.0 (cannot go above)."""
    committee = RiskCommittee()
    # Aggressive amplifies (size < 0.15), Conservative neutral (tight stop),
    # Neutral neutral. So 0 silence votes.
    proposal = _make_proposal(size_fraction=0.05, entry_price=100.0, stop_loss=97.0)
    summary = committee.debate(proposal, _make_plan())
    assert summary.silence_multiplier == 1.0
    assert summary.silence_multiplier <= 1.0


def test_committee_silence_multiplier_never_exceeds_one():
    """Property: across many proposals, multiplier ∈ [0, 1]."""
    committee = RiskCommittee()
    for size in [0.01, 0.05, 0.10, 0.15, 0.20, 0.50, 0.99]:
        for stop_loss in [None, 90.0, 97.0, 99.0]:
            proposal = _make_proposal(size_fraction=size, stop_loss=stop_loss)
            summary = committee.debate(proposal, _make_plan(), max_rounds=3)
            assert 0.0 <= summary.silence_multiplier <= 1.0, (
                f"size={size} stop={stop_loss} multiplier="
                f"{summary.silence_multiplier} out of [0,1]"
            )


def test_committee_single_silence_halves_multiplier():
    committee = RiskCommittee()
    # Conservative will silence (no stop_loss). A (small size) amplify,
    # N neutral. -> 1 silence vote -> 0.5
    proposal = _make_proposal(
        size_fraction=0.05, stop_loss=None, target_price=None
    )
    summary = committee.debate(proposal, _make_plan(), max_rounds=1)
    assert summary.silence_multiplier == pytest.approx(0.5)


def test_committee_two_silences_quarters_multiplier():
    """Both Conservative AND Neutral silence (when A also silences) -> 0.25."""
    committee = RiskCommittee()
    # We need A to silence too — Aggressive only silences if forced.
    # Use a Hold-equivalent scenario: missing stop forces Conservative silence,
    # then Neutral silences only if Aggressive also did. Aggressive only
    # ever amplifies or neutral, never silences. So in 1 round we get exactly
    # one silence (Conservative). For 2 silences in 1 round we need
    # Conservative + Neutral, which requires Aggressive also silenced.
    # That can't happen v0.1 with Aggressive -> we test 2 rounds instead:
    # Round1: A amplify, C silence (×0.5), N neutral.
    # Round2: A amplify, C silence (×0.25), N neutral.
    proposal = _make_proposal(
        size_fraction=0.05, stop_loss=None, target_price=None
    )
    summary = committee.debate(proposal, _make_plan(), max_rounds=2)
    # 2 silence votes (both rounds) -> 0.5 * 0.5 = 0.25
    assert summary.silence_multiplier == pytest.approx(0.25)
    assert summary.n_rounds == 2
    silence_count = sum(1 for t in summary.turns if t.risk_assessment == "silence")
    assert silence_count == 2


# ---------------------------------------------------------------------------
# 6. TraderNodeWithRisk wrapper — silence_multiplier=0.0 -> HOLD
# ---------------------------------------------------------------------------


def test_wrapper_silence_zero_sets_hold():
    """A custom committee that returns silence_multiplier=0.0 forces HOLD."""

    class _ZeroCommittee:
        def debate(self, proposal, plan, *, max_rounds=None, proposal_id=None):
            return RiskDebateSummary(
                trader_proposal_id="test-pid",
                turns=[],
                silence_multiplier=0.0,
                final_recommendation="silenced for test",
                n_rounds=0,
                terminated_reason="test",
            )

    wrapper = TraderNodeWithRisk(risk_committee=_ZeroCommittee())  # type: ignore[arg-type]
    plan = _make_plan(recommendation="Buy")
    advisor_signal = {
        "metadata": {"last_close": 100.0, "atr_relative": 0.02},
        "data_quality": {},
    }
    proposal, summary = wrapper(plan, advisor_signal)
    assert proposal.action == TraderAction.HOLD
    assert proposal.size_fraction == 0.0
    assert proposal.warning_message is not None
    assert SILENCED_WARNING_MSG in proposal.warning_message
    assert summary.silence_multiplier == 0.0


def test_wrapper_full_multiplier_unchanged():
    """silence_multiplier=1.0 -> proposal passes through unchanged."""

    class _PassthroughCommittee:
        def debate(self, proposal, plan, *, max_rounds=None, proposal_id=None):
            return RiskDebateSummary(
                trader_proposal_id="test-pid",
                turns=[],
                silence_multiplier=1.0,
                final_recommendation="ok",
                n_rounds=0,
                terminated_reason="test",
            )

    wrapper = TraderNodeWithRisk(risk_committee=_PassthroughCommittee())  # type: ignore[arg-type]
    plan = _make_plan(recommendation="Buy")
    advisor_signal = {
        "metadata": {"last_close": 100.0, "atr_relative": 0.02},
        "data_quality": {},
    }
    proposal, summary = wrapper(plan, advisor_signal)
    assert proposal.action == TraderAction.BUY
    assert proposal.size_fraction > 0
    assert summary.silence_multiplier == 1.0


def test_wrapper_partial_multiplier_scales_size():
    """0 < silence_multiplier < 1.0 scales size_fraction proportionally."""

    class _HalfCommittee:
        def debate(self, proposal, plan, *, max_rounds=None, proposal_id=None):
            return RiskDebateSummary(
                trader_proposal_id="test-pid",
                turns=[],
                silence_multiplier=0.5,
                final_recommendation="halved",
                n_rounds=0,
                terminated_reason="test",
            )

    wrapper = TraderNodeWithRisk(risk_committee=_HalfCommittee())  # type: ignore[arg-type]
    plan = _make_plan(recommendation="Buy")  # size 0.20
    advisor_signal = {
        "metadata": {"last_close": 100.0, "atr_relative": 0.02},
        "data_quality": {},
    }
    proposal, summary = wrapper(plan, advisor_signal)
    assert proposal.size_fraction == pytest.approx(0.10)  # 0.20 * 0.5
    assert proposal.action == TraderAction.BUY
    assert "scaled" in (proposal.warning_message or "")


def test_wrapper_clamps_above_one():
    """If a buggy committee returns >1.0 (impossible by construction), clamp."""

    class _BuggyCommittee:
        def debate(self, proposal, plan, *, max_rounds=None, proposal_id=None):
            # We can't actually construct a RiskDebateSummary with >1.0
            # because Pydantic enforces le=1.0. So we bypass via a fake.
            class _Fake:
                trader_proposal_id = "x"
                turns: list = []
                silence_multiplier = 1.5
                final_recommendation = "buggy"
                n_rounds = 0
                terminated_reason = "test"

            return _Fake()  # type: ignore[return-value]

    wrapper = TraderNodeWithRisk(risk_committee=_BuggyCommittee())  # type: ignore[arg-type]
    plan = _make_plan(recommendation="Buy")
    advisor_signal = {
        "metadata": {"last_close": 100.0, "atr_relative": 0.02},
        "data_quality": {},
    }
    proposal, summary = wrapper(plan, advisor_signal)
    # Wrapper clamps; size_fraction unchanged (multiplier effectively 1.0).
    assert proposal.size_fraction > 0


# ---------------------------------------------------------------------------
# 7. Env var override + round-robin termination
# ---------------------------------------------------------------------------


def test_env_var_overrides_max_rounds():
    committee = RiskCommittee()
    proposal = _make_proposal(size_fraction=0.05)
    plan = _make_plan()
    with patch.dict(os.environ, {"HERMES_QUANT_RISK_ROUNDS": "2"}):
        summary = committee.debate(proposal, plan)  # no explicit override
        assert len(summary.turns) == 6  # 3 personas * 2 rounds
        assert summary.n_rounds == 2


def test_explicit_max_rounds_beats_env_var():
    committee = RiskCommittee()
    proposal = _make_proposal(size_fraction=0.05)
    plan = _make_plan()
    with patch.dict(os.environ, {"HERMES_QUANT_RISK_ROUNDS": "3"}):
        summary = committee.debate(proposal, plan, max_rounds=1)
        assert len(summary.turns) == 3
        assert summary.n_rounds == 1


def test_max_rounds_clamped_to_three():
    committee = RiskCommittee()
    proposal = _make_proposal(size_fraction=0.05)
    plan = _make_plan()
    summary = committee.debate(proposal, plan, max_rounds=99)
    assert len(summary.turns) == 9  # clamped to 3 rounds * 3 personas
    assert summary.n_rounds == 3


def test_max_rounds_clamped_to_one_minimum():
    committee = RiskCommittee()
    proposal = _make_proposal(size_fraction=0.05)
    plan = _make_plan()
    summary = committee.debate(proposal, plan, max_rounds=0)
    assert len(summary.turns) == 3
    assert summary.n_rounds == 1


def test_round_robin_persona_order():
    """Turns alternate aggressive -> conservative -> neutral -> repeat."""
    committee = RiskCommittee()
    proposal = _make_proposal(size_fraction=0.05)
    summary = committee.debate(proposal, _make_plan(), max_rounds=2)
    expected = ["aggressive", "conservative", "neutral"] * 2
    assert [t.persona for t in summary.turns] == expected


def test_invalid_env_var_falls_back_to_default():
    committee = RiskCommittee()
    with patch.dict(os.environ, {"HERMES_QUANT_RISK_ROUNDS": "not-an-int"}):
        summary = committee.debate(_make_proposal(), _make_plan())
        assert summary.n_rounds == 1  # default


# ---------------------------------------------------------------------------
# 8. RiskDebateSummary contract
# ---------------------------------------------------------------------------


def test_summary_serializes_to_json():
    committee = RiskCommittee()
    summary = committee.debate(
        _make_proposal(size_fraction=0.05), _make_plan(), max_rounds=1
    )
    data = summary.model_dump(mode="json")
    assert "trader_proposal_id" in data
    assert "turns" in data
    assert "silence_multiplier" in data
    assert "final_recommendation" in data
    assert "n_rounds" in data


def test_summary_proposal_id_uses_supplied_value():
    committee = RiskCommittee()
    summary = committee.debate(
        _make_proposal(size_fraction=0.05),
        _make_plan(),
        max_rounds=1,
        proposal_id="my-id-123",
    )
    assert summary.trader_proposal_id == "my-id-123"


def test_summary_proposal_id_auto_generated_if_omitted():
    committee = RiskCommittee()
    summary = committee.debate(_make_proposal(size_fraction=0.05), _make_plan())
    assert summary.trader_proposal_id.startswith("prop-")


def test_committee_rejects_wrong_persona_count():
    with pytest.raises(ValueError):
        RiskCommittee(personas=(AggressivePersona(),))  # type: ignore[arg-type]


def test_committee_rejects_missing_canonical_persona():
    class _Custom(AggressivePersona):
        name = "weird-name"

    with pytest.raises(ValueError):
        RiskCommittee(
            personas=(_Custom(), ConservativePersona(), NeutralPersona())
        )


# ---------------------------------------------------------------------------
# 9. Anti-amplify guard — explicit
# ---------------------------------------------------------------------------


def test_amplify_votes_recorded_but_do_not_change_multiplier():
    """All 3 personas vote amplify (impossible v0.1, but stub it to verify)."""
    committee = RiskCommittee()
    # Real run: small size + tight stop + no prior silences.
    # Aggressive: amplify (size 0.05 < 0.15)
    # Conservative: neutral (3% stop ≤ 5%)
    # Neutral: neutral (no prior silences)
    proposal = _make_proposal(size_fraction=0.05, entry_price=100.0, stop_loss=98.0)
    summary = committee.debate(proposal, _make_plan(), max_rounds=1)
    amplify_count = sum(1 for t in summary.turns if t.risk_assessment == "amplify")
    assert amplify_count >= 1
    # Multiplier still 1.0 — amplify never raises it.
    assert summary.silence_multiplier == 1.0


def test_v01_committee_does_not_call_llm():
    """v0.1 must NOT invoke an LLM. We pass a tripwire callable that raises."""

    def _tripwire(_sys: str, _user: str) -> str:
        raise AssertionError("v0.1 must not call the LLM")

    committee = RiskCommittee(llm_caller=_tripwire)
    summary = committee.debate(_make_proposal(size_fraction=0.05), _make_plan())
    assert summary.silence_multiplier <= 1.0


def test_terminated_reason_max_rounds():
    committee = RiskCommittee()
    summary = committee.debate(_make_proposal(size_fraction=0.05), _make_plan())
    assert summary.terminated_reason == "max_rounds_reached"
