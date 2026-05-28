"""hermes_quant.observability — silence-by-default observability surfaces.

ADR-0060: fallback probe — synthetic smoke test that intentionally fails the
LLMCaller (timeout, malformed JSON, rate-limit, server error, schema invalid,
empty) to prove all v0.2 LLM-wired surfaces (TraderNodeLLM, RiskCommittee v0.2,
Reflector v0.2, HMM regime classifier) gracefully fall back to deterministic
v0.1 outputs.

The probe is non-destructive — it never makes a real network call.  All
LLMCaller stubs are in-process Python objects that raise / return synthetic
values to drive each fallback branch.
"""

from hermes_quant.observability.fallback_probe import (
    FallbackProbeResult,
    FAILURE_MODES,
    SURFACES,
    run_fallback_probe,
    probe_trader_node,
    probe_risk_committee,
    probe_reflector,
    probe_regime_hmm,
    format_results_human,
    format_results_json,
)

__all__ = [
    "FallbackProbeResult",
    "FAILURE_MODES",
    "SURFACES",
    "run_fallback_probe",
    "probe_trader_node",
    "probe_risk_committee",
    "probe_reflector",
    "probe_regime_hmm",
    "format_results_human",
    "format_results_json",
]
