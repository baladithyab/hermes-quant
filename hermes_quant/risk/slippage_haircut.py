"""hermes_quant.risk.slippage_haircut — paper-vs-live execution penalty (ADR-0097).

THE PROBLEM (operator risk requirement, 2026-06-17): Alpaca's PAPER engine fills more
optimistically than Alpaca LIVE (near mid, no real queue/liquidity pressure). When the
broker path (``HERMES_QUANT_ALPACA_PAPER=1``) is on, the recorded ``filled_avg_price`` is
taken raw — so the paper P&L the ADR-0029 evidence gate + ADR-0125 promotion gate consume
OVERSTATES live profitability. A track record built on optimistic fills is a fail-open into
live money.

THE FIX: a conservative, FAIL-CLOSED estimate of the per-trade live-execution penalty (in
NAV-fraction return terms), used two ways by the caller:
  1. haircut-toward-silence at the gate/sizer (a play whose edge < penalty is SILENCED);
  2. a live-realistic P&L marking the evidence/promotion series reads (instead of raw paper).

WHAT WE CAN AND CANNOT MEASURE (the honesty that makes this conservative, not precise):
  * The ``HERMES_QUANT_ALPACA_SHADOW`` log measures ALPACA-PAPER vs SYNTHETIC divergence —
    NOT live-vs-paper (we have no live fills). So the measured divergence is only a PARTIAL
    signal: it captures how the synthetic model differs from Alpaca-paper, but the
    paper->live optimism gap is UNMEASURED.
  * Therefore the estimate = max(measured-component, a conservative LIVE-VS-PAPER PRIOR).
    The prior dominates until we have live fills; it is deliberately pessimistic so the
    error is on the over-haircut (safe) side. An UNKNOWN estimate haircuts MORE, never less.

POSTURE: pure + finite-guarded (a NaN/inf in any input yields the prior, never a free pass —
the ar08 family). DEFAULT-OFF: the caller only consults this when
``HERMES_QUANT_SLIPPAGE_HAIRCUT=1``; this module is import-safe and has no global effect.
"""
from __future__ import annotations

import json
import math
import os
import statistics
from dataclasses import dataclass
from pathlib import Path

# Conservative LIVE-VS-PAPER priors, as a one-way penalty on the trade's NAV-fraction
# return (a fraction: 0.0010 = 10 bps of round-trip execution cost beyond what paper shows).
# These are STARTING POINTS to be eval-gated once real live fills accrue — NOT ground truth.
# They are intentionally pessimistic: a defined options spread crosses TWO wide books, an
# MLEG fills leg-by-leg, so options dwarf the equity prior. Equity prior > the v0.2 model's
# typical ~13-18 bps synthetic estimate because live adds queue position + adverse selection
# the simulator does not model.
_LIVE_VS_PAPER_PRIOR: dict[str, float] = {
    "equity": 0.0025,   # 25 bps round-trip beyond paper
    "etf": 0.0020,
    "crypto": 0.0040,
    "fx": 0.0015,
    "option": 0.0080,     # single option leg: wide per-contract spread
    "us_option": 0.0080,
}
_DEFAULT_PRIOR = 0.0050  # an unknown asset_class is treated worse than equity

# Per-leg prior used to BUILD a multi-leg penalty: an N-leg structure crosses N books, so
# its penalty >= sum of per-leg priors (ADR-0097 sl02). A single-name option leg.
_PER_OPTION_LEG_PRIOR = 0.0080
_PER_STOCK_LEG_PRIOR = 0.0025

# Below this many shadow samples for an asset_class, the measured component is considered
# too thin to trust -> the prior alone governs (fail-closed).
_MIN_SHADOW_SAMPLES = 20

_FLAG = "HERMES_QUANT_SLIPPAGE_HAIRCUT"

SHADOW_DIVERGENCE_PATH = Path.home() / ".hermes" / "quant" / "alpaca-shadow-divergence.jsonl"


def haircut_enabled() -> bool:
    """True iff the operator opted in. The caller gates on this; default-OFF."""
    return os.environ.get(_FLAG, "0") == "1"


def _finite(x: object) -> float | None:
    """Coerce to a finite float, else None (ar08 finite-guard family)."""
    try:
        f = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


@dataclass(frozen=True)
class PenaltyEstimate:
    """The estimated live-execution penalty for a (asset_class, structure) trade.

    ``penalty_frac`` is a positive one-way fraction of the trade's NAV-fraction return to
    subtract (the conservative live-vs-paper cost). ``basis`` says what governed it
    (``prior`` when measured data is thin/absent — the fail-closed path).
    """

    penalty_frac: float
    basis: str  # "prior" | "measured" | "measured+prior"
    n_samples: int
    detail: str


