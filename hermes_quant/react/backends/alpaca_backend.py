"""hermes_quant.react.backends.alpaca_backend — Alpaca paper BrokerBackend (ADR-0088).

Wraps the Alpaca paper ``TradingClient`` for EQUITY and OPTIONS (single-leg AND the
deferred multi-leg / ``OrderClass.MLEG`` path), implementing the ``BrokerBackend``
Protocol from ``react.backend``. The broker is the source of truth: it enforces
buying power / margin / shorting natively and reports REAL fills.

Rails (ADR-0088), inherited from the proven equity path in ``react.alpaca_paper``
(PR #69) via the shared ``react._alpaca_exec`` helpers — DO NOT regress:
  * INJECTABLE client (``client=`` ctor arg) so unit tests run with NO network; the
    real paper ``TradingClient`` is lazily built from the EXISTING env-var pattern.
  * FAIL-CLOSED: a submit reject (insufficient BP / 422 / dup id), an empty order id,
    or a terminal reject with no fill RAISES (``BackendUnavailableError`` /
    ``AlpacaSubmitError``) — never a fabricated fill.
  * TRUE units in ``FillResult.filled_qty`` (signed shares / signed contracts).
  * Poll-to-terminal with partial-is-non-terminal (P1-D), cancel-on-timeout (P1-C),
    done_for_day/canceled partials recorded (P3-B), sub-$1 / empty-id guards.

Order-shape notes verified against alpaca-py:
  * ``OptionLegRequest`` fields: symbol, ratio_qty, side, position_intent.
  * ``LimitOrderRequest`` fields include: qty, side, type, time_in_force,
    order_class, client_order_id, legs, position_intent, limit_price.
  * OCC-21 symbols (``hermes_quant.options.occ.format_occ``) are exactly Alpaca's
    option symbol format — passed through unchanged.

limit_price SIGN ASSUMPTION (mleg): Alpaca's mleg ``limit_price`` is a POSITIVE
magnitude; the DIRECTION (net debit you pay vs net credit you receive) is encoded by
the per-leg ``side`` / ``position_intent``, not by the sign of ``limit_price``. We
therefore submit ``limit_price=abs(net_limit_price)`` and preserve the caller's
signed intent only in the returned ``FillResult.net_fill_price`` (signed net:
negative = net credit received, positive = net debit paid). Same convention is
applied to single-leg (``limit_price=abs(limit_price)``).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .. import _alpaca_exec
from .._alpaca_exec import AlpacaSubmitError
from ..backend import BackendUnavailableError, FillResult

logger = logging.getLogger(__name__)

# OptionLeg.position_intent (str) -> alpaca PositionIntent enum. Built lazily so a
# flag-off / no-alpaca import path is never forced to import the enum at module load.
_POSITION_INTENT_STRINGS = frozenset(
    {"buy_to_open", "buy_to_close", "sell_to_open", "sell_to_close"}
)


def _position_intent_enum(intent: str) -> Any:
    """Map an ``OptionLeg.position_intent`` string to alpaca's PositionIntent enum.

    {'buy_to_open':BUY_TO_OPEN, 'buy_to_close':BUY_TO_CLOSE,
     'sell_to_open':SELL_TO_OPEN, 'sell_to_close':SELL_TO_CLOSE}.
    Fail-closed on an unrecognized intent (never silently mis-route a position).
    """
    from alpaca.trading.enums import PositionIntent

    key = (intent or "").strip().lower()
    if key not in _POSITION_INTENT_STRINGS:
        raise AlpacaSubmitError(
            f"unrecognized option position_intent {intent!r}; expected one of "
            f"{sorted(_POSITION_INTENT_STRINGS)}"
        )
    return PositionIntent(key)


def _order_side_enum(side: str) -> Any:
    """Map a 'buy'/'sell' string to alpaca's OrderSide enum. Fail-closed otherwise."""
    from alpaca.trading.enums import OrderSide

    key = (side or "").strip().lower()
    if key not in {"buy", "sell"}:
        raise AlpacaSubmitError(f"option leg side must be 'buy'/'sell', got {side!r}")
    return OrderSide(key)


