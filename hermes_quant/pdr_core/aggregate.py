"""hermes_quant.pdr_core.aggregate — the PURE BMA vote arithmetic (ADR-0092 Inc-1 step 6).

A host-blind port of the FLAGS-OFF, COLD-START path of
``hermes_quant.aggregators.bma.BMAAggregator.aggregate`` (ADR-0003 — the numeric
fusion that turns a list of :class:`~hermes_quant.pdr_core.contracts.AnalystView`
into ONE aggregated directional signal the deterministic risk gate consumes).
SAME vote_share, agreement_bonus, single-source / require_ensemble gate, net-flat
silence, weighted-mean magnitude, modal-horizon + multi-timeframe agreement
adjustment, and the cold-start confidence map — byte-for-byte with the live BMA
when every ``HERMES_QUANT_*`` learning flag is unset and the aggregator is fresh
(uniform 0.5 posterior weights) on the cold-start calibrator. Parity is proven
against the live oracle in ``tests/pdr_core/test_aggregate_parity.py``.

WHAT THIS PORTS (the pure vote surface):
  - abstain filter (``confidence < ABSTAIN_THRESHOLD`` dropped before the vote)
  - uniform base weight 0.5 × per-horizon weight (DEFAULT_HORIZON_WEIGHTS)
  - signed direction terms ``direction × weight × confidence``; net-flat silence
    (``|Σ| < 1e-9 → direction 0``); sign rule for the composite direction
  - weighted-mean magnitude over contributing-direction views
  - ``vote_share = |net| / Σ|term|``
  - single-source gate: ``require_ensemble`` True → silence; False → lone view's
    own clipped ``confidence_raw``
  - multi-contributor: unanimous → ``vote_share + agreement_bonus`` (clipped);
    dissent → ``vote_share``
  - cold-start confidence ``(confidence_raw + 2) / 8`` (the live
    ColdStartCalibrator / CalibratorNotReady arm), THEN the multi-timeframe
    horizon-agreement multiplier (×1.10 all-agree / ×0.85 mixed), matching the
    live ORDERING (calibrate first, horizon multiplier second).

WHAT THIS DELIBERATELY DOES NOT PORT (stays in the host SHELL):
  - any calibrator import / pickle / disk IO. The core takes per-view CALIBRATED
    confidence as INPUT (``AnalystView.confidence``) and EMITS ``confidence_raw``;
    the only calibration math the core reproduces is the PURE cold-start
    ``(raw + 2) / 8`` arithmetic, exposed as the default so a host-blind harness
    can reproduce the live CalibratorNotReady arm. A shell that has a fitted
    isotonic calibrator applies it to ``confidence_raw`` and overwrites
    ``confidence`` itself (set ``cold_start=False`` to get the raw vote share back
    as ``confidence`` for the shell to recalibrate).
  - the 7 default-OFF learning subsystems (decay / per-analyst-calib /
    lesson-haircut / stacking / posterior-persist / dissent-cap / L2-decay) and
    their producers (IC-dedup gate, regime detector). Their flags-off metadata
    sentinels (``ic_dedup_excluded_analysts=[]`` / ``regime_state=None`` /
    ``regime_weight_multipliers=None``) ARE reproduced so the metadata dict is
    byte-identical to the live OFF path.

PURITY: stdlib only (``math`` / ``dataclasses`` / ``collections.abc`` /
``typing``). No host/infra import, no numpy, no sklearn, no pickle — clips are
``max/min`` with an explicit NaN guard (np.clip propagates NaN; we fail those to
a finite value to match the bounded inputs the live path always carries). The
purity gate (``tests/pdr_core/test_contract_purity.py``) stays green.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from hermes_quant.pdr_core.contracts import AnalystView, Direction

# ---------------------------------------------------------------------------
# Constants — VERBATIM mirror of the live BMA flags-off defaults.
# ---------------------------------------------------------------------------

# ADR-0018 §D4: views with confidence below this are abstains, dropped before the
# vote (mirrors bma.ABSTAIN_THRESHOLD).
ABSTAIN_THRESHOLD: float = 0.10

# The fresh-aggregator, flags-off uniform per-analyst weight. In the live BMA this
# is ``_weight_for`` returning 0.5 because ``n_observations(0) < n_min(30)`` and
# decay is off. The pure core has no posteriors, so the uniform weight is fixed.
UNIFORM_WEIGHT: float = 0.5

# ADR-0036 per-horizon weight multipliers (mirror bma.DEFAULT_HORIZON_WEIGHTS).
# Views whose horizon is absent get 1.0 (no suppression).
DEFAULT_HORIZON_WEIGHTS: Mapping[str, float] = {
    "1d": 1.00,
    "1w": 1.20,
    "1M": 0.80,
    "1Q": 0.60,
}

# ADR-0003: agreement bonus added to vote_share when all voters agree (bma default).
DEFAULT_AGREEMENT_BONUS: float = 0.10

# ADR-0036 multi-timeframe agreement multipliers (mirror bma HORIZON_* defaults).
HORIZON_AGREEMENT_BONUS: float = 1.10
HORIZON_DISAGREEMENT_PENALTY: float = 0.85

# Cold-start Beta(2,5) prior — the PURE arithmetic of ColdStartCalibrator.calibrate
# and the live CalibratorNotReady fallback: confidence = (raw + alpha) / (1 + alpha + beta).
_COLD_START_ALPHA: float = 2.0
_COLD_START_BETA: float = 5.0

# Net-flat silence threshold (mirror bma's 1e-9).
_NET_FLAT_EPS: float = 1e-9


def _clip01(x: float) -> float:
    """``max(0, min(1, x))`` with an explicit NaN guard.

    The live BMA uses ``float(np.clip(x, 0.0, 1.0))``. For finite inputs the two
    are bit-identical; np.clip propagates NaN whereas the established core style
    (kelly.py / gate.py) uses stdlib ``max``/``min`` clamps. The live vote path
    only ever clips already-bounded quantities (vote_share in [0,1], a contract
    confidence in [0,1]), so NaN cannot arise on the parity path — but guard it
    explicitly to 0.0 (the silence-safe value) so a pathological shell input can
    never launder a NaN confidence past this clamp.
    """
    if not math.isfinite(x):
        return 0.0
    return max(0.0, min(1.0, x))


def _cold_start_calibrate(confidence_raw: float) -> float:
    """The PURE cold-start confidence map: ``(raw + alpha) / (1 + alpha + beta)``.

    VERBATIM arithmetic of ``calibrators.ColdStartCalibrator.calibrate`` (raw is
    clipped to [0,1] first) and the live ``aggregate`` CalibratorNotReady
    fallback. This is the ONLY calibrator math the core reproduces; it imports
    no calibrator and touches no disk.
    """
    raw = _clip01(confidence_raw)
    return (raw + _COLD_START_ALPHA) / (1.0 + _COLD_START_ALPHA + _COLD_START_BETA)


# ---------------------------------------------------------------------------
# CoreAggregateContext — the host-blind decision-context read-surface.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoreAggregateContext:
    """The 4 scalar fields the BMA vote stamps onto its output signal.

    The live ``aggregate`` reads ``context.asset`` / ``context.timeframe`` /
    ``context.asset_class`` / ``context.asof`` off ``protocol.MarketContext`` —
    which ALSO carries a pandas ``bars`` DataFrame the core must never depend on.
    This read-surface lifts ONLY those 4 scalars (the same severance pattern as
    ``gate_types.CoreSignal`` vs ``protocol.AggregatedSignal``). ``asof`` is typed
    ``Any`` so a shell may pass an ISO-8601 string or a ``pandas.Timestamp``
    without the core importing pandas.
    """

    asset: str
    timeframe: str
    asset_class: str
    asof: Any


# ---------------------------------------------------------------------------
# CoreAggregatedSignal — the fused directional verdict the gate consumes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoreAggregatedSignal:
    """The BMA's combined, calibrated directional view (host-blind output type).

    Field-for-field mirror of the surface ``protocol.AggregatedSignal`` carries
    out of ``BMAAggregator.aggregate`` — the input the deterministic gate sizes
    from. ``confidence`` is the CALIBRATED probability (cold-start ``(raw+2)/8``
    then the horizon multiplier, when ``cold_start=True``); ``confidence_raw`` is
    the vote share ± agreement/dissent / lone-voice raw. ``components`` is the
    tuple of (abstain-filtered) :class:`AnalystView` that fed the vote — required
    for downstream per-analyst outcome crediting (ADR-0009 §P1-10). ``aggregator``
    is fixed ``"bma"``.

    ``evidence_ids`` and ``message_kind`` are defaulted (not populated by the
    vote) to mirror ``protocol.AggregatedSignal``'s defaults; a shell maps this
    onto the live ``AggregatedSignal`` mechanically.
    """

    asset: str
    timeframe: str
    asset_class: str
    asof: Any
    direction: Direction
    magnitude: float
    confidence: float  # CALIBRATED (cold-start (raw+2)/8 then horizon multiplier)
    confidence_raw: float  # vote share +/- agreement / dissent / lone-voice raw
    horizon: str
    components: tuple[AnalystView, ...]
    aggregator: str = "bma"
    metadata: Mapping[str, Any] | None = None
    evidence_ids: tuple[str, ...] = ()
    message_kind: str = "discussion"


def _flat_signal(
    context: CoreAggregateContext,
    *,
    components: tuple[AnalystView, ...] = (),
    reason: str = "flat_or_no_views",
) -> CoreAggregatedSignal:
    """The silence constructor — VERBATIM port of ``BMAAggregator._flat_signal``.

    Every guard (empty views, net-flat, single-source-silenced, total_w<=0)
    returns this: direction 0, zero magnitude/confidence, horizon ``"0m"``, and a
    metadata dict carrying only the ``reason``. ``components`` carries the lone /
    abstain-filtered views so a shell can still credit per-analyst outcomes.
    """
    return CoreAggregatedSignal(
        asset=context.asset,
        timeframe=context.timeframe,
        asset_class=context.asset_class,
        asof=context.asof,
        direction=0,
        magnitude=0.0,
        confidence=0.0,
        confidence_raw=0.0,
        horizon="0m",
        components=components,
        aggregator="bma",
        metadata={"reason": reason},
    )


def core_aggregate(
    views: Sequence[AnalystView],
    context: CoreAggregateContext,
    *,
    require_ensemble: bool = True,
    agreement_bonus: float = DEFAULT_AGREEMENT_BONUS,
    horizon_weights: Mapping[str, float] = DEFAULT_HORIZON_WEIGHTS,
    horizon_agreement_bonus: float = HORIZON_AGREEMENT_BONUS,
    horizon_disagreement_penalty: float = HORIZON_DISAGREEMENT_PENALTY,
    uniform_weight: float = UNIFORM_WEIGHT,
    abstain_threshold: float = ABSTAIN_THRESHOLD,
    cold_start: bool = True,
) -> CoreAggregatedSignal:
    """PURE BMA vote — the FLAGS-OFF, cold-start fusion of analyst views.

    Mirrors ``BMAAggregator.aggregate`` on the flags-off path EXACTLY. Takes per-
    view CALIBRATED ``confidence`` as INPUT and computes ``confidence_raw`` (the
    vote share) host-blind. With ``cold_start=True`` (default) the emitted
    ``confidence`` reproduces the live cold-start arm: ``(confidence_raw + 2) / 8``
    THEN the multi-timeframe horizon multiplier — the same ORDERING as live
    (calibrate first, horizon adjust second). With ``cold_start=False`` the
    emitted ``confidence`` is the un-calibrated horizon-adjusted vote share, for a
    shell that owns calibration (it overwrites ``confidence`` from its own
    calibrator).

    The core imports NO calibrator and touches NO disk. The three SILENCE exits
    (direction 0): empty views, ``|weighted_dir_sum| < 1e-9`` net-flat, and a
    single-source candidate under ``require_ensemble``.
    """
    # ADR-0018 §D4 abstain filter — drop views below the abstain threshold so an
    # 'I have no view' signal does not pollute the vote share OR the voice count.
    views = [v for v in (views or []) if v.confidence >= abstain_threshold]
    if not views:
        return _flat_signal(context)

    def _horizon_weight(horizon: str) -> float:
        return float(horizon_weights.get(horizon, 1.0))

    # Per-view weight = uniform base × per-horizon multiplier; signed direction
    # term = direction × weight × CALIBRATED confidence (flags-off _vote_confidence
    # collapses to v.confidence).
    weights: list[float] = []
    signed_dir_terms: list[float] = []
    for v in views:
        w = uniform_weight * _horizon_weight(v.horizon)
        weights.append(w)
        signed_dir_terms.append(v.direction * w * v.confidence)

    weighted_dir_sum = sum(signed_dir_terms)
    if abs(weighted_dir_sum) < _NET_FLAT_EPS:
        # Net flat — silence.
        return _flat_signal(context)

    composite_direction: Direction = 1 if weighted_dir_sum > 0 else -1

    # Magnitude: weighted mean of magnitudes over contributing-direction views.
    contributing = [
        (v, w)
        for v, w in zip(views, weights, strict=False)
        if v.direction == composite_direction
    ]
    total_w = sum(w for _, w in contributing)
    if total_w <= 0:
        return _flat_signal(context)
    magnitude = sum(v.magnitude * w for v, w in contributing) / total_w

    # Vote share: |net signed| / Σ |contribution|.
    denom = sum(abs(t) for t in signed_dir_terms)
    vote_share = 0.0 if denom <= 0 else abs(weighted_dir_sum) / denom

    # Single-source / multi-contributor confidence_raw.
    n_distinct_analysts = len({v.analyst for v in views})
    non_flat = [v for v in views if v.direction != 0]
    if n_distinct_analysts <= 1:
        if require_ensemble:
            # A lone voice is silenced (BMA's value-add is ensemble disagreement
            # resolution). Carry the views so a shell can still credit outcomes.
            return _flat_signal(
                context,
                components=tuple(views),
                reason="silenced_single_source",
            )
        # Pass-through: honest confidence is the lone analyst's own clipped raw.
        sole_v, _w = contributing[0]
        confidence_raw = _clip01(sole_v.confidence_raw)
    elif non_flat and all(v.direction == composite_direction for v in non_flat):
        # Multi-contributor unanimous: vote_share + agreement bonus.
        confidence_raw = _clip01(vote_share + agreement_bonus)
    else:
        # Multi-contributor with dissent: vote_share only.
        confidence_raw = vote_share

    # Calibrate. The core reproduces ONLY the cold-start arm (no calibrator
    # import); a shell with a fitted calibrator sets cold_start=False and
    # overwrites confidence itself.
    confidence = _cold_start_calibrate(confidence_raw) if cold_start else confidence_raw

    # Horizon: modal horizon among contributing views (default to first view).
    horizons = [v.horizon for v, _ in contributing]
    horizon = max(set(horizons), key=horizons.count) if horizons else views[0].horizon

    # ADR-0036 multi-timeframe agreement adjustment over ALL distinct horizons.
    horizons_present = sorted({v.horizon for v in views})
    if len(horizons_present) <= 1:
        horizon_agreement = "single_horizon"
    else:
        per_horizon_dir: dict[str, float] = {}
        for v, w in zip(views, weights, strict=False):
            per_horizon_dir[v.horizon] = (
                per_horizon_dir.get(v.horizon, 0.0) + v.direction * w * v.confidence
            )
        horizon_signs = {
            h: (1 if s > 0 else (-1 if s < 0 else 0))
            for h, s in per_horizon_dir.items()
        }
        non_zero_signs = [s for s in horizon_signs.values() if s != 0]
        if non_zero_signs and all(s == composite_direction for s in non_zero_signs):
            horizon_agreement = "all_agree"
            confidence = _clip01(confidence * horizon_agreement_bonus)
        else:
            horizon_agreement = "mixed"
            confidence = _clip01(confidence * horizon_disagreement_penalty)

    # OFF-path metadata: the base dict + the flags-off sentinels for the producers
    # that stay in the shell (IC-dedup gate, regime detector). The 4 flag-gated
    # metadata injections (stacking / per-analyst-calib / lesson-haircut) are NOT
    # added — byte-identical to the live OFF path.
    metadata: dict[str, Any] = {
        "weights": {v.analyst: w for v, w in zip(views, weights, strict=False)},
        "vote_share": float(vote_share),
        "n_contributing": len(contributing),
        "n_views": len(views),
        "horizons_present": horizons_present,
        "horizon_agreement": horizon_agreement,
        "ic_dedup_excluded_analysts": [],
        "regime_state": None,
        "regime_weight_multipliers": None,
    }

    return CoreAggregatedSignal(
        asset=context.asset,
        timeframe=context.timeframe,
        asset_class=context.asset_class,
        asof=context.asof,
        direction=composite_direction,
        magnitude=float(magnitude),
        confidence=float(confidence),
        confidence_raw=float(confidence_raw),
        horizon=horizon,
        components=tuple(views),
        aggregator="bma",
        metadata=metadata,
    )
