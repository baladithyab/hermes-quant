"""hermes_quant.eval — STOCKBENCH-style evaluation harness and PromotionGate.

Exports:
    STOCKBENCHHarness   — contamination-safe evaluation against buy-and-hold.
    STOCKBENCHResult    — result dataclass.
    PromotionGate       — decision-support gate for production promotion.
    PromotionDecision   — result of PromotionGate.check().
    ContaminationError  — raised when window_start precedes the cutoff.
"""

from hermes_quant.eval.stockbench import (
    STOCKBENCHHarness,
    STOCKBENCHResult,
    ContaminationError,
)
from hermes_quant.eval.promotion_gate import PromotionGate, PromotionDecision

__all__ = [
    "STOCKBENCHHarness",
    "STOCKBENCHResult",
    "ContaminationError",
    "PromotionGate",
    "PromotionDecision",
]
