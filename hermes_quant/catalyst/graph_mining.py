"""hermes_quant.catalyst.graph_mining — B10 learned-graph mining (DESIGN ONLY).

THIS MODULE IS A DESIGN SPECIFICATION, NOT A BUILD. No executable mining logic
ships in Wave C2 (per docs/plans/wave-c2-catalyst.md §C2-5). The build is gated on
accumulated corpus volume and is tracked as B10 remaining-open. This docstring is
the deliverable so the design lives next to the code it will become.

----------------------------------------------------------------------------------
The corpus (exists today)
----------------------------------------------------------------------------------
``hermes_quant.catalyst.propagation.log_propagations`` appends one row per
propagation to ``~/.hermes/quant/catalyst/propagation-log.jsonl``::

    {symbol, source, relation, effect_sign, weight, symbol_sign, catalyst_sign, asof}

``profitability.measure_profitability`` already joins this log against realized
forward returns BY RELATION CLASS — so the join infrastructure is proven. B10 is the
next layer: learn PER-EDGE corrected signs and weights from accumulated outcomes.

----------------------------------------------------------------------------------
Planned interface
----------------------------------------------------------------------------------
    mine_graph(fetcher, *, path=propagation-log.jsonl, min_sample=30, horizon_days=21)
        -> {edge_key: EdgeEvidence}

    edge_key = (source, target_symbol, relation)

    EdgeEvidence:
        n_scored: int
        sign_hit_rate: float          # P(sign(fwd_return) == propagated symbol_sign)
        mean_signed_return: float
        suggested_effect_sign: int    # flip iff sign_hit_rate < 0.5 AND n>=min_sample
        confidence_multiplier: float  # downweight toward 0 for low-hit-rate edges;
                                      # NEVER amplifies above the curated weight (<=1.0)
        verdict: KEEP | FLIP_SIGN | DOWNWEIGHT | PRUNE

The fetcher is the same injected ``ForwardReturnFetcher`` contract as
``profitability.py`` (offline-testable; no network in unit tests).

----------------------------------------------------------------------------------
Honesty rails (non-negotiable — AGENTS.md + ADR-0074)
----------------------------------------------------------------------------------
* Forward return is measured from the NEXT bar after ``asof`` (lookahead-honest);
  the miner never sees returns when the graph propagates.
* The miner PROPOSES edge edits; it NEVER auto-mutates ``propagation_graph.seed.yaml``.
  Output is a report + a CANDIDATE graph diff the operator reviews. This preserves
  "hard rules over learned policy" — the curated graph stays operator-authored; the
  miner is evidence, not authority (mirrors catalyst-as-evidence-never-authority).
* ``confidence_multiplier`` is SILENCE-ONLY: it can pull an edge's weight toward 0
  (a wrong edge gets quieter) but never above its curated weight (no amplification).
* The OPEC-removal lesson (``propagation.py`` energy edge, removed 2026-05-29) is the
  canonical positive case: the sign-consistency eval caught one mis-signed edge by
  hand; the miner is the systematic version. A FLIP_SIGN verdict MUST additionally
  pass the existing market-data-free sign-consistency check before the operator
  applies it (don't flip on noise).

----------------------------------------------------------------------------------
Cron design (when built)
----------------------------------------------------------------------------------
Job ``quant-catalyst-graph-mine-weekly``, ``0 6 * * 6`` (Sat 06:00 PT, after the
profitability cron), ``deliver=origin`` no_agent. Silent unless an edge crosses
``min_sample`` with a FLIP_SIGN/PRUNE verdict (same change-detecting watchdog pattern
as coverage + profitability). Emits a candidate graph diff to
``~/.hermes/quant/catalyst/graph-mine-candidates.json`` for operator review; never
writes the live YAML.

----------------------------------------------------------------------------------
Open questions for the build (not this wave)
----------------------------------------------------------------------------------
1. Minimum corpus volume before any edge is trustworthy — ``min_sample=30`` is a
   starting guess; calibrate against ``MIN_SAMPLE=20`` in profitability.
2. Survivorship / point-in-time: the log is already point-in-time (each row carries
   ``asof``), so the corpus is replayable — but the universe membership at ``asof``
   must be reconstructed for fillability (ties to ADR-0075's ``admitted_via`` log +
   B34/B36).
3. Multi-edge interaction: a symbol hit by two opposing edges — does the miner learn
   per-edge or per-(symbol, event) sign? Start per-edge (matches ``propagate``'s
   noisy-OR × agreement structure).
"""

from __future__ import annotations

# Intentionally no implementation: B10 build is deferred (corpus-volume gated).
# See the module docstring above for the full specification.
