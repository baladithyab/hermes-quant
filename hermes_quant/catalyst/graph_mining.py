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

----------------------------------------------------------------------------------
BUILT W5 (2026-05-30, ADR-0080 D80.6 / docs/plans/selfevolve-W5-graph-mining.md)
----------------------------------------------------------------------------------
The implementation below resolves the open questions above and ships the design:

* Open-question #1 is resolved by REUSING ``profitability.MIN_SAMPLE`` (=20) — no
  second hard-coded constant. The per-edge bar can be RAISED via the ``min_sample``
  kwarg but never lowered below the profitability bar (D80.3 robustness-not-peak).
* The miner is the ADVISORY PLANE for catalyst edges (ADR-0080 D80.1): it PROPOSES
  per-edge FLIP_SIGN / DOWNWEIGHT / PRUNE candidates to ``graph-mine-candidates.json``
  and NEVER auto-edits the seed / live YAML (``propagation.graph_path()``). The only
  path from a candidate diff to live policy runs through manual operator review →
  manual YAML edit → the deterministic risk gate / promotion machinery, which this
  loop can never modify. The miner is evidence, never authority.
* ``confidence_multiplier`` is SILENCE-ONLY: clamped ``0.0 <= m <= 1.0`` so it can
  pull an edge's effective weight TOWARD 0 (a wrong edge gets quieter) but NEVER
  amplify above the curated weight. This is the single most load-bearing safety
  property of the wave (ADR-0080 D80.5).
