"""hermes_quant.react.mleg_fill — PaperBroker mleg/single-leg/equity submit + poll.

Split out so ``react/multileg.py`` stays the orchestrator and the broker/fill
mechanics are independently testable. NO alpaca-py import at module top — the live
adapter imports it lazily inside the method so ``import hermes_quant.react.multileg``
is clean without the ``[alpaca]`` extra.

Two modes, selected at submit time (research §2):
  - LIVE-PAPER: ``HERMES_QUANT_MULTILEG_REACTOR=1`` AND APCA creds present -> a real
    Alpaca paper ``POST /v2/orders`` + ``GET /v2/orders/{id}`` poll. DEFERRED this
    wave (paper-only loop ships the deterministic model; the live-paper HTTP body is
    a follow-up — see ``_submit_live_paper`` which raises until the go-live wave).
  - DETERMINISTIC MODEL: no creds (CI / offline) -> fill each leg at its
    decision-time mid (from the per-leg ``greeks_at_decision``-bearing snapshot the
    proposal's legs carry), else apportion the proposal ``net_debit_credit`` by
    ``ratio_qty``. Fully deterministic (no RNG) so replays match byte-for-byte.

A live (non-paper) account => ``LiveMultiLegNotAuthorized`` (never reached this wave;
the live-paper path itself is paper-only and the non-paper guard is defence-in-depth).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from hermes_quant.options.data import OptionLeg

logger = logging.getLogger(__name__)

# Terminal vs non-terminal order statuses (research §2.1 / §1.4).
_TERMINAL = frozenset(
    {"filled", "partially_filled", "rejected", "expired", "canceled", "cancelled"}
)
_NON_TERMINAL = frozenset({"accepted", "new", "pending_new", "held", "calculated"})


class PaperBrokerError(RuntimeError):
    """Raised on a malformed mleg/single-leg/equity submission (caller bug)."""


@dataclass(frozen=True)
class LegFill:
    """Per-leg fill detail (mirrors a child ``Order`` in research §1.4)."""

    symbol: str  # OCC-21 (option) or ticker (equity)
    filled_avg_price: float
    filled_qty: float  # signed: + long / - short, in contracts (option) or shares (equity)
    status: str  # 'filled' | 'partially_filled' | 'rejected' | 'expired'
    position_intent: str | None


@dataclass(frozen=True)
class MlegFillResult:
    """Parent fill result; ``legs`` carries the per-leg detail (research §1.4)."""

    broker_order_id: str
    client_order_id: str
    status: str  # parent status (the atomicity gate)
    legs: tuple[LegFill, ...]
    net_fill_price: float  # signed net debit/credit actually filled
    source: str  # 'alpaca_paper' | 'deterministic_model'

    @property
    def is_filled(self) -> bool:
        return self.status in {"filled", "partially_filled"}


def _leg_decision_mid(leg: OptionLeg) -> float | None:
    """The per-leg decision-time price for the deterministic model.

    Prefers an explicit ``fill_price`` carried on the leg (the recipe producer may
    set the decision-time mid here); else None so the caller apportions the net
    price. We deliberately do NOT synthesize a price from greeks alone (greeks carry
    no mid), keeping the model honest: a leg without a recorded price falls back to
    the gate-approved net, never to a fabricated quote.
    """
    if leg.fill_price is not None and leg.fill_price > 0:
        return float(leg.fill_price)
    return None


class PaperBroker:
    """Paper multi-leg + single-leg option + equity submit/poll.

    INERT (deterministic local fill) unless ``HERMES_QUANT_MULTILEG_REACTOR=1`` AND
    APCA creds present; the live-paper HTTP body is deferred to the go-live wave.
    Live (non-paper) construction => ``LiveMultiLegNotAuthorized``.
    """

    def __init__(self, *, paper: bool = True) -> None:
        # Defence-in-depth: a non-paper account can never be constructed in this
        # wave. The live multi-leg rail stays behind the LiveTradingApproval
        # type-level guard in react/live.py (ADR-0029 D7).
        if not paper:
            from hermes_quant.react.multileg import LiveMultiLegNotAuthorized

            raise LiveMultiLegNotAuthorized(
                "non-paper multi-leg is gated behind LiveTradingApproval (ADR-0029 D7); "
                "PaperBroker is paper-only"
            )
        self.paper = paper

    # ------------------------------------------------------------------
    # Mode selection
    # ------------------------------------------------------------------
    @staticmethod
    def _live_paper_available() -> bool:
        """True iff the reactor flag is set AND APCA creds are present.

        With the flag unset (the default everywhere) OR creds absent (CI/offline),
        the deterministic model is used — the eval gate path. Live-paper HTTP itself
        is deferred this wave (``_submit_live_paper`` raises NotImplementedError).
        """
        if os.environ.get("HERMES_QUANT_MULTILEG_REACTOR", "0") != "1":
            return False
        return bool(
            os.environ.get("APCA_API_KEY_ID") and os.environ.get("APCA_API_SECRET_KEY")
        )

    # ------------------------------------------------------------------
    # mleg order body builder (research §1.1 shape)
    # ------------------------------------------------------------------
    @staticmethod
    def build_mleg_body(
        option_legs: tuple[OptionLeg, ...],
        *,
        outer_qty: int,
        net_limit_price: float,
        tif: str,
        client_order_id: str,
    ) -> dict:
        """Build the research §1.1 mleg ``POST /v2/orders`` body.

        OUTER ``qty``/``type``/``limit_price``; per-leg ``position_intent`` +
        ``ratio_qty`` + ``side``. NO equity leg in ``legs[]`` (options-only, 2-4
        legs) — a CC's equity leg is a SEPARATE equity order. ``limit_price`` sign:
        POSITIVE = net debit paid, NEGATIVE = net credit received.
        """
        if not (2 <= len(option_legs) <= 4):
            raise PaperBrokerError(
                f"mleg order requires 2-4 option legs, got {len(option_legs)}"
            )
        symbols = [leg.symbol for leg in option_legs]
        if len(set(symbols)) != len(symbols):
            raise PaperBrokerError(f"mleg legs must be unique OCC symbols, got {symbols}")
        return {
            "order_class": "mleg",
            "qty": str(outer_qty),
            "type": "limit",
            "limit_price": f"{net_limit_price}",
            "time_in_force": tif,
            "client_order_id": client_order_id,
            "legs": [
                {
                    "symbol": leg.symbol,
                    "ratio_qty": str(leg.ratio_qty),
                    "side": leg.side,
                    "position_intent": leg.position_intent,
                }
                for leg in option_legs
            ],
        }

    # ------------------------------------------------------------------
    # Submit: mleg
    # ------------------------------------------------------------------
    def submit_mleg_order(
        self,
        option_legs: tuple[OptionLeg, ...],
        *,
        outer_qty: int,
        net_limit_price: float,
        tif: str = "day",
        client_order_id: str,
    ) -> MlegFillResult:
        """Submit a >=2-option-leg structure (vertical/condor/PMCC/roll)."""
        # Validate the body shape even on the deterministic path (a CC mistakenly
        # routed through submit_mleg_order must raise, never silently fill).
        self.build_mleg_body(
            option_legs,
            outer_qty=outer_qty,
            net_limit_price=net_limit_price,
            tif=tif,
            client_order_id=client_order_id,
        )
        if self._live_paper_available():
            return self._submit_live_paper(
                option_legs,
                outer_qty=outer_qty,
                net_limit_price=net_limit_price,
                tif=tif,
                client_order_id=client_order_id,
            )
        return self._fill_mleg_deterministic(
            option_legs,
            outer_qty=outer_qty,
            net_limit_price=net_limit_price,
            client_order_id=client_order_id,
        )

    def _fill_mleg_deterministic(
        self,
        option_legs: tuple[OptionLeg, ...],
        *,
        outer_qty: int,
        net_limit_price: float,
        client_order_id: str,
    ) -> MlegFillResult:
        """Deterministic no-network fill: each leg at its decision-time mid, else
        the net price apportioned by ratio_qty. No RNG -> byte-replay-equal."""
        leg_fills: list[LegFill] = []
        priced_sum = 0.0
        unpriced: list[OptionLeg] = []
        for leg in option_legs:
            mid = _leg_decision_mid(leg)
            if mid is None:
                unpriced.append(leg)
            else:
                sgn = 1.0 if leg.side == "buy" else -1.0
                signed_qty = sgn * leg.ratio_qty * outer_qty
                priced_sum += (sgn * mid) * leg.ratio_qty * outer_qty
                leg_fills.append(
                    LegFill(
                        symbol=leg.symbol,
                        filled_avg_price=mid,
                        filled_qty=signed_qty,
                        status="filled",
                        position_intent=leg.position_intent,
                    )
                )
        # Apportion the residual net (net_limit_price - priced_sum) across unpriced
        # legs by ratio_qty so the per-leg sum reconstructs the gate-approved net.
        if unpriced:
            ratio_total = sum(leg.ratio_qty for leg in unpriced) or 1
            residual = (net_limit_price - priced_sum) / ratio_total
            for leg in unpriced:
                sgn = 1.0 if leg.side == "buy" else -1.0
                signed_qty = sgn * leg.ratio_qty * outer_qty
                # residual is the per-ratio signed net contribution; the per-share
                # price is its magnitude divided by (outer_qty) so the leg's signed
                # contribution (sgn*price*ratio*qty) lands on the residual.
                per_share = abs(residual) / max(outer_qty, 1)
                leg_fills.append(
                    LegFill(
                        symbol=leg.symbol,
                        filled_avg_price=per_share,
                        filled_qty=signed_qty,
                        status="filled",
                        position_intent=leg.position_intent,
                    )
                )
        # Order the fills back to the input leg order for determinism.
        order = {leg.symbol: i for i, leg in enumerate(option_legs)}
        leg_fills.sort(key=lambda f: order.get(f.symbol, 0))
        return MlegFillResult(
            broker_order_id=f"paper-mleg-{client_order_id[:16]}",
            client_order_id=client_order_id,
            status="filled",
            legs=tuple(leg_fills),
            net_fill_price=net_limit_price,
            source="deterministic_model",
        )

    def _submit_live_paper(self, *args: object, **kwargs: object) -> MlegFillResult:
        """Live-paper Alpaca submit + poll. DEFERRED to the go-live wave.

        Never reached in this wave (the eval gate runs the deterministic model with
        no creds). When it lands it will POST the research §1.1 body and poll
        ``GET /v2/orders/{id}`` to terminal status with bounded backoff (alpaca-py
        imported lazily here). Raises until then so a flag-on + creds-present run
        fails loud rather than silently no-op."""
        raise NotImplementedError(  # pragma: no cover - deferred to go-live wave
            "live-paper mleg submit/poll is deferred to the ADR-0029 go-live wave; "
            "the deterministic model is the only paper-fill path this wave"
        )

    # ------------------------------------------------------------------
    # Submit: single-leg option (CC short call / CSP short put)
    # ------------------------------------------------------------------
    def submit_single_leg_option(
        self,
        leg: OptionLeg,
        *,
        qty: int,
        limit_price: float,
        tif: str = "day",
        client_order_id: str,
    ) -> LegFill:
        """Submit a single-leg L1 option order (CC short call / CSP short put)."""
        if self._live_paper_available():
            return self._submit_live_paper()  # type: ignore[return-value]
        mid = _leg_decision_mid(leg)
        price = mid if mid is not None else abs(limit_price)
        sgn = 1.0 if leg.side == "buy" else -1.0
        return LegFill(
            symbol=leg.symbol,
            filled_avg_price=price,
            filled_qty=sgn * qty,
            status="filled",
            position_intent=leg.position_intent,
        )

    # ------------------------------------------------------------------
    # Submit: equity (CC +100 shares)
    # ------------------------------------------------------------------
    def submit_equity(
        self,
        *,
        symbol: str,
        qty: int,
        decision_price: float,
        client_order_id: str,
    ) -> LegFill:
        """Submit the equity leg of a covered call (+100 shares). Signed qty:
        +long / -short. Deterministic: fills at decision_price (the reactor applies
        ADR-0070 slippage on top, asymmetric to the option legs — research §2.2)."""
        if self._live_paper_available():
            return self._submit_live_paper()  # type: ignore[return-value]
        return LegFill(
            symbol=symbol,
            filled_avg_price=decision_price,
            filled_qty=float(qty),
            status="filled",
            position_intent="buy_to_open" if qty > 0 else "sell_to_open",
        )

    # ------------------------------------------------------------------
    # Poll (live-paper only; deterministic fills are already terminal)
    # ------------------------------------------------------------------
    def poll_order(self, broker_order_id: str, *, timeout_s: int = 30) -> MlegFillResult:
        """Poll ``GET /v2/orders/{id}`` to terminal status (live-paper only).

        DEFERRED to the go-live wave; the deterministic model returns already-
        terminal results, so nothing in this wave calls poll_order."""
        raise NotImplementedError(  # pragma: no cover - deferred to go-live wave
            "poll_order is live-paper-only and deferred to the go-live wave"
        )


def is_terminal_status(status: str) -> bool:
    """True iff a broker status is terminal (research §2.1)."""
    return status.lower() in _TERMINAL


def is_fill_status(status: str) -> bool:
    """True iff a terminal status represents an actual (full or partial) fill."""
    return status.lower() in {"filled", "partially_filled"}
