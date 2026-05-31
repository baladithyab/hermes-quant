"""hermes_quant.react.dispatch — reactor selection by proposal kind (ADR-0029 §2.5).

``quant_approve`` (the HITL/CLI money seam) calls ``select_reactor(proposal)`` instead
of hardcoding ``PaperReactor()``. Equity ``Proposal`` -> ``PaperReactor``;
``MultiLegProposal`` -> ``MultiLegPaperReactor`` (DEFAULT-OFF; if the flag is unset its
``execute()`` raises ``MultiLegReactorDisabled``, surfaced to the operator as a clear
"multi-leg reactor not enabled" error — NEVER a silent equity fill).

This is the ONLY money seam touched by the multi-leg wave, and it stays HITL/CLI-only
and operator-confirmed (AGENTS.md money-never-through-tools). ``autonomous.py`` is NOT
changed — no autonomous multi-leg this wave (ADR-0016 deferred).
"""

from __future__ import annotations

from typing import Any

from .base import Reactor
from .multileg import MultiLegPaperReactor
from .paper import PaperReactor


def is_multi_leg_proposal(proposal: Any) -> bool:
    """True iff the proposal routes to the multi-leg reactor.

    Keys on the structural discriminator, not isinstance, so a proposal-store record
    carrying ``proposal_kind == 'multi_leg'`` OR a runtime ``MultiLegProposal`` (which
    has ``option_legs`` / ``strategy_kind``) both route correctly.
    """
    kind = getattr(proposal, "proposal_kind", None)
    if kind == "multi_leg":
        return True
    if kind == "equity":
        return False
    # Fall back to the structural shape: a MultiLegProposal carries option_legs +
    # strategy_kind; an equity Proposal does not.
    return hasattr(proposal, "option_legs") and hasattr(proposal, "strategy_kind")


def select_reactor(proposal: Any) -> Reactor:
    """Return the reactor for a proposal (ADR-0029 §2.5).

    Equity ``Proposal`` -> ``PaperReactor``. ``MultiLegProposal`` ->
    ``MultiLegPaperReactor`` (default-OFF; execute() raises ``MultiLegReactorDisabled``
    when the flag is unset, surfaced as a clear error rather than a silent equity fill).
    """
    if is_multi_leg_proposal(proposal):
        return MultiLegPaperReactor()
    return PaperReactor()
