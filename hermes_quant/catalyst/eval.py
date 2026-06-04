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

import math
from dataclasses import dataclass
from numbers import Real
from typing import Any

from hermes_quant.catalyst.ingest import CatalystItem
from hermes_quant.catalyst.onboarding import TAU_CONF, TAU_MAG
from hermes_quant.catalyst.propagation import PropagationEdge
from hermes_quant.catalyst.synthesize import synthesize_packets


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, Real) and math.isfinite(float(value))


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
    velocity_by_symbol: dict[str, dict] | None = None,  # PDR-2: pass-through to synthesize_packets
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
        packets = synthesize_packets(
            [case.item], graph=graph, aliases=aliases,
            velocity_by_symbol=velocity_by_symbol,
        )
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


@dataclass(frozen=True)
class SignCase:
    """One edge-sign expectation: a catalyst of ``polarity`` on ``source_text``
    must propagate ``expected_stance`` to ``symbol``. Market-data-free — this
    validates the GRAPH'S SIGN (the highest-risk modeling field) deterministically,
    so every sector can be guarded even when real forward-return labels aren't
    available for a precision case.
    """

    source_text: str  # a headline that names the source entity + a catalyst word
    symbol: str
    polarity: str  # "negative" | "positive" — the catalyst polarity in source_text
    expected_stance: str  # "bullish" | "bearish"


@dataclass(frozen=True)
class SignConsistencyResult:
    n_cases: int
    n_correct: int
    passed: bool
    mismatches: tuple[str, ...]
    unvalidated_sectors: tuple[str, ...]


def run_sign_consistency(
    cases: list[SignCase],
    *,
    graph: dict[str, list[PropagationEdge]] | None = None,
    aliases: dict[str, str] | None = None,
) -> SignConsistencyResult:
    """Deterministic edge-sign check across sectors (no market data needed).

    For each case, synthesize packets from the headline and confirm the packet
    for ``symbol`` carries ``expected_stance``. A wrong sign here is the
    catastrophic failure the ADR warns about (a sector-contagion edge that
    propagates bullish on a disaster). Unlike precision, this needs no realized
    returns — it asserts the graph encodes the defensible short-horizon reading.
    A case that produces NO packet for the symbol is a mismatch (the edge or the
    classifier word is missing — the silent-miss class found live 2026-05-29).
    """
    correct = 0
    mismatches: list[str] = []
    for c in cases:
        packets = synthesize_packets([_sign_item(c.source_text)], graph=graph, aliases=aliases)
        sym_packets = [p for p in packets if p.asset == c.symbol]
        if not sym_packets:
            mismatches.append(f"{c.symbol}: no packet from {c.source_text!r} (missing edge or catalyst word)")
            continue
        pkt = max(sym_packets, key=lambda p: p.confidence)
        if pkt.stance == c.expected_stance:
            correct += 1
        else:
            mismatches.append(
                f"{c.symbol}: got {pkt.stance}, expected {c.expected_stance} from {c.source_text!r}"
            )
    return SignConsistencyResult(
        n_cases=len(cases),
        n_correct=correct,
        passed=(len(cases) > 0 and correct == len(cases)),
        mismatches=tuple(mismatches),
        unvalidated_sectors=(),
    )


def _sign_item(text: str) -> CatalystItem:
    from datetime import UTC, datetime

    return CatalystItem(
        title=text,
        published_at=datetime.now(UTC),
        source="sign-eval",
        link="n/a",
    )


