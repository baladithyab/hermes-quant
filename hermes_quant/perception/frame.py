"""hermes_quant.perception.frame — the ``PerceptionFrame`` carrier (ADR-0079 PDR-1).

A single frozen container that *is* "everything perceived about ONE symbol at ONE
asof" — the typed PERCEPTION boundary the system lacked (audit GAP-E). Built ONCE
per symbol by ``build_perception_frame`` so the catalyst flag/wiring decoupling
(audit GAP-D) is structurally impossible to reintroduce.

Field set matches ADR-0079 §D79.2 / design ``pdr-unified-architecture.md`` §4.1
exactly. Add-only versioning (mirrors ``protocol.py:14-16``: fields are added only,
never renamed/removed before a major bump; consumers ignore unknown fields).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class PerceptionFrame:
    """Everything perceived about ONE symbol at ONE asof — the typed PERCEPTION
    boundary. A CONTAINER, never an authority. Built once per symbol so the
    catalyst flag/wiring decoupling (GAP-D) is structurally impossible.

    Add-only versioning (mirrors ``protocol.py:14-16``: fields added only, never
    renamed/removed before a major bump; consumers ignore unknown fields).
    """

    symbol: str
    asof: pd.Timestamp  # BAR-asof, UTC = the replay anchor (== ctx.asof / advisor.py:847)
    bars: pd.DataFrame  # canonical OHLCV, already as_of-filtered + still-forming-dropped
    last_close: float
    regime: Any | None = None  # the ADR-0063 RegimePacket OBJECT (adapter re-expands to 3 keys)
    semantic_packets: tuple[Any, ...] = ()  # finished catalyst/social packet dicts (already validated)
    trend_velocity: Mapping[str, Any] | None = None  # GAP-A (HERMES_QUANT_TREND_VELOCITY) — empty until PDR-2
    convergence: Mapping[str, Any] | None = None  # GAP-B (HERMES_QUANT_CONVERGENCE) — empty until PDR-3
    saturation: Mapping[str, Any] | None = None  # GAP-C (HERMES_QUANT_SATURATION) — empty until PDR-4
    event_risk: Mapping[str, Any] | None = None  # ADR-0084 (HERMES_QUANT_CALENDAR_ENABLED) — None until ON; outcome-free, asof-honest
    provenance: tuple[str, ...] = ()  # evidence_ids / source URLs / fetch run-ids (ADR-0033/0041)
    extras: Mapping[str, Any] = field(default_factory=dict)
    """Forward-compat escape hatch. Carries EXACTLY the non-regime/non-semantic
    keys the advisor builds today: ``decision_asof`` (ISO str), the four
    ``still_forming_*`` keys (when a still-forming bar was dropped),
    ``regime_failure``, and ``regime_classifier_kind``."""


__all__ = ["PerceptionFrame"]
