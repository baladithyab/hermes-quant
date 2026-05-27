"""hermes_quant.memory — 3-layer persistent memory system (ADR-0042).

Wave 4 of the hermes-quant pipeline uplift.  Three layers, all default OFF
behind env vars so pre-Wave-4 behavior is bit-identical:

  Layer 1  decisions.py   — append-only JSONL decision log
  Layer 2  reflector.py   — deferred post-trade reflection (HERMES_QUANT_REFLECTION=1)
  Layer 3  retriever.py   — BM25 retriever + Oracle-Fallacy guard
                            (HERMES_QUANT_MEMORY_INJECT=1)

Oracle Fallacy guard (arxiv:2605.19337 §4.2):
  Any reflection whose tau_observable >= asof is EXCLUDED from retrieval.
  This is the canonical, non-negotiable regression test for this module.
"""

from __future__ import annotations

from hermes_quant.memory.decisions import (
    AppendOnlyViolation,
    DecisionLog,
)
from hermes_quant.memory.reflector import (
    LessonCategory,
    Reflection,
    Reflector,
)
from hermes_quant.memory.retriever import (
    AggregateStats,
    PastContext,
    ResolvedDecision,
    get_past_context,
)

__all__ = [
    # decisions
    "AppendOnlyViolation",
    "DecisionLog",
    # reflector
    "LessonCategory",
    "Reflection",
    "Reflector",
    # retriever
    "AggregateStats",
    "PastContext",
    "ResolvedDecision",
    "get_past_context",
]
