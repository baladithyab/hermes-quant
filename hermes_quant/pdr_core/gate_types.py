"""hermes_quant.pdr_core.gate_types — host-blind read-interfaces for the gate.

ADR-0092 Increment-1-cont, STAGE 2. The host-agnostic deterministic risk gate
(landing next in :mod:`hermes_quant.pdr_core.gate`) consumes a NARROW read-surface
of the live :mod:`hermes_quant.protocol` money types and emits a verdict. This
module lifts that read-surface into the core as frozen, host-blind dataclasses /
Protocols, plus :class:`GateDecision` — the gate's OUTPUT type.

The four read-interfaces mirror exactly the fields/methods the live
``risk/gate.py`` reaches for (nothing wider):

  - :class:`CoreMarketState` — ``asset``, ``asof``, ``volatility``, ``commission``,
    ``spread``, ``slippage_estimate``, ``tz``. (Cost gate + Kelly variance + the
    daily-loss session reset all read these.)
  - :class:`CorePortfolio` — ``account_id`` / ``asset_class`` / ``asof`` plus the
    THREE derived money-state reads the gate calls: ``drawdown_pct`` (Rule 1),
    ``daily_loss_pct`` (Rule 2), ``current_position_pct(asset)`` (Rule 7 + event
    guard).
  - :class:`CoreHaltState` — a runtime-checkable Protocol exposing ``is_halted``
    (Rule 0). A host shell's halt registry satisfies it structurally.
  - :class:`GateDecision` — the gate OUTPUT, carrying the FULL halt triple.

NaN-FAIL-CLOSED PARITY (money-safety, ADR-0004 + protocol.py deep-review
2026-06-07): ``CorePortfolio``'s three derived reads are a BIT-FOR-BIT lift of
``protocol.Portfolio``'s property bodies, including the ``_finite_or`` sentinel.
A non-finite peak/equity must NEVER launder into a benign ``0.0`` drawdown — that
would let the gate emit a trade instead of flattening. The fail-closed reads
return a sentinel ABOVE any plausible threshold so the Rule-1/2 circuit breaker
trips on unknowable state. The parity is proven against the live
``protocol.Portfolio`` over a fixture matrix in ``tests/pdr_core/test_gate_types``.

GateDecision vs Proposal (the riskiest coupling): the gate's verdict carries a
durable-HALT triple (``halt`` / ``halt_scope`` / ``halt_until``) that
:class:`~hermes_quant.pdr_core.contracts.Proposal` has NO fields for. Collapsing
the verdict onto a bare Proposal would SILENTLY DROP the halt — a money-safety
regression. ``GateDecision`` is the halt-triple-preserving type; the host shell
maps it onto ``protocol.Action`` (which carries the identical triple).

PURITY: stdlib only (math / dataclasses / collections.abc / typing). Timestamps
are typed ``Any`` so a shell may pass an ISO-8601 string or a ``pandas.Timestamp``
without the core importing pandas. No host/infra import — the purity gate
(``tests/pdr_core/test_contract_purity.py``) stays green.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


def _finite_or(value: Any, fallback: float) -> float:
    """Return ``float(value)`` only if finite; else ``fallback``.

    VERBATIM lift of ``hermes_quant.protocol._finite_or`` (deep-review
    2026-06-07). NaN-fail-open defense: callers pass a fallback that is the SAFE /
    max-risk value for their context, so a non-finite (NaN/inf) account-state
    field can never be laundered into a benign finite number that bypasses the
    risk gate's own finite checks. Used by the drawdown / daily-loss /
    position-pct reads below.
    """
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return f if math.isfinite(f) else fallback


# ---------------------------------------------------------------------------
# CoreSignal — the host-blind PERCEPTION read-surface the gate consumes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoreSignal:
    """The aggregated, calibrated directional view the gate sizes from.

    Mirrors EXACTLY the surface ``risk/gate.py`` reads off
    ``protocol.AggregatedSignal`` — no wider:
      - ``direction`` in {-1, 0, +1} (Rule 3 flat check, edge sign, sizer).
      - ``confidence`` — CALIBRATED probability in [0, 1] (cost gate + Kelly).
      - ``magnitude`` — expected return as a fraction (abs-valued by the edge fn).
      - ``asset`` / ``asset_class`` — partition + Rule-7 position-pct lookup.
      - ``asof`` — decision timestamp (cooldown elapsed + audit asof).
      - ``metadata`` — carries ``id`` (→ GateDecision.signal_id) and the ADR-0084
        ``event_risk`` payload the (default-off) event guard inspects.

    The live gate ALSO reads ``signal.components`` for the ADR-0033 lookahead
    check — but that check is DROPPED TO THE SHELL (coupling edit c): the core
    gate imports no evidence module and accepts PRE-FILTERED views, so
    ``CoreSignal`` deliberately omits ``components``. A host shell runs its own
    lookahead filter upstream and only constructs a CoreSignal for views that
    pass.

    ``asof`` is typed ``Any`` (ISO str or pandas.Timestamp). Cooldown elapsed
    arithmetic happens against ``CorePortfolio.asof`` downstream where pandas is
    available.
    """

    asset: str
    asset_class: str
    asof: Any
    direction: Any  # -1 | 0 | +1
    magnitude: float
    confidence: float  # CALIBRATED probability in [0, 1]
    metadata: Mapping[str, Any] | None = None


# ---------------------------------------------------------------------------
# CoreMarketState — per-asset cost + risk environment the gate reads.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoreMarketState:
    """Per-asset cost + risk environment at decision time (gate read-surface).

    Mirrors the fields ``risk/gate.py`` reads off ``protocol.MarketState``:
      - ``volatility`` — per-period stdev of LOG returns; Kelly variance is
        ``volatility ** 2`` (ADR-0009 §P0-1).
      - ``commission`` / ``spread`` / ``slippage_estimate`` — round-trip cost
        fractions feeding the cost-gate threshold.
      - ``tz`` — for the daily-loss session reset (``_next_session_open``).

    ``asof`` is typed ``Any`` (ISO str or pandas.Timestamp) so the core needs no
    pandas import; the gate's session-reset arithmetic happens in the host shell
    against a real timestamp.
    """

    asset: str
    asof: Any
    volatility: float  # per-period stdev of LOG returns (NOT variance)
    commission: float  # round-trip cost fraction
    spread: float  # round-trip cost fraction
    slippage_estimate: float  # cost fraction
    tz: str = "UTC"  # daily-loss session reset


# ---------------------------------------------------------------------------
# CorePortfolio — the per-(account, asset_class) money-state read-surface.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CorePortfolio:
    """Per-(account, asset_class) portfolio read-surface the gate consumes.

    Exposes ONLY what ``risk/gate.py`` reads: the partition identity
    (``account_id`` / ``asset_class`` / ``asof``) and the three derived
    money-state reads (drawdown / daily-loss / position-fraction). The underlying
    state fields (``equity_total`` / ``peak_equity`` / ``daily_open_equity`` /
    ``positions``) are kept so the derived reads can compute the SAME arithmetic
    as ``protocol.Portfolio`` — this is a read-interface, not a full ledger.

    The three derived reads are a VERBATIM lift of ``protocol.Portfolio`` (same
    ``_finite_or`` NaN-fail-CLOSED sentinels). Parity is proven against the live
    type in ``tests/pdr_core/test_gate_types``.
    """

    account_id: str
    asset_class: str
    asof: Any
    positions: Mapping[str, Any]
    equity_total: float
    peak_equity: float
    daily_open_equity: float

    @property
    def drawdown_pct(self) -> float:
        # NaN-fail-CLOSED (VERBATIM from protocol.Portfolio.drawdown_pct): a
        # non-finite peak_equity or equity_total must NOT be laundered into a
        # benign 0.0 drawdown — that would let the gate emit a trade instead of
        # flattening. Return a sentinel ABOVE any plausible max_drawdown_pct so
        # the Rule-1 circuit breaker trips (flatten + halt) on unknowable state.
        peak = _finite_or(self.peak_equity, fallback=float("nan"))
        eq = _finite_or(self.equity_total, fallback=float("nan"))
        if not (math.isfinite(peak) and math.isfinite(eq)) or peak <= 0:
            return 0.0 if (math.isfinite(peak) and peak <= 0) else 1.0
        return max(0.0, (peak - eq) / peak)

    @property
    def daily_loss_pct(self) -> float:
        # NaN-fail-CLOSED (VERBATIM from protocol.Portfolio.daily_loss_pct): same
        # rationale as drawdown_pct. Non-finite inputs return a sentinel above
        # any plausible max_daily_loss_pct.
        base = _finite_or(self.daily_open_equity, fallback=float("nan"))
        eq = _finite_or(self.equity_total, fallback=float("nan"))
        if not (math.isfinite(base) and math.isfinite(eq)) or base <= 0:
            return 0.0 if (math.isfinite(base) and base <= 0) else 1.0
        return max(0.0, (base - eq) / base)

    def current_position_pct(self, asset: str) -> float:
        """Position size as fraction of total equity. 0 if no position.

        VERBATIM from ``protocol.Portfolio.current_position_pct``. NaN-fail-CLOSED:
        a non-finite qty / mark_price / equity_total returns a non-finite sentinel
        so the gate's ``_is_finite_number(current)`` check trips
        ``_flatten_nonfinite_portfolio`` rather than acting on garbage.

        Reads ``pos.qty`` / ``pos.mark_price`` by attribute, so any object exposing
        those (the live ``protocol.Position`` or a shell-supplied equivalent)
        satisfies the read-interface.
        """
        pos = self.positions.get(asset)
        eq = _finite_or(self.equity_total, fallback=float("nan"))
        if pos is None or not math.isfinite(eq) or eq <= 0:
            return 0.0 if (pos is None and math.isfinite(eq)) else (0.0 if pos is None else float("nan"))
        qty = _finite_or(pos.qty, fallback=float("nan"))
        mark = _finite_or(pos.mark_price, fallback=float("nan"))
        if not (math.isfinite(qty) and math.isfinite(mark)):
            return float("nan")
        return (qty * mark) / eq


# ---------------------------------------------------------------------------
# CoreHaltState — read-only access to the durable halt registry (Rule 0).
# ---------------------------------------------------------------------------


@runtime_checkable
class CoreHaltState(Protocol):
    """Read-only halt-registry interface the gate's Rule 0 consults.

    Mirrors the ``is_halted`` read on ``protocol.HaltState`` — the ONLY method the
    gate calls. A host shell's halt registry (SQLite-backed in hermes-quant)
    satisfies this structurally; the core never constructs one.

    Halts are NEVER cleared by trading signals (ADR-0009 §P0-4) — they are cleared
    only by an operator resume or a ``halted_until`` timestamp passing. The core
    only READS halt state; it never mutates it.
    """

    def is_halted(self, account_id: str, asset_class: str, asset: str | None = None) -> bool: ...


# ---------------------------------------------------------------------------
# GateDecision — the gate OUTPUT (halt-triple preserving).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateDecision:
    """The deterministic gate's verdict — the halt-triple-preserving OUTPUT type.

    Field-for-field mirror of ``protocol.Action`` (the live gate emit), kept
    host-blind in the core so the shell maps ``GateDecision -> protocol.Action``
    mechanically. ``None`` from the gate means silence (do nothing); a
    ``GateDecision`` means a sized verdict OR a flatten+halt verdict.

    THE RISKIEST COUPLING (money-safety): a drawdown (Rule 1) or daily-loss
    (Rule 2) breaker emits ``target_position_pct=0.0`` AND the durable-HALT triple
    ``halt`` / ``halt_scope`` / ``halt_until``. ``pdr_core.Proposal`` has NO halt
    fields — collapsing the verdict onto a Proposal would SILENTLY DROP the halt
    verdict, letting trading resume after a circuit breaker = money regression.
    ``GateDecision`` carries the full triple so the verdict survives the core
    boundary intact (ADR-0004: the gate is the FINAL flatten + halt authority).

    Fields (semantics inherited from ``protocol.Action``):
      - ``target_position_pct`` — signed NAV fraction; ``0.0`` on a flatten verdict.
      - ``reason`` — human-readable justification (mirrors the live reason strings).
      - ``signal_id`` — links to the AggregatedSignal that drove this (or None).
      - ``halt`` — if True, also enter halt for ``halt_scope``.
      - ``halt_scope`` — ``(account_id, asset_class, asset?)`` or None.
      - ``halt_until`` — daily-loss auto-clear timestamp (ISO str or
        pandas.Timestamp); ``None`` = until explicit resume. Typed ``Any`` so the
        core imports no pandas.
    """

    target_position_pct: float
    reason: str
    signal_id: str | None = None
    halt: bool = False
    halt_scope: tuple[str, str, str | None] | None = None
    halt_until: Any = field(default=None)