def run_precision_with_convergence(
    case_item_sets: list[tuple[EvalCase, list[CatalystItem]]],
    *,
    min_hit_rate: float = 0.65,
    require_convergence: bool = True,
    graph: dict[str, list[PropagationEdge]] | None = None,
    aliases: dict[str, str] | None = None,
) -> PrecisionResult:
    """Directional precision with the PDR-3 cross-SOURCE require_ensemble ON.

    The sibling of :func:`run_precision` for the convergence eval (ADR-0079 PDR-3,
    plan §4). ``run_precision`` synthesizes ONE packet per case *item* and cannot
    exercise multi-source convergence (each case has one item). This runner takes
    ``(EvalCase, item SET)`` pairs and synthesizes from the FULL set, so the
    ``ConvergenceValidator`` can VALIDATE the trend across families:

      * validated (>=2 independent source families) cases SURVIVE and are scored
        for directional correctness vs the case's realized forward return.
      * un-validated single-source cases are DROPPED at emission (no packet) and
        are therefore NOT scored — the higher bar (default 0.65, the ADR-0079
        Rollout PDR-3 promise, vs the 0.60 D74.7 floor) is cleared by the
        surviving validated set, not by counting the dropped cases as misses.

    Runs with ``HERMES_QUANT_CONVERGENCE`` ON for the duration (read at call time
    inside ``synthesize_packets``); the prior value is restored on exit so the
    runner is side-effect-free. External truth (realized returns), never
    self-graded. Complementary to BMA cross-ANALYST require_ensemble, never a
    replacement (a validated packet must still find a corroborator in BMA).
    """
    import os

    prev = os.environ.get("HERMES_QUANT_CONVERGENCE")
    if require_convergence:
        os.environ["HERMES_QUANT_CONVERGENCE"] = "1"
    try:
        hits = 0
        scored = 0
        misses: list[str] = []
        for case, items in case_item_sets:
            packets = synthesize_packets(items, graph=graph, aliases=aliases)
            sym_packets = [p for p in packets if p.asset == case.symbol]
            if not sym_packets:
                # dropped by convergence (single-source) or produced no packet:
                # not scored (the require_ensemble default is abstain, not a miss).
                continue
            realized_dir = 1 if case.realized_forward_return > 0 else (
                -1 if case.realized_forward_return < 0 else 0)
            if realized_dir == 0:
                continue
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
    finally:
        if prev is None:
            os.environ.pop("HERMES_QUANT_CONVERGENCE", None)
        else:
            os.environ["HERMES_QUANT_CONVERGENCE"] = prev

    hit_rate = (hits / scored) if scored else 0.0
    return PrecisionResult(
        n_cases=len(case_item_sets),
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
    sign_cases: list[SignCase] | None = None,
    graph: dict[str, list[PropagationEdge]] | None = None,
    aliases: dict[str, str] | None = None,
) -> tuple[bool, NegControlResult, PrecisionResult, SignConsistencyResult]:
    """The combined live-gate: negative-control + precision + edge-sign consistency.

    Three axes (all must pass when supplied):
      * negative control — benign headlines produce ZERO packets.
      * directional precision — synthesized stance matches REAL forward returns
        (needs labeled return data; typically covers a subset of sectors).
      * edge-sign consistency — every sector's curated edge propagates the
        DEFENSIBLE stance under a known catalyst polarity (deterministic, no
        market data; covers the sectors precision can't reach).

    ``sign_cases`` is optional for backwards compatibility, but SHOULD be supplied
    so the gate covers every sector the graph reaches — precision alone validating
    one sector is "encouraging, not proof" for an N-sector graph.
    """
    neg = run_negative_control(benign_items, graph=graph, aliases=aliases)
    prec = run_precision(precision_cases, min_hit_rate=min_hit_rate, graph=graph, aliases=aliases)
    sign = run_sign_consistency(sign_cases or [], graph=graph, aliases=aliases)
    sign_ok = sign.passed if sign_cases else True
    return (neg.passed and prec.passed and sign_ok), neg, prec, sign


# ---------------------------------------------------------------------------
# ADR-0075 admission-precision axis (B05 CODE half) — the eval GATE for
# HERMES_QUANT_CATALYST_ONBOARDING.
#
# `run_precision` (above) asks "did the packet's stance match the realized
# move?" over ALL cases. But only ADMITTED out-of-universe names get traded, so
# the gate-relevant question is precision CONDITIONAL ON ADMISSION: of the names
# `catalyst_admissions` would actually admit (fresh, conf>=TAU_CONF, mag>=TAU_MAG,
# non-neutral stance, tradeable), what fraction moved in the stance direction?
#
# Scoring only the admitted set is what makes the measurement un-gameable: a
# directionally-correct name the system would NOT admit (sub-magnitude, untradeable)
# cannot pad the hit-rate, and a directionally-wrong name it would NOT admit
# (sub-confidence, in-universe) cannot tank it. The admission predicate mirrors
# `onboarding.catalyst_admissions` EXCEPT it deliberately omits MAX_ADMISSIONS —
# the cap is a live-resource limit, not a precision question.
#
# Offline/deterministic: realized forward returns are captured ONCE offline and
# passed in (never fetched in-test). The flag-flip is an OPERATOR action; this
# axis only MEASURES whether the bar is cleared. See ADR-0075 + the versioned
# fixture tests/fixtures/catalyst_onboarding/admission_episodes.v1.json.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdmissionEpisode:
    """One ADR-0075 catalyst-admission episode with its REAL forward return.

    The realized move is signed % over ``horizon``, captured offline and
    committed — external truth, never self-graded.
    """

    symbol: str
    stance: str  # bullish | bearish | neutral
    confidence: float
    magnitude: float
    realized_forward_return: float
    in_universe: bool = False
    tradeable: bool = True
    horizon: str = "1d"
    label: str = ""


