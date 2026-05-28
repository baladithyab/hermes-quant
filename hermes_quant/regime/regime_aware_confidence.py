"""hermes_quant.regime.regime_aware_confidence — per-analyst regime-conditioned multipliers.

Per ADR-0063 §"Implementation Plan" step 4. Each of the 4 analysts can call
``apply_regime_multiplier`` to adjust its emitted confidence based on the
current regime tier (-1/0/+1) — gated by env var HERMES_QUANT_ANALYSTS_USE_REGIME.

Default is OFF for v0.6.0 ship; observation period before flipping to ON in v0.6.1.

Per ADR-0058 label-stability invariant:
- ClassicalTA, Microstructure, Semantic branch on volatility_tier (stable)
- Kronos branches on label == RegimeState.UNKNOWN (the one safe label carve-out)
"""

from __future__ import annotations

import os
from typing import Optional

from hermes_quant.regime.detector import RegimeState
from hermes_quant.regime.extras_builder import RegimePacket

ENV_FLAG = "HERMES_QUANT_ANALYSTS_USE_REGIME"


def _flag_on() -> bool:
    return os.environ.get(ENV_FLAG, "0") in ("1", "true", "True", "yes", "on")


def apply_regime_multiplier(
    confidence: float,
    regime: Optional[RegimePacket],
    analyst_kind: str,
) -> float:
    """Return adjusted confidence based on regime + analyst kind.

    Args:
        confidence: Original analyst confidence in [0, 1].
        regime: RegimePacket | None from ``ctx.extras.get("regime")``.
        analyst_kind: One of "classical_ta", "microstructure", "semantic", "kronos".

    Returns:
        Adjusted confidence (caller is responsible for any further clipping).

    Behavior (per ADR-0063):
    - Flag off OR regime is None: return confidence unchanged
    - ClassicalTA in volatility_tier == +1 (high vol): * 0.7 (trend less reliable)
    - Microstructure in volatility_tier == -1 (low vol): * 1.15 (orderbook more meaningful)
    - Semantic in volatility_tier == +1 (high vol): * 1.20 (news drives more)
    - Kronos when label == UNKNOWN: * 0.85 (transition uncertainty)
    """
    if not _flag_on():
        return confidence
    if regime is None:
        return confidence

    tier = regime.volatility_tier
    label = regime.label

    if analyst_kind == "classical_ta" and tier == 1:
        return confidence * 0.7
    if analyst_kind == "microstructure" and tier == -1:
        return confidence * 1.15
    if analyst_kind == "semantic" and tier == 1:
        return confidence * 1.20
    if analyst_kind == "kronos" and label == RegimeState.UNKNOWN:
        return confidence * 0.85

    return confidence


__all__ = ["apply_regime_multiplier", "ENV_FLAG"]