class AlpacaBackend:
    """BrokerBackend over the Alpaca paper TradingClient (equity + options).

    Implements ``react.backend.BrokerBackend``. The client is injectable for tests
    (no network) and lazily built in production from the existing Alpaca env vars.
    """

    name = "alpaca"
    requires_credentials = True

    def __init__(
        self,
        *,
        client: Any | None = None,
        poll_timeout_s: float = _alpaca_exec.POLL_TIMEOUT_S,
        poll_interval_s: float = _alpaca_exec.POLL_INTERVAL_S,
    ) -> None:
        # INJECTABLE: tests pass a fake; production passes None and the real paper
        # TradingClient is lazily built on first use from the existing env vars.
        self._client = client
        self._poll_timeout_s = poll_timeout_s
        self._poll_interval_s = poll_interval_s

    def _resolve_client(self) -> Any:
        if self._client is None:
            self._client = _alpaca_exec.build_paper_trading_client()
        return self._client

    # ------------------------------------------------------------------
    # Account state
    # ------------------------------------------------------------------
    def account_equity(self) -> float | None:
        """Account NAV (USD), or None if unknown (caller fails closed)."""
        return self._account_field("equity")

    def buying_power(self) -> float | None:
        """Available buying power (USD), or None if unknown."""
        return self._account_field("buying_power")

    def _account_field(self, attr: str) -> float | None:
        try:
            account = self._resolve_client().get_account()
        except Exception as exc:  # noqa: BLE001 — None signals "unknown" to caller
            logger.warning("alpaca-backend: get_account() failed reading %s: %s", attr, exc)
            return None
        return _alpaca_exec.to_float(getattr(account, attr, None))

    # ------------------------------------------------------------------
    # Equity
    # ------------------------------------------------------------------
    def submit_equity(
        self,
        *,
        symbol: str,
        signed_qty: float,
        decision_price: float,
        client_order_id: str,
    ) -> FillResult:
        """Submit an equity market order (qty=abs(signed_qty)); poll to terminal.

        Alpaca enforces buying power natively — an over-BP order is REJECTED and
        surfaces as ``AlpacaSubmitError`` (fail-closed), never a fabricated fill.
        ``filled_qty`` in the result is signed TRUE shares (+long / -short).
        """
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        qty = abs(float(signed_qty))
        if qty <= 0:
            raise AlpacaSubmitError(
                f"submit_equity for {symbol} got non-positive signed_qty="
                f"{signed_qty!r}; refusing to submit a zero-share order"
            )
        side = OrderSide.BUY if signed_qty > 0 else OrderSide.SELL

        client = self._resolve_client()
        req_kwargs: dict[str, Any] = {
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "time_in_force": TimeInForce.DAY,
        }
        if client_order_id:
            req_kwargs["client_order_id"] = str(client_order_id)
        req = MarketOrderRequest(**req_kwargs)

        order = self._submit(client, req, what=f"equity {symbol}")
        order_id = self._require_order_id(order, symbol)
        filled = self._poll(client, order, order_id)
        if filled is None:
            return FillResult(
                symbol=symbol,
                filled_avg_price=0.0,
                filled_qty=0.0,
                status="unfilled_timeout",
                order_id=order_id,
                source=self.name,
            )
        avg_price, abs_qty, status = filled
        signed = abs_qty if signed_qty > 0 else -abs_qty
        return FillResult(
            symbol=symbol,
            filled_avg_price=avg_price,
            filled_qty=signed,
            status=status,
            order_id=order_id,
            source=self.name,
        )

    # ------------------------------------------------------------------
    # Single-leg option
    # ------------------------------------------------------------------
    def submit_option_single(
        self,
        leg: Any,  # hermes_quant.options.data.OptionLeg
        *,
        qty: int,
        limit_price: float,
        client_order_id: str,
    ) -> FillResult:
        """Submit a single-leg option LIMIT order; poll to terminal.

        ``leg`` is an ``OptionLeg`` (.symbol OCC-21, .side 'buy'/'sell',
        .position_intent). ``limit_price`` is submitted as a positive magnitude
        (sign convention documented in the module docstring). signed contracts =
        (+filled_qty if buy else -filled_qty).
        """
        from alpaca.trading.enums import TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        n = int(qty)
        if n <= 0:
            raise AlpacaSubmitError(
                f"submit_option_single for {leg.symbol} got non-positive qty={qty!r}"
            )
        side = _order_side_enum(leg.side)
        intent = _position_intent_enum(leg.position_intent)

        client = self._resolve_client()
        req_kwargs: dict[str, Any] = {
            "symbol": leg.symbol,  # OCC-21 == Alpaca option symbol
            "qty": n,
            "side": side,
            "limit_price": abs(float(limit_price)),
            "time_in_force": TimeInForce.DAY,
            "position_intent": intent,
        }
        if client_order_id:
            req_kwargs["client_order_id"] = str(client_order_id)
        req = LimitOrderRequest(**req_kwargs)

        order = self._submit(client, req, what=f"option {leg.symbol}")
        order_id = self._require_order_id(order, leg.symbol)
        filled = self._poll(client, order, order_id)

        is_buy = leg.side.strip().lower() == "buy"
        if filled is None:
            return FillResult(
                symbol=leg.symbol,
                filled_avg_price=0.0,
                filled_qty=0.0,
                status="unfilled_timeout",
                position_intent=leg.position_intent,
                order_id=order_id,
                source=self.name,
            )
        avg_price, abs_qty, status = filled
        signed = abs_qty if is_buy else -abs_qty
        return FillResult(
            symbol=leg.symbol,
            filled_avg_price=avg_price,
            filled_qty=signed,
            status=status,
            position_intent=leg.position_intent,
            order_id=order_id,
            source=self.name,
        )

    # ------------------------------------------------------------------
    # Multi-leg option (the deferred path)
    # ------------------------------------------------------------------
    def submit_option_mleg(
        self,
        option_legs: tuple[Any, ...],  # tuple[OptionLeg, ...]
        *,
        outer_qty: int,
        net_limit_price: float,
        client_order_id: str,
    ) -> FillResult:
        """Submit a >=2-leg ``OrderClass.MLEG`` LIMIT order; poll the PARENT to terminal.

        Builds one ``OptionLegRequest`` per ``OptionLeg`` (symbol/ratio_qty/side/
        position_intent), submits a parent ``LimitOrderRequest`` with
        ``order_class=MLEG``, ``type=LIMIT``, ``qty=outer_qty``,
        ``limit_price=abs(net_limit_price)`` (positive magnitude — see module
        docstring), and DAY TIF. After the parent reaches terminal, the parent's
        ``.legs`` (child orders) carry per-leg fills, assembled into per-leg
        ``FillResult``s. The returned parent ``FillResult`` carries
        ``net_fill_price`` = signed net (sign preserved from ``net_limit_price``),
        ``filled_qty=outer_qty``, ``legs=(per-child...)``, and the parent order id.
        """
        from alpaca.trading.enums import OrderClass, OrderType, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

        if not option_legs or len(option_legs) < 2:
            raise AlpacaSubmitError(
                f"submit_option_mleg requires >=2 legs, got {len(option_legs)}"
            )
        n = int(outer_qty)
        if n <= 0:
            raise AlpacaSubmitError(
                f"submit_option_mleg got non-positive outer_qty={outer_qty!r}"
            )

        legs = [
            OptionLegRequest(
                symbol=leg.symbol,
                ratio_qty=int(leg.ratio_qty),
                side=_order_side_enum(leg.side),
                position_intent=_position_intent_enum(leg.position_intent),
            )
            for leg in option_legs
        ]

        client = self._resolve_client()
        req_kwargs: dict[str, Any] = {
            "qty": n,
            "order_class": OrderClass.MLEG,
            "type": OrderType.LIMIT,
            "time_in_force": TimeInForce.DAY,
            "limit_price": abs(float(net_limit_price)),
            "legs": legs,
        }
        if client_order_id:
            req_kwargs["client_order_id"] = str(client_order_id)
        req = LimitOrderRequest(**req_kwargs)

        symbols = "+".join(leg.symbol for leg in option_legs)
        parent = self._submit(client, req, what=f"mleg {symbols}")
        parent_id = self._require_order_id(parent, symbols)

        # Poll the PARENT to terminal. Unlike a single equity/option order, an mleg
        # PARENT typically carries NO single avg fill price (the fills live on the
        # child .legs), so we cannot rely on the price>0 fill-extract guard. We
        # instead poll the parent STATUS to a terminal state (P1-D: partial is
        # non-terminal) and read fills off the children. Fail-closed semantics are
        # preserved: a terminal reject with no child fills raises.
        final_parent, parent_status = self._poll_mleg_parent(client, parent, parent_id)
        if final_parent is None:
            return FillResult(
                symbol=symbols,
                filled_avg_price=0.0,
                filled_qty=0.0,
                status="unfilled_timeout",
                order_id=parent_id,
                source=self.name,
                net_fill_price=None,
            )

        leg_fills = self._build_leg_fills(final_parent, option_legs)
        if parent_status in _alpaca_exec.REJECT_STATUSES and not any(
            f.is_fill for f in leg_fills
        ):
            raise AlpacaSubmitError(
                f"mleg order {parent_id} reached terminal status {parent_status!r} "
                "with no leg fills — surfacing rejection, not fabricating a fill"
            )

        # Signed net: preserve the caller's intent sign (negative = net credit
        # received, positive = net debit paid). limit_price itself is sent as a
        # positive magnitude (sign convention documented in module docstring).
        signed_net = (
            -abs(float(net_limit_price))
            if net_limit_price < 0
            else abs(float(net_limit_price))
        )

        return FillResult(
            symbol=symbols,
            filled_avg_price=0.0,  # parent has no single avg price; see per-leg legs
            filled_qty=float(n),  # outer (structure) quantity actually requested
            status=parent_status,
            order_id=parent_id,
            legs=tuple(leg_fills),
            source=self.name,
            net_fill_price=signed_net,
        )

    def _poll_mleg_parent(
        self, client: Any, parent: Any, parent_id: str
    ) -> tuple[Any | None, str]:
        """Poll an mleg parent to a TERMINAL status; return (order, status).

        Mirrors the shared poll's P1-D / P1-C semantics but keys on the parent
        STATUS rather than a parent avg price (an mleg parent carries fills on its
        child ``.legs``, not a single avg price). On timeout, cancel the still-
        working parent then re-read once (P1-C). Returns ``(None, 'unfilled_*')``
        when the budget elapses with the parent never terminal.
        """
        deadline = time.monotonic() + self._poll_timeout_s
        current = parent
        while True:
            status = str(getattr(current, "status", "") or "").lower()
            if status == "filled" or status in _alpaca_exec.REJECT_STATUSES:
                return current, status
            # 'partially_filled' / 'new' / 'accepted' / 'pending_*' — NON-terminal.
            if time.monotonic() >= deadline:
                # P1-C: cancel the working parent, then re-read once.
                try:
                    client.cancel_order_by_id(parent_id)
                except Exception as exc:  # noqa: BLE001 — best-effort cancel
                    logger.warning(
                        "alpaca-backend: cancel mleg parent %s failed: %s",
                        parent_id,
                        exc,
                    )
                time.sleep(self._poll_interval_s)
                final = self._refresh(client, parent_id)
                fstatus = str(getattr(final, "status", "") or "").lower()
                # P3-B (parity with _alpaca_exec.cancel_and_settle): the cancel
                # only removes the UNfilled remainder, so a cancel-vs-fill race can
                # leave the parent reading back NON-terminal (e.g.
                # 'partially_filled') in the brief window before its status settles
                # — yet its child .legs already carry REAL fills. Record ANY
                # realized child partial regardless of the (non-terminal) parent
                # status; never discard a parent that demonstrably moved a position
                # (else a genuinely-filled mleg is orphaned live). Only a parent
                # with no child fill at all falls through to unfilled_timeout.
                if final is not None and (
                    fstatus == "filled"
                    or fstatus in _alpaca_exec.REJECT_STATUSES
                    or self._any_child_filled(final)
                ):
                    return final, fstatus
                return None, "unfilled_timeout"
            time.sleep(self._poll_interval_s)
            try:
                current = client.get_order_by_id(parent_id)
            except Exception as exc:  # noqa: BLE001 — poll error, surface it
                raise AlpacaSubmitError(
                    f"get_order_by_id({parent_id}) failed during mleg poll: {exc}"
                ) from exc

    @staticmethod
    def _any_child_filled(parent: Any) -> bool:
        """True iff any child ``.leg`` of an mleg parent carries a positive fill.

        Mirrors ``_alpaca_exec.extract_fill``'s positive-fill test (price>0 AND
        qty>0) but applied to the child legs — an mleg parent carries no single avg
        price, so a cancel-vs-fill race is detected on the children. Used to decide
        whether a NON-terminal post-cancel parent re-read still represents a real
        realized partial that must be recorded (P3-B), not discarded as a no-fill.
        """
        for child in list(getattr(parent, "legs", None) or []):
            price = _alpaca_exec.to_float(getattr(child, "filled_avg_price", None))
            qty = _alpaca_exec.to_float(getattr(child, "filled_qty", None))
            if price is not None and price > 0 and qty is not None and qty > 0:
                return True
        return False

    def _build_leg_fills(
        self, parent: Any, option_legs: tuple[Any, ...]
    ) -> list[FillResult]:
        """Assemble per-leg ``FillResult``s from the parent's child ``.legs`` orders.

        Each child order carries its own symbol / side / filled_avg_price /
        filled_qty / status / position_intent. signed contracts per child =
        (+filled_qty if child side buy else -filled_qty). Falls back to matching
        the requested ``option_legs`` by symbol for side/intent if the child order
        omits them.
        """
        children = list(getattr(parent, "legs", None) or [])
        by_symbol = {leg.symbol: leg for leg in option_legs}
        fills: list[FillResult] = []
        for child in children:
            symbol = str(getattr(child, "symbol", "") or "")
            req_leg = by_symbol.get(symbol)
            side = str(
                getattr(child, "side", None)
                or (getattr(req_leg, "side", "") if req_leg else "")
                or ""
            ).lower()
            # alpaca enum reprs as 'OrderSide.BUY'; normalize to the value.
            is_buy = side.endswith("buy") or side == "buy"
            intent = getattr(child, "position_intent", None)
            intent_str = (
                str(getattr(intent, "value", intent))
                if intent is not None
                else (getattr(req_leg, "position_intent", None) if req_leg else None)
            )
            price = _alpaca_exec.to_float(getattr(child, "filled_avg_price", None)) or 0.0
            abs_qty = _alpaca_exec.to_float(getattr(child, "filled_qty", None)) or 0.0
            signed = abs_qty if is_buy else -abs_qty
            status = str(getattr(child, "status", "") or "").lower() or "filled"
            fills.append(
                FillResult(
                    symbol=symbol,
                    filled_avg_price=price,
                    filled_qty=signed,
                    status=status,
                    position_intent=intent_str,
                    order_id=_alpaca_exec.order_id_of(child) or None,
                    source=self.name,
                )
            )
        return fills

    # ------------------------------------------------------------------
    # Shared submit / poll plumbing (delegates to ._alpaca_exec)
    # ------------------------------------------------------------------
    def _submit(self, client: Any, req: Any, *, what: str) -> Any:
        """Submit a request; a reject (BP/422/dup id) RAISES (fail-closed)."""
        try:
            return client.submit_order(req)
        except Exception as exc:  # noqa: BLE001 — insufficient BP / 422 / dup / net
            raise AlpacaSubmitError(f"submit_order rejected for {what}: {exc}") from exc

    def _require_order_id(self, order: Any, what: str) -> str:
        """Extract a usable order id or fail closed (cannot poll a blank id, P2-B)."""
        order_id = _alpaca_exec.order_id_of(order)
        if not order_id:
            raise BackendUnavailableError(
                f"submit_order for {what} returned no order id; refusing to "
                "proceed (cannot poll/reconcile a blank id)"
            )
        return order_id

    def _poll(
        self, client: Any, order: Any, order_id: str
    ) -> tuple[float, float, str] | None:
        """Poll to terminal via the shared P1/P2/P3 mechanics."""
        return _alpaca_exec.poll_until_filled(
            client,
            order,
            order_id,
            poll_timeout_s=self._poll_timeout_s,
            poll_interval_s=self._poll_interval_s,
            logger=logger,
        )

    @staticmethod
    def _refresh(client: Any, order_id: str) -> Any | None:
        """Best-effort re-read of an order (for parent .legs after terminal)."""
        try:
            return client.get_order_by_id(order_id)
        except Exception as exc:  # noqa: BLE001 — non-fatal; caller falls back
            logger.warning(
                "alpaca-backend: get_order_by_id(%s) refresh failed: %s", order_id, exc
            )
            return None
