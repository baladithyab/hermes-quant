"""hermes_quant.regime.per_regime_weights — Wave 7 per-regime BMA weight multipliers.

Provides:
  - RegimeWeightTable: type alias for per-regime analyst weight multiplier tables.
  - DEFAULT_REGIME_WEIGHTS: hardcoded priors from Mantshimuli & Mwamba (2026).
  - apply_regime_weights(base_weights, regime, table): multiply base weights by
    the regime-specific multipliers (missing analyst names default to 1.0).
  - load_regime_weights(path): load from JSON, falling back to DEFAULT on error.
  - save_regime_weights(table, path): persist to JSON.

Weight-multiplier invariant (from ADR-0047):
    Regime multipliers NEVER change the sign of or zero out a weight.
    Multipliers are positive floats; the floor is enforced in apply_regime_weights.
    This preserves the identity of each analyst signal — regime awareness modulates
    confidence, not analyst inclusion (IC dedup handles exclusion separately).

Reference: Mantshimuli & Mwamba, Springer 2026, §5.4 "Regime-conditional priors".
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from hermes_quant.regime.detector import RegimeState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type alias
# ---------------------------------------------------------------------------

# dict[RegimeState, dict[analyst_name, float]]
RegimeWeightTable = dict[RegimeState, dict[str, float]]

# Default persistence path
_DEFAULT_WEIGHTS_PATH = (
    Path.home() / ".hermes" / "quant" / "regime" / "weights.json"
)

# Minimum multiplier value — weights can be suppressed but never zeroed.
_MIN_MULTIPLIER = 1e-6

# ---------------------------------------------------------------------------
# DEFAULT_REGIME_WEIGHTS
# ---------------------------------------------------------------------------

# Priors from Mantshimuli & Mwamba, Springer 2026, §5.4, Table 3.
# Rationale per regime:
#   BULL:     Sentiment (momentum) gets a small boost; TA slightly suppressed
#             (trend-following less discriminative in a clean bull run);
#             fundamentals mildly boosted (mean-reversion opportunity smaller).
#   BEAR:     Sentiment sharply suppressed (contrarian signal, high noise);
#             classical TA boosted (support/resistance more actionable in falls);
#             fundamentals stable (fair-value floor still matters).
#   VOLATILE: Sentiment very noisy → heavily suppressed; TA most actionable
#             (breakouts / reversals visible on charts); semantic search
#             and fundamentals suppressed (delayed reaction in high noise);
#             kronos boosted slightly (high-frequency signal useful in spikes).
#   UNKNOWN:  All 1.0 — no adjustment; equivalent to the pre-Wave-7 baseline.
DEFAULT_REGIME_WEIGHTS: RegimeWeightTable = {
    RegimeState.BULL: {
        "semantic": 1.0,
        "sentiment": 1.2,
        "classical_ta": 0.9,
        "fundamentals": 1.1,
        "kronos": 1.0,
    },
    RegimeState.BEAR: {
        "semantic": 1.0,
        "sentiment": 0.6,
        "classical_ta": 1.3,
        "fundamentals": 1.1,
        "kronos": 1.0,
    },
    RegimeState.VOLATILE: {
        "semantic": 0.7,
        "sentiment": 0.4,
        "classical_ta": 1.5,
        "fundamentals": 0.8,
        "kronos": 1.2,
    },
    RegimeState.UNKNOWN: {
        "semantic": 1.0,
        "sentiment": 1.0,
        "classical_ta": 1.0,
        "fundamentals": 1.0,
        "kronos": 1.0,
    },
}

# ---------------------------------------------------------------------------
# apply_regime_weights
# ---------------------------------------------------------------------------


def apply_regime_weights(
    base_weights: dict[str, float],
    regime: RegimeState,
    table: RegimeWeightTable | None = None,
) -> dict[str, float]:
    """Multiply base BMA weights by regime-specific multipliers.

    For any analyst not present in the regime row of the table the multiplier
    defaults to 1.0 (no adjustment — safe for unknown analysts added after the
    table was last updated).

    Multiplier invariant: result weights are floored at _MIN_MULTIPLIER so that
    regime adjustment can suppress but never zero out a signal.  (Zeroing a
    weight is the job of IC dedup, not regime conditioning.)

    Args:
        base_weights: Dict of {analyst_name: weight} as produced by BMA's
            internal _weight_for() loop.
        regime: Current RegimeState from RegimeDetector.classify().
        table: Override table.  If None, uses DEFAULT_REGIME_WEIGHTS.

    Returns:
        New dict of {analyst_name: adjusted_weight}.  Original dict is not
        mutated.
    """
    if table is None:
        table = DEFAULT_REGIME_WEIGHTS

    # Fallback: if regime not in table at all, use UNKNOWN row if present
    if regime not in table:
        regime_row: dict[str, float] = table.get(RegimeState.UNKNOWN, {})
        logger.warning(
            "per_regime_weights: regime %r not found in table; using UNKNOWN row", regime
        )
    else:
        regime_row = table[regime]

    result: dict[str, float] = {}
    for analyst, w in base_weights.items():
        multiplier = float(regime_row.get(analyst, 1.0))
        adjusted = max(w * multiplier, _MIN_MULTIPLIER)
        result[analyst] = adjusted

    return result


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _regime_state_to_str(regime: RegimeState) -> str:
    return regime.value  # "bull", "bear", "volatile", "unknown"


def _str_to_regime_state(s: str) -> RegimeState:
    return RegimeState(s.lower())


def save_regime_weights(
    table: RegimeWeightTable,
    path: Path = _DEFAULT_WEIGHTS_PATH,
) -> None:
    """Persist a RegimeWeightTable to JSON.

    Creates parent directories if missing.  Any existing file is overwritten.

    JSON format:
        {"bull": {"semantic": 1.0, ...}, "bear": {...}, ...}
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    serializable = {_regime_state_to_str(r): v for r, v in table.items()}
    with open(path, "w") as f:
        json.dump(serializable, f, indent=2)
    logger.debug("regime: saved weight table to %s", path)


def load_regime_weights(
    path: Path = _DEFAULT_WEIGHTS_PATH,
) -> RegimeWeightTable:
    """Load a RegimeWeightTable from JSON.

    Falls back to DEFAULT_REGIME_WEIGHTS on any error (missing file, bad JSON,
    schema mismatch).  Always returns a complete table (all four RegimeState
    keys present).

    Args:
        path: Path to the JSON file.  Default: ~/.hermes/quant/regime/weights.json.

    Returns:
        RegimeWeightTable with entries for all four RegimeState values.
    """
    path = Path(path)
    if not path.exists():
        logger.debug("regime: weights file not found at %s; returning defaults", path)
        return dict(DEFAULT_REGIME_WEIGHTS)

    try:
        with open(path) as f:
            raw: dict[str, Any] = json.load(f)
        table: RegimeWeightTable = {}
        for k, v in raw.items():
            state = _str_to_regime_state(k)
            if not isinstance(v, dict):
                raise ValueError(f"Expected dict for regime {k!r}, got {type(v)}")
            table[state] = {str(analyst): float(w) for analyst, w in v.items()}
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "regime: failed to load weight table from %s (%s); using defaults", path, exc
        )
        return dict(DEFAULT_REGIME_WEIGHTS)

    # Fill in any missing regime rows with UNKNOWN (all-1.0)
    for regime in RegimeState:
        if regime not in table:
            logger.warning(
                "regime: weight table missing row for %r; using all-1.0", regime
            )
            table[regime] = {k: 1.0 for k in DEFAULT_REGIME_WEIGHTS[RegimeState.UNKNOWN]}

    return table
