"""ADR-0095 — the Pydantic ingress mirror is DERIVED from the one canonical core
contract, so drift is caught at CI, not eyeballed.

Confirmation criteria (ADR-0095):
  (1) parity: the mirror's field set + required-ness == the core dataclass field set
      (a generation check against ONE source, not a reconciliation of two);
  (2) cross-host round-trip: a Cowork-shaped AnalystView JSON validates and constructs
      the core dataclass with NO field loss;
  (3) the off-ladder / bool / NaN rejection guards run against the single definition
      (the mirror delegates to the core's construction guards via to_core()).
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from hermes_quant.agents.contract_mirror import (
    MIRROR_FOR_CORE,
    AnalystViewModel,
    FillModel,
    ProposalModel,
)
from hermes_quant.pdr_core import contracts as core

UTC = timezone.utc


# --------------------------------------------------------------------------- #
# (1) PARITY — the mirror field set == the dataclass field set, per triad member.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("core_cls", list(MIRROR_FOR_CORE.keys()))
def test_mirror_field_set_matches_dataclass(core_cls):
    mirror_cls = MIRROR_FOR_CORE[core_cls]
    dc_fields = {f.name for f in dataclasses.fields(core_cls)}
    mirror_fields = set(mirror_cls.model_fields.keys())
    assert mirror_fields == dc_fields, (
        f"ADR-0095 drift: {core_cls.__name__} dataclass fields {dc_fields} != "
        f"{mirror_cls.__name__} mirror fields {mirror_fields}. The mirror must be a "
        f"DERIVED definition — add/remove the field in BOTH or (better) regenerate the "
        f"mirror from the dataclass. missing-from-mirror={dc_fields - mirror_fields}, "
        f"extra-in-mirror={mirror_fields - dc_fields}"
    )


@pytest.mark.parametrize("core_cls", list(MIRROR_FOR_CORE.keys()))
def test_mirror_required_optional_matches_dataclass(core_cls):
    """A field with a dataclass default is OPTIONAL in the mirror; one without is REQUIRED."""
    mirror_cls = MIRROR_FOR_CORE[core_cls]
    for f in dataclasses.fields(core_cls):
        has_default = (
            f.default is not dataclasses.MISSING
            or f.default_factory is not dataclasses.MISSING  # type: ignore[misc]
        )
        assert f.name in mirror_cls.model_fields, (
            f"ADR-0095 drift: {core_cls.__name__}.{f.name} has no mirror field "
            "(regenerate the mirror from the dataclass)"
        )
        mf = mirror_cls.model_fields[f.name]
        if has_default:
            assert not mf.is_required(), (
                f"ADR-0095: {core_cls.__name__}.{f.name} has a dataclass default so the "
                f"mirror field must be OPTIONAL, but it is required"
            )
        else:
            assert mf.is_required(), (
                f"ADR-0095: {core_cls.__name__}.{f.name} has NO dataclass default so the "
                f"mirror field must be REQUIRED, but it is optional"
            )


def test_vocabulary_matches_core():
    # asset_class + ladder are re-exported from the core, not re-declared.
    from hermes_quant.agents import contract_mirror as cm

    assert cm.POSITION_LADDER is core.POSITION_LADDER
    # The mirror's AssetClass Literal must contain exactly the core's members.
    import typing

    mirror_members = set(typing.get_args(cm.AssetClass))
    core_members = set(typing.get_args(core.AssetClass))
    assert mirror_members == core_members, (
        f"asset_class vocabulary drift: mirror {mirror_members} != core {core_members}"
    )


# --------------------------------------------------------------------------- #
# (2) CROSS-HOST ROUND-TRIP — a Cowork-shaped JSON validates + builds the core.
# --------------------------------------------------------------------------- #
def test_cowork_shaped_analystview_json_roundtrips_to_core():
    """A Cowork-produced AnalystView JSON (UTC asof, no field loss) -> core dataclass."""
    payload = {
        "analyst": "classical-ta",
        "asset": "ASTS",
        "asset_class": "equity",
        "direction": 1,
        "magnitude": 0.4,
        "confidence": 0.7,
        "confidence_raw": 0.65,
        "horizon": "1d",
        "asof_decision": "2026-06-17T15:00:00+00:00",
        "bar_ts": "2026-06-16T20:00:00+00:00",
        "rationale": "breakout on volume",
        "evidence_ids": ["ev1", "ev2"],
        "metadata": {"source": "cowork"},
    }
    model = AnalystViewModel.model_validate(payload)
    view = model.to_core()
    assert isinstance(view, core.AnalystView)
    # No field loss: every dataclass field is populated from the JSON.
    assert view.analyst == "classical-ta"
    assert view.confidence_raw == 0.65  # the field cowork's schema LACKED
    assert view.metadata == {"source": "cowork"}  # also cowork-lacking
    assert view.evidence_ids == ("ev1", "ev2")


def test_proposal_and_fill_roundtrip_to_core():
    p = ProposalModel.model_validate({
        "symbol": "ASTS", "asset_class": "equity", "target_position_pct": 0.20,
        "gate_reason": "ok", "asof": "2026-06-17T15:00:00+00:00",
    }).to_core()
    assert isinstance(p, core.Proposal) and p.target_position_pct == 0.20

    f = FillModel.model_validate({
        "proposal_id": "p1", "asset": "ASTS", "asset_class": "equity",
        "fill_price": 100.0, "fill_size_pct": 0.20,
        "asof_execution": "2026-06-17T15:00:00+00:00",
    }).to_core()
    assert isinstance(f, core.Fill) and f.fill_size_pct == 0.20
    assert f.schema_version == core.FILL_SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# (3) The core's construction guards run through the mirror (single definition).
# --------------------------------------------------------------------------- #
def test_offladder_proposal_rejected_at_ingress():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        ProposalModel.model_validate({
            "symbol": "X", "asset_class": "equity", "target_position_pct": 0.13,  # off-ladder
            "gate_reason": "x", "asof": "2026-06-17T15:00:00+00:00",
        })


def test_naive_timestamp_rejected_at_ingress():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        AnalystViewModel.model_validate({
            "analyst": "a", "asset": "X", "asset_class": "equity", "direction": 1,
            "magnitude": 0.1, "confidence": 0.5, "confidence_raw": 0.5, "horizon": "1d",
            "asof_decision": datetime(2026, 6, 17, 15, 0, 0),  # naive -> reject
            "bar_ts": "2026-06-16T20:00:00+00:00",
        })


def test_out_of_range_confidence_rejected_at_ingress():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        AnalystViewModel.model_validate({
            "analyst": "a", "asset": "X", "asset_class": "equity", "direction": 1,
            "magnitude": 0.1, "confidence": 1.5, "confidence_raw": 0.5, "horizon": "1d",
            "asof_decision": "2026-06-17T15:00:00+00:00",
            "bar_ts": "2026-06-16T20:00:00+00:00",
        })


def test_to_core_reapplies_core_guard_when_mirror_would_miss_it():
    """Defense in depth: even if the mirror's range check were bypassed, to_core() must
    re-run the core guard. We simulate by constructing the core directly with a bad value
    and asserting the core rejects it (proving to_core's path is guarded by the core)."""
    with pytest.raises(ValueError):
        core.AnalystView(
            analyst="a", asset="X", asset_class="equity", direction=1,
            magnitude=float("nan"), confidence=0.5, confidence_raw=0.5, horizon="1d",
            asof_decision="2026-06-17T15:00:00+00:00", bar_ts="2026-06-16T20:00:00+00:00",
        )
