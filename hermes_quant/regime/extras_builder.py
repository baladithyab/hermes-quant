"""hermes_quant.regime.extras_builder — populate MarketContext.extras["regime"].

Per ADR-0063: closes the docstring-vs-reality gap at protocol.py:80 (regime
claimed to be in extras but never populated). This helper is the canonical
producer of the regime field; advisor.py merges its output OVER caller-provided
market_extras so callers cannot shadow it.

Public API:
    extras = build_regime_extras(symbol, bars)
    # extras["regime"] is RegimePacket | None
    # extras["regime_failure"] is reason_str | None
    # extras["regime_classifier_kind"] is "rule_based" | "hmm" | "unavailable"

Per ADR-0036 silence-by-default: never raises. Any exception inside the
classifier path becomes a populated regime_failure with regime=None.

Per ADR-0058 label-stability invariant: downstream consumers MUST branch on
RegimePacket.volatility_tier (-1/0/+1, derived from realized_vol_percentile),
NOT on RegimePacket.label string. The label is for human-readable audit only.
The one carve-out is RegimeState.UNKNOWN, which is fixed and safe to check
by name.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, Protocol

import pandas as pd

from hermes_quant.regime.detector import RegimeDetector, RegimeState
from hermes_quant.regime.state_variables import StateVariables, compute_state_variables

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RegimePacket — the canonical shape passed to analysts via ctx.extras["regime"]
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegimePacket:
    """Canonical regime info passed to analysts via ``ctx.extras["regime"]``.

    Per ADR-0063:
    - ``label`` is for human-readable audit. Downstream MUST NOT branch on it
      (one carve-out: RegimeState.UNKNOWN is fixed and stable).
    - ``volatility_tier`` is the stable channel: -1 (low), 0 (normal), +1 (high).
      Derived from ``state_vars.realized_vol_percentile`` (< 0.33 -> -1,
      >= 0.67 -> +1, else 0). Survives HMM retraining and label remapping.
    - ``posterior`` is classifier confidence in the label (None if unsupported).
    - ``state_vars`` is the full feature snapshot (vol, trend, yield curve).
    - ``classifier_kind`` is one of: "rule_based", "hmm", "hmm_synthetic".
    """

    label: RegimeState
    volatility_tier: int  # -1, 0, +1
    posterior: Optional[float]
    state_vars: StateVariables
    asof: pd.Timestamp
    classifier_kind: str
    reason: Optional[str] = None  # populated for UNKNOWN; debug aid otherwise


# ---------------------------------------------------------------------------
# Detector protocol — for testability + dependency injection
# ---------------------------------------------------------------------------


class _DetectorLike(Protocol):
    """Anything implementing ``classify(state_vars) -> tuple[RegimeState, str]``.

    Both ``RegimeDetector`` and ``HMMClassifier`` (per the canonical pipeline)
    satisfy this. Tests can inject mock detectors here.
    """

    def classify(self, state_vars: StateVariables) -> tuple[RegimeState, str]: ...


# ---------------------------------------------------------------------------
# Internal helpers (patchable seams for tests)
# ---------------------------------------------------------------------------


def _build_classifier() -> _DetectorLike:
    """Build the default classifier. Patchable seam for test 2.

    Per ADR-0058: HMM is opt-in via HERMES_QUANT_REGIME_HMM=1. This helper
    delegates that decision to RegimeDetector.__init__ (which already honors
    the env var). Previously this function unconditionally tried to wire HMM,
    creating a helper↔aggregator divergence under the default rule-based
    deployment (Claude review C1, 2026-05-27).
    """
    return RegimeDetector()


def _classifier_kind(detector: _DetectorLike) -> str:
    """Best-effort classification of the detector kind for audit fields."""
    if not isinstance(detector, RegimeDetector):
        return "custom"
    if getattr(detector, "_hmm_classifier", None) is not None or \
       hasattr(detector, "hmm_classifier") and detector.__dict__.get("hmm_classifier"):
        return "hmm"
    # The canonical RegimeDetector stores the hmm under a private attr; if absent it's rule-based
    for attr_name in ("hmm_classifier", "_hmm_classifier", "_hmm"):
        if getattr(detector, attr_name, None) is not None:
            return "hmm"
    return "rule_based"


def _derive_volatility_tier(rv_percentile: Optional[float]) -> int:
    """Map realized_vol_percentile in [0, 1] to a stable tier in {-1, 0, +1}.

    Cutoffs: < 0.33 -> -1 (low vol), >= 0.67 -> +1 (high vol), else 0 (normal).
    None percentile maps to 0 (neutral) — analysts see a regime packet with
    label=UNKNOWN and tier=0.
    """
    if rv_percentile is None:
        return 0
    try:
        v = float(rv_percentile)
    except (TypeError, ValueError):
        return 0
    if v < 0.33:
        return -1
    if v >= 0.67:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_regime_extras(
    symbol: str,
    bars: pd.DataFrame,
    *,
    asof: Optional[pd.Timestamp] = None,
    min_bars: int = 61,  # matches state_variables._MIN_BARS_FOR_VOL (61 closes = 60 log-returns)
    detector: Optional[_DetectorLike] = None,
) -> dict[str, Any]:
    """Classify the regime for ``symbol`` and return a dict for MarketContext.extras.

    Args:
        symbol: Ticker (used for logging only — classifier is symbol-agnostic).
        bars: OHLCV DataFrame with at least a ``close`` column. Need >= ``min_bars``
            rows. Default 61 matches ``state_variables._MIN_BARS_FOR_VOL``;
            specifying < 61 will surface as a clean step-1 abstain rather than
            a step-3 ValueError WARNING (Claude review H4, 2026-05-27).
        asof: Optional timestamp; defaults to last bar's timestamp via
            ``compute_state_variables``.
        min_bars: Minimum bars required for classification (default 61).
        detector: Optional dependency-injected detector for testing. When
            None, ``_build_classifier()`` is called.

    Returns:
        Dict with three always-set keys:
        - ``regime``: ``RegimePacket | None``
        - ``regime_failure``: ``str | None`` (populated iff regime is None)
        - ``regime_classifier_kind``: ``str`` (always populated; "unavailable"
          when classifier construction failed)

    Per ADR-0036 silence-by-default: this function never raises. The entire
    body is wrapped in an outer try/except (Claude review H3, 2026-05-27);
    inner per-step try/except blocks are retained for granular failure
    attribution.
    """
    try:
        return _build_regime_extras_impl(symbol, bars, asof=asof, min_bars=min_bars, detector=detector)
    except Exception as exc:  # noqa: BLE001 — ADR-0036 outer guard
        logger.warning("build_regime_extras(%s): unexpected failure: %s",
                       symbol, exc, exc_info=True)
        return {
            "regime": None,
            "regime_failure": f"unexpected_error: {type(exc).__name__}: {exc}",
            "regime_classifier_kind": "unavailable",
        }


def _build_regime_extras_impl(
    symbol: str,
    bars: pd.DataFrame,
    *,
    asof: Optional[pd.Timestamp] = None,
    min_bars: int = 61,
    detector: Optional[_DetectorLike] = None,
) -> dict[str, Any]:
    """Inner implementation; outer ``build_regime_extras`` wraps in try/except."""
    # ---- guard: insufficient bars ----
    if bars is None or len(bars) < min_bars:
        n = 0 if bars is None else len(bars)
        return {
            "regime": None,
            "regime_failure": f"insufficient_bars: have {n}, need {min_bars}",
            "regime_classifier_kind": "unavailable",
        }

    # ---- step 1: build (or accept) the detector ----
    if detector is None:
        try:
            detector = _build_classifier()
        except ImportError as exc:
            return {
                "regime": None,
                "regime_failure": f"classifier_unavailable: {exc}",
                "regime_classifier_kind": "unavailable",
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("build_regime_extras(%s): classifier build failed: %s",
                           symbol, exc, exc_info=True)
            return {
                "regime": None,
                "regime_failure": f"classifier_unavailable: {exc}",
                "regime_classifier_kind": "unavailable",
            }

    # ---- step 2: compute state variables ----
    try:
        state_vars = compute_state_variables(bars)
    except Exception as exc:  # noqa: BLE001
        logger.warning("build_regime_extras(%s): state_variables failed: %s",
                       symbol, exc, exc_info=True)
        return {
            "regime": None,
            "regime_failure": f"state_variables_error: {exc}",
            "regime_classifier_kind": _classifier_kind(detector),
        }

    # ---- step 3: classify (this is where most failure happens) ----
    try:
        result = detector.classify(state_vars)
    except Exception as exc:  # noqa: BLE001
        logger.warning("build_regime_extras(%s): classify raised: %s",
                       symbol, exc, exc_info=True)
        return {
            "regime": None,
            "regime_failure": f"classify_error: {type(exc).__name__}: {exc}",
            "regime_classifier_kind": _classifier_kind(detector),
        }

    # detector.classify returns tuple[RegimeState, reason_str] per RegimeDetector
    if isinstance(result, tuple) and len(result) >= 2:
        label, reason = result[0], result[1]
    elif isinstance(result, RegimeState):
        label, reason = result, None
    else:
        return {
            "regime": None,
            "regime_failure": f"unexpected_classify_shape: {type(result).__name__}",
            "regime_classifier_kind": _classifier_kind(detector),
        }

    if not isinstance(label, RegimeState):
        return {
            "regime": None,
            "regime_failure": f"non_regime_state_label: {type(label).__name__}",
            "regime_classifier_kind": _classifier_kind(detector),
        }

    # ---- step 4: derive stable channel + assemble packet ----
    tier = _derive_volatility_tier(state_vars.realized_vol_percentile)
    kind = _classifier_kind(detector)

    packet = RegimePacket(
        label=label,
        volatility_tier=tier,
        posterior=None,  # detector API does not currently expose a posterior
        state_vars=state_vars,
        asof=state_vars.as_of,
        classifier_kind=kind,
        reason=reason if isinstance(reason, str) else None,
    )

    return {
        "regime": packet,
        "regime_failure": None,
        "regime_classifier_kind": kind,
    }


__all__ = ["RegimePacket", "build_regime_extras"]
