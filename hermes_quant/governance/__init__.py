"""hermes_quant.governance — fourth runtime plane (ADR-0031).

Append-only governance artifacts: invariants, kill switch, HITL approvals,
paper→live promotion evaluator, audit log. This package is excluded from
the retro-amendment loop's code_change allowlist (ADR-0026 D5 / ADR-0031 D7).
"""
from __future__ import annotations

from hermes_quant.governance import (  # noqa: F401
    approvals,
    audit_log,
    invariants,
    kill_switch,
    promotion,
    static_scanner,
)
from hermes_quant.governance.invariants import IMMUTABLE_INVARIANTS  # noqa: F401

__all__ = [
    "audit_log",
    "kill_switch",
    "approvals",
    "promotion",
    "invariants",
    "static_scanner",
    "IMMUTABLE_INVARIANTS",
]
