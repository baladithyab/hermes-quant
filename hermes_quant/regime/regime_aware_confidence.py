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
        Adjusted confidence:
        - For "kronos": clipped to ADR-0018 §D8 hard band [0.30, 0.85] when
          a multiplier was applied (i.e. label == UNKNOWN). The Kairos A-shares
          neg-IC failure-mode mitigation is preserved post-multiplier.
        - For all other analysts: clipped to [0, 1] when a multiplier was
          applied. AnalystView.confidence's documented [0, 1] invariant is
          honored even when × 1.15 / × 1.20 push above 1.0.
        - When no multiplier applies (flag off, regime None, or no rule
          matches), confidence is returned unchanged (NO clip — preserves
          analyst's pre-multiplier value as-is).

    Behavior (per ADR-0063):
    - Flag off OR regime is None: return confidence unchanged
    - ClassicalTA in volatility_tier == +1 (high vol): * 0.7 then clip [0,1]
    - Microstructure in volatility_tier == -1 (low vol): * 1.15 then clip [0,1]
    - Semantic in volatility_tier == +1 (high vol): * 1.20 then clip [0,1]
    - Kronos when label == UNKNOWN: * 0.85 then clip [0.30, 0.85] (ADR-0018 §D8)

    Claude review H1+H2 (2026-05-27): clipping is now centralized here so all
    four call sites are uniform; per-analyst clip bounds reflect each analyst's
    documented invariants.
    """
    if not _flag_on():
        return confidence
    if regime is None:
        return confidence

    tier = regime.volatility_tier
    label = regime.label

    adjusted: Optional[float] = None
    clip_lo: float = 0.0
    clip_hi: float = 1.0

    if analyst_kind == "classical_ta" and tier == 1:
        adjusted = confidence * 0.7
    elif analyst_kind == "microstructure" and tier == -1:
        adjusted = confidence * 1.15
    elif analyst_kind == "semantic" and tier == 1:
        adjusted = confidence * 1.20
    elif analyst_kind == "kronos" and label == RegimeState.UNKNOWN:
        adjusted = confidence * 0.85
        # ADR-0018 §D8 hard clip — preserve Kairos overconfidence-guard band
        clip_lo, clip_hi = 0.30, 0.85

    if adjusted is None:
        return confidence

    return max(clip_lo, min(clip_hi, float(adjusted)))


__all__ = ["apply_regime_multiplier", "ENV_FLAG"]
