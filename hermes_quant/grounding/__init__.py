"""hermes_quant.grounding — Data Grounding Block, Citation HARD RULE, and ClaimVerifier.

Wave 5 deliverable (ADR-0038 §W5).

Eliminates the LLM-fabricated-price-level / phantom-chart-pattern failure mode
(failure F3 in the SOTA LLM-trading research). Every analyst is forced to cite
injected ground-truth data; the verifier rejects views with un-cited numerical claims.

Public API
----------
    from hermes_quant.grounding import (
        GroundTruthBlock,
        Bar,
        build_ground_truth_block,
        render_for_prompt,
        HARD_RULE_PREAMBLE,
        ClaimVerifier,
        VerificationResult,
        current_clear,
    )
"""

from hermes_quant.grounding.data_grounding import (
    Bar,
    GroundTruthBlock,
    HARD_RULE_PREAMBLE,
    build_ground_truth_block,
    render_for_prompt,
)
from hermes_quant.grounding.verifier import ClaimVerifier, VerificationResult
from hermes_quant.grounding.current_clear import current_clear

__all__ = [
    "Bar",
    "GroundTruthBlock",
    "HARD_RULE_PREAMBLE",
    "build_ground_truth_block",
    "render_for_prompt",
    "ClaimVerifier",
    "VerificationResult",
    "current_clear",
]