def _measured_component(asset_class: str, shadow_log: Path) -> tuple[float | None, int]:
    """Conservative measured penalty from the shadow log for this asset_class.

    Returns (penalty_or_None, n_samples). The penalty is a HIGH-percentile (not mean) of
    the |fill_price_divergence| / synthetic_fill_price ratio — conservative by construction.
    Returns (None, n) when fewer than _MIN_SHADOW_SAMPLES finite rows exist (fail-closed).
    """
    if not shadow_log.exists():
        return None, 0
    ratios: list[float] = []
    try:
        for line in shadow_log.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or row.get("asset_class") != asset_class:
                continue
            div = _finite(row.get("fill_price_divergence"))
            syn = _finite(row.get("synthetic_fill_price"))
            if div is None or syn is None or syn <= 0.0:
                continue
            ratios.append(abs(div) / syn)
    except OSError:
        return None, 0
    n = len(ratios)
    if n < _MIN_SHADOW_SAMPLES:
        return None, n
    ratios.sort()
    # 90th percentile (conservative): the cost we'd see on a bad-but-not-pathological fill.
    idx = min(n - 1, int(math.ceil(0.90 * n)) - 1)
    p90 = ratios[idx]
    return (p90 if math.isfinite(p90) else None), n


def estimate_live_penalty(
    asset_class: str,
    structure_kind: str | None = None,
    *,
    n_legs: int = 1,
    leg_asset_classes: tuple[str, ...] | None = None,
    shadow_log: Path | None = None,
) -> PenaltyEstimate:
    """Estimate the conservative live-execution penalty (NAV-fraction) for a trade.

    Args:
      asset_class: the trade's asset class ("equity"/"us_option"/...).
      structure_kind: optional structure label (e.g. "vertical_spread", "covered_call");
        currently used only to flag multi-leg via n_legs/leg_asset_classes.
      n_legs: number of legs (>1 => multi-leg penalty = sum of per-leg priors, ADR-0097 sl02).
      leg_asset_classes: per-leg asset classes when known (overrides n_legs sizing).
      shadow_log: override the divergence log path (testing).

    Returns a PenaltyEstimate whose ``penalty_frac`` is always > 0 and finite (fail-closed:
    an unknown/thin estimate falls back to a positive prior, NEVER 0).
    """
    log = shadow_log or SHADOW_DIVERGENCE_PATH

    # --- multi-leg: penalty >= sum of per-leg priors (each leg crosses its own book) ---
    if leg_asset_classes:
        per_leg = sum(
            _PER_OPTION_LEG_PRIOR if _is_option(ac) else _PER_STOCK_LEG_PRIOR
            for ac in leg_asset_classes
        )
        return PenaltyEstimate(
            penalty_frac=per_leg,
            basis="prior",
            n_samples=0,
            detail=f"multi-leg sum-of-{len(leg_asset_classes)}-leg priors = {per_leg:.4f}",
        )
    if n_legs > 1:
        # No per-leg classes given: assume option legs (the conservative case).
        per_leg = n_legs * _PER_OPTION_LEG_PRIOR
        return PenaltyEstimate(
            penalty_frac=per_leg,
            basis="prior",
            n_samples=0,
            detail=f"multi-leg {n_legs}x option-leg prior = {per_leg:.4f}",
        )

    # --- single leg: max(measured-component, prior). The prior dominates until the
    # measured component is BOTH present AND larger (we never trust a measured value that
    # is SMALLER than the prior, because the shadow log can't see the paper->live gap). ---
    prior = _LIVE_VS_PAPER_PRIOR.get(asset_class, _DEFAULT_PRIOR)
    measured, n = _measured_component(asset_class, log)
    if measured is None:
        return PenaltyEstimate(
            penalty_frac=prior,
            basis="prior",
            n_samples=n,
            detail=f"thin/absent shadow data (n={n} < {_MIN_SHADOW_SAMPLES}) -> conservative prior {prior:.4f}",
        )
    penalty = max(measured, prior)
    basis = "measured+prior" if penalty == prior else "measured"
    return PenaltyEstimate(
        penalty_frac=penalty,
        basis=basis,
        n_samples=n,
        detail=f"max(measured p90 {measured:.4f}, prior {prior:.4f}) = {penalty:.4f} (n={n})",
    )


def _is_option(asset_class: str | None) -> bool:
    return asset_class in ("option", "us_option")


def apply_edge_haircut(expected_edge_frac: float, penalty: PenaltyEstimate) -> float:
    """Subtract the live-execution penalty from a play's expected edge (haircut-toward-silence).

    Returns the net edge. A caller silences the play when this is <= 0. Fail-closed: a
    non-finite expected_edge is treated as 0 edge (so the penalty makes it negative -> silence).
    """
    e = _finite(expected_edge_frac)
    if e is None:
        return -abs(penalty.penalty_frac)
    return e - abs(penalty.penalty_frac)
