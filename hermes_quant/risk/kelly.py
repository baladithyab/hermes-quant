"""Back-compat shim — kelly.py moved to hermes_quant.pdr_core.kelly (ADR-0092).

The Kelly-fraction sizing math is provably host-agnostic (imports only
``__future__`` + ``math``), so it was moved VERBATIM into the host-agnostic
``pdr_core`` during the gate-port extraction. This module re-exports the moved
symbols so every legacy importer (``hermes_quant.risk.gate``, the BMA layer, the
react fill-size invariant, ...) keeps working unchanged.

New code should import from ``hermes_quant.pdr_core.kelly`` directly.
"""

from __future__ import annotations

from hermes_quant.pdr_core.kelly import (
    cost_gate_threshold,
    expected_log_return,
    expected_signed_edge,
    quarter_kelly_size,
    round_to_step,
)

__all__ = [
    "cost_gate_threshold",
    "expected_log_return",
    "expected_signed_edge",
    "quarter_kelly_size",
    "round_to_step",
]
