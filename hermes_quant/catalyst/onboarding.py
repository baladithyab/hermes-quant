"""hermes_quant.catalyst.onboarding — ADR-0075 catalyst-driven universe onboarding.

When a fresh, strong catalyst packet targets an OUT-OF-UNIVERSE symbol (a name the
daily liquidity screen omits — exactly the high-beta catalyst reactors the feature
targets, e.g. LUNR on the Blue Origin move), temporarily admit that symbol to the
candidate set fed to the watchlist evolver, for the catalyst's horizon, subject to a
hard tradeability gate.

Rails (money-software, ADR-0075):
  * DEFAULT-OFF behind HERMES_QUANT_CATALYST_ONBOARDING=1, AND-gated on
    HERMES_QUANT_SEMANTIC_ENABLED=1 (onboarding without semantic is meaningless).
    Both flags read at call time. With either OFF -> [] (byte-identical to today).
  * This is admissibility/perception: it can only ADMIT a name to the candidate
    set. The ADR-0004 deterministic risk gate remains FINAL authority and can still
    silence. Onboarding never forces, amplifies, or overrides a gate decision.
  * Hard cap MAX_ADMISSIONS (<=3) simultaneous catalyst-admitted names.
  * Tradeability gate is fail-closed: a name with no liquidity / not tradeable is
    rejected even if the catalyst is strong (admission must not create an
    unfillable order). Reuses the ADR-0077 oracle (do NOT duplicate get_asset).
  * Silence-by-default on every error path: returns [], never raises.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from numbers import Real
from typing import Any

# ADR-0075 thresholds (conservative start; tune via eval-gate before flag-flip).
TAU_CONF = 0.60  # packet confidence floor for admission
TAU_MAG = 0.04  # packet magnitude floor for admission
MAX_ADMISSIONS = 3  # hard cap on simultaneous catalyst-admitted names
ONBOARD_ADV_FLOOR = 1_000_000.0  # dollar-volume floor < universe screen (5M), > 0


@dataclass(frozen=True)
class CatalystAdmission:
    symbol: str
    stance: str  # bullish | bearish
    direction: int  # +1 | -1  (derived from stance)
    confidence: float
    magnitude: float
    horizon: str
    packet_asof: str
    admitted_via: str = "catalyst"


# tradeable(symbol) -> bool : injected so this module is offline/testable.
# Production wires default_tradeable, which prefers the ADR-0077 oracle's
# is_tradeable_long(symbol) (asset.tradable AND asset.fractionable).
TradeabilityCheck = Callable[[str], bool]


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, Real) and math.isfinite(float(value))


def _packet_rank_fields(packet: dict) -> tuple[float, float, float] | None:
    try:
        conf = float(packet.get("confidence", 0.0))
        mag = float(packet.get("magnitude", 0.0))
    except (TypeError, ValueError, OverflowError):
        return None
    if not _is_finite_number(conf) or not _is_finite_number(mag):
        return None
    rank = conf * mag
    if not _is_finite_number(rank):
        return None
    return conf, mag, rank


def catalyst_admissions(
    universe_symbols: set[str],
    *,
    tradeable: TradeabilityCheck,
    asof: datetime | None = None,
    tau_conf: float = TAU_CONF,
    tau_mag: float = TAU_MAG,
    max_admissions: int = MAX_ADMISSIONS,
) -> list[CatalystAdmission]:
    """Return <=max_admissions out-of-universe symbols with a fresh, strong catalyst
    packet that pass the tradeability gate. [] unless BOTH flags are on
    (silence-by-default; never raises).

    Flow (research §1.2 Seam A):
      1. coverage_against_universe(universe) -> dead_on_arrival symbols
      2. load_packets_for(sym, asof) -> freshest packet; keep iff conf>=tau_conf
         AND mag>=tau_mag
      3. tradeable(sym) gate (ADR-0077 oracle in prod; fail-closed on error)
      4. rank by confidence*magnitude, cap to max_admissions, tag admitted_via=catalyst
    """
    if (
        os.environ.get("HERMES_QUANT_CATALYST_ONBOARDING", "0") != "1"
        or os.environ.get("HERMES_QUANT_SEMANTIC_ENABLED", "0") != "1"
    ):
        return []
    if not _is_finite_number(tau_conf) or not _is_finite_number(tau_mag):
        return []
    try:
        from hermes_quant.catalyst.propagation import coverage_against_universe, load_graph
        from hermes_quant.catalyst.synthesize import load_packets_for

        asof = asof or datetime.now(UTC)
        graph, _ = load_graph()
        cov = coverage_against_universe(universe_symbols, graph)
        dead: list[str] = list(cov.get("dead_on_arrival", []))  # type: ignore[arg-type]
        candidates: list[CatalystAdmission] = []
        for sym in dead:
            packets = load_packets_for(sym, asof)
            if not packets:
                continue
            ranked_packets: list[tuple[dict, float, float, float]] = []
            for packet in packets:
                fields = _packet_rank_fields(packet)
                if fields is None:
                    continue
                conf, mag, rank = fields
                ranked_packets.append((packet, conf, mag, rank))
            if not ranked_packets:
                continue
            best, conf, mag, _rank = max(
                ranked_packets,
                key=lambda p: (p[1], p[0].get("asof", "")),
            )
            if conf < tau_conf or mag < tau_mag:
                continue
            stance = best.get("stance", "")
            direction = 1 if stance == "bullish" else -1 if stance == "bearish" else 0
            if direction == 0:
                continue
            try:
                if not tradeable(sym):  # fail-closed: any falsy -> reject
                    continue
            except Exception:  # noqa: BLE001
                continue
            candidates.append(
                CatalystAdmission(
                    symbol=sym,
                    stance=stance,
                    direction=direction,
                    confidence=conf,
                    magnitude=mag,
                    horizon=best.get("horizon", "1d"),
                    packet_asof=best.get("asof", ""),
                )
            )
        candidates.sort(key=lambda a: a.confidence * a.magnitude, reverse=True)
        return candidates[:max_admissions]
    except Exception:  # noqa: BLE001 — silence-by-default
        return []


def default_tradeable(symbol: str, *, adv_floor: float = ONBOARD_ADV_FLOOR) -> bool:
    """Production tradeability check: ADR-0077 admissibility oracle long-tradeable
    read (get_asset: tradable AND fractionable). Fail-closed: any error or missing
    oracle -> False (reject).

    ``adv_floor`` is the intended dollar-volume floor (lower than the universe
    screen's 5M but > 0); the per-symbol volume estimate is wired by the production
    caller when available — absent that, the oracle's tradable+fractionable read is
    the binding gate (fail-closed by construction).
    """
    try:
        from hermes_quant.admissibility.oracle import AlpacaShortabilityOracle

        # Long-side admissibility: tradable + fractionable (no short borrow needed
        # for a long admit). Reuses the ADR-0077 oracle's get_asset plumbing.
        oracle = AlpacaShortabilityOracle()
        return oracle.is_tradeable_long(symbol)
    except Exception:  # noqa: BLE001
        return False
