"""hermes_quant.react — React adapters (ADR-0015 §D5).

The React layer is the third stage of the PDR pipeline. It executes
approved proposals by writing to an executions bus that the daemon's
calibrator+settlement loop consumes (matching the existing daemon-side
shape).

v0.1.2 ships ONE concrete reactor:
- PaperReactor — appends to ~/.hermes/quant/executions.jsonl with
  fill_price=decision_price (no slippage simulation in v0.1.2).

v0.2 adds:
- AlpacaReactor — live US equity broker (gated behind --live opt-in)
- CcxtReactor — live crypto exchanges (gated behind --live opt-in)
- ManualReactor — emits "paste this into your broker" instructions
  for operators who want the system as a co-pilot but not actuator

Per ADR-0015 §D5 the Protocol contract is intentionally minimal so v0.2
adapters drop in without touching the proposals/store/tools layer.
"""

from __future__ import annotations

from .base import ExecutionRecord, Reactor
from .multileg import MultiLegPaperReactor
from .paper import FillSizeInvariantError, PaperReactor

# ADR-0029 B01 go-live: MultiLegPaperReactor is exported in THIS wave so the
# quant_approve dispatch (react/dispatch.py) can import it from the package. It
# remains DEFAULT-OFF (HERMES_QUANT_MULTILEG_REACTOR set NOWHERE) — un-fired unless
# the operator deliberately flips the flag after the ADR-0029 D7 evidence window.
__all__ = [
    "ExecutionRecord",
    "Reactor",
    "PaperReactor",
    "FillSizeInvariantError",
    "MultiLegPaperReactor",
]
