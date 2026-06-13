"""Host-side drift guard (seed pdr1): the pdr_core POSITION_LADDER must stay
byte-for-byte equal to the canonical governance ACTION_SPACE.

pdr_core deliberately keeps its OWN copy of the discrete sizing ladder rather than
importing governance (the ADR-0092 purity contract forbids the core reaching into
host/governance modules — see tests/pdr_core/test_contract_purity.py). That clean
extraction boundary means the constant is duplicated, so it CAN silently diverge.

This test lives in the HOST test tree (not under tests/pdr_core/) precisely because
it imports BOTH the core and governance — which the core's own purity-gated tests may
not do. It is the safety net for the duplication: any edit to one ladder that is not
mirrored to the other fails here.
"""

from __future__ import annotations

from hermes_quant.governance.invariants import ACTION_SPACE
from hermes_quant.pdr_core.contracts import POSITION_LADDER


def test_pdr_core_ladder_matches_governance_action_space():
    assert POSITION_LADDER == ACTION_SPACE, (
        "pdr_core.POSITION_LADDER has drifted from governance.invariants.ACTION_SPACE. "
        "The discrete sizing ladder is a money-safety invariant (ADR-0004); the two "
        "copies MUST stay byte-for-byte equal. Mirror the edit, or reconsider the "
        f"duplication.\n  POSITION_LADDER={sorted(POSITION_LADDER)}\n  ACTION_SPACE={sorted(ACTION_SPACE)}"
    )
