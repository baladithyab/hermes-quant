"""hermes_quant.perception — the typed PERCEPTION boundary (ADR-0079 PDR-1).

This package makes the perception layer first-class. ``PerceptionFrame`` is the
single typed container for "everything perceived about ONE symbol at ONE asof";
``frame_to_context`` projects it (purely) into the existing ``MarketContext``;
``build_perception_frame`` is the ONE loader the three live decision paths call.

Per ADR-0079 §D79.2 the frame is a CONTAINER, never an authority — the analyst
Protocol (ADR-0002), BMA (ADR-0003), and the deterministic gate (ADR-0004) are
unchanged. With ``recommend(perception_frame=None)`` (the default), behavior is
byte-identical to today (PDR-1 ships no behavior flag — None = today).
"""

from __future__ import annotations

from hermes_quant.perception.adapter import frame_to_context
from hermes_quant.perception.builder import (
    build_perception_frame,
    build_perception_frame_live,
)
from hermes_quant.perception.frame import PerceptionFrame

__all__ = [
    "PerceptionFrame",
    "build_perception_frame",
    "build_perception_frame_live",
    "frame_to_context",
]
