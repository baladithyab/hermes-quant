"""quantcore.config — RiskConfig profiles (ADR-0004 port) + state-dir config.

Config lives at <quant-state>/config.json (JSON, not YAML — zero extra deps).
Unknown keys are rejected (typo-safety in money-adjacent config).
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator

from quantcore.schemas import SIZING_LADDER

#: Rail #3 bounds: the sizing ladder {0, ±0.05 .. ±0.20} is IMMUTABLE.
#: No profile or override may imply off-ladder targets; widening the ladder
#: requires an ADR, never a config knob (R1-07).
_LADDER_MAX = max(SIZING_LADDER)  # 0.20
_LADDER_STEP = 0.05


class RiskConfig(BaseModel):
    """Per ADR-0004 + ADR-0009 §P0-5. Frozen; profiles are constructors."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_position_pct: float = 0.20
    action_step: float = 0.05
    cost_multiple: float = 2.0
    max_drawdown_pct: float = 0.15
    max_daily_loss_pct: float = 0.05
    min_trade_size: float = 0.02
    quarter_kelly: float = 0.25
    cooldown_after_loss_minutes: int = 60
    event_risk_enabled: bool = False
    event_risk_window_days: float = 1.0
    min_distinct_analysts: int = 2
    paper_zero_costs: bool = False
    # Portfolio caps (ADR-0087). Default ON: these are risk-TIGHTENING, so the
    # default-OFF discipline (which applies to risk-loosening/behavioral
    # features) does not apply here.
    max_gross_exposure_pct: float = 0.40
    max_concurrent_positions: int = 4
    # Deterministic proposal expiry (R1-05): pending proposals older than this
    # are stale — approval is refused and `expire` sweeps them into the ledger.
    proposal_ttl_hours: float = 24.0

    @model_validator(mode="after")
    def _enforce_sizing_ladder_rail(self) -> RiskConfig:
        """Rail #3 enforcement at the config boundary (R1-07): a RiskConfig
        that could make the gate emit off-ladder targets must not exist."""
        if self.max_position_pct > _LADDER_MAX + 1e-9:
            raise ValueError(
                f"max_position_pct={self.max_position_pct} exceeds the immutable "
                f"sizing-ladder rail (rail #3: ladder {SIZING_LADDER}, max "
                f"{_LADDER_MAX}); widening the ladder requires an ADR, not config"
            )
        if abs(self.action_step - _LADDER_STEP) > 1e-9:
            raise ValueError(
                f"action_step={self.action_step} violates the immutable sizing "
                f"ladder (rail #3: step is {_LADDER_STEP}); changing the ladder "
                "requires an ADR, not config"
            )
        return self

    @classmethod
    def conservative(cls) -> RiskConfig:
        return cls(
            max_position_pct=0.10,
            action_step=0.05,
            cost_multiple=3.0,
            max_drawdown_pct=0.10,
            max_daily_loss_pct=0.03,
            max_gross_exposure_pct=0.25,
            max_concurrent_positions=3,
        )

    @classmethod
    def moderate(cls) -> RiskConfig:
        return cls()

    @classmethod
    def aggressive(cls) -> RiskConfig:
        # NOTE (R1-07): "aggressive" loosens breakers/costs, NOT the ladder.
        # max_position_pct/action_step stay on the rail-#3 ladder bounds —
        # 0.40/0.10 previously emitted off-ladder targets that crashed
        # Proposal validation.
        return cls(
            max_position_pct=0.20,
            action_step=0.05,
            cost_multiple=1.5,
            max_drawdown_pct=0.20,
            max_daily_loss_pct=0.10,
            max_gross_exposure_pct=0.60,
            max_concurrent_positions=6,
        )


PROFILES = {
    "conservative": RiskConfig.conservative,
    "moderate": RiskConfig.moderate,
    "aggressive": RiskConfig.aggressive,
}


class StateConfig(BaseModel):
    """Top-level quant-state/config.json."""

    model_config = ConfigDict(extra="forbid")

    profile: str = "conservative"
    risk_overrides: dict = {}
    watchlist: list[str] = []
    paper_nav: float = 100_000.0

    def risk_config(self) -> RiskConfig:
        base = PROFILES.get(self.profile, RiskConfig.conservative)()
        if self.risk_overrides:
            return RiskConfig(**{**base.model_dump(), **self.risk_overrides})
        return base


def load_state_config(state_dir: Path) -> StateConfig:
    path = state_dir / "config.json"
    if not path.exists():
        return StateConfig()
    return StateConfig(**json.loads(path.read_text()))
