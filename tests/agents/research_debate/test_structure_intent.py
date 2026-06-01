"""Contract tests for the additive, optional ``ResearchPlan.structure_intent``
field + the ``StructureIntent`` coarse enum (ADR-0082 Part B).

Scope of the seed under test (hermes-quant-26dc): JUST the contract field and
its plumbing through the debate schema. NOTHING fires off it — the deterministic
structure-selection table + the options_gate (separate seeds) decide the actual
StrategyKind/legs; the LLM never picks legs and ``structure_intent`` is never a
money-path lever.

What is pinned here:
  * existing/legacy ResearchPlan rows (no ``structure_intent`` key) parse, with
    the field defaulting to ``None`` (≡ today's equity path);
  * a plan CAN carry an intent (each enum member accepted, on the wire too);
  * the field is OPTIONAL + ADDITIVE (off-state dump is byte-identical to a
    pre-field plan dump);
  * the enum is a label-stable ``StrEnum`` mirroring ADR-0082 §Part B exactly;
  * ``extra='forbid'`` is unchanged (unknown fields still rejected);
  * junk intent strings are rejected.

Offline + deterministic: no network, no fixtures, pure schema round-trips.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from hermes_quant.agents.research_debate.schemas import (
    PortfolioRating,
    ResearchPlan,
    StructureIntent,
)

# A minimal, valid legacy-shaped ResearchPlan payload (NO structure_intent key),
# representing rows produced before ADR-0082.
_LEGACY_ROW = {
    "recommendation": "BUY",
    "confidence": 0.7,
    "rationale": "Bull case stronger than bear case net of evidence.",
    "strategic_actions": "Enter on close above prior high; stop below day low.",
}


def _legacy_plan() -> ResearchPlan:
    return ResearchPlan.model_validate(dict(_LEGACY_ROW))


# ---------------------------------------------------------------------------
# Backward-compat: existing rows parse, default None (equity path)
# ---------------------------------------------------------------------------
def test_legacy_row_parses_with_structure_intent_none() -> None:
    """A ResearchPlan row with NO ``structure_intent`` key parses unchanged and
    the field defaults to ``None`` (≡ silence-by-default equity path)."""
    plan = _legacy_plan()
    assert plan.structure_intent is None


def test_kwarg_construction_defaults_none() -> None:
    """Constructing via kwargs without ``structure_intent`` defaults to None."""
    plan = ResearchPlan(
        recommendation=PortfolioRating.HOLD,
        confidence=0.5,
        rationale="x",
        strategic_actions="y",
    )
    assert plan.structure_intent is None


# ---------------------------------------------------------------------------
# A plan CAN carry an intent
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("member", list(StructureIntent))
def test_plan_can_carry_each_intent(member: StructureIntent) -> None:
    """Every StructureIntent member is an accepted value for the field, both as
    the enum and via its wire (string) label."""
    # via enum
    plan = ResearchPlan(
        recommendation=PortfolioRating.OVERWEIGHT,
        confidence=0.6,
        rationale="range-bound thesis",
        strategic_actions="prefer premium capture",
        structure_intent=member,
    )
    assert plan.structure_intent is member
    assert plan.structure_intent == member

    # via wire string (case-sensitive lowercase labels, mirroring ADR-0082)
    plan_wire = ResearchPlan.model_validate(
        {**_LEGACY_ROW, "structure_intent": member.value}
    )
    assert plan_wire.structure_intent is member


def test_explicit_none_member_distinct_from_unset() -> None:
    """The explicit ``StructureIntent.NONE`` member is accepted and is the
    equity-path member; it is the semantic equal of an unset (None) field."""
    plan = ResearchPlan.model_validate(
        {**_LEGACY_ROW, "structure_intent": "none"}
    )
    assert plan.structure_intent is StructureIntent.NONE
    # Both NONE-member and unset mean "no structure preference / equity path".
    assert _legacy_plan().structure_intent is None


# ---------------------------------------------------------------------------
# Additive + optional: off-state byte-identical
# ---------------------------------------------------------------------------
def test_off_state_dump_byte_identical_to_legacy() -> None:
    """A plan built from a legacy row dumps to the SAME JSON as a plan that
    explicitly leaves ``structure_intent`` at its default — i.e. the field's
    presence does not perturb the off-state serialisation. The defaulted field
    serialises to ``"structure_intent": null`` and nothing else changes."""
    legacy = _legacy_plan()
    explicit_default = ResearchPlan(
        recommendation=PortfolioRating.BUY,
        confidence=0.7,
        rationale="Bull case stronger than bear case net of evidence.",
        strategic_actions="Enter on close above prior high; stop below day low.",
    )
    assert legacy.model_dump_json() == explicit_default.model_dump_json()

    # The defaulted field is present-and-null in the dump (declared field).
    dumped = json.loads(legacy.model_dump_json())
    assert dumped["structure_intent"] is None


def test_round_trip_preserves_intent() -> None:
    """A carried intent survives a model_dump_json → model_validate_json round
    trip (label-stable per ADR-0058)."""
    plan = ResearchPlan(
        recommendation=PortfolioRating.SELL,
        confidence=0.55,
        rationale="bearish, range-bound",
        strategic_actions="define risk",
        structure_intent=StructureIntent.DEFINED_RISK_CREDIT,
    )
    restored = ResearchPlan.model_validate_json(plan.model_dump_json())
    assert restored.structure_intent is StructureIntent.DEFINED_RISK_CREDIT
    assert restored == plan


# ---------------------------------------------------------------------------
# Enum is the ADR-0082 contract, label-stable
# ---------------------------------------------------------------------------
def test_structure_intent_members_match_adr_0082() -> None:
    """The enum members + wire labels mirror ADR-0082 §Part B exactly."""
    assert {m.value for m in StructureIntent} == {
        "none",
        "defined_risk_credit",
        "defined_risk_debit",
        "premium_capture",
        "long_premium",
    }


def test_structure_intent_is_label_stable_strenum() -> None:
    """StrEnum: members serialise to their string label (ADR-0058)."""
    assert StructureIntent.PREMIUM_CAPTURE == "premium_capture"
    assert json.dumps({"si": StructureIntent.LONG_PREMIUM}) == '{"si": "long_premium"}'


# ---------------------------------------------------------------------------
# Rails: extra='forbid' unchanged; junk rejected
# ---------------------------------------------------------------------------
def test_extra_forbid_still_rejects_unknown_field() -> None:
    """Adding ``structure_intent`` did not relax ``extra='forbid'`` — the legacy
    ``overrules_baseline`` (and any unknown field) is still rejected."""
    with pytest.raises(ValidationError):
        ResearchPlan.model_validate(
            {**_LEGACY_ROW, "overrules_baseline": True}
        )


def test_junk_structure_intent_rejected() -> None:
    """A non-canonical structure_intent string is rejected by the enum."""
    with pytest.raises(ValidationError):
        ResearchPlan.model_validate(
            {**_LEGACY_ROW, "structure_intent": "iron_condor"}  # a leg-level kind, not an intent
        )
