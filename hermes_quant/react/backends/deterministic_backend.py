"""hermes_quant.react.backends.deterministic_backend — local trading simulator.

``DeterministicBackend`` is the correctness-complete LOCAL fallback engine for any
exchange config with no live API (ADR-0088). It is a TRUSTWORTHY simulator, NOT an
append-log: it ENFORCES buying power before admitting a fill, tracks TRUE signed
units, and NEVER fabricates a price. The old append-log path never checked BP — it
let gross exposure run to ~880%. This backend fails closed instead.

Design (v1):
  * NAV / equity source: ``state.portfolio_state`` cash row for ``paper-default``
    (``equity_total``), mirroring ``react.paper._account_nav_usd`` and
    ``react.multileg._account_nav_usd`` so all three seams agree. Falls back to the
    bootstrap initial cash; returns ``None`` on any failure (fail-closed — the caller
    must NOT assume infinite NAV).
  * Buying power: a simple, HONEST cash-account model — BP == current free cash
    (``cash.balance_usd``). No simulated margin in v1 (documented limitation). A
    real margin model is a later wave; under-stating BP is the conservative default.
  * Buying-power enforcement: an equity order's notional (|qty| * price) and an
    option net-DEBIT (premium paid * 100 * qty) are checked against BP. Exceed it ->
    ``InsufficientBuyingPowerError``. Unknown BP -> ``BackendUnavailableError`` (we
    refuse to fabricate a fill against an unknown account). A net CREDIT (short
    option / credit spread) is NOT BP-blocked here — the collateral check is the
    upstream gate's job (``options_gate``); blocking a credit on cash BP would be
    wrong (the position receives premium).
  * Fills are deterministic: equity fills at ``decision_price``, option legs at their
    recorded decision mid (``leg.fill_price``) else the limit/apportioned net. NO RNG
    anywhere, so replays match byte-for-byte. Slippage is applied by the REACTOR on
    top (ADR-0070), NOT here — the backend fill stays at the decision price.

This module imports NO network client and performs NO I/O beyond the local state DB
read used by every other reactor seam.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from ..backend import (
    BACKEND_DETERMINISTIC,
    BackendUnavailableError,
    FillResult,
    InsufficientBuyingPowerError,
)

logger = logging.getLogger(__name__)

# Tiny tolerance so a notional that equals BP to the penny (float rounding) is not
# spuriously rejected. Anything materially over BP is still refused.
_BP_EPSILON = 1e-6

# Equity-options contract multiplier (one option contract controls 100 shares).
_CONTRACT_MULTIPLIER = 100.0

# The single paper account these seams agree on (see paper.py / multileg.py).
_ACCOUNT_ID = "paper-default"


class DeterministicBackend:
    """Correctness-complete local trading simulator (``name='deterministic'``).

    Implements the ``BrokerBackend`` protocol with NO network and NO creds. Enforces
    buying power, tracks true signed units, and fails closed on unknown account
    state. See module docstring for the BP/margin model.
    """

    name: str = BACKEND_DETERMINISTIC

    # ------------------------------------------------------------------
    # Account state (fail-closed: unknown => None / raise, never fabricate)
    # ------------------------------------------------------------------
    def account_equity(self) -> float | None:
        """Account NAV (USD) for sizing, or ``None`` on any failure (fail-closed).

        Mirrors ``react.paper._account_nav_usd`` / ``react.multileg._account_nav_usd``
        so the simulator's NAV matches the reactor admissibility seam exactly. Source
        priority: materialized ``cash.equity_total`` (truth after fills), else the
        bootstrap initial cash. Returns ``None`` (not 0.0) on failure so the caller
        fails closed rather than sizing against a fabricated NAV.
        """
        try:
            from hermes_quant.state.portfolio_state import (
                _default_initial_cash,
                get_portfolio_state,
            )

            cash = get_portfolio_state().get_cash(_ACCOUNT_ID)
            if cash is not None and cash.equity_total > 0:
                return float(cash.equity_total)
            boot = _default_initial_cash()
            return float(boot) if boot > 0 else None
        except Exception as exc:  # noqa: BLE001 — fail-closed: unknown NAV => None.
            logger.warning(
                "deterministic-backend: equity lookup failed (fail-closed): %s", exc
            )
            return None

    def buying_power(self) -> float | None:
        """Available buying power (USD) == current free cash, or ``None`` on failure.

        v1 cash-account model: BP is the literal free-cash balance
        (``cash.balance_usd``) — no simulated margin. If no cash row exists yet
        (pre-first-fill) we fall back to the bootstrap initial cash (all of which is
        free at t0). ``None`` on any failure so the caller fails closed (the backend
        will then refuse to fabricate a fill against an unknown account).
        """
        try:
            from hermes_quant.state.portfolio_state import (
                _default_initial_cash,
                get_portfolio_state,
            )

            cash = get_portfolio_state().get_cash(_ACCOUNT_ID)
            if cash is not None:
                return float(cash.balance_usd)
            boot = _default_initial_cash()
            return float(boot) if boot > 0 else None
        except Exception as exc:  # noqa: BLE001 — fail-closed: unknown BP => None.
            logger.warning(
                "deterministic-backend: buying-power lookup failed (fail-closed): %s",
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # BP gate (the whole point: refuse over-leverage, never fabricate)
    # ------------------------------------------------------------------
    def _require_bp(self, required_usd: float, *, what: str) -> None:
        """Enforce that ``required_usd`` (a debit) fits in available buying power.

        Raises ``BackendUnavailableError`` if BP is unknown (fail-closed: we do NOT
        assume infinite BP) and ``InsufficientBuyingPowerError`` if the debit exceeds
        BP (beyond a penny-rounding epsilon). A non-positive ``required_usd`` (a
        credit / zero-debit) is always allowed — credit collateral is the gate's job.
        """
        # ar31: a NON-FINITE required notional must fail CLOSED. nan/inf defeat every
        # comparison below (`nan <= 0` is False -> skips the credit early-return;
        # `nan > bp + eps` is False -> skips the insufficiency raise), so a NaN notional
        # would be ADMITTED past this anti-over-leverage BP rail and book a NaN-priced
        # fill. This is the LIVE deterministic-equity path (HERMES_QUANT_DETERMINISTIC_
        # EQUITY=1). A notional we cannot verify is finite is unverifiable against BP, so
        # refuse it as a backend fault (mirrors the unknown-BP fail-closed below).
        if not math.isfinite(required_usd):
            raise BackendUnavailableError(
                f"deterministic-backend: non-finite required notional ({required_usd!r}) "
                f"for {what}; refusing the fill (fail-closed - cannot verify buying power)"
            )
        if required_usd <= 0:
            return
        bp = self.buying_power()
        if bp is None:
            raise BackendUnavailableError(
                f"deterministic-backend: buying power unknown; refusing {what} "
                "(fail-closed — never assume infinite BP)"
            )
        if required_usd > bp + _BP_EPSILON:
            raise InsufficientBuyingPowerError(
                f"deterministic-backend: {what} requires ${required_usd:,.2f} but only "
                f"${bp:,.2f} buying power is available; refusing the fill"
            )

    # ------------------------------------------------------------------
    # Submit: equity (CORRECTNESS-COMPLETE — enforces BP)
    # ------------------------------------------------------------------
    def submit_equity(
        self,
        *,
        symbol: str,
        signed_qty: float,
        decision_price: float,
        client_order_id: str,
    ) -> FillResult:
        """Submit an equity order at ``decision_price``, enforcing buying power.

        ``signed_qty`` is signed TRUE shares (+long / -short). A BUY (signed_qty>0)
        consumes cash buying power; its notional (|qty| * price) is checked against
        BP — exceed it -> ``InsufficientBuyingPowerError``; unknown BP ->
        ``BackendUnavailableError``. A SELL (signed_qty<=0) is NOT cash-BP-checked
        (ADR-0088 F4): selling to close a long RAISES cash, and opening a short is a
        margin/collateral question that the upstream admissibility gate owns, not a
        free-cash check. On pass: a deterministic ``filled`` result at the decision
        price (the reactor applies ADR-0070 slippage on top).
        """
        if signed_qty > 0:
            notional = abs(signed_qty) * decision_price
            self._require_bp(notional, what=f"equity {symbol} ({signed_qty:+g} sh)")
        return FillResult(
            symbol=symbol,
            filled_avg_price=decision_price,
            filled_qty=float(signed_qty),
            status="filled",
            position_intent="buy_to_open" if signed_qty > 0 else "sell_to_open",
            order_id=f"det-{client_order_id[:16]}",
            source=self.name,
        )

    # ------------------------------------------------------------------
    # Submit: single-leg option (CC short call / CSP short put)
    # ------------------------------------------------------------------
    def submit_option_single(
        self,
        leg: Any,  # hermes_quant.options.data.OptionLeg
        *,
        qty: int,
        limit_price: float,
        client_order_id: str,
    ) -> FillResult:
        """Submit a single-leg option order, enforcing BP on a net-debit (buy) only.

        Price: the leg's recorded decision mid (``leg.fill_price`` if set and > 0)
        else ``abs(limit_price)`` (never a fabricated quote). Signed contracts:
        +qty for a buy, -qty for a sell. A BUY costs ``premium * qty * 100`` — checked
        against BP. A SELL (short) RECEIVES premium and is not BP-blocked (the gate's
        collateral check is upstream). Mirrors ``mleg_fill.PaperBroker.submit_single_leg_option``.
        """
        price = (
            float(leg.fill_price)
            if leg.fill_price is not None and leg.fill_price > 0
            else abs(limit_price)
        )
        sgn = 1.0 if leg.side == "buy" else -1.0
        signed_contracts = sgn * qty
        if leg.side == "buy":
            debit = price * qty * _CONTRACT_MULTIPLIER
            self._require_bp(debit, what=f"option buy {leg.symbol} ({qty}x)")
        return FillResult(
            symbol=leg.symbol,
            filled_avg_price=price,
            filled_qty=signed_contracts,
            status="filled",
            position_intent=leg.position_intent,
            order_id=f"det-{client_order_id[:16]}",
            source=self.name,
        )

    # ------------------------------------------------------------------
    # Submit: multi-leg option (vertical/condor/PMCC/roll)
    # ------------------------------------------------------------------
    def submit_option_mleg(
        self,
        option_legs: tuple[Any, ...],  # tuple[OptionLeg, ...]
        *,
        outer_qty: int,
        net_limit_price: float,
        client_order_id: str,
    ) -> FillResult:
        """Submit a >=2-leg option structure, enforcing BP on a net-debit only.

        Replicates ``mleg_fill.PaperBroker._fill_mleg_deterministic``: each leg fills
        at its recorded decision mid (``leg.fill_price``), else the residual net
        (``net_limit_price`` minus the priced legs' contribution) is apportioned
        across unpriced legs by ``ratio_qty`` so the per-leg signed sum reconstructs
        the gate-approved net. ``signed_qty = sign(side) * ratio_qty * outer_qty``.

        BP: a net DEBIT (``net_limit_price > 0``) costs ``net * 100 * outer_qty`` —
        checked against BP; a net CREDIT is not BP-blocked (collateral is the gate's
        job). No RNG -> byte-replay-equal.
        """
        # BP gate first: a net debit must fit before we admit ANY leg fill.
        if net_limit_price > 0:
            debit = net_limit_price * _CONTRACT_MULTIPLIER * outer_qty
            self._require_bp(debit, what=f"mleg debit ({len(option_legs)} legs)")

        leg_fills: list[FillResult] = []
        priced_sum = 0.0
        unpriced: list[Any] = []
        for leg in option_legs:
            mid = (
                float(leg.fill_price)
                if leg.fill_price is not None and leg.fill_price > 0
                else None
            )
            if mid is None:
                unpriced.append(leg)
            else:
                sgn = 1.0 if leg.side == "buy" else -1.0
                signed_qty = sgn * leg.ratio_qty * outer_qty
                priced_sum += (sgn * mid) * leg.ratio_qty * outer_qty
                leg_fills.append(
                    FillResult(
                        symbol=leg.symbol,
                        filled_avg_price=mid,
                        filled_qty=signed_qty,
                        status="filled",
                        position_intent=leg.position_intent,
                        source=self.name,
                    )
                )
        # Apportion the residual net across unpriced legs by ratio_qty so the per-leg
        # SIGNED contributions reconstruct the gate-approved net (mirrors the
        # PaperBroker math). The residual is a SIGNED per-structure dollar amount:
        # ``priced_sum`` carries an outer_qty factor that net_limit_price does NOT,
        # so divide it back out before differencing. The per-contract price for a
        # leg is ``sgn * residual_net / ratio_total`` so that the leg's signed
        # contribution ``sgn * per_share * ratio_qty`` sums (over unpriced legs) to
        # residual_net even when the unpriced legs have MIXED sides (a long+short
        # pair or a net-credit). The old ``abs(residual)/outer_qty`` discarded the
        # residual sign AND assumed same-sign legs, so opposite-side legs cancelled
        # to a phantom net (e.g. a 4.00 debit booked as 0.00 cash + per-leg basis).
        if unpriced:
            ratio_total = sum(leg.ratio_qty for leg in unpriced) or 1
            residual_net = net_limit_price - priced_sum / max(outer_qty, 1)
            for leg in unpriced:
                sgn = 1.0 if leg.side == "buy" else -1.0
                signed_qty = sgn * leg.ratio_qty * outer_qty
                per_share = sgn * residual_net / ratio_total
                leg_fills.append(
                    FillResult(
                        symbol=leg.symbol,
                        filled_avg_price=per_share,
                        filled_qty=signed_qty,
                        status="filled",
                        position_intent=leg.position_intent,
                        source=self.name,
                    )
                )
        # Restore input leg order for deterministic, replay-stable output.
        order = {leg.symbol: i for i, leg in enumerate(option_legs)}
        leg_fills.sort(key=lambda f: order.get(f.symbol, 0))
        return FillResult(
            symbol=option_legs[0].symbol if option_legs else "",
            filled_avg_price=0.0,  # parent has no single avg; per-leg + net carry it
            filled_qty=float(outer_qty),  # parent count (structures filled)
            status="filled",
            position_intent=None,
            order_id=f"det-{client_order_id[:16]}",
            legs=tuple(leg_fills),
            source=self.name,
            net_fill_price=net_limit_price,
        )
