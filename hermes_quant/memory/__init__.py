"""hermes_quant.memory — 3-layer persistent memory system (ADR-0042).

Wave 4 of the hermes-quant pipeline uplift.  Three layers, env-var gated; each
flag's explicit =0 path stays bit-identical to pre-Wave-4 behavior:

  Layer 1  decisions.py   — append-only JSONL decision log
  Layer 2  reflector.py   — deferred post-trade reflection (HERMES_QUANT_REFLECTION, default ON; set =0 to opt out)
  Layer 3  retriever.py   — BM25 retriever + Oracle-Fallacy guard
                            (HERMES_QUANT_MEMORY_INJECT, default ON; set =0 to opt out)

Oracle Fallacy guard (arxiv:2605.19337 §4.2):
  Any reflection whose tau_observable >= asof is EXCLUDED from retrieval.
  This is the canonical, non-negotiable regression test for this module.
"""

from __future__ import annotations

from hermes_quant.memory.decisions import (
    AppendOnlyViolation,
    DecisionLog,
)
from hermes_quant.memory.decisions_render import (
    render_decision_block,
    render_decisions_md,
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
    "render_decision_block",
    "render_decisions_md",
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
