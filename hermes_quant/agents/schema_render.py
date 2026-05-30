"""hermes_quant.agents.schema_render — canonical markdown renderers for the
LLM-stage Pydantic schemas (Wave C G10).

One PURE ``render_X(schema_obj) -> str`` helper per stage schema, mirroring
``journal/render.py``'s discipline (ADR-0010 §8): markdown is a function of the
typed object, never authored ad-hoc at each call site. Keeping all helpers in a
single module means the operator/brief layer imports ONE place and the
formatting cannot drift per-schema.

Conventions:
  * deterministic + pure — same object in ⇒ byte-identical string out;
  * every load-bearing field appears verbatim;
  * a signed direction renders with the shared arrow idiom
    ``{1:"↑", -1:"↓", 0:"→"}``;
  * sizing/confidence floats render with explicit precision (``:+.2%`` /
    ``:.2f``) so the operator sees the discrete-ladder value exactly.
"""

from __future__ import annotations

from typing import Any

from hermes_quant.agents.research_debate.schemas import ResearchPlan
from hermes_quant.agents.risk_committee.committee import RiskDebateSummary
from hermes_quant.agents.trader import TraderProposal
from hermes_quant.aggregators.llm_committee import PortfolioDecision

_ARROW = {1: "↑", -1: "↓", 0: "→"}


def _fmt_opt_price(value: float | None) -> str:
    return f"{value:.4f}" if value is not None else "—"


def render_trader_proposal(p: TraderProposal) -> str:
    """Render a :class:`TraderProposal` to a markdown bullet block."""
    action = p.action.value if hasattr(p.action, "value") else str(p.action)
    lines: list[str] = []
    lines.append(f"### TraderProposal — {action}")
    lines.append(f"- action: {action}")
    lines.append(f"- size_fraction: {p.size_fraction:+.2%}")
    lines.append(f"- confidence: {p.confidence:.2f}")
    lines.append(f"- entry_price: {_fmt_opt_price(p.entry_price)}")
    lines.append(f"- stop_loss: {_fmt_opt_price(p.stop_loss)}")
    lines.append(f"- target_price: {_fmt_opt_price(p.target_price)}")
    horizon = p.time_horizon_days if p.time_horizon_days is not None else "—"
    lines.append(f"- time_horizon_days: {horizon}")
    lines.append(f"- rationale: {p.rationale}")
    if p.warning_message:
        lines.append(f"- warning: {p.warning_message}")
    return "\n".join(lines)


def render_research_plan(p: ResearchPlan) -> str:
    """Render a :class:`ResearchPlan` (Bull/Bear debate output) to markdown."""
    rec = p.recommendation.value if hasattr(p.recommendation, "value") else str(
        p.recommendation
    )
    intensity = getattr(p.recommendation, "signed_intensity", None)
    arrow = ""
    if intensity is not None:
        arrow = " " + _ARROW.get(1 if intensity > 0 else (-1 if intensity < 0 else 0), "?")
    lines: list[str] = []
    lines.append(f"### ResearchPlan — {rec}{arrow}")
    lines.append(f"- recommendation: {rec}")
    lines.append(f"- confidence: {p.confidence:.2f}")
    if p.horizon_emphasis:
        lines.append(f"- horizon_emphasis: {p.horizon_emphasis}")
    lines.append(f"- rationale: {p.rationale}")
    lines.append(f"- strategic_actions: {p.strategic_actions}")
    return "\n".join(lines)


def render_risk_debate_summary(s: RiskDebateSummary) -> str:
    """Render a :class:`RiskDebateSummary` to markdown."""
    lines: list[str] = []
    lines.append(f"### RiskDebateSummary — proposal {s.trader_proposal_id}")
    lines.append(f"- silence_multiplier: {s.silence_multiplier:.2f}")
    lines.append(f"- n_rounds: {s.n_rounds}")
    lines.append(f"- terminated_reason: {s.terminated_reason}")
    lines.append(f"- final_recommendation: {s.final_recommendation}")
    if s.turns:
        lines.append("- turns:")
        for turn in s.turns:
            lines.append(
                f"  - [{turn.turn_index}] {turn.persona}: "
                f"{turn.risk_assessment} (conf={turn.confidence:.2f})"
            )
    return "\n".join(lines)


def render_portfolio_decision(d: PortfolioDecision) -> str:
    """Render a :class:`PortfolioDecision` (PM final synthesis) to markdown."""
    lines: list[str] = []
    lines.append(f"### PortfolioDecision — {d.action}")
    lines.append(f"- action: {d.action}")
    lines.append(f"- size_multiplier: {d.size_multiplier:.2f}")
    lines.append(f"- confidence: {d.confidence:.2f}")
    lines.append(f"- vetoed: {d.vetoed}")
    if d.vetoed or d.veto_source:
        lines.append(f"- veto_source: {d.veto_source or '—'}")
    lines.append(f"- rationale: {d.rationale}")
    return "\n".join(lines)


# Dispatch table — order matters only for readability; isinstance is exact.
_DISPATCH = (
    (TraderProposal, render_trader_proposal),
    (ResearchPlan, render_research_plan),
    (RiskDebateSummary, render_risk_debate_summary),
    (PortfolioDecision, render_portfolio_decision),
)


def render_schema(obj: Any) -> str:
    """Dispatch on isinstance to the matching ``render_*`` helper.

    Raises ``TypeError`` for an unregistered schema type — fail-loud here is
    correct: a brief that silently drops a stage's output is worse than a crash
    in the offline render path.
    """
    for schema_cls, renderer in _DISPATCH:
        if isinstance(obj, schema_cls):
            return renderer(obj)
    raise TypeError(
        f"render_schema: no renderer registered for type {type(obj).__name__!r}"
    )
