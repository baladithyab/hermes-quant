"""cs10 regression: PaperReactor._resolve_account_id is the SINGLE account-partition
seam for the tick-lock key, the cap-read, and the state.db write.

The pre-fix body read ``getattr(proposal, "reactor_metadata", ...).get("account_id")``
and documented an account-override safety property the data model does NOT provide:
the canonical ``Proposal`` dataclass has no ``reactor_metadata`` field, so the getattr
ALWAYS missed and the function ALWAYS returned "paper-default". These tests pin the
real v0.1 single-account invariant (every input resolves to "paper-default") AND guard
that ``Proposal`` still has no ``reactor_metadata`` field — so a future schema change
that adds one is forced to revisit this seam rather than silently re-introduce a dead
override path.
"""

from __future__ import annotations

from types import SimpleNamespace

from hermes_quant.proposals import Proposal
from hermes_quant.react.paper import _resolve_account_id


def test_resolve_account_id_always_paper_default_no_attr():
    """A plain Proposal (no reactor_metadata field) resolves to paper-default."""
    proposal = Proposal(
        proposal_id="prop_x",
        state="pending",
        symbol="AAPL",
        asset_class="equity",
        timeframe="1d",
        created_at="2026-06-13T10:00:00Z",
        expires_at="2026-06-13T10:15:00Z",
        advisor_result={},
    )
    assert _resolve_account_id(proposal) == "paper-default"


def test_resolve_account_id_metadata_none_is_paper_default():
    """An object carrying reactor_metadata=None still resolves to paper-default
    (the tick-lock race harness passes exactly this shape)."""
    proposal = SimpleNamespace(reactor_metadata=None, symbol="AAPL")
    assert _resolve_account_id(proposal) == "paper-default"


def test_resolve_account_id_ignores_account_override():
    """v0.1 is single-account: even a reactor_metadata.account_id override is
    IGNORED (the override path is dead until v0.2 named accounts land)."""
    proposal = SimpleNamespace(reactor_metadata={"account_id": "other-account"})
    assert _resolve_account_id(proposal) == "paper-default"


def test_proposal_has_no_reactor_metadata_field():
    """Guard the invariant the fix relies on: the canonical Proposal dataclass has
    NO reactor_metadata field. If this ever changes, _resolve_account_id must be
    revisited (it currently hardcodes the single-account sentinel)."""
    assert "reactor_metadata" not in Proposal.__dataclass_fields__
