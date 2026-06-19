"""Unit tests for ``DeterministicBackend`` — the local trading simulator (ADR-0088).

Covers the correctness rails that distinguish a TRUSTWORTHY simulator from an
append-log: buying-power enforcement (the old append-log allowed ~880% gross),
fail-closed on unknown account state, true signed units, honest option pricing,
mleg net reconstruction, and byte-for-byte determinism (no RNG, no network).

Account state is controlled by monkeypatching ``get_portfolio_state`` /
``_default_initial_cash`` in ``hermes_quant.state.portfolio_state`` (the lazy import
the backend uses), so no DB and no network are touched.
"""

from __future__ import annotations

import pytest

from hermes_quant.options.data import OptionLeg
from hermes_quant.react.backend import (
    BackendUnavailableError,
    FillResult,
    InsufficientBuyingPowerError,
)
from hermes_quant.react.backends.deterministic_backend import DeterministicBackend

# A real OCC-21 symbol (mirrors the options-data test fixtures).
_OCC_CALL = "NVDA260612C00150000"
_OCC_PUT = "NVDA260612P00140000"
_OCC_LONG = "NVDA260612C00130000"


class _FakeCash:
    def __init__(self, *, balance_usd: float, equity_total: float) -> None:
        self.account_id = "paper-default"
        self.balance_usd = balance_usd
        self.last_update_at = "2026-06-05T00:00:00Z"
        self.equity_total = equity_total


class _FakePortfolio:
    def __init__(self, cash: _FakeCash | None) -> None:
        self._cash = cash

    def get_cash(self, account_id: str) -> _FakeCash | None:
        assert account_id == "paper-default"
        return self._cash


def _install_account(
    monkeypatch: pytest.MonkeyPatch,
    *,
    balance_usd: float = 100_000.0,
    equity_total: float = 100_000.0,
    cash_present: bool = True,
    boot: float = 100_000.0,
    raise_on_lookup: bool = False,
) -> None:
    """Wire fake portfolio state into the lazy import the backend performs."""
    import hermes_quant.state.portfolio_state as ps

    if raise_on_lookup:

        def _boom(*_a: object, **_k: object) -> object:
            raise RuntimeError("state db unavailable")

        monkeypatch.setattr(ps, "get_portfolio_state", _boom)
        monkeypatch.setattr(ps, "_default_initial_cash", _boom)
        return

    cash = (
        _FakeCash(balance_usd=balance_usd, equity_total=equity_total)
        if cash_present
        else None
    )
    monkeypatch.setattr(
        ps, "get_portfolio_state", lambda *a, **k: _FakePortfolio(cash)
    )
    monkeypatch.setattr(ps, "_default_initial_cash", lambda: boot)


def _leg(
    symbol: str,
    side: str,
    *,
    intent: str | None = None,
    ratio_qty: int = 1,
    fill_price: float | None = None,
) -> OptionLeg:
    if intent is None:
        intent = "buy_to_open" if side == "buy" else "sell_to_open"
    return OptionLeg(
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        position_intent=intent,  # type: ignore[arg-type]
        ratio_qty=ratio_qty,
        fill_price=fill_price,
    )


# ---------------------------------------------------------------------------
# Account state
# ---------------------------------------------------------------------------


