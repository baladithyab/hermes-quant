"""Host-side drift guard (ac1): the pdr_core AssetClass Literal must stay
byte-for-byte equal to the canonical protocol.AssetClass.

pdr_core deliberately keeps its OWN copy of the AssetClass vocabulary rather than
importing the host protocol (the ADR-0092 purity contract forbids the core reaching
into host modules — see tests/pdr_core/test_contract_purity.py). That clean
extraction boundary means the Literal is DUPLICATED, so it CAN silently diverge.

This matters for money-safety: the host money-state (state.portfolio_state) gates the
×100 option contract multiplier on the literal string "us_option". The host-agnostic
seam that will own settlement (ADR-0092) keys the multiplier on the option FAMILY
(pdr_core.contracts.OPTION_ASSET_CLASSES). If the two AssetClass copies drift — e.g.
the host adds a new option-like token that the core copy lacks — a Fill carrying that
token, settled by a core that only knows the stale family, silently misses the ×100
(the ac1 failure mode this guard is named for).

This test lives in the HOST test tree (not under tests/pdr_core/) precisely because it
imports BOTH the core and the host protocol — which the core's own purity-gated tests
may not do. It is the safety net for the duplication: any edit to one AssetClass copy
that is not mirrored to the other fails here. Mirrors the POSITION_LADDER drift guard
(tests/test_pdr_core_ladder_drift.py).
"""

from __future__ import annotations

from hermes_quant.pdr_core.contracts import (
    OPTION_ASSET_CLASSES,
    is_option_asset_class,
)
from hermes_quant.pdr_core.contracts import AssetClass as CoreAssetClass
from hermes_quant.protocol import AssetClass as ProtocolAssetClass


def test_pdr_core_asset_class_matches_protocol():
    core_members = set(CoreAssetClass.__args__)
    protocol_members = set(ProtocolAssetClass.__args__)
    assert core_members == protocol_members, (
        "pdr_core.contracts.AssetClass has drifted from protocol.AssetClass. "
        "The asset_class vocabulary is the host-blind seam (ADR-0092); the two "
        "duplicated Literals MUST stay byte-for-byte equal or a Fill carrying a "
        "host-only token can be mis-settled (ac1 ×100-miss). Mirror the edit.\n"
        f"  core={sorted(core_members)}\n  protocol={sorted(protocol_members)}"
    )


def test_us_option_is_in_both_literals():
    # The live host stamp (react.multileg) must be a recognized member of BOTH
    # the contract and protocol vocabulary, else the settlement seam cannot key
    # the multiplier on it.
    assert "us_option" in CoreAssetClass.__args__
    assert "us_option" in ProtocolAssetClass.__args__
    # The generic family token stays too — both are recognized.
    assert "option" in CoreAssetClass.__args__


def test_option_family_recognizer_covers_live_host_token():
    # The future-core settlement multiplier keys on this family, NOT a bare
    # `== "option"`. Both the generic token and the LIVE host stamp must be
    # recognized so an option Fill never silently misses the ×100 (ac1).
    assert is_option_asset_class("us_option") is True
    assert is_option_asset_class("option") is True
    # Non-option classes are NOT in the family (no spurious ×100).
    for non_option in ("equity", "etf", "crypto", "fx", "multi_leg", None, "stock"):
        assert is_option_asset_class(non_option) is False
    # The family set is exactly the option members of the Literal.
    assert OPTION_ASSET_CLASSES == frozenset({"option", "us_option"})
    assert OPTION_ASSET_CLASSES.issubset(set(CoreAssetClass.__args__))


def test_us_option_accepted_by_contract_triad():
    # Adding the member must not break construction of any TRIAD member with the
    # live host token (the fields are typed `str`, so this is a vocabulary check,
    # not a runtime-validation change).
    from hermes_quant.pdr_core.contracts import AnalystView, Fill, Proposal

    av = AnalystView(
        analyst="multileg_v1",
        asset="NVDA260626C00160000",
        asset_class="us_option",
        direction=1,
        magnitude=0.4,
        confidence=0.6,
        confidence_raw=0.8,
        horizon="1d",
        asof_decision="2026-06-13T15:00:00+00:00",
        bar_ts="2026-06-13T14:59:00+00:00",
    )
    assert av.asset_class == "us_option"
    prop = Proposal(
        symbol="NVDA",
        asset_class="us_option",
        target_position_pct=0.05,
        gate_reason="multileg-passed-gate",
        asof="2026-06-13T15:00:01+00:00",
    )
    assert prop.asset_class == "us_option"
    fill = Fill(
        proposal_id="prop_ml_1",
        asset="NVDA260626C00160000",
        asset_class="us_option",
        fill_price=3.25,
        fill_size_pct=-0.05,
        asof_execution="2026-06-13T15:00:02+00:00",
    )
    assert fill.asset_class == "us_option"
    assert is_option_asset_class(fill.asset_class) is True
