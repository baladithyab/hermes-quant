"""hermes_quant.reconcile — post-fill reconciliation helpers (ADR-0029).

Pure read/compare helpers that join reactor-written records back to shadow / model
counterfactuals. Writes nothing to executions.jsonl / state.db.
"""

from __future__ import annotations

from .pmcc_shadow import PMCCShadowDivergence, reconcile_pmcc_shadow

__all__ = ["PMCCShadowDivergence", "reconcile_pmcc_shadow"]
