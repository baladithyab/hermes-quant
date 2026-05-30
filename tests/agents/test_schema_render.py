"""tests/agents/test_schema_render.py — G10 per-schema markdown renderers.

Pure, deterministic formatters over the LLM-stage Pydantic schemas. No network.
"""

from __future__ import annotations

import pytest

from hermes_quant.aggregators.llm_committee import PortfolioDecision
from hermes_quant.agents.research_debate.schemas import PortfolioRating, ResearchPlan
from hermes_quant.agents.risk_committee.committee import (
    RiskCommitteeTurn,
    RiskDebateSummary,
)
from hermes_quant.agents.schema_render import (
    render_portfolio_decision,
    render_research_plan,
    render_risk_debate_summary,
    render_schema,
    render_trader_proposal,
)
from hermes_quant.agents.trader import TraderAction, TraderProposal


def _trader_proposal(**overrides) -> TraderProposal:
    kwargs = dict(
        action=TraderAction.BUY,
        size_fraction=0.10,
        confidence=0.70,
        rationale="momentum confirmed by multiple analysts",
    )
    kwargs.update(overrides)
    return TraderProposal(**kwargs)


def _research_plan(**overrides) -> ResearchPlan:
    kwargs = dict(
        recommendation=PortfolioRating.SELL,
        confidence=0.65,
        rationale="deteriorating fundamentals",
        strategic_actions="reduce exposure",
    )
    kwargs.update(overrides)
    return ResearchPlan(**kwargs)


def _risk_summary() -> RiskDebateSummary:
    turns = [
        RiskCommitteeTurn(
            persona="aggressive",
            turn_index=0,
            critique_text="size up",
            risk_assessment="amplify",
            confidence=0.6,
        ),
        RiskCommitteeTurn(
            persona="conservative",
            turn_index=1,
            critique_text="too risky",
            risk_assessment="silence",
            confidence=0.8,
        ),
    ]
    return RiskDebateSummary(
        trader_proposal_id="tp_123",
        turns=turns,
        silence_multiplier=0.5,
        final_recommendation="reduce size by half",
        n_rounds=1,
        terminated_reason="max_rounds_reached",
    )


def _portfolio_decision(**overrides) -> PortfolioDecision:
    kwargs = dict(
        action="Hold",
        size_multiplier=1.0,
        confidence=0.5,
        rationale="balanced",
        vetoed=False,
    )
    kwargs.update(overrides)
    return PortfolioDecision(**kwargs)


def test_render_trader_proposal_contains_size_and_action():
    out = render_trader_proposal(_trader_proposal())
    assert "BUY" in out
    assert "10.00%" in out or "0.10" in out
    assert "0.70" in out


def test_render_trader_proposal_surfaces_warning():
    out = render_trader_proposal(_trader_proposal(warning_message="fallback"))
    assert "fallback" in out


def test_render_research_plan_shows_rating():
    out = render_research_plan(_research_plan())
    assert "SELL" in out
    assert "0.65" in out


def test_render_risk_summary_lists_turns_and_multiplier():
    out = render_risk_debate_summary(_risk_summary())
    assert "0.50" in out  # silence_multiplier
    assert "aggressive" in out
    assert "conservative" in out
    assert "reduce size by half" in out  # final_recommendation


def test_render_portfolio_decision_veto():
    out = render_portfolio_decision(_portfolio_decision(vetoed=True, veto_source="risk"))
    assert "vetoed" in out.lower()
    assert "risk" in out


def test_render_schema_dispatch_each_type():
    cases = [
        _trader_proposal(),
        _research_plan(),
        _risk_summary(),
        _portfolio_decision(),
    ]
    direct = [
        render_trader_proposal,
        render_research_plan,
        render_risk_debate_summary,
        render_portfolio_decision,
    ]
    for obj, fn in zip(cases, direct, strict=True):
        assert render_schema(obj) == fn(obj)


def test_render_schema_unknown_type_raises():
    with pytest.raises(TypeError):
        render_schema(object())


def test_all_renderers_pure():
    for obj, fn in [
        (_trader_proposal(), render_trader_proposal),
        (_research_plan(), render_research_plan),
        (_risk_summary(), render_risk_debate_summary),
        (_portfolio_decision(), render_portfolio_decision),
    ]:
        assert fn(obj) == fn(obj)
