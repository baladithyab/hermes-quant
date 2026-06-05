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

import os
from typing import Any

from .backend import BACKEND_DETERMINISTIC, resolve_backend_choice
from .base import Reactor
from .multileg import MultiLegPaperReactor
from .paper import PaperReactor

# Flag that ROUTES equity paper fills through Alpaca's paper broker instead of
# the synthetic append-only book. ADDITIVE + DEFAULT-OFF: with the flag unset,
# select_reactor() returns PaperReactor for equity exactly as before.
ALPACA_PAPER_FLAG = "HERMES_QUANT_ALPACA_PAPER"

# Flag that ROUTES equity paper fills through the BP-enforcing DeterministicBackend
# (ADR-0088 follow-up) instead of the synthetic append-only book. ADDITIVE +
# DEFAULT-OFF + EXPLICIT OPT-IN: resolve_backend_choice() returns 'deterministic' by
# DEFAULT, so gating on it ALONE would silently change everyone's equity path — this
# REQUIRES the explicit flag so a flag-OFF run is bit-for-bit the legacy PaperReactor.
DETERMINISTIC_EQUITY_FLAG = "HERMES_QUANT_DETERMINISTIC_EQUITY"


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
    # ADDITIVE + DEFAULT-OFF: route equity fills through Alpaca's paper broker
    # (real buying-power/margin/shorting enforcement + real fills) ONLY when the
    # flag is explicitly set. With the flag unset the equity path is bit-for-bit
    # the legacy PaperReactor — no import, no behavior change. The multi-leg
    # branch above is untouched.
    if os.environ.get(ALPACA_PAPER_FLAG, "0") == "1":
        from .alpaca_paper import AlpacaPaperReactor

        return AlpacaPaperReactor()
    # ADDITIVE + DEFAULT-OFF + EXPLICIT OPT-IN: route equity fills through the
    # BP-enforcing DeterministicBackend (enforces buying power + tracks true shares,
    # closing the 880%-gross root cause on the equity path) ONLY when the explicit
    # HERMES_QUANT_DETERMINISTIC_EQUITY=1 flag is set AND the resolved backend is the
    # deterministic simulator. We require BOTH: resolve_backend_choice() returns
    # 'deterministic' by default, so gating on it alone would silently change every
    # equity path — the explicit flag keeps flag-OFF bit-for-bit the legacy reactor.
    if (
        os.environ.get(DETERMINISTIC_EQUITY_FLAG, "0") == "1"
        and resolve_backend_choice() == BACKEND_DETERMINISTIC
    ):
        from .deterministic_equity import DeterministicEquityReactor

        return DeterministicEquityReactor()
    return PaperReactor()