@dataclass(frozen=True)
class AdmissionPrecisionResult:
    n_episodes: int
    n_admitted: int
    n_scored: int  # admitted AND directionally scorable (non-flat realized return)
    hits: int
    hit_rate: float
    passed: bool
    misses: tuple[str, ...]  # "SYMBOL:stance vs realized" for admitted+scored misses
    rejected: tuple[str, ...]  # "SYMBOL:reason" for episodes the admission gate excluded


def run_admission_precision(
    episodes: list[AdmissionEpisode],
    *,
    min_hit_rate: float = 0.6,
    tau_conf: float = TAU_CONF,
    tau_mag: float = TAU_MAG,
) -> AdmissionPrecisionResult:
    """Precision CONDITIONAL ON ADMISSION (the ADR-0075 onboarding eval gate).

    Replays the admission predicate over ``episodes`` and scores the admitted set
    against the committed realized returns:

      * ADMIT iff out-of-universe AND conf>=tau_conf AND mag>=tau_mag AND stance in
        (bullish, bearish) AND tradeable — the predicate of ``catalyst_admissions``
        WITHOUT the MAX_ADMISSIONS cap (a precision question, not a resource one).
      * Of the admitted, score only the directionally scorable (non-flat realized
        return): a HIT iff sign(realized) matches the stance direction.
      * PASS iff ``n_scored > 0`` AND ``hit_rate >= min_hit_rate`` — an empty
        measurement is a FAIL, never a vacuous free pass (a flag must never flip on
        zero evidence).

    Deterministic + offline (no network, no price fetch).
    """
    rejected: list[str] = []
    admitted: list[AdmissionEpisode] = []
    for e in episodes:
        reason = _admission_reject_reason(e, tau_conf=tau_conf, tau_mag=tau_mag)
        if reason is None:
            admitted.append(e)
        else:
            rejected.append(f"{e.symbol}:{reason}")

    hits = 0
    n_scored = 0
    misses: list[str] = []
    for e in admitted:
        direction = 1 if e.stance == "bullish" else -1 if e.stance == "bearish" else 0
        ret = e.realized_forward_return
        if direction == 0 or ret == 0.0:
            # admitted but not directionally scorable (flat move) -> neither hit nor miss.
            continue
        n_scored += 1
        if (ret > 0.0) == (direction > 0):
            hits += 1
        else:
            misses.append(f"{e.symbol}:{e.stance} vs {ret:+.2f}%")

    hit_rate = (hits / n_scored) if n_scored > 0 else 0.0
    passed = n_scored > 0 and hit_rate >= min_hit_rate
    return AdmissionPrecisionResult(
        n_episodes=len(episodes),
        n_admitted=len(admitted),
        n_scored=n_scored,
        hits=hits,
        hit_rate=hit_rate,
        passed=passed,
        misses=tuple(misses),
        rejected=tuple(rejected),
    )


def _admission_reject_reason(
    e: AdmissionEpisode, *, tau_conf: float, tau_mag: float
) -> str | None:
    """Return the reason the admission gate EXCLUDES ``e``, or None if admitted.

    Mirrors ``onboarding.catalyst_admissions`` (minus the MAX_ADMISSIONS cap):
    in-universe names are screen artifacts (already recommended), sub-threshold
    conf/mag are below the act-on floor, neutral has no tradeable direction, and
    untradeable is fail-closed (admission must never mint an unfillable order).
    """
    if not _is_finite_number(tau_conf):
        return "non_finite_tau_conf"
    if not _is_finite_number(tau_mag):
        return "non_finite_tau_mag"
    if not _is_finite_number(e.confidence):
        return "non_finite_confidence"
    if not _is_finite_number(e.magnitude):
        return "non_finite_magnitude"
    if not _is_finite_number(e.realized_forward_return):
        return "non_finite_realized_forward_return"
    if e.in_universe:
        return "in_universe (screen artifact, not a catalyst admission)"
    if e.confidence < tau_conf:
        return f"confidence {e.confidence:.2f} < tau_conf {tau_conf:.2f}"
    if e.magnitude < tau_mag:
        return f"magnitude {e.magnitude:.3f} < tau_mag {tau_mag:.3f}"
    if e.stance not in ("bullish", "bearish"):
        return f"stance {e.stance!r} has no tradeable direction"
    if not e.tradeable:
        return "not tradeable (fail-closed)"
    return None
