"""hermes_quant.agents.trader_node — TraderNodeWithRisk wrapper (Wave 3).

ADR-0043: Combines the deterministic Wave 2 TraderNode with the Wave 3
RiskCommittee debate. Returns BOTH the (possibly silenced) TraderProposal
and the RiskDebateSummary so the brief script can render the debate.

Pipeline:
    research_plan, advisor_signal
        |
        v
    TraderNode  ->  TraderProposal (raw)
        |
        v
    RiskCommittee.debate  ->  RiskDebateSummary (silence_multiplier ≤ 1.0)
        |
        v
    apply silence_multiplier  ->  TraderProposal (silenced if needed)

Invariants (CV5 anti-pattern guard, ADR-0043):
  * silence_multiplier == 1.0 -> proposal is unchanged.
  * 0 < silence_multiplier < 1.0 -> size_fraction is scaled down.
  * silence_multiplier == 0.0 -> action becomes HOLD, size_fraction = 0.0,
    warning_message reflects the silencing.
  * silence_multiplier can NEVER exceed 1.0 (committee can only silence).
"""

from __future__ import annotations

import logging
from typing import Any

from hermes_quant.agents.risk_committee.committee import (
    RiskCommittee,
    RiskDebateSummary,
)
from hermes_quant.agents.trader import (
    TraderAction,
    TraderNode,
    TraderProposal,
)

logger = logging.getLogger(__name__)


SILENCED_WARNING_MSG: str = "silenced by risk committee"


class TraderNodeWithRisk:
    """TraderNode + 3-way RiskCommittee, returning both outputs.

    Args:
        trader_node: Optional pre-constructed TraderNode. Defaults to a
            fresh deterministic instance.
        risk_committee: Optional pre-constructed RiskCommittee. Defaults
            to a fresh deterministic instance.
    """

    def __init__(
        self,
        trader_node: TraderNode | None = None,
        risk_committee: RiskCommittee | None = None,
    ) -> None:
        self._trader = trader_node if trader_node is not None else TraderNode()
        self._committee = (
            risk_committee if risk_committee is not None else RiskCommittee()
        )

    # ------------------------------------------------------------------

    def __call__(
        self,
        research_plan: dict[str, Any],
        advisor_signal: dict[str, Any] | None = None,
        *,
        max_rounds: int | None = None,
        proposal_id: str | None = None,
    ) -> tuple[TraderProposal, RiskDebateSummary]:
        """Build a TraderProposal and run the risk committee against it.

        Returns:
            (proposal, summary) where proposal already reflects the
            silence_multiplier (size_fraction is scaled, or action=HOLD
            if multiplier == 0.0).
        """
        raw_proposal = self._trader(research_plan, advisor_signal)
        summary = self._committee.debate(
            raw_proposal,
            research_plan,
            max_rounds=max_rounds,
            proposal_id=proposal_id,
        )
        adjusted = self._apply_silence_multiplier(raw_proposal, summary)
        return adjusted, summary

    # ------------------------------------------------------------------

    @staticmethod
    def _apply_silence_multiplier(
        proposal: TraderProposal,
        summary: RiskDebateSummary,
    ) -> TraderProposal:
        """Return a copy of proposal with size_fraction scaled by the
        committee's silence_multiplier.

        CV5 anti-pattern guard: silence_multiplier > 1.0 is impossible by
        construction (RiskCommittee enforces ≤ 1.0). We re-assert here
        defensively in case a future change weakens the upstream invariant.
        """
        m = float(summary.silence_multiplier)
        if m > 1.0:
            # Defensive — should never happen.
            logger.warning(
                "silence_multiplier=%.4f > 1.0; clamping to 1.0 "
                "(CV5 anti-pattern guard).",
                m,
            )
            m = 1.0
        m = max(0.0, m)

        if m == 1.0:
            return proposal  # no-op fast-path

        if m == 0.0:
            existing_warning = proposal.warning_message or ""
            new_warning = (
                f"{SILENCED_WARNING_MSG}; "
                f"silence_multiplier=0.0; original_action={proposal.action.value}"
            )
            if existing_warning:
                new_warning = f"{existing_warning} | {new_warning}"
            return proposal.model_copy(
                update={
                    "action": TraderAction.HOLD,
                    "size_fraction": 0.0,
                    "warning_message": new_warning,
                }
            )

        # Partial silence: 0 < m < 1.0
        new_size = round(proposal.size_fraction * m, 6)
        # size_fraction floor at 0 to satisfy ge=0.0 constraint
        new_size = max(0.0, new_size)
        existing_warning = proposal.warning_message or ""
        scale_warning = (
            f"size scaled by risk committee (silence_multiplier={m:.2f})"
        )
        new_warning = (
            f"{existing_warning} | {scale_warning}"
            if existing_warning
            else scale_warning
        )
        return proposal.model_copy(
            update={
                "size_fraction": new_size,
                "warning_message": new_warning,
            }
        )
