"""hermes_quant.catalyst.eval — negative-control + precision harness (ADR-0074, D74.7).

The HARD PREREQUISITE before Catalyst Sense influences any live decision: prove
it doesn't cry wolf. A butterfly engine with poor precision is worse than none.

Two checks:

  1. NEGATIVE CONTROL — benign headlines on benign entities produce ZERO packets.
     If neutral market chatter ("Company reports quarterly results in line") fires
     bearish/bullish stances, the classifier/propagator is too trigger-happy.

  2. DIRECTIONAL PRECISION — given a labeled set of (headline, asof, symbol,
     realized_forward_return) cases, the synthesized packets' stances must match
     the realized direction at >= a threshold hit-rate, measured lookahead-honestly
     (forward return from the next tradeable bar AFTER publication).

This module provides the harness; the labeled eval SET is supplied by the caller
(a fixture for tests, a curated historical set for the live-gate decision). The
harness does NOT fetch prices itself — realized returns are passed in, keeping it
deterministic and offline-testable.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes_quant.catalyst.ingest import CatalystItem
from hermes_quant.catalyst.propagation import PropagationEdge
from hermes_quant.catalyst.synthesize import synthesize_packets


@dataclass(frozen=True)
class EvalCase:
    """One labeled eval case."""

    item: CatalystItem
    symbol: str
    realized_forward_return: float  # signed % move from next bar after publication


@dataclass(frozen=True)
class NegControlResult:
    n_benign_items: int
    n_spurious_packets: int
    passed: bool
    spurious: tuple[str, ...]  # symbols spuriously flagged


@dataclass(frozen=True)
class PrecisionResult:
    n_cases: int
    n_scored: int
    hits: int
    hit_rate: float
    passed: bool
    misses: tuple[str, ...]


def run_negative_control(
    benign_items: list[CatalystItem],
    *,
    graph: dict[str, list[PropagationEdge]] | None = None,
    aliases: dict[str, str] | None = None,
) -> NegControlResult:
    """Benign items MUST produce zero packets. Any packet is a false positive."""
    packets = synthesize_packets(benign_items, graph=graph, aliases=aliases)
    spurious = tuple(p.asset for p in packets)
    return NegControlResult(
        n_benign_items=len(benign_items),
        n_spurious_packets=len(packets),
        passed=len(packets) == 0,
        spurious=spurious,
    )


def run_precision(
    cases: list[EvalCase],
    *,
    min_hit_rate: float = 0.6,
    graph: dict[str, list[PropagationEdge]] | None = None,
    aliases: dict[str, str] | None = None,
) -> PrecisionResult:
    """Directional precision: synthesized stance must match realized direction.

    For each case, synthesize packets from its item and check whether any packet
    for the case's symbol predicts the same direction as the realized forward
    return. Scored only when both a packet and a non-zero realized return exist.
    """
    hits = 0
    scored = 0
    misses: list[str] = []
    for case in cases:
        packets = synthesize_packets([case.item], graph=graph, aliases=aliases)
        sym_packets = [p for p in packets if p.asset == case.symbol]
        if not sym_packets:
            continue
        realized_dir = 1 if case.realized_forward_return > 0 else (
            -1 if case.realized_forward_return < 0 else 0)
        if realized_dir == 0:
            continue
        # take the highest-confidence packet for this symbol
        pkt = max(sym_packets, key=lambda p: p.confidence)
        pkt_dir = {"bullish": 1, "bearish": -1, "neutral": 0}[pkt.stance]
        scored += 1
        if pkt_dir == realized_dir:
            hits += 1
        else:
            misses.append(
                f"{case.symbol}: predicted {pkt.stance} "
                f"({pkt_dir:+d}), realized {case.realized_forward_return:+.2f}%"
            )
    hit_rate = (hits / scored) if scored else 0.0
    return PrecisionResult(
        n_cases=len(cases),
        n_scored=scored,
        hits=hits,
        hit_rate=round(hit_rate, 4),
        passed=(scored > 0 and hit_rate >= min_hit_rate),
        misses=tuple(misses),
    )


def eval_gate(
    benign_items: list[CatalystItem],
    precision_cases: list[EvalCase],
    *,
    min_hit_rate: float = 0.6,
    graph: dict[str, list[PropagationEdge]] | None = None,
    aliases: dict[str, str] | None = None,
) -> tuple[bool, NegControlResult, PrecisionResult]:
    """The combined live-gate: BOTH negative-control and precision must pass."""
    neg = run_negative_control(benign_items, graph=graph, aliases=aliases)
    prec = run_precision(precision_cases, min_hit_rate=min_hit_rate, graph=graph, aliases=aliases)
    return (neg.passed and prec.passed), neg, prec
