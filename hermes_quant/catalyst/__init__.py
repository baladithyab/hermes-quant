"""hermes_quant.catalyst — Catalyst Sense (ADR-0074).

A semantic-perception subsystem that runs PARALLEL to the numerical universe
scan: ingest free news feeds → classify catalyst severity → correlate entities
to symbols via a propagation graph (the "butterfly engine") → synthesize into
SemanticPackets the existing HermesSemanticAnalyst consumes → fuse into the BMA
aggregator as a PEER analyst view (never an override).

Design constraints (from spike 001-003 caveats, ADR-0074):
  * Packet ``asof`` = the headline's PUBLICATION time, never wall-clock-now.
    This is the one rule that keeps backtests honest; the existing lookahead
    gate (hermes_quant.semantic.validate_semantic_packet) enforces the rest.
  * Propagation-graph score → packet ``confidence`` (how sure the symbol is
    touched). Headline severity → packet ``magnitude`` (how big the move).
    Do NOT conflate them.
  * Edge SIGN is the highest-risk modeling choice — curated v1, surfaced for
    review, every propagation logged so a learned graph can replace it later.
  * Default ON (HERMES_QUANT_SEMANTIC_ENABLED, FLAGS.md Tier A; set =0 to opt
    out) — the negative-control eval (hermes_quant.catalyst.eval) has cleared
    and it ran weeks live in .env=1. Abstains when no packet is present, so a
    butterfly engine that cries wolf never fires on empty data.

This subsystem is purely ADDITIVE: it writes packets that the advisor loads via
the existing ``market_extras`` param. No change to the core advisor/gate.
"""

from __future__ import annotations

CATALYST_SCHEMA_VERSION = 1
