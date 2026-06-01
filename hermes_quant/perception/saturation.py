"""hermes_quant.perception.saturation — the SaturationScore primitive (ADR-0079 PDR-4 / GAP-C).

The Camillo EXIT-on-information-parity, expressed as a confidence DECAY multiplier
m in (0, 1] applied to the HermesSemanticAnalyst's OWN view (semantic.py:130-132),
BEFORE BMA. Silence-only (post <= pre) and view-local by construction (D79.4).

This module is PURE: no I/O, no env reads. The HERMES_QUANT_SATURATION flag-gate
lives at the two call sites (builder.py produce, semantic.py apply). asof-honest:
reads only past observations (packet.asof, the velocity peak week <= asof, the
confirm-date <= asof). Empty/unknown inputs -> m=1.0 (no decay: do NOT silence a
position you cannot prove is stale).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd

# Decay shape constants (tunable; the backtest in section 6.5 calibrates these).
_HALF_LIFE_DAYS_DEFAULT = 14.0   # post-peak edge half-life (Camillo: weeks, not months)
_FLOOR = 0.05                    # m never reaches exactly 0 from age alone; a hard confirm-date can


def compute_saturation(
    *,
    packet_asof: Any,                       # SemanticPacket.asof (ISO str or Timestamp)
    asof: pd.Timestamp,                     # decision/bar asof, UTC -- the lookahead anchor
    trend_velocity: Mapping[str, Any] | None = None,   # frame.trend_velocity (PDR-2), may be None
    confirm_date: Any | None = None,        # earnings/credit-card confirm date (metadata), may be None
    half_life_days: float = _HALF_LIFE_DAYS_DEFAULT,
) -> dict[str, Any]:
    """Return {"score": s in [0,1], "decay_multiplier": m in (0,1], "asof": iso, "basis": str}.

    score = saturation in [0,1]; m = _FLOOR + (1-_FLOOR)*decay, clamped to (0, 1].
    BASIS precedence (asof-honest, most-confident first):
      1. confirm_date passed (<= asof)            -> fully saturated, m -> _FLOOR  (hard exit)
      2. velocity peak week passed (PDR-2)        -> age-from-peak exponential decay
      3. packet age fallback (no PDR-2)           -> age-from-publication exponential decay
      4. nothing usable                           -> m = 1.0 (NO decay)
    Never raises; on any parse failure returns m=1.0 (silence-only safety: an
    un-estimable saturation must not silence a live signal).
    """
    asof_ts = _as_utc(asof)
    out_asof = asof_ts.isoformat()

    # ---- basis 1: hard confirm-date ----
    cd = _as_utc_or_none(confirm_date)
    if cd is not None and cd <= asof_ts:
        m = _FLOOR
        return {"score": round(1.0 - m, 6), "decay_multiplier": round(m, 6),
                "asof": out_asof, "basis": "confirm_date_passed"}

    # ---- basis 2: velocity peak (PDR-2) ----
    # The ONLY producer is VelocityScore.to_mapping() (velocity.py), which emits the
    # series-peak under the key "peak_period". Accept "peak_asof" too for back-compat
    # with older synthetic fixtures / mappings. Prefer whichever is present (they are
    # the same anchor); "peak_asof" wins only when both are supplied.
    peak = None
    if trend_velocity is not None:
        peak = _as_utc_or_none(
            trend_velocity.get("peak_asof", trend_velocity.get("peak_period"))
        )
    anchor, basis = (peak, "velocity_peak") if (peak is not None and peak <= asof_ts) else (None, None)

    # ---- basis 3: packet-age fallback ----
    if anchor is None:
        pub = _as_utc_or_none(packet_asof)
        if pub is not None and pub <= asof_ts:
            anchor, basis = pub, "packet_age"

    if anchor is None:
        return {"score": 0.0, "decay_multiplier": 1.0, "asof": out_asof, "basis": "no_basis"}

    age_days = max(0.0, (asof_ts - anchor).total_seconds() / 86400.0)
    decay = 0.5 ** (age_days / max(1e-9, half_life_days))   # 1.0 at age 0, ->0 with age
    m = _FLOOR + (1.0 - _FLOOR) * decay
    m = max(_FLOOR, min(1.0, m))
    return {"score": round(1.0 - m, 6), "decay_multiplier": round(m, 6),
            "asof": out_asof, "basis": basis}


def apply_saturation(confidence: float, saturation: Mapping[str, Any] | None) -> float:
    """Apply the decay multiplier to a confidence. SILENCE-ONLY: returns
    confidence * m with m in (0,1] (or confidence unchanged when saturation is
    None / malformed). Never raises; never raises confidence."""
    if saturation is None:
        return confidence
    try:
        m = float(saturation.get("decay_multiplier", 1.0))
    except (TypeError, ValueError):
        # RR14: narrowed from a bare `except Exception`. float() on a missing /
        # non-numeric decay_multiplier raises exactly TypeError (e.g. None) or
        # ValueError (e.g. "garbage"); narrowing keeps the silence-only no-op for
        # malformed input while no longer masking an unexpected programming error.
        # NaN/inf do NOT raise here -- they pass float() and are rejected by the
        # (0,1] contract guard below, so behavior is byte-identical for every
        # input the test matrix exercises.
        return confidence
    if not (0.0 < m <= 1.0):   # defensive: any out-of-contract m is treated as a no-op
        return confidence
    return float(confidence) * m


def _as_utc(ts: Any) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _as_utc_or_none(ts: Any) -> pd.Timestamp | None:
    if ts is None:
        return None
    try:
        return _as_utc(ts)
    except Exception:  # noqa: BLE001
        return None


__all__ = ["compute_saturation", "apply_saturation"]
