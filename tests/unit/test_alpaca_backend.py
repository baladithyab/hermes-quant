"""Unit tests for AlpacaBackend (react.backends.alpaca_backend) — equity + options.

NO network: a FAKE Alpaca TradingClient is injected (``client=`` ctor arg). The fake
mimics the alpaca-py surface AlpacaBackend uses:
  * ``get_account()`` -> object with ``.equity`` / ``.buying_power``.
  * ``submit_order(req)`` -> records the request, returns a preset order (with
    ``.id``/``.status``/``.filled_avg_price``/``.filled_qty``/``.legs``) or raises.
  * ``get_order_by_id(id)`` -> a (possibly evolving) order; supports a poll sequence
    to model partially_filled -> filled.
  * ``cancel_order_by_id(id)`` -> records the cancel.

Coverage (>=12 tests):
  1. account_equity / buying_power happy + failure(None).
  2. equity LONG fill: BUY side, qty=abs, signed +shares, fields.
  3. equity SHORT fill: SELL side, signed -shares.
  4. equity poll-to-terminal: partially_filled -> filled (P1-D, no premature return).
  5. equity reject raises AlpacaSubmitError (NOT a silent fabricated fill).
  6. equity empty order id -> BackendUnavailableError (P2-B).
  7. equity unfilled timeout -> 0-fill, status unfilled_timeout (no fabricated price).
  8. option single BUY: LimitOrderRequest built w/ symbol/side/intent/qty/limit_price
     positive; signed +contracts.
  9. option single SELL: SELL side; signed -contracts.
 10. option single reject raises.
 11. mleg build: OrderClass.MLEG + legs list (OptionLegRequest each) + outer qty +
     abs limit_price; per-leg child FillResults + signed net_fill_price.
 12. mleg net credit: negative net_limit_price -> abs limit_price submitted but
     signed net_fill_price stays negative.
 13. position_intent string<->enum mapping (all four) + bad intent raises.
 14. mleg reject raises (fail-closed).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from alpaca.trading.enums import OrderClass, OrderSide, OrderType, PositionIntent
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    OptionLegRequest,
)

from hermes_quant.options.data import OptionLeg
from hermes_quant.options.occ import format_occ
from hermes_quant.react._alpaca_exec import AlpacaSubmitError
from hermes_quant.react.backend import BackendUnavailableError, BrokerBackend, FillResult
from hermes_quant.react.backends.alpaca_backend import (
    AlpacaBackend,
    _position_intent_enum,
)

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _FakeAccount:
    def __init__(self, *, equity: float = 100_000.0, buying_power: float = 200_000.0) -> None:
        self.equity = equity
        self.buying_power = buying_power


class _FakeOrder:
    """Mimics an alpaca-py Order; ``status`` may evolve across get_order_by_id."""

    def __init__(
        self,
        *,
        order_id: str = "ord-1",
        status: str = "filled",
        filled_avg_price: float | None = None,
        filled_qty: float | None = None,
        legs: list[Any] | None = None,
        side: Any | None = None,
        position_intent: Any | None = None,
        symbol: str | None = None,
    ) -> None:
        self.id = order_id
        self.client_order_id = order_id
        self.status = status
        self.filled_avg_price = filled_avg_price
        self.filled_qty = filled_qty
        self.legs = legs
        self.side = side
        self.position_intent = position_intent
        self.symbol = symbol


class _FakeClient:
    """Injectable fake Alpaca TradingClient (no network)."""

    def __init__(
        self,
        *,
        account: _FakeAccount | None = None,
        account_raises: Exception | None = None,
        submit_result: _FakeOrder | None = None,
        submit_raises: Exception | None = None,
        poll_sequence: list[_FakeOrder] | None = None,
        poll_order: _FakeOrder | None = None,
        post_cancel_order: _FakeOrder | None = None,
    ) -> None:
        self._account = account if account is not None else _FakeAccount()
        self._account_raises = account_raises
        self._submit_result = submit_result
        self._submit_raises = submit_raises
        self._poll_sequence = list(poll_sequence) if poll_sequence else None
        self._poll_order = poll_order
        self._post_cancel_order = post_cancel_order
        self.submitted: list[Any] = []
        self.poll_calls = 0
        self.cancel_calls: list[str] = []
        self._cancelled = False

    def get_account(self) -> _FakeAccount:
        if self._account_raises is not None:
            raise self._account_raises
        return self._account

    def submit_order(self, request: Any) -> _FakeOrder:
        self.submitted.append(request)
        if self._submit_raises is not None:
            raise self._submit_raises
        assert self._submit_result is not None
        return self._submit_result

    def get_order_by_id(self, order_id: str) -> _FakeOrder:
        self.poll_calls += 1
        if self._cancelled and self._post_cancel_order is not None:
            return self._post_cancel_order
        if self._poll_sequence:
            idx = min(self.poll_calls - 1, len(self._poll_sequence) - 1)
            return self._poll_sequence[idx]
        if self._poll_order is not None:
            return self._poll_order
        return self._submit_result  # type: ignore[return-value]

    def cancel_order_by_id(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)
        self._cancelled = True


def _backend(client: _FakeClient) -> AlpacaBackend:
    # Tiny poll cadence so timeout paths don't slow the suite.
    return AlpacaBackend(client=client, poll_timeout_s=0.05, poll_interval_s=0.01)


def _occ(strike: str = "150", right: str = "C") -> str:
    return format_occ("AAPL", date(2025, 6, 20), right, Decimal(strike))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Protocol conformance + account state
# --------------------------------------------------------------------------- #


def test_conforms_to_broker_backend_protocol() -> None:
    assert isinstance(_backend(_FakeClient()), BrokerBackend)
    assert _backend(_FakeClient()).name == "alpaca"


def test_account_equity_and_buying_power_happy() -> None:
    client = _FakeClient(account=_FakeAccount(equity=98_765.0, buying_power=150_000.0))
    b = _backend(client)
    assert b.account_equity() == pytest.approx(98_765.0)
    assert b.buying_power() == pytest.approx(150_000.0)


def test_account_field_failure_returns_none() -> None:
    b = _backend(_FakeClient(account_raises=RuntimeError("boom")))
    assert b.account_equity() is None
    assert b.buying_power() is None


# --------------------------------------------------------------------------- #
# Equity
# --------------------------------------------------------------------------- #


def test_submit_equity_long_fill() -> None:
    order = _FakeOrder(status="filled", filled_avg_price=190.25, filled_qty=10)
    client = _FakeClient(submit_result=order)
    res = _backend(client).submit_equity(
        symbol="AAPL", signed_qty=10, decision_price=190.0, client_order_id="cid-long"
    )
    assert isinstance(res, FillResult)
    assert res.is_fill
    assert res.symbol == "AAPL"
    assert res.filled_avg_price == pytest.approx(190.25)
    assert res.filled_qty == pytest.approx(10.0)  # signed +long
    assert res.status == "filled"
    assert res.order_id == "ord-1"
    assert res.source == "alpaca"
    # request shape
    req = client.submitted[0]
    assert isinstance(req, MarketOrderRequest)
    assert req.symbol == "AAPL"
    assert req.qty == 10  # abs(signed_qty)
    assert req.side == OrderSide.BUY
    assert req.client_order_id == "cid-long"


def test_submit_equity_short_fill_signed_negative() -> None:
    order = _FakeOrder(status="filled", filled_avg_price=190.0, filled_qty=5)
    client = _FakeClient(submit_result=order)
    res = _backend(client).submit_equity(
        symbol="AAPL", signed_qty=-5, decision_price=190.0, client_order_id="cid-short"
    )
    assert res.filled_qty == pytest.approx(-5.0)  # signed -short
    assert client.submitted[0].side == OrderSide.SELL
    assert client.submitted[0].qty == 5  # abs


def test_submit_equity_partial_then_filled_polls_to_terminal() -> None:
    # P1-D: partially_filled is NON-terminal; the loop must keep polling to 'filled'.
    submitted = _FakeOrder(order_id="ord-9", status="new")
    seq = [
        _FakeOrder(order_id="ord-9", status="partially_filled", filled_avg_price=50.0, filled_qty=3),
        _FakeOrder(order_id="ord-9", status="filled", filled_avg_price=50.5, filled_qty=8),
    ]
    client = _FakeClient(submit_result=submitted, poll_sequence=seq)
    res = _backend(client).submit_equity(
        symbol="MSFT", signed_qty=8, decision_price=50.0, client_order_id="cid-p"
    )
    assert res.status == "filled"
    assert res.filled_qty == pytest.approx(8.0)  # the FINAL fill, not the partial 3
    assert res.filled_avg_price == pytest.approx(50.5)


def test_submit_equity_reject_raises_not_silent() -> None:
    client = _FakeClient(submit_raises=RuntimeError("insufficient buying power"))
    with pytest.raises(AlpacaSubmitError):
        _backend(client).submit_equity(
            symbol="AAPL", signed_qty=1000, decision_price=190.0, client_order_id="cid"
        )


def test_submit_equity_empty_order_id_fails_closed() -> None:
    # P2-B: a submit returning no usable id cannot be polled/reconciled.
    order = _FakeOrder(order_id="", status="filled", filled_avg_price=1.0, filled_qty=1)
    order.client_order_id = ""
    client = _FakeClient(submit_result=order)
    with pytest.raises(BackendUnavailableError):
        _backend(client).submit_equity(
            symbol="AAPL", signed_qty=1, decision_price=1.0, client_order_id="cid"
        )


def test_submit_equity_unfilled_timeout_zero_fill() -> None:
    # Never reaches a fill within the poll budget -> 0-fill, no fabricated price.
    working = _FakeOrder(order_id="ord-7", status="new")  # never fills, no partial
    client = _FakeClient(submit_result=working, poll_order=working)
    res = _backend(client).submit_equity(
        symbol="AAPL", signed_qty=10, decision_price=190.0, client_order_id="cid-t"
    )
    assert res.status == "unfilled_timeout"
    assert res.filled_qty == 0.0
    assert res.filled_avg_price == 0.0
    assert not res.is_fill
    assert res.order_id == "ord-7"
    assert client.cancel_calls == ["ord-7"]  # P1-C cancel on timeout


# --------------------------------------------------------------------------- #
# Single-leg option
# --------------------------------------------------------------------------- #


def test_submit_option_single_buy() -> None:
    sym = _occ("150", "C")
    leg = OptionLeg(symbol=sym, side="buy", position_intent="buy_to_open")
    order = _FakeOrder(status="filled", filled_avg_price=2.50, filled_qty=3)
    client = _FakeClient(submit_result=order)
    res = _backend(client).submit_option_single(
        leg, qty=3, limit_price=2.50, client_order_id="opt-buy"
    )
    assert res.filled_qty == pytest.approx(3.0)  # signed +contracts (buy)
    assert res.filled_avg_price == pytest.approx(2.50)
    assert res.position_intent == "buy_to_open"
    assert res.source == "alpaca"
    req = client.submitted[0]
    assert isinstance(req, LimitOrderRequest)
    assert req.symbol == sym
    assert req.qty == 3
    assert req.side == OrderSide.BUY
    assert req.position_intent == PositionIntent.BUY_TO_OPEN
    assert req.limit_price == pytest.approx(2.50)
    assert req.client_order_id == "opt-buy"


def test_submit_option_single_sell_signed_negative_and_abs_limit() -> None:
    sym = _occ("160", "C")
    leg = OptionLeg(symbol=sym, side="sell", position_intent="sell_to_open")
    order = _FakeOrder(status="filled", filled_avg_price=1.10, filled_qty=2)
    client = _FakeClient(submit_result=order)
    res = _backend(client).submit_option_single(
        leg, qty=2, limit_price=-1.10, client_order_id="opt-sell"
    )
    assert res.filled_qty == pytest.approx(-2.0)  # signed -contracts (sell)
    req = client.submitted[0]
    assert req.side == OrderSide.SELL
    assert req.position_intent == PositionIntent.SELL_TO_OPEN
    assert req.limit_price == pytest.approx(1.10)  # abs() applied to the limit


def test_submit_option_single_reject_raises() -> None:
    sym = _occ("150", "C")
    leg = OptionLeg(symbol=sym, side="buy", position_intent="buy_to_open")
    client = _FakeClient(submit_raises=RuntimeError("422 rejected"))
    with pytest.raises(AlpacaSubmitError):
        _backend(client).submit_option_single(
            leg, qty=1, limit_price=2.0, client_order_id="cid"
        )


# --------------------------------------------------------------------------- #
# Multi-leg option (the deferred path)
# --------------------------------------------------------------------------- #


def _vertical_legs() -> tuple[OptionLeg, OptionLeg]:
    short = OptionLeg(symbol=_occ("150", "C"), side="sell", position_intent="sell_to_open")
    long = OptionLeg(symbol=_occ("160", "C"), side="buy", position_intent="buy_to_open", ratio_qty=1)
    return short, long


def test_submit_option_mleg_build_and_legs() -> None:
    short, long = _vertical_legs()
    # Parent terminal with child legs carrying fills.
    child_short = _FakeOrder(
        order_id="leg-s", status="filled", filled_avg_price=1.20, filled_qty=2,
        side=OrderSide.SELL, position_intent=PositionIntent.SELL_TO_OPEN, symbol=short.symbol,
    )
    child_long = _FakeOrder(
        order_id="leg-l", status="filled", filled_avg_price=0.70, filled_qty=2,
        side=OrderSide.BUY, position_intent=PositionIntent.BUY_TO_OPEN, symbol=long.symbol,
    )
    parent = _FakeOrder(
        order_id="parent-1", status="filled", filled_avg_price=0.50, filled_qty=2,
        legs=[child_short, child_long],
    )
    client = _FakeClient(submit_result=parent, poll_order=parent)
    res = _backend(client).submit_option_mleg(
        (short, long), outer_qty=2, net_limit_price=0.50, client_order_id="mleg-1"
    )
    # Parent FillResult shape
    assert res.order_id == "parent-1"
    assert res.filled_qty == pytest.approx(2.0)  # outer qty
    assert res.status == "filled"
    assert res.net_fill_price == pytest.approx(0.50)  # net debit (positive)
    assert res.source == "alpaca"
    assert len(res.legs) == 2
    fills_by_sym = {f.symbol: f for f in res.legs}
    assert fills_by_sym[short.symbol].filled_qty == pytest.approx(-2.0)  # sell -> -
    assert fills_by_sym[long.symbol].filled_qty == pytest.approx(2.0)  # buy -> +
    assert fills_by_sym[short.symbol].position_intent == "sell_to_open"

    # Request shape
    req = client.submitted[0]
    assert isinstance(req, LimitOrderRequest)
    assert req.order_class == OrderClass.MLEG
    assert req.type == OrderType.LIMIT
    assert req.qty == 2
    assert req.limit_price == pytest.approx(0.50)
    assert len(req.legs) == 2
    assert all(isinstance(lg, OptionLegRequest) for lg in req.legs)
    leg_syms = {lg.symbol for lg in req.legs}
    assert leg_syms == {short.symbol, long.symbol}
    short_req = next(lg for lg in req.legs if lg.symbol == short.symbol)
    assert short_req.side == OrderSide.SELL
    assert short_req.position_intent == PositionIntent.SELL_TO_OPEN
    assert short_req.ratio_qty == 1


def test_submit_option_mleg_net_credit_sign_preserved_abs_submitted() -> None:
    short, long = _vertical_legs()
    parent = _FakeOrder(order_id="parent-2", status="filled", filled_qty=1, legs=[])
    client = _FakeClient(submit_result=parent, poll_order=parent)
    res = _backend(client).submit_option_mleg(
        (short, long), outer_qty=1, net_limit_price=-1.25, client_order_id="mleg-2"
    )
    # limit_price submitted as positive magnitude...
    assert client.submitted[0].limit_price == pytest.approx(1.25)
    # ...but the signed net is preserved as a credit (negative).
    assert res.net_fill_price == pytest.approx(-1.25)


def test_submit_option_mleg_reject_raises() -> None:
    short, long = _vertical_legs()
    client = _FakeClient(submit_raises=RuntimeError("mleg rejected"))
    with pytest.raises(AlpacaSubmitError):
        _backend(client).submit_option_mleg(
            (short, long), outer_qty=1, net_limit_price=0.5, client_order_id="cid"
        )


def test_submit_option_mleg_requires_two_legs() -> None:
    one = OptionLeg(symbol=_occ("150", "C"), side="buy", position_intent="buy_to_open")
    client = _FakeClient(submit_result=_FakeOrder())
    with pytest.raises(AlpacaSubmitError):
        _backend(client).submit_option_mleg(
            (one,), outer_qty=1, net_limit_price=0.5, client_order_id="cid"
        )


# --------------------------------------------------------------------------- #
# position_intent mapping
# --------------------------------------------------------------------------- #


def test_position_intent_mapping_all_four() -> None:
    assert _position_intent_enum("buy_to_open") == PositionIntent.BUY_TO_OPEN
    assert _position_intent_enum("buy_to_close") == PositionIntent.BUY_TO_CLOSE
    assert _position_intent_enum("sell_to_open") == PositionIntent.SELL_TO_OPEN
    assert _position_intent_enum("sell_to_close") == PositionIntent.SELL_TO_CLOSE


def test_position_intent_bad_value_raises() -> None:
    with pytest.raises(AlpacaSubmitError):
        _position_intent_enum("hold_forever")
