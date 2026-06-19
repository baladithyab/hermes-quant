"""hermes_quant.risk.ptrar — Per-Trip Risk-Adjusted Return (PTRAR) metric.

ADR-0099 Part B: PTRAR is the COMMON cross-class Sharpe denominator that places
equity and defined-risk options on an identical footing for the Decide committee.

Formula
-------
  PTRAR  = realized_pnl_usd / committed_capital_at_risk

Capital-at-risk definitions:
  Equity:          entry_price × abs_position_usd   (position notional in USD)
  Defined-risk options:  max_loss_usd               (finite MultiLegProposal.max_loss,
                                                      per ADR-0027 — naked/undefined-risk
                                                      is BLOCKED upstream; None here is
                                                      a fail-closed sentinel)

  An equity trade returning +2 % of notional and an options trade returning +2 %
  of max_loss both yield PTRAR = 0.02 — identical committee weight.

PTRAR_sharpe = mean(PTRAR) / std(PTRAR, ddof=1) × sqrt(annual_freq)
             (mirrors evaluation.validation._sharpe; None if < 2 finite observations)

Design constraints (money-software posture)
-------------------------------------------
- Finite-guard every numeric input: NaN/inf defeats every <= comparison gate
  (the ar08/ar09 family).  Non-finite or zero capital → returns None (excluded,
  not silently folded in).
- None max_loss → None (fail-CLOSED): an undefined-risk trade should never reach
  this function (ADR-0027 blocks naked), but if it does we refuse to compute
  rather than fabricate a denominator.
- This module is PURELY ADDITIVE.  Nothing on the live path calls it; a future
  Decide-stage reads PTRAR behind a flag (default-OFF until that increment ships).
- The LLM (risk/deliberation agents) MAY silence on a PTRAR value but MUST NOT
  re-rank stock vs options using it — structure_select owns that decision.

References
----------
- ADR-0099 §B: "Per-Trip Risk-Adjusted Return (PTRAR)"
- evaluation.validation._sharpe — source-of-truth annualized Sharpe shape
- MultiLegProposal.max_loss (options/multileg.py:77)
- SettledRoundTrip (daemon/settlement_loop.py:419)
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — typing-only
    from hermes_quant.daemon.settlement_loop import SettledRoundTrip

__all__ = [
    "ptrar_equity",
    "ptrar_options",
    "ptrar_sharpe",
    "ptrar_for_trip",
]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_finite_positive(value: float) -> bool:
    """True iff value is a finite, strictly-positive number."""
    return math.isfinite(value) and value > 0.0


# ---------------------------------------------------------------------------
# Public primitives
# ---------------------------------------------------------------------------


def ptrar_equity(
    realized_pnl_usd: float,
    entry_price: float,
    abs_position_usd: float,
) -> float | None:
    """PTRAR for a single equity round-trip.

    Parameters
    ----------
    realized_pnl_usd:
        Net realised P&L in USD for the matched lot (direction-signed:
        positive = profit, negative = loss).
    entry_price:
        The entry fill price per share (must be finite and > 0).
    abs_position_usd:
        The absolute position size in shares (units) at entry.  The capital
        at risk is ``entry_price × abs_position_usd`` (position notional).

    Returns
    -------
    float | None
        PTRAR (dimensionless fraction).  None when the capital denominator is
        non-finite or zero — the trip is excluded from the PTRAR series.
    """
    if not math.isfinite(realized_pnl_usd):
        return None
    if not _is_finite_positive(entry_price):
        return None
    if not _is_finite_positive(abs_position_usd):
        return None
    capital_at_risk = entry_price * abs_position_usd
    if not _is_finite_positive(capital_at_risk):
        # Overflow guard: entry_price * abs_position_usd == inf
        return None
    return realized_pnl_usd / capital_at_risk


def ptrar_options(
    realized_pnl_usd: float,
    max_loss_usd: float | None,
) -> float | None:
    """PTRAR for a single defined-risk options round-trip.

    Parameters
    ----------
    realized_pnl_usd:
        Net realised P&L in USD for the matched lot (direction-signed).
    max_loss_usd:
        The maximum possible loss in USD from ``MultiLegProposal.max_loss``
        (after Decimal → float coercion at the call site).  Must be finite
        and > 0.  None signals an undefined-risk structure — fail-CLOSED
        (returns None regardless of the P&L).

    Returns
    -------
    float | None
        PTRAR (dimensionless fraction).  None when max_loss is None, non-finite,
        or zero — the trip is excluded.

    Notes
    -----
    ADR-0027 blocks naked / undefined-risk structures upstream, so
    ``max_loss_usd is None`` should be unreachable in production.  This
    function returns None (fail-closed) rather than raising so the committee
    silently skips the malformed trip without crashing.
    """
    if not math.isfinite(realized_pnl_usd):
        return None
    if max_loss_usd is None:
        return None
    if not _is_finite_positive(max_loss_usd):
        return None
    return realized_pnl_usd / max_loss_usd


def ptrar_sharpe(
    ptrar_series: "list[float | None] | tuple[float | None, ...]",
    *,
    annual_freq: float,
) -> float | None:
    """Annualised PTRAR Sharpe ratio across a series of round-trips.

    PTRAR_sharpe = mean(PTRAR) / std(PTRAR, ddof=1) × sqrt(annual_freq)

    Mirrors ``evaluation.validation._sharpe`` (the source-of-truth shape).
    None / non-finite values in the series are silently excluded (they
    represent trips where capital could not be computed — the ADR-0099 §B
    exclusion rule).

    Parameters
    ----------
    ptrar_series:
        Iterable of per-trip PTRAR floats; None entries are excluded.
    annual_freq:
        Annualisation factor — number of trips per year (e.g. 252 for daily
        equity, 52 for weekly, or an empirical trips-per-year count).
        Must be finite and > 0; returns None if not.

    Returns
    -------
    float | None
        Annualised PTRAR Sharpe.  None if:
        - annual_freq is non-finite or zero,
        - fewer than 2 finite PTRAR observations remain after exclusion.
    """
    if not _is_finite_positive(annual_freq):
        return None
    # Filter to finite values only (exclude None + inf + nan trips).
    finite_pts: list[float] = [
        v for v in ptrar_series if v is not None and math.isfinite(v)
    ]
    n = len(finite_pts)
    if n < 2:
        return None
    # Avoid numpy to keep this module dependency-light; stdlib statistics or
    # manual computation is fine — the values are already Python floats.
    mean_v = sum(finite_pts) / n
    variance = sum((x - mean_v) ** 2 for x in finite_pts) / (n - 1)  # ddof=1
    std_v = math.sqrt(variance)
    if std_v == 0.0 or math.isnan(std_v):
        # Degenerate: all identical PTRAR values.  Mirror _sharpe convention:
        # zero mean → 0.0, non-zero mean → signed infinity.
        if mean_v == 0.0:
            return 0.0
        return math.inf if mean_v > 0.0 else -math.inf
    result = mean_v / std_v * math.sqrt(annual_freq)
    if not math.isfinite(result):
        return None  # overflow guard
    return result


# ---------------------------------------------------------------------------
# Unified dispatch entry
# ---------------------------------------------------------------------------

# Asset-class strings the settlement_loop / reactor bus emit.
_EQUITY_CLASSES = frozenset({"equity", "us_equity", "crypto"})
_OPTION_CLASSES = frozenset({"us_option"})
_MULTILEG_PARENT = "multi_leg"  # parent-only sentinel — no real position


def ptrar_for_trip(
    trip: "SettledRoundTrip",
    *,
    max_loss_usd: float | None = None,
) -> float | None:
    """Unified PTRAR entry: compute PTRAR for any SettledRoundTrip.

    Dispatches on ``trip.asset_class`` / structure:

    - Equity (asset_class in {"equity", "us_equity", "crypto"}):
        capital_at_risk = trip.entry_price × trip.qty × trip.notional_multiplier
        realized_pnl_usd is reconstructed from trip.realized_return.

    - Defined-risk options (asset_class == "us_option"):
        capital_at_risk = max_loss_usd (caller MUST provide this from the
        matched MultiLegProposal.max_loss before calling).  Returns None
        (fail-CLOSED) if max_loss_usd is None or non-finite.

    - Multi-leg parent (asset_class == "multi_leg"):
        Not a real position — returns None.  Callers should iterate over the
        CHILD legs instead.

    Risk/deliberation agents read this metric; the LLM may silence on the
    computed value but MUST NOT re-rank stock vs options using it —
    structure_select owns that decision.

    Parameters
    ----------
    trip:
        A SettledRoundTrip (from daemon/settlement_loop.py).
    max_loss_usd:
        Required for the options path.  Caller retrieves this from the
        MultiLegProposal that generated the trip (float(proposal.max_loss)).
        Ignored for equity trips.

    Returns
    -------
    float | None
        PTRAR for this trip, or None if excluded (non-finite capital / unknown
        asset class / multi_leg parent / undefined-risk options).
    """
    asset_class = trip.asset_class

    # Multi-leg parent rows are accounting artefacts; skip them.
    if asset_class == _MULTILEG_PARENT:
        return None

    # Reconstruct realized P&L in USD from the holding-period return.
    # realized_return is the net fractional return on entry_price per share.
    # For a long lot: realized_return = (exit - entry) / entry (net of fees).
    # For a short lot: realized_return = (entry - exit) / entry (net of fees).
    # In both cases: pnl_usd = realized_return × entry_price × abs_units × multiplier.
    entry_price = trip.entry_price
    qty = trip.qty  # always > 0 (matched quantity)
    multiplier = trip.notional_multiplier  # 100 for us_option; 1 for equity
    realized_return = trip.realized_return

    if not math.isfinite(realized_return):
        return None
    if not _is_finite_positive(entry_price) or not _is_finite_positive(qty):
        return None

    realized_pnl_usd = realized_return * entry_price * qty * multiplier

    if asset_class in _EQUITY_CLASSES:
        # capital_at_risk = entry_price × qty × multiplier  (same as denominator)
        abs_position = qty * multiplier  # position size in shares (equity) or units
        return ptrar_equity(realized_pnl_usd, entry_price, abs_position)

    if asset_class in _OPTION_CLASSES:
        return ptrar_options(realized_pnl_usd, max_loss_usd)

    # Unknown asset class — fail-closed rather than fabricate.
    return None
