"""Unit tests for hermes_quant.react.mleg_fill.PaperBroker (deterministic model).

Deterministic, no network. Verifies the deterministic fill model, the mleg body
shape (research §1.1), the non-paper guard, and idempotency-handle stability.
"""

from __future__ import annotations

import pytest

from hermes_quant.options.data import OptionGreeksSnapshot, OptionLeg
from hermes_quant.react.mleg_fill import (
    PaperBroker,
    PaperBrokerError,
    is_terminal_status,
)
from hermes_quant.react.multileg import LiveMultiLegNotAuthorized


def _leg(symbol, side, intent, *, price=None, ratio=1) -> OptionLeg:
    return OptionLeg(
        symbol=symbol,
        side=side,
        position_intent=intent,
        ratio_qty=ratio,
        greeks_at_decision=OptionGreeksSnapshot(
            delta=0.3, gamma=0.01, theta=-0.02, vega=0.1, rho=0.01, iv=0.4
        ),
        fill_price=price,
    )


def test_deterministic_mleg_fill_sums_to_net(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_QUANT_MULTILEG_REACTOR", raising=False)
    broker = PaperBroker(paper=True)
    legs = (
        _leg("NVDA271217C00120000", "buy", "buy_to_open", price=45.0),
        _leg("NVDA260626C00160000", "sell", "sell_to_open", price=4.5),
    )
    res = broker.submit_mleg_order(
        legs, outer_qty=1, net_limit_price=40.5, tif="day", client_order_id="coid1"
    )
    assert res.source == "deterministic_model"
    assert res.status == "filled"
    # net = +45.0 (buy) - 4.5 (sell) = 40.5
    assert abs(res.net_fill_price - 40.5) < 1e-9
    assert len(res.legs) == 2
    # Signed per-leg qty: +1 long, -1 short
    by_sym = {f.symbol: f for f in res.legs}
    assert by_sym["NVDA271217C00120000"].filled_qty == 1
    assert by_sym["NVDA260626C00160000"].filled_qty == -1


def test_non_paper_account_raises(monkeypatch) -> None:
    with pytest.raises(LiveMultiLegNotAuthorized):
        PaperBroker(paper=False)


def test_mleg_body_shape(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_QUANT_MULTILEG_REACTOR", raising=False)
    legs = (
        _leg("AAPL250117P00190000", "buy", "buy_to_open"),
        _leg("AAPL250117P00195000", "sell", "sell_to_open"),
    )
    body = PaperBroker.build_mleg_body(
        legs, outer_qty=2, net_limit_price=-1.8, tif="day", client_order_id="coidX"
    )
    assert body["order_class"] == "mleg"
    assert body["qty"] == "2"
    assert body["type"] == "limit"
    assert body["limit_price"] == "-1.8"  # NEGATIVE = net credit
    assert body["client_order_id"] == "coidX"
    assert len(body["legs"]) == 2
    leg0 = body["legs"][0]
    assert set(leg0) == {"symbol", "ratio_qty", "side", "position_intent"}
    # No equity leg, no per-leg type/limit_price.
    assert all("type" not in lg and "limit_price" not in lg for lg in body["legs"])


def test_mleg_body_rejects_one_leg() -> None:
    with pytest.raises(PaperBrokerError):
        PaperBroker.build_mleg_body(
            (_leg("AAPL250117P00190000", "buy", "buy_to_open"),),
            outer_qty=1,
            net_limit_price=1.0,
            tif="day",
            client_order_id="x",
        )


def test_mleg_body_rejects_duplicate_symbols() -> None:
    legs = (
        _leg("AAPL250117P00190000", "buy", "buy_to_open"),
        _leg("AAPL250117P00190000", "sell", "sell_to_open"),
    )
    with pytest.raises(PaperBrokerError):
        PaperBroker.build_mleg_body(
            legs, outer_qty=1, net_limit_price=1.0, tif="day", client_order_id="x"
        )


def test_single_leg_and_equity_fills(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_QUANT_MULTILEG_REACTOR", raising=False)
    broker = PaperBroker(paper=True)
    call = _leg("NVDA260626C00160000", "sell", "sell_to_open", price=4.5)
    lf = broker.submit_single_leg_option(
        call, qty=1, limit_price=-4.5, tif="day", client_order_id="c"
    )
    assert lf.filled_avg_price == 4.5
    assert lf.filled_qty == -1  # short

    eq = broker.submit_equity(
        symbol="NVDA", qty=100, decision_price=160.0, client_order_id="c-eq"
    )
    assert eq.filled_avg_price == 160.0
    assert eq.filled_qty == 100.0


def test_terminal_status_helper() -> None:
    assert is_terminal_status("filled")
    assert is_terminal_status("rejected")
    assert not is_terminal_status("accepted")
    assert not is_terminal_status("pending_new")


def test_deterministic_replay_equality(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_QUANT_MULTILEG_REACTOR", raising=False)
    broker = PaperBroker(paper=True)
    legs = (
        _leg("NVDA271217C00120000", "buy", "buy_to_open", price=45.0),
        _leg("NVDA260626C00160000", "sell", "sell_to_open", price=4.5),
    )
    a = broker.submit_mleg_order(
        legs, outer_qty=1, net_limit_price=40.5, tif="day", client_order_id="coid1"
    )
    b = broker.submit_mleg_order(
        legs, outer_qty=1, net_limit_price=40.5, tif="day", client_order_id="coid1"
    )
    assert a == b  # frozen dataclass equality => byte-replay-equal