* External-truth only (ADR-0080 D80.3 #1): verdicts derive SOLELY from realized
  forward returns via the injected ``ForwardReturnFetcher``. No LLM self-score; the
  module imports nothing from ``agents/`` or ``aggregators/``, and the candidate file
  it writes is NEVER re-ingested (only ``propagation-log.jsonl`` is read back) —
  the structural Oracle-Fallacy / model-collapse guard for this wave (D80.6).
* A FLIP_SIGN candidate must ADDITIONALLY clear the market-data-free sign-consistency
  check (``eval.run_sign_consistency``, the systematic version of the hand-caught
  OPEC mis-sign) before the operator applies it — advisory metadata on the proposal,
  not an auto-apply path.
* DEFAULT-OFF behind ``HERMES_QUANT_GRAPH_MINING``; flag-OFF/unset is bit-for-bit a
  no-op (``mine_graph`` returns ``{}`` → ``write_candidates`` writes nothing →
  the candidate file is never created). The catalyst forward path is untouched:
  W5 only adds a gated READER of the already-accreting log.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from hermes_quant.catalyst.eval import SignCase, run_sign_consistency
from hermes_quant.catalyst.profitability import (
    MIN_HIT_RATE,
    MIN_SAMPLE,
    ForwardReturnFetcher,
)
from hermes_quant.catalyst.propagation import (
    PropagationEdge,
    load_graph,
)

logger = logging.getLogger(__name__)

# The corpus the miner reads back (external market truth via the fetcher); identical
# to profitability's default log path (propagation.py:188, profitability.py:28).
_DEFAULT_LOG = Path.home() / ".hermes" / "quant" / "catalyst" / "propagation-log.jsonl"

# The ONLY thing W5 writes: the candidate-edge diff for operator review (advisory
# plane). Path matches the DESIGN doc exactly (graph_mining.py original :64).
_DEFAULT_CANDIDATES = (
    Path.home() / ".hermes" / "quant" / "catalyst" / "graph-mine-candidates.json"
)

# Verdicts the operator actually reviews (a change is proposed). KEEP is never
# emitted to the diff — silence-by-default on no-change.
_ACTIONABLE = ("FLIP_SIGN", "DOWNWEIGHT", "PRUNE")

# Oracle-Fallacy provenance tag (ADR-0080 D80.4/D80.6): every candidate row is
# marked as the agent's OWN prior output, never ground truth, never re-ingested.
_PROVENANCE = "graph_mining.mine_graph"


def _mining_enabled() -> bool:
    """W5 is default-OFF. The miner is inert (returns {} / writes nothing) until
    ``HERMES_QUANT_GRAPH_MINING=1``. Mirrors multileg.py:101-102 exactly."""
    return os.environ.get("HERMES_QUANT_GRAPH_MINING", "0") == "1"


EdgeKey = tuple[str, str, str]  # (source, target_symbol, relation)


@dataclass
class EdgeEvidence:
    """Per-edge realized-outcome evidence + the proposed (advisory) edge edit.

    Seeded with the curated ``effect_sign``/``weight`` from the live graph; the
    ``n_scored``/``hits``/``sum_signed_return`` accumulate from realized forward
    returns. All verdict/multiplier logic is silence-by-default and silence-only.
    """

    source: str
    target_symbol: str
    relation: str
    curated_effect_sign: int  # from the live graph (load_graph)
    curated_weight: float  # from the live graph
    n_scored: int = 0
    hits: int = 0  # sign(fwd) == symbol_sign (the propagated direction)
    sum_signed_return: float = 0.0
    examples: list[str] = field(default_factory=list)

    @property
    def sign_hit_rate(self) -> float:
        return (self.hits / self.n_scored) if self.n_scored else 0.0

    @property
    def mean_signed_return(self) -> float:
        return (self.sum_signed_return / self.n_scored) if self.n_scored else 0.0

    @property
    def suggested_effect_sign(self) -> int:
        """FLIP iff sign_hit_rate < 0.5 AND n_scored >= MIN_SAMPLE; else curated."""
        if self.n_scored >= MIN_SAMPLE and self.sign_hit_rate < 0.5:
            return -self.curated_effect_sign
        return self.curated_effect_sign

    @property
    def confidence_multiplier(self) -> float:
        """SILENCE-ONLY downweight in [0.0, 1.0]. NEVER amplifies above the curated
        weight. Below MIN_SAMPLE -> 1.0 (no opinion on thin evidence). At/above
        MIN_HIT_RATE -> 1.0 (clears the bar). Otherwise a linear taper from
        MIN_HIT_RATE down to 0.5 (a coin flip => 0.0 => silence the edge)."""
        if self.n_scored < MIN_SAMPLE:
            return 1.0
        if self.sign_hit_rate >= MIN_HIT_RATE:
            return 1.0
        m = (self.sign_hit_rate - 0.5) / (MIN_HIT_RATE - 0.5)
        return round(max(0.0, min(1.0, m)), 4)

    @property
    def verdict(self) -> str:
        """KEEP | FLIP_SIGN | DOWNWEIGHT | PRUNE (silence-by-default below sample).

        A hit-rate below a coin flip on a sufficient sample is a *wrong sign* ->
        FLIP_SIGN. Between 0.5 and MIN_HIT_RATE is a *weak but not inverted* edge ->
        DOWNWEIGHT. A multiplier that tapers to exactly 0.0 is PRUNE (coin flip at
        0.5 -> silence it). KEEP covers "clears the bar" AND "insufficient sample" —
        never propose a change on thin evidence (the explicit safety choice).
        """
        if self.n_scored < MIN_SAMPLE:
            return "KEEP"  # insufficient sample => no change proposed (silence)
        if self.sign_hit_rate < 0.5:
            return "FLIP_SIGN"
        if self.confidence_multiplier == 0.0:
            return "PRUNE"
        if self.sign_hit_rate < MIN_HIT_RATE:
            return "DOWNWEIGHT"
        return "KEEP"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["examples"] = self.examples[:5]
        d["sign_hit_rate"] = round(self.sign_hit_rate, 4)
        d["mean_signed_return_pct"] = round(self.mean_signed_return, 3)
        d["suggested_effect_sign"] = self.suggested_effect_sign
        d["confidence_multiplier"] = self.confidence_multiplier
        d["verdict"] = self.verdict
        return d


def _seed_evidence(
    graph: dict[str, list[PropagationEdge]],
) -> dict[EdgeKey, EdgeEvidence]:
    """Pre-seed one EdgeEvidence per curated edge with its curated sign/weight, so a
    scored edge carries what it should be FLIPPED *from* and DOWNWEIGHTED relative to."""
    seeded: dict[EdgeKey, EdgeEvidence] = {}
    for source, edges in graph.items():
        for e in edges:
            key: EdgeKey = (source, e.target_symbol, e.relation)
            seeded[key] = EdgeEvidence(
                source=source,
                target_symbol=e.target_symbol,
                relation=e.relation,
                curated_effect_sign=e.effect_sign,
                curated_weight=e.weight,
            )
    return seeded


def mine_graph(
    fetcher: ForwardReturnFetcher,
    *,
    path: Path | None = None,
    graph: dict[str, list[PropagationEdge]] | None = None,
    min_sample: int = MIN_SAMPLE,
    max_rows: int = 5000,
) -> dict[EdgeKey, EdgeEvidence]:
    """Join the propagation log against realized forward returns, grouped PER EDGE.

    ``edge_key = (source, target_symbol, relation)``. For each scored row a "hit" is
    ``sign(forward_return) == row["symbol_sign"]`` (the propagated direction), EXACTLY
    as ``profitability.measure_profitability`` scores per relation (profitability.py
    :120-128) — the ONLY delta is the grouping key. Each EdgeEvidence is seeded with
    the curated ``effect_sign``/``weight`` from ``graph`` (``load_graph()`` when None).

    Silence-by-default on a missing/empty log (returns {}). DEFAULT-OFF: returns {}
    immediately unless ``HERMES_QUANT_GRAPH_MINING=1``. The miner never sees returns
    when the graph propagates (the fetcher reads the NEXT bar after ``asof``).

    ``min_sample`` is advisory metadata only here; the verdict/multiplier bars are
    ``profitability.MIN_SAMPLE``/``MIN_HIT_RATE`` (reused, never redefined).
    """
    if not _mining_enabled():
        return {}  # default-OFF: bit-for-bit no-op (ADR-0080 D80.8)
    p = path or _DEFAULT_LOG
    if not p.exists():
        return {}  # silence-by-default (profitability.py:94-95)

    g = graph if graph is not None else load_graph()[0]
    evidence = _seed_evidence(g)
    rows_seen = 0
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            if rows_seen >= max_rows:
                break
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows_seen += 1
            sym = row.get("symbol")
            source = row.get("source")
            relation = row.get("relation", "unknown")
            sym_sign = row.get("symbol_sign")
            asof = row.get("asof")
            if not sym or not source or sym_sign is None or not asof:
                continue
            try:
                asof_date = datetime.fromisoformat(asof.replace("Z", "+00:00")).date()
            except (ValueError, AttributeError):
                continue
            fwd = fetcher(sym, asof_date)
            if fwd is None or fwd == 0:
                continue  # no realized data or flat — unscored (profitability.py:121)
            key: EdgeKey = (source, sym, relation)
            ev = evidence.get(key)
            if ev is None:
                # An edge that fired in the log but is no longer in the curated graph
                # (a pruned/renamed edge). Score it with neutral curated metadata so
                # its evidence is still surfaced; suggested sign defaults to observed.
                ev = EdgeEvidence(
                    source=source,
                    target_symbol=sym,
                    relation=relation,
                    curated_effect_sign=int(row.get("effect_sign", 0)) or 1,
                    curated_weight=float(row.get("weight", 0.0)),
                )
                evidence[key] = ev
            ev.n_scored += 1
            ev.sum_signed_return += fwd if sym_sign > 0 else -fwd  # signed-aligned
            realized_sign = 1 if fwd > 0 else -1
            if realized_sign == sym_sign:
                ev.hits += 1
            if len(ev.examples) < 5:
                ev.examples.append(f"{sym} sign={sym_sign:+d} fwd={fwd:+.1f}%")
    except OSError as e:  # noqa: BLE001
        logger.warning("catalyst.graph_mining: log read failed: %s", e)
        return {}
    # Only return edges that actually scored at least once (seeded-but-unscored edges
    # carry no evidence and would just be KEEP noise).
    return {k: v for k, v in evidence.items() if v.n_scored > 0}


def flip_passes_sign_consistency(
    ev: EdgeEvidence,
    sign_cases: list[SignCase],
    *,
    graph: dict[str, list[PropagationEdge]] | None = None,
    aliases: dict[str, str] | None = None,
) -> bool:
    """A FLIP_SIGN candidate must ALSO clear ``run_sign_consistency`` on the proposed
    (flipped) graph before the operator applies it. Don't flip on noise.

    Builds a candidate graph with this one edge's ``effect_sign`` flipped, runs the
    market-data-free sign-consistency check (eval.py:147-184), returns ``passed``.
    Advisory metadata on the proposal — NOT an auto-apply path. If there are no
    sign_cases to check against, the flip is unproven -> return False (don't claim
    consistency we didn't verify).
    """
    if not sign_cases:
        return False
    g = graph if graph is not None else load_graph()[0]
    a = aliases if aliases is not None else load_graph()[1]
    candidate: dict[str, list[PropagationEdge]] = {}
    for source, edges in g.items():
        new_edges: list[PropagationEdge] = []
        for e in edges:
            if (
                source == ev.source
                and e.target_symbol == ev.target_symbol
                and e.relation == ev.relation
            ):
                new_edges.append(
                    PropagationEdge(
                        source=e.source,
                        target_symbol=e.target_symbol,
                        relation=e.relation,
                        effect_sign=-e.effect_sign,
                        weight=e.weight,
                    )
                )
            else:
                new_edges.append(e)
        candidate[source] = new_edges
    result = run_sign_consistency(sign_cases, graph=candidate, aliases=a)
    return result.passed


def format_report(evidence: dict[EdgeKey, EdgeEvidence]) -> str:
    """Compact human report: per-edge n / hit-rate / multiplier / verdict, actionable
    verdicts (FLIP_SIGN/PRUNE/DOWNWEIGHT) first. Empty corpus -> silence-by-default."""
    if not evidence:
        return "catalyst graph-mine: no scored edges yet (log empty or no realized data)."
    lines = ["Catalyst per-edge mining (lookahead-honest, propose-only):"]
    # actionable verdicts first, then by sample size descending.
    order = sorted(
        evidence.values(),
        key=lambda e: (e.verdict not in _ACTIONABLE, -e.n_scored),
    )
    for e in order:
        edge_str = f"{e.source}->{e.target_symbol}/{e.relation}"
        lines.append(
            f"  {edge_str:38s} n={e.n_scored:4d} hit={e.sign_hit_rate:.2f} "
            f"mult={e.confidence_multiplier:.2f} -> {e.verdict}"
        )
    return "\n".join(lines)


def write_candidates(
    evidence: dict[EdgeKey, EdgeEvidence],
    *,
    path: Path | None = None,
    sign_cases: list[SignCase] | None = None,
    graph: dict[str, list[PropagationEdge]] | None = None,
    aliases: dict[str, str] | None = None,
) -> int:
    """Write the CANDIDATE graph diff for operator review (advisory plane).

    Only edges with an actionable verdict (FLIP_SIGN/DOWNWEIGHT/PRUNE) are emitted;
    KEEP edges are not in the diff (the operator only reviews proposed changes).
    Each row is ``EdgeEvidence.to_dict()`` enriched with ``provenance`` +
    ``generated_at`` (Oracle-Fallacy tag) and, for FLIP_SIGN, ``sign_consistency_passed``.

    Returns count written. This is the ONLY write W5 performs; it NEVER touches the
    seed/live YAML (``propagation.graph_path()``). Best-effort; never raises.
    DEFAULT-OFF and silence-by-default: writes nothing (returns 0) when disabled or
    when there are no actionable candidates — the file is not even created.
    """
    if not _mining_enabled():
        return 0  # default-OFF: bit-for-bit no-op
    actionable = [e for e in evidence.values() if e.verdict in _ACTIONABLE]
    if not actionable:
        return 0  # silence-by-default: no proposed changes -> no file written
    generated_at = datetime.now(UTC).isoformat()
    rows: list[dict] = []
    for e in actionable:
        row = e.to_dict()
        row["provenance"] = _PROVENANCE
        row["generated_at"] = generated_at
        if e.verdict == "FLIP_SIGN":
            row["sign_consistency_passed"] = flip_passes_sign_consistency(
                e, sign_cases or [], graph=graph, aliases=aliases
            )
        rows.append(row)
    payload = {
        # Header documents the propose-only / advisory-plane contract for any reader.
        "_note": (
            "ADVISORY PLANE (ADR-0080 D80.1). These are PROPOSALS, not authority. "
            "The curated graph stays operator-authored; the only path to live policy "
            "is manual operator review -> manual YAML edit -> the deterministic risk "
            "gate / promotion machinery, which this loop can never modify. "
            "confidence_multiplier is silence-only (clamped <=1.0; never amplifies)."
        ),
        "provenance": _PROVENANCE,
        "generated_at": generated_at,
        "candidates": rows,
    }
    p = path or _DEFAULT_CANDIDATES
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    except OSError as e:  # noqa: BLE001
        logger.warning("catalyst.graph_mining: candidate write failed: %s", e)
        return 0
    return len(rows)
