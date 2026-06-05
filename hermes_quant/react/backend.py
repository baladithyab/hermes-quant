"""hermes_quant.react.backend — pluggable broker-backend abstraction (ADR-0088).

A ``BrokerBackend`` owns the *venue mechanics* for one execution target: account
state (equity / buying power), order submission (equity + options), and the fill
result. Reactors (``PaperReactor`` equity, ``MultiLegPaperReactor`` options) become
thin orchestrators that keep their precondition chains (admissibility, gate-is-final,
idempotency, slippage, reconciliation) and delegate the fill to a selected backend.

Two backends ship:
  * ``DeterministicBackend`` — a correctness-complete LOCAL SIMULATOR (no network,
    no creds). Enforces buying power, tracks TRUE units, fills against last-known
    decision prices. The fallback ANY exchange config routes to with no live API
    (operator requirement: "exchange configurations without direct API access").
  * ``AlpacaBackend`` — wraps the Alpaca paper TradingClient for equity AND options
    (OrderClass.MLEG + OptionLegRequest), reusing the existing auth pattern.

Selection (``select_backend``): Alpaca when ``HERMES_QUANT_ALPACA_PAPER=1`` AND creds
are present, else the deterministic simulator. ``HERMES_QUANT_BROKER_BACKEND`` is an
explicit override (``deterministic`` | ``alpaca``).

Rails (ADR-0088): the backend NEVER re-runs the gate or widens a size; fail-closed on
unknown NAV / BP / submit reject (clear raise or no-fill, never a fabricated fill);
true units in ``FillResult.filled_qty``; live (real-money) stays behind
``LiveTradingApproval`` (ADR-0029 D7) — backends here are paper/simulator only.

The ``FillResult`` shape intentionally mirrors the existing ``LegFill`` vocabulary
(``react/mleg_fill.py``) so record-building + reconciliation stay backend-independent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# Backend selection flags (ADR-0088).
ALPACA_PAPER_FLAG = "HERMES_QUANT_ALPACA_PAPER"
BACKEND_OVERRIDE_FLAG = "HERMES_QUANT_BROKER_BACKEND"

BACKEND_DETERMINISTIC = "deterministic"
BACKEND_ALPACA = "alpaca"


@dataclass(frozen=True)
class FillResult:
    """Backend-agnostic fill detail for one order (equity, single-leg, or mleg parent).

    Mirrors ``react.mleg_fill.LegFill`` plus the multi-leg parent fields so the
    same shape covers an equity fill, a single-leg option fill, and a multi-leg
    parent fill. ``legs`` is empty for a single equity/option order and carries the
    per-leg detail for an mleg fill.

    Honesty rails:
      * ``filled_qty`` is in TRUE units (signed shares for equity, signed contracts
        for options) — never NAV-fractions.
      * a no-fill / unfilled-timeout yields ``status`` in the reject family and
        ``filled_qty == 0.0`` / ``filled_avg_price == 0.0`` — NEVER a fabricated
        price. The caller treats a zero-fill as a non-position-moving event.
    """

    symbol: str  # OCC-21 (option) or ticker (equity)
    filled_avg_price: float  # broker-reported avg (or simulated decision price); 0.0 on no-fill
    filled_qty: float  # signed TRUE units; + long / - short; 0.0 on no-fill
    status: str  # 'filled' | 'partially_filled' | 'rejected' | 'expired' | 'unfilled_timeout'
    position_intent: str | None = None
    order_id: str | None = None  # venue order id (audit / later reconciliation)
    legs: tuple[FillResult, ...] = field(default_factory=tuple)  # mleg per-leg detail
    source: str = ""  # backend name that produced the fill
    net_fill_price: float | None = None  # signed net debit/credit for an mleg parent

    @property
    def is_fill(self) -> bool:
        """True iff the order actually moved a position (full or partial)."""
        return self.status in {"filled", "partially_filled"} and self.filled_qty != 0.0


class InsufficientBuyingPowerError(RuntimeError):
    """A simulated/real order exceeded available buying power. Fail-closed: the
    backend REFUSES the fill (never silently over-leverages). This is the
    deterministic-backend equivalent of a broker BP rejection — a legitimate,
    surfaced outcome, not an error to swallow."""


class BackendUnavailableError(RuntimeError):
    """The selected backend cannot operate (missing creds / unavailable client).
    Fail-closed so a flag-on run without a usable backend fails loud."""


@runtime_checkable
class BrokerBackend(Protocol):
    """Venue mechanics for one execution target (ADR-0088).

    Implementations: ``DeterministicBackend`` (local simulator) and
    ``AlpacaBackend`` (Alpaca paper, equity + options).
    """

    name: str

    def account_equity(self) -> float | None:
        """Account NAV (USD) for sizing, or None if unknown (caller fails closed)."""
        ...

    def buying_power(self) -> float | None:
        """Available buying power (USD), or None if unknown."""
        ...

    def submit_equity(
        self,
        *,
        symbol: str,
        signed_qty: float,
        decision_price: float,
        client_order_id: str,
    ) -> FillResult:
        """Submit an equity order. ``signed_qty`` is signed TRUE shares (+long/-short)."""
        ...

    def submit_option_single(
        self,
        leg: Any,  # hermes_quant.options.data.OptionLeg
        *,
        qty: int,
        limit_price: float,
        client_order_id: str,
    ) -> FillResult:
        """Submit a single-leg option order (CC short call / CSP short put)."""
        ...

    def submit_option_mleg(
        self,
        option_legs: tuple[Any, ...],  # tuple[OptionLeg, ...]
        *,
        outer_qty: int,
        net_limit_price: float,
        client_order_id: str,
    ) -> FillResult:
        """Submit a >=2-leg option structure (vertical/condor/PMCC/roll)."""
        ...


def resolve_backend_choice() -> str:
    """Decide which backend to use from config/flags (ADR-0088).

    Priority:
      1. Explicit ``HERMES_QUANT_BROKER_BACKEND`` override (deterministic | alpaca).
      2. Alpaca when ``HERMES_QUANT_ALPACA_PAPER=1`` AND creds present.
      3. Deterministic simulator (the default fallback).
    """
    override = (os.environ.get(BACKEND_OVERRIDE_FLAG) or "").strip().lower()
    if override in {BACKEND_DETERMINISTIC, BACKEND_ALPACA}:
        return override
    if os.environ.get(ALPACA_PAPER_FLAG, "0") == "1" and _alpaca_creds_present():
        return BACKEND_ALPACA
    return BACKEND_DETERMINISTIC


def _alpaca_creds_present() -> bool:
    key = os.environ.get("ALPACA_API_KEY") or os.environ.get("ALPACA_API_KEY_ID")
    secret = os.environ.get("ALPACA_API_SECRET") or os.environ.get("ALPACA_API_SECRET_KEY")
    return bool(key and secret)


def select_backend(*, client: Any | None = None) -> BrokerBackend:
    """Return the configured BrokerBackend (ADR-0088).

    ``client`` is an injectable Alpaca TradingClient (tests pass a fake); ignored
    by the deterministic backend. Auto-selects per ``resolve_backend_choice()``.
    """
    choice = resolve_backend_choice()
    if choice == BACKEND_ALPACA:
        from .backends.alpaca_backend import AlpacaBackend

        return AlpacaBackend(client=client)
    from .backends.deterministic_backend import DeterministicBackend

    return DeterministicBackend()
