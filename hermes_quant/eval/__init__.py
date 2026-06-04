"""hermes_quant.eval — STOCKBENCH-style evaluation harness and PromotionGate.

Exports:
    STOCKBENCHHarness   — contamination-safe evaluation against buy-and-hold.
    STOCKBENCHResult    — result dataclass.
    PromotionGate       — decision-support gate for production promotion.
    PromotionDecision   — result of PromotionGate.check().
    ContaminationError  — raised when window_start precedes the cutoff.

LLM-beats-fallback gate (Gate-3 keystone, lane W2B / ADR-4665 §7.2) — an
OFFLINE, deterministic, advisory-plane gate proving an LLM decision stage beats
its deterministic fallback on realized decision quality over a fixed corpus.
Lives BESIDE PromotionGate (does not modify it):
    RiskCommitteeAxis   — approval-quality axis (committee approval ∈ {0,1}).
    TraderAxis          — proposal-quality axis (trader position ∈ [-1,1]).
    Episode / GateConfig / GateVerdict / CriterionResult — the corpus + verdict
                          model. Flips no flag; produces a verdict a human reads.
"""

from hermes_quant.eval.llm_beats_fallback_gate import (
    CriterionResult,
    Episode,
    GateConfig,
    GateVerdict,
    RiskCommitteeAxis,
    TraderAxis,
)
from hermes_quant.eval.promotion_gate import PromotionDecision, PromotionGate
from hermes_quant.eval.stockbench import (
    ContaminationError,
    STOCKBENCHHarness,
    STOCKBENCHResult,
)

__all__ = [
    "STOCKBENCHHarness",
    "STOCKBENCHResult",
    "ContaminationError",
    "PromotionGate",
    "PromotionDecision",
    "Episode",
    "GateConfig",
    "GateVerdict",
    "CriterionResult",
    "RiskCommitteeAxis",
    "TraderAxis",
]
