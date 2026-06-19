"""hermes_quant.agents.contract_mirror — the DERIVED Pydantic mirror of the
Aegis-core contract triad (ADR-0095).

ADR-0095 (one canonical contract): the Aegis core
(:mod:`hermes_quant.pdr_core.contracts`) owns the ONE canonical contract triad as
stdlib frozen dataclasses — ``AnalystView`` / ``Proposal`` / ``Fill`` + the signed
``POSITION_LADDER``. The core stays stdlib-pure so it is "trivially movable to a
standalone ``aegis`` package" (ADR-0092/0093). But the LLM/JSON ingress still needs
real schema validation (free text must NEVER drive state — ABSTAIN on a bad parse).

This module is that ingress validator — a Pydantic v2 mirror of the core triad that
is **DERIVED FROM the dataclasses, never an independent second definition** (the very
drift ADR-0095 kills). "Derived" is enforced two ways:

  1. The field set is read from ``dataclasses.fields(core.AnalystView)`` etc. at import
     time, and :func:`hermes_quant.agents.contract_mirror` ships a parity test
     (``tests/contract/test_contract_mirror_parity_adr0095.py``) asserting the mirror's
     ``model_fields`` exactly equals the dataclass field set + types + required-ness.
     Add a field to the core dataclass and the parity test goes RED until the mirror is
     updated — drift is *caught at CI*, not eyeballed.
  2. Every mirror carries a ``to_core()`` that constructs the canonical dataclass. The
     dataclass ``__post_init__`` re-runs ALL the core guards (bool/str/NaN/off-ladder),
     so a value that satisfies Pydantic's range but violates a core invariant is still
     rejected — the core remains the final authority (defense in depth).

Validation posture (silence-by-default): a malformed model payload raises
``pydantic.ValidationError`` at ``model_validate``; the caller ABSTAINS (the
``structured_output`` helpers already return ``(None, raw)`` on a validation failure).
The mirror enforces the ingress-only invariants the dataclass leaves to the shell —
notably UTC-aware timestamps at the boundary — while the core stores ``Any`` timestamps
(it never parses them for arithmetic at the contract layer).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from hermes_quant.pdr_core import contracts as core

UTC = timezone.utc

# Re-export the canonical vocabulary from the core so the mirror cannot drift on it.
Direction = Literal[-1, 0, 1]
AssetClass = Literal["crypto", "equity", "etf", "fx", "option", "us_option"]
POSITION_LADDER = core.POSITION_LADDER


def _require_utc(v: Any) -> Any:
    """Ingress invariant: a datetime must be tz-aware; normalize to UTC.

    Non-datetime values (an ISO string, a pandas.Timestamp) pass through untouched —
    the core stores ``Any`` and downstream settlement does its own typing. We only
    enforce tz-awareness when the shell handed us a bare ``datetime`` (the common
    Pydantic-parsed case), to forbid the naive-local-time foot-gun at the boundary.
    """
    if isinstance(v, datetime):
        if v.tzinfo is None:
            raise ValueError("timestamp must be tz-aware (UTC)")
        return v.astimezone(UTC)
    return v


class AnalystViewModel(BaseModel):
    """Pydantic ingress mirror of :class:`core.AnalystView`.

    Field set + types are parity-tested against the dataclass. ``to_core()`` builds the
    canonical frozen dataclass (which re-validates bool/str/NaN/range).
    """

    model_config = ConfigDict(extra="forbid")

    analyst: str = Field(min_length=1)
    asset: str = Field(min_length=1)
    asset_class: AssetClass
    direction: Direction
    magnitude: float = Field(ge=0.0, le=1.0, description="normalized expected-move strength [0,1]")
    confidence: float = Field(ge=0.0, le=1.0, description="CALIBRATED P(direction correct) [0,1]")
    confidence_raw: float = Field(ge=0.0, le=1.0, description="raw pre-calibration score [0,1]")
    horizon: str = Field(min_length=1)
    asof_decision: Any = Field(description="decision timestamp; tz-aware UTC if a datetime")
    bar_ts: Any = Field(description="bar the view was computed on; bar_ts <= asof_decision")
    rationale: str | None = Field(default=None, max_length=2000)
    evidence_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] | None = None

    _utc_asof = field_validator("asof_decision")(_require_utc)
    _utc_bar = field_validator("bar_ts")(_require_utc)

    def to_core(self) -> core.AnalystView:
        """Construct the canonical dataclass (re-runs the core's construction guards)."""
        return core.AnalystView(
            analyst=self.analyst,
            asset=self.asset,
            asset_class=self.asset_class,
            direction=self.direction,
            magnitude=self.magnitude,
            confidence=self.confidence,
            confidence_raw=self.confidence_raw,
            horizon=self.horizon,
            asof_decision=self.asof_decision,
            bar_ts=self.bar_ts,
            rationale=self.rationale,
            evidence_ids=tuple(self.evidence_ids),
            metadata=self.metadata,
        )


class ProposalModel(BaseModel):
    """Pydantic ingress mirror of :class:`core.Proposal` (the sized decision)."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1)
    asset_class: AssetClass
    target_position_pct: float
    gate_reason: str
    asof: Any = Field(description="decision timestamp; tz-aware UTC if a datetime")

    _utc_asof = field_validator("asof")(_require_utc)

    @field_validator("target_position_pct")
    @classmethod
    def _on_ladder(cls, v: float) -> float:
        # Mirror the core's signed-ladder membership (the anti-leverage-gambling
        # invariant). The core re-checks in __post_init__, but rejecting here gives the
        # ingress a clean ValidationError -> ABSTAIN instead of a downstream raise.
        if not core._on_ladder(v):
            raise ValueError(
                f"target_position_pct {v!r} not on the signed ladder {sorted(POSITION_LADDER)}"
            )
        return v

    def to_core(self) -> core.Proposal:
        return core.Proposal(
            symbol=self.symbol,
            asset_class=self.asset_class,
            target_position_pct=self.target_position_pct,
            gate_reason=self.gate_reason,
            asof=self.asof,
        )


class FillModel(BaseModel):
    """Pydantic ingress mirror of :class:`core.Fill` (the Option-E absolute-target feedback)."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1)
    asset: str = Field(min_length=1)
    asset_class: AssetClass
    fill_price: float = Field(gt=0.0)
    fill_size_pct: float = Field(description="ABSOLUTE post-fill target (NAV fraction), Option E")
    asof_execution: Any = Field(description="execution timestamp; tz-aware UTC if a datetime")
    schema_version: str = core.FILL_SCHEMA_VERSION
    metadata: dict[str, Any] | None = None

    _utc_exec = field_validator("asof_execution")(_require_utc)

    def to_core(self) -> core.Fill:
        return core.Fill(
            proposal_id=self.proposal_id,
            asset=self.asset,
            asset_class=self.asset_class,
            fill_price=self.fill_price,
            fill_size_pct=self.fill_size_pct,
            asof_execution=self.asof_execution,
            schema_version=self.schema_version,
            metadata=self.metadata,
        )


# The mirror <-> core pairing the parity test introspects. Keeping it here (not in the
# test) means any new triad member is registered in ONE place the test reads.
MIRROR_FOR_CORE: dict[type, type[BaseModel]] = {
    core.AnalystView: AnalystViewModel,
    core.Proposal: ProposalModel,
    core.Fill: FillModel,
}