def test_account_equity_from_equity_total(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_account(monkeypatch, equity_total=123_456.0)
    assert DeterministicBackend().account_equity() == pytest.approx(123_456.0)


def test_account_equity_falls_back_to_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_account(monkeypatch, cash_present=False, boot=50_000.0)
    assert DeterministicBackend().account_equity() == pytest.approx(50_000.0)


def test_account_equity_none_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_account(monkeypatch, raise_on_lookup=True)
    assert DeterministicBackend().account_equity() is None


def test_buying_power_is_free_cash(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_account(monkeypatch, balance_usd=42_000.0, equity_total=99_000.0)
    # BP is the literal free cash, NOT equity_total (cash-account model).
    assert DeterministicBackend().buying_power() == pytest.approx(42_000.0)


def test_buying_power_none_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_account(monkeypatch, raise_on_lookup=True)
    assert DeterministicBackend().buying_power() is None


# ---------------------------------------------------------------------------
# Equity submit — fields + buying-power enforcement
# ---------------------------------------------------------------------------


def test_equity_long_fill_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_account(monkeypatch, balance_usd=100_000.0)
    be = DeterministicBackend()
    r = be.submit_equity(
        symbol="NVDA",
        signed_qty=100.0,
        decision_price=150.0,
        client_order_id="abcdef0123456789longtail",
    )
    assert isinstance(r, FillResult)
    assert r.symbol == "NVDA"
    assert r.filled_avg_price == pytest.approx(150.0)
    assert r.filled_qty == pytest.approx(100.0)
    assert r.status == "filled"
    assert r.position_intent == "buy_to_open"
    assert r.source == "deterministic"
    assert r.order_id == "det-abcdef0123456789"  # truncated to 16 chars
    assert r.is_fill is True


def test_equity_short_fill_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_account(monkeypatch, balance_usd=100_000.0)
    r = DeterministicBackend().submit_equity(
        symbol="NVDA",
        signed_qty=-100.0,
        decision_price=150.0,
        client_order_id="short01",
    )
    assert r.filled_qty == pytest.approx(-100.0)
    assert r.position_intent == "sell_to_open"
    assert r.filled_avg_price == pytest.approx(150.0)


def test_equity_rejects_when_notional_exceeds_bp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # BP = $10k; 100 sh * $150 = $15k notional -> must REFUSE (the whole point).
    _install_account(monkeypatch, balance_usd=10_000.0)
    with pytest.raises(InsufficientBuyingPowerError):
        DeterministicBackend().submit_equity(
            symbol="NVDA",
            signed_qty=100.0,
            decision_price=150.0,
            client_order_id="overlev",
        )


def test_equity_fill_exactly_at_bp_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Notional exactly equals BP -> allowed (epsilon tolerance, no spurious reject).
    _install_account(monkeypatch, balance_usd=15_000.0)
    r = DeterministicBackend().submit_equity(
        symbol="NVDA",
        signed_qty=100.0,
        decision_price=150.0,
        client_order_id="exact",
    )
    assert r.is_fill is True


def test_equity_unknown_bp_raises_backend_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Unknown BP must FAIL CLOSED (never assume infinite BP / fabricate a fill).
    _install_account(monkeypatch, raise_on_lookup=True)
    with pytest.raises(BackendUnavailableError):
        DeterministicBackend().submit_equity(
            symbol="NVDA",
            signed_qty=1.0,
            decision_price=1.0,
            client_order_id="unknownbp",
        )


# ---------------------------------------------------------------------------
# Single-leg option
# ---------------------------------------------------------------------------


def test_option_single_buy_uses_premium_and_signs_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_account(monkeypatch, balance_usd=100_000.0)
    leg = _leg(_OCC_CALL, "buy", intent="buy_to_open", fill_price=2.50)
    r = DeterministicBackend().submit_option_single(
        leg, qty=3, limit_price=9.99, client_order_id="optbuy"
    )
    # fill_price (2.50) preferred over abs(limit_price); +3 contracts (long).
    assert r.filled_avg_price == pytest.approx(2.50)
    assert r.filled_qty == pytest.approx(3.0)
    assert r.position_intent == "buy_to_open"
    assert r.source == "deterministic"


def test_option_single_buy_rejected_when_premium_exceeds_bp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 3 contracts * $2.50 * 100 = $750 debit; BP = $500 -> refuse.
    _install_account(monkeypatch, balance_usd=500.0)
    leg = _leg(_OCC_CALL, "buy", fill_price=2.50)
    with pytest.raises(InsufficientBuyingPowerError):
        DeterministicBackend().submit_option_single(
            leg, qty=3, limit_price=2.50, client_order_id="optbuybig"
        )


def test_option_single_sell_receives_premium_not_bp_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A SHORT option receives premium; even with ~zero BP it is NOT blocked here
    # (collateral is the upstream gate's responsibility).
    _install_account(monkeypatch, balance_usd=1.0)
    leg = _leg(_OCC_CALL, "sell", intent="sell_to_open", fill_price=2.50)
    r = DeterministicBackend().submit_option_single(
        leg, qty=2, limit_price=2.50, client_order_id="optsell"
    )
    assert r.filled_qty == pytest.approx(-2.0)  # short = negative contracts
    assert r.filled_avg_price == pytest.approx(2.50)
    assert r.position_intent == "sell_to_open"


def test_option_single_falls_back_to_limit_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_account(monkeypatch, balance_usd=100_000.0)
    leg = _leg(_OCC_CALL, "buy", fill_price=None)  # no recorded mid
    r = DeterministicBackend().submit_option_single(
        leg, qty=1, limit_price=-3.25, client_order_id="optlim"
    )
    assert r.filled_avg_price == pytest.approx(3.25)  # abs(limit_price)


# ---------------------------------------------------------------------------
# Multi-leg option
# ---------------------------------------------------------------------------


def test_mleg_priced_legs_reconstruct_net(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_account(monkeypatch, balance_usd=100_000.0)
    # Debit vertical: buy @ 5.00, sell @ 2.00 -> net debit 3.00.
    legs = (
        _leg(_OCC_LONG, "buy", intent="buy_to_open", fill_price=5.00),
        _leg(_OCC_CALL, "sell", intent="sell_to_open", fill_price=2.00),
    )
    r = DeterministicBackend().submit_option_mleg(
        legs, outer_qty=1, net_limit_price=3.00, client_order_id="mlegdebit"
    )
    assert r.status == "filled"
    assert r.net_fill_price == pytest.approx(3.00)
    assert r.filled_qty == pytest.approx(1.0)  # parent structure count
    assert len(r.legs) == 2
    # Per-leg signed price contribution reconstructs the net debit.
    net = sum(
        (1.0 if lf.filled_qty > 0 else -1.0) * lf.filled_avg_price * abs(lf.filled_qty)
        for lf in r.legs
    )
    assert net == pytest.approx(3.00)
    # Signed contracts: +1 long, -1 short.
    by_sym = {lf.symbol: lf.filled_qty for lf in r.legs}
    assert by_sym[_OCC_LONG] == pytest.approx(1.0)
    assert by_sym[_OCC_CALL] == pytest.approx(-1.0)


def test_mleg_apportions_unpriced_net_by_ratio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_account(monkeypatch, balance_usd=100_000.0)
    # No leg has a recorded mid -> the net is apportioned by ratio_qty.
    legs = (
        _leg(_OCC_LONG, "buy", intent="buy_to_open", ratio_qty=1, fill_price=None),
        _leg(_OCC_CALL, "sell", intent="sell_to_open", ratio_qty=1, fill_price=None),
    )
    r = DeterministicBackend().submit_option_mleg(
        legs, outer_qty=2, net_limit_price=4.00, client_order_id="mlegappt"
    )
    # signed_qty = sign * ratio_qty * outer_qty.
    by_sym = {lf.symbol: lf.filled_qty for lf in r.legs}
    assert by_sym[_OCC_LONG] == pytest.approx(2.0)
    assert by_sym[_OCC_CALL] == pytest.approx(-2.0)
    assert r.net_fill_price == pytest.approx(4.00)
    assert r.filled_qty == pytest.approx(2.0)


def _reconstruct_net_per_struct(r: FillResult) -> float:
    """Per-structure net = Sum(sign * per_contract_price * ratio_qty).

    ``filled_qty`` carries ``sign * ratio_qty * outer_qty``, so the per-structure
    ratio is ``abs(filled_qty) / outer_qty``. The per-leg signed price
    contributions MUST sum to the gate-approved ``net_limit_price`` so the cash /
    per-leg-basis / equity_total booked downstream reconstruct the family the gate
    sized — not a sign-collapsed phantom.
    """
    outer = r.filled_qty or 1.0
    return sum(
        (1.0 if lf.filled_qty > 0 else -1.0)
        * lf.filled_avg_price
        * (abs(lf.filled_qty) / outer)
        for lf in r.legs
    )


def test_mleg_unpriced_opposite_side_legs_reconstruct_net(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ar50 RED: two UNPRICED opposite-side legs (buy + sell) whose per-leg signed
    # price contributions must reconstruct the net debit. The old abs(residual)/
    # outer_qty discarded the residual sign -> both legs got the same magnitude
    # price -> long(+) and short(-) contributions cancelled to 0.00, NOT the net.
    _install_account(monkeypatch, balance_usd=100_000.0)
    legs = (
        _leg(_OCC_LONG, "buy", intent="buy_to_open", ratio_qty=1, fill_price=None),
        _leg(_OCC_CALL, "sell", intent="sell_to_open", ratio_qty=1, fill_price=None),
    )
    r = DeterministicBackend().submit_option_mleg(
        legs, outer_qty=1, net_limit_price=4.00, client_order_id="mlegrecon"
    )
    # The defect: per-leg signed contributions sum to 0.00, not 4.00.
    assert _reconstruct_net_per_struct(r) == pytest.approx(4.00)


def test_mleg_unpriced_net_credit_reconstructs_signed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ar50 RED: a net-CREDIT structure (net_limit_price < 0) with unpriced
    # opposite-side legs. The signed residual must carry through so the credit is
    # reconstructed; abs(residual) booked a debit-magnitude on the wrong sign.
    _install_account(monkeypatch, balance_usd=100_000.0)
    legs = (
        _leg(_OCC_CALL, "sell", intent="sell_to_open", ratio_qty=1, fill_price=None),
        _leg(_OCC_LONG, "buy", intent="buy_to_open", ratio_qty=1, fill_price=None),
    )
    r = DeterministicBackend().submit_option_mleg(
        legs, outer_qty=1, net_limit_price=-3.00, client_order_id="mlegcreditrecon"
    )
    assert r.net_fill_price == pytest.approx(-3.00)
    assert _reconstruct_net_per_struct(r) == pytest.approx(-3.00)


def test_mleg_unpriced_mixed_with_priced_leg_reconstructs_net(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ar50 RED: one PRICED short leg + one UNPRICED long leg, outer_qty > 1. Both
    # the residual sign AND the per-structure scaling (priced_sum carries an
    # outer_qty factor that net_limit_price does not) must be handled so the
    # per-leg signed contributions reconstruct net_limit_price exactly.
    _install_account(monkeypatch, balance_usd=100_000.0)
    legs = (
        _leg(_OCC_LONG, "buy", intent="buy_to_open", ratio_qty=1, fill_price=None),
        _leg(_OCC_CALL, "sell", intent="sell_to_open", ratio_qty=1, fill_price=2.00),
    )
    # net debit 3.00 = (unpriced long) - 2.00 (priced short) -> long resid = 5.00.
    r = DeterministicBackend().submit_option_mleg(
        legs, outer_qty=3, net_limit_price=3.00, client_order_id="mlegmixed"
    )
    assert _reconstruct_net_per_struct(r) == pytest.approx(3.00)


def test_mleg_net_debit_rejected_when_exceeds_bp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # net debit 3.00 * 100 * outer_qty 5 = $1500 debit; BP = $1000 -> refuse.
    _install_account(monkeypatch, balance_usd=1_000.0)
    legs = (
        _leg(_OCC_LONG, "buy", fill_price=5.00),
        _leg(_OCC_CALL, "sell", fill_price=2.00),
    )
    with pytest.raises(InsufficientBuyingPowerError):
        DeterministicBackend().submit_option_mleg(
            legs, outer_qty=5, net_limit_price=3.00, client_order_id="mlegbig"
        )


def test_mleg_net_credit_not_bp_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    # A net CREDIT spread is not BP-blocked even with ~zero cash.
    _install_account(monkeypatch, balance_usd=1.0)
    legs = (
        _leg(_OCC_CALL, "sell", intent="sell_to_open", fill_price=5.00),
        _leg(_OCC_LONG, "buy", intent="buy_to_open", fill_price=2.00),
    )
    r = DeterministicBackend().submit_option_mleg(
        legs, outer_qty=1, net_limit_price=-3.00, client_order_id="mlegcredit"
    )
    assert r.status == "filled"
    assert r.net_fill_price == pytest.approx(-3.00)


# ---------------------------------------------------------------------------
# Determinism (no RNG -> identical inputs yield identical results)
# ---------------------------------------------------------------------------


def test_equity_determinism(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_account(monkeypatch, balance_usd=100_000.0)
    be = DeterministicBackend()
    r1 = be.submit_equity(
        symbol="NVDA", signed_qty=10.0, decision_price=150.0, client_order_id="detrepeat"
    )
    r2 = be.submit_equity(
        symbol="NVDA", signed_qty=10.0, decision_price=150.0, client_order_id="detrepeat"
    )
    assert r1 == r2  # frozen dataclass equality — byte-for-byte replay


def test_mleg_determinism(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_account(monkeypatch, balance_usd=100_000.0)
    be = DeterministicBackend()
    legs = (
        _leg(_OCC_LONG, "buy", fill_price=5.00),
        _leg(_OCC_CALL, "sell", fill_price=2.00),
    )
    r1 = be.submit_option_mleg(
        legs, outer_qty=1, net_limit_price=3.00, client_order_id="detmleg"
    )
    r2 = be.submit_option_mleg(
        legs, outer_qty=1, net_limit_price=3.00, client_order_id="detmleg"
    )
    assert r1 == r2
    assert r1.legs == r2.legs
