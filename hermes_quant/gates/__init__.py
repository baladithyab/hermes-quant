"""hermes_quant.gates — Decision gates for autonomous mode.

Per ADR-0016. The silence-bias gate is the autonomous-mode-specific
"is this signal worth even considering" filter, applied AFTER the
advisor's risk gate is evaluated but BEFORE the React adapter fires.

The principle is the founding charter's "rewarded for correct inaction"
invariant — autonomous mode must default to silence and only fire when
all four dimensions cross their thresholds.
"""
from __future__ import annotations

from .silence_bias import (
    GateConfig,
    GateDecision,
    GateResult,
    SilenceBiasGate,
    silence_bias_gate,
)

__all__ = [
    "GateConfig",
    "GateDecision",
    "GateResult",
    "SilenceBiasGate",
    "silence_bias_gate",
]
