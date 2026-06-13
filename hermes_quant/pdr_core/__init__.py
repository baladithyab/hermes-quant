"""hermes_quant.pdr_core — the host-agnostic Perception -> Decision -> Reaction core (ADR-0092).

This package owns the money-state, the arithmetic, the deterministic gate, and the
frozen contract TRIAD. It is the part of hermes-quant that is destined to become a
standalone ``pdr-core`` repo shared by multiple host shells (hermes-quant and
cowork-quant). For now it lives inside ``hermes_quant`` so the eventual extraction is
a mechanical ``git mv`` — which is only possible if the import discipline below holds.

Architecture (ADR-0092):
  - A host SHELL (hermes-quant, cowork-quant) is responsible for perception: it produces
    an :class:`~hermes_quant.pdr_core.contracts.AnalystView` (the host-blind, modality-blind
    seam) from whatever data/LLM/numerical pipeline it runs.
  - The CORE consumes AnalystViews, applies the deterministic risk gate + discrete sizer,
    and returns an authorized :class:`~hermes_quant.pdr_core.contracts.Proposal`.
  - The host SHELL executes the Proposal against a broker and feeds the resulting
    :class:`~hermes_quant.pdr_core.contracts.Fill` back to the core for settlement /
    money-state update (Option E absolute-target accounting; see
    ``hermes_quant/state/fill_delta_normalizer.py`` for the reference normalizer).

PURITY CONTRACT (the host-agnostic invariant, enforced by
``tests/pdr_core/test_contract_purity.py``):
  - The core must NEVER import a host or infra module — no daemon, no react backends,
    no MCP, no advisor, no tools, no data providers, no broker SDKs (alpaca/ccxt/yfinance),
    no discord, no torch/sklearn.
  - It may depend only on the standard library and lightweight pure-data deps. A host
    shell depends on the core; the core never depends on a shell.

This Increment-1 module is ADDITIVE and contract-only: nothing in the existing code
paths imports ``pdr_core`` yet. It is the contract foundation for the extraction.
"""

from __future__ import annotations

from hermes_quant.pdr_core.contracts import (
    POSITION_LADDER,
    AnalystView,
    Fill,
    Proposal,
)

__all__ = [
    "POSITION_LADDER",
    "AnalystView",
    "Fill",
    "Proposal",
]
