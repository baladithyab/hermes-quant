"""quantcore.schemas — Pydantic contracts at the LLM/script boundary.

Free text NEVER drives state: Claude emits JSON matching these models; the
gate and ledger validate before anything is persisted. On validation failure
the correct behavior is ABSTAIN (silence-by-default), not retry-into-compliance.

Lean port of hermes_quant.protocol (AnalystView / AggregatedSignal / Action)
with Cowork-specific additions (Proposal, Fill).
"""

from __future__ import annotations

from datetime import datetime, timezone
UTC = timezone.utc
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Direction = Literal[-1, 0, 1]
AssetClass = Literal["equity", "etf", "crypto", "fx", "option"]

#: The immutable discrete sizing ladder (fractions of NAV). Rail #3.
SIZING_LADDER: tuple[float, ...] = (0.0, 0.05, 0.10, 0.15, 0.20)


def _require_utc(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamp must be tz-aware (UTC)")
    return v.astimezone(UTC)


class AnalystView(BaseModel):
    """One analyst's view. Uniform schema so the committee is analyst-agnostic."""

    analyst: str = Field(min_length=1, description="e.g. 'classical-ta', 'fundamentals'")
    asset: str = Field(min_length=1)
    asset_class: AssetClass
    direction: Direction
    magnitude: float = Field(ge=0.0, le=1.0, description="expected |return| over horizon")
    confidence: float = Field(ge=0.0, le=1.0, description="calibrated P(direction correct)")
    horizon: str = Field(min_length=1, description="e.g. '5d', '1h'")
    asof_decision: datetime = Field(description="wall-clock decision time (UTC)")
    bar_ts: datetime | None = Field(default=None, description="last CLOSED bar used")
    rationale: str = Field(default="", max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list)

    _utc = field_validator("asof_decision")(_require_utc)

    @field_validator("bar_ts")
    @classmethod
    def _bar_utc(cls, v: datetime | None) -> datetime | None:
        return None if v is None else _require_utc(v)


class CommitteeSignal(BaseModel):
    """Aggregated committee output entering the gate (lean AggregatedSignal)."""

    asset: str = Field(min_length=1)
    asset_class: AssetClass
    direction: Direction
    magnitude: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    horizon: str
    asof_decision: datetime
    views: list[AnalystView] = Field(default_factory=list)
    dissent: str = Field(default="", max_length=2000, description="recorded disagreement")
    event_risk: list[dict] = Field(
        default_factory=list,
        description="asof-honest scheduled events: {kind, impact, scheduled_for}",
    )

    _utc = field_validator("asof_decision")(_require_utc)

    @property
    def n_distinct_analysts(self) -> int:
        return len({v.analyst for v in self.views})


class MarketCosts(BaseModel):
    """Cost + vol inputs for the gate (lean MarketState)."""

    commission: float = Field(ge=0.0, description="per-side, fraction")
    spread: float = Field(ge=0.0, description="round-trip, fraction")
    slippage_estimate: float = Field(ge=0.0, description="round-trip, fraction")
    volatility: float = Field(gt=0.0, description="per-period log-return stdev")
    tz: str = "UTC"


class Position(BaseModel):
    asset: str
    asset_class: AssetClass
    position_pct: float = Field(ge=-1.0, le=1.0, description="signed fraction of NAV")
    avg_price: float = Field(gt=0.0)
    opened_at: datetime

    _utc = field_validator("opened_at")(_require_utc)


class PortfolioState(BaseModel):
    """Reconstructed from the ledger; never hand-edited."""

    nav: float = Field(gt=0.0)
    peak_nav: float = Field(gt=0.0)
    day_start_nav: float = Field(gt=0.0)
    positions: list[Position] = Field(default_factory=list)
    asof: datetime
    halted: bool = False
    halt_reason: str | None = None
    halt_until: datetime | None = None
    last_loss_at: datetime | None = None

    _utc = field_validator("asof")(_require_utc)

    @property
    def drawdown_pct(self) -> float:
        return max(0.0, (self.peak_nav - self.nav) / self.peak_nav)

    @property
    def daily_loss_pct(self) -> float:
        return max(0.0, (self.day_start_nav - self.nav) / self.day_start_nav)

    def current_position_pct(self, asset: str) -> float:
        return sum(p.position_pct for p in self.positions if p.asset == asset)


class Proposal(BaseModel):
    """Gate-approved, human-pending trade proposal. The ONLY path to the book."""

    proposal_id: str = Field(min_length=8)
    signal: CommitteeSignal
    target_position_pct: float
    current_position_pct: float
    delta_pct: float
    gate_reason: str
    created_at: datetime
    status: Literal["pending", "approved", "rejected", "expired", "filled"] = "pending"

    _utc = field_validator("created_at")(_require_utc)

    @field_validator("target_position_pct")
    @classmethod
    def _on_ladder(cls, v: float) -> float:
        if not any(abs(abs(v) - rung) < 1e-9 for rung in SIZING_LADDER):
            raise ValueError(f"target {v} not on sizing ladder {SIZING_LADDER}")
        return v


class Fill(BaseModel):
    """Human-confirmed execution (manual entry or read-only broker MCP readback)."""

    proposal_id: str
    asset: str
    fill_price: float = Field(gt=0.0)
    filled_position_pct: float
    filled_at: datetime
    source: Literal["manual", "broker-readback"] = "manual"

    _utc = field_validator("filled_at")(_require_utc)
