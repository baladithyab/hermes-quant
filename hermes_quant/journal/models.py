"""hermes_quant.journal.models — Pydantic models for the settlement journal.

Per ADR-0010 §Schema. Pydantic-only writer means these are the source of
truth; markdown is a render derivative.

Pydantic optional: hermes-quant doesn't pin pydantic as a hard dep, so
we fall back to a dataclass shim if pydantic isn't available. The shim
mimics the validation surface enough that callers don't need to branch.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

try:
    from pydantic import BaseModel, Field

    _USE_PYDANTIC = True
except ImportError:
    _USE_PYDANTIC = False
    BaseModel = object  # type: ignore[assignment,misc]

    def Field(*args: Any, **kwargs: Any) -> Any:  # type: ignore[no-redef]
        return None


if _USE_PYDANTIC:

    class AnalystComponent(BaseModel):  # type: ignore[no-redef]
        analyst: str
        direction: Literal[-1, 0, 1]
        confidence: float = Field(ge=0.0, le=1.0)
        weight: float = Field(ge=0.0, le=1.0)

    class Reflection(BaseModel):  # type: ignore[no-redef]
        thesis_held: bool
        magnitude_error: float
        rule_version: str = "deterministic-v1"

    class SettlementEntry(BaseModel):  # type: ignore[no-redef]
        # Phase A (required at decision time)
        entry_id: str
        asof_decision: datetime
        symbol: str
        asset_class: Literal["crypto", "equity", "etf", "fx", "futures"]
        direction: Literal[-1, 0, 1]
        confidence: float = Field(ge=0.0, le=1.0)
        target_position_pct: float
        decision_price: float
        benchmark_symbol: str
        per_analyst_components: list[AnalystComponent]
        reason: str

        # Phase B (None until settlement)
        asof_settlement: Optional[datetime] = None
        exit_price: Optional[float] = None
        raw_return: Optional[float] = None
        alpha_return: Optional[float] = None
        hold_minutes: Optional[int] = None
        reflection: Optional[Reflection] = None

        # HITL extension (Wave A) — populated when entry came from
        # quant_approve/quant_reject rather than the autopilot daemon
        hitl_kind: Optional[Literal["approve", "reject", "expire"]] = None
        hitl_reason: Optional[str] = None
        hitl_approver: Optional[str] = None

        @classmethod
        def from_dict(cls, data: dict) -> "SettlementEntry":
            return cls(**data)

        def is_resolved(self) -> bool:
            return self.asof_settlement is not None

else:
    # Dataclass shim — runs without pydantic. Strict validation isn't
    # possible without pydantic but the field set is identical.
    from dataclasses import asdict, dataclass, field

    @dataclass
    class AnalystComponent:  # type: ignore[no-redef]
        analyst: str
        direction: int
        confidence: float
        weight: float

        def model_dump(self) -> dict:
            return asdict(self)

    @dataclass
    class Reflection:  # type: ignore[no-redef]
        thesis_held: bool
        magnitude_error: float
        rule_version: str = "deterministic-v1"

        def model_dump(self) -> dict:
            return asdict(self)

    @dataclass
    class SettlementEntry:  # type: ignore[no-redef]
        entry_id: str
        asof_decision: datetime
        symbol: str
        asset_class: str
        direction: int
        confidence: float
        target_position_pct: float
        decision_price: float
        benchmark_symbol: str
        per_analyst_components: list = field(default_factory=list)
        reason: str = ""
        asof_settlement: Optional[datetime] = None
        exit_price: Optional[float] = None
        raw_return: Optional[float] = None
        alpha_return: Optional[float] = None
        hold_minutes: Optional[int] = None
        reflection: Optional[Reflection] = None
        hitl_kind: Optional[str] = None
        hitl_reason: Optional[str] = None
        hitl_approver: Optional[str] = None

        @classmethod
        def from_dict(cls, data: dict) -> "SettlementEntry":
            return cls(**data)

        def is_resolved(self) -> bool:
            return self.asof_settlement is not None

        def model_dump(self) -> dict:
            d = asdict(self)
            # serialize datetime to ISO
            for k in ["asof_decision", "asof_settlement"]:
                v = d.get(k)
                if isinstance(v, datetime):
                    d[k] = v.isoformat()
            return d
