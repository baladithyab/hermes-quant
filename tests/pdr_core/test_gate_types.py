"""ADR-0092 Increment-1-cont, STAGE 2: read-interfaces for the gate port.

The host-agnostic gate (landing next stage in ``hermes_quant.pdr_core.gate``)
consumes a NARROW read-surface of ``hermes_quant.protocol``'s money types and
emits ``protocol.Action``. This stage lifts that read-surface into
``hermes_quant.pdr_core.gate_types`` as frozen, host-blind dataclasses /
Protocols, plus the gate's OUTPUT type :class:`GateDecision` — the halt-triple
preserving verdict the live ``protocol.Action`` carries.

Three contracts are proven here:

  1. SHAPE — each read-interface exposes EXACTLY the fields/methods the live
     ``gate.py`` consumes (no more, no less of the load-bearing surface).
       * CoreMarketState: asset, asof, volatility, commission, spread,
         slippage_estimate, tz.
       * CorePortfolio: account_id, asset_class, asof + the three derived
         money-state reads drawdown_pct / daily_loss_pct / current_position_pct.
       * CoreHaltState: a runtime-checkable Protocol with is_halted(...).

  2. NaN-FAIL-CLOSED PARITY — CorePortfolio's drawdown_pct / daily_loss_pct /
     current_position_pct reproduce ``protocol.Portfolio``'s sentinels
     BIT-FOR-BIT (same ``_finite_or`` semantics): a non-finite peak/equity must
     NOT launder into a benign 0.0 — it returns the >max sentinel so the Rule-1/2
     circuit breaker trips (money-safety, ADR-0004). Parity is checked against
     the LIVE protocol.Portfolio over a fixture matrix.

  3. GateDecision — the gate OUTPUT, a frozen dataclass carrying the FULL halt
     triple (halt / halt_scope / halt_until). This is the type that resolves the
     riskiest coupling: collapsing the gate verdict onto pdr_core.Proposal (which
     has NO halt fields) would silently DROP the durable-HALT verdict = money
     regression. GateDecision must be frozen and round-trip the triple verbatim.

RED-first: with ``hermes_quant.pdr_core.gate_types`` absent the module import
below fails at collection → every test errors. Creating the module turns GREEN.
"""

from __future__ import annotations

import dataclasses
import math

import pytest

# --- the module under construction (new home) ------------------------------
from hermes_quant.pdr_core.gate_types import (
    CoreHaltState,
    CoreMarketState,
    CorePortfolio,
    GateDecision,
)

# --- the LIVE money contracts (parity oracle) ------------------------------
from hermes_quant.protocol import Portfolio, Position

NAN = float("nan")
INF = float("inf")


# ---------------------------------------------------------------------------
# Helpers — build a live protocol.Portfolio and a CorePortfolio with the SAME
# money-state numbers so the NaN-fail-closed sentinels can be compared.
# ---------------------------------------------------------------------------


def _live_portfolio(
    *,
    equity_total: float,
    peak_equity: float,
    daily_open_equity: float,
    positions=None,
) -> Portfolio:
    return Portfolio(
        account_id="alpaca-paper",
        asset_class="equity",
        asof="2026-06-12T15:00:00+00:00",
        positions=positions or {},
        cash=1000.0,
        equity_total=equity_total,
        realized_pnl_total=0.0,
        realized_fees_total=0.0,
        peak_equity=peak_equity,
        daily_open_equity=daily_open_equity,
    )


def _core_portfolio(
    *,
    equity_total: float,
    peak_equity: float,
    daily_open_equity: float,
    positions=None,
) -> CorePortfolio:
    return CorePortfolio(
        account_id="alpaca-paper",
        asset_class="equity",
        asof="2026-06-12T15:00:00+00:00",
        positions=positions or {},
        equity_total=equity_total,
        peak_equity=peak_equity,
        daily_open_equity=daily_open_equity,
    )


def _eq_nan(a: float, b: float) -> bool:
    """Exact float equality with NaN == NaN."""
    if isinstance(a, float) and math.isnan(a):
        return isinstance(b, float) and math.isnan(b)
    return a == b


# ---------------------------------------------------------------------------
# Gate 1 — SHAPE: each read-interface exposes the gate-consumed surface.
# ---------------------------------------------------------------------------


def test_core_market_state_has_gate_consumed_fields() -> None:
    ms = CoreMarketState(
        asset="AAPL",
        asof="2026-06-12T15:00:00+00:00",
        volatility=0.02,
        commission=0.001,
        spread=0.0008,
        slippage_estimate=0.0005,
        tz="America/New_York",
    )
    assert ms.asset == "AAPL"
    assert ms.volatility == 0.02
    assert ms.commission == 0.001
    assert ms.spread == 0.0008
    assert ms.slippage_estimate == 0.0005
    assert ms.tz == "America/New_York"
    # Default tz mirrors protocol.MarketState (UTC).
    ms_utc = CoreMarketState(
        asset="BTC/USDT",
        asof="2026-06-12T15:00:00+00:00",
        volatility=0.03,
        commission=0.001,
        spread=0.0008,
        slippage_estimate=0.0012,
    )
    assert ms_utc.tz == "UTC"


def test_core_portfolio_has_gate_consumed_surface() -> None:
    pf = _core_portfolio(equity_total=100_000.0, peak_equity=100_000.0, daily_open_equity=100_000.0)
    assert pf.account_id == "alpaca-paper"
    assert pf.asset_class == "equity"
    # The three derived money-state reads the gate calls.
    assert hasattr(pf, "drawdown_pct")
    assert hasattr(pf, "daily_loss_pct")
    assert callable(pf.current_position_pct)


def test_core_halt_state_is_runtime_checkable_protocol() -> None:
    class _Halts:
        def is_halted(self, account_id, asset_class, asset=None) -> bool:
            return account_id == "blocked"

    h = _Halts()
    assert isinstance(h, CoreHaltState)
    assert h.is_halted("blocked", "equity", "AAPL") is True
    assert h.is_halted("ok", "equity", "AAPL") is False

    class _NotHalts:
        pass

    assert not isinstance(_NotHalts(), CoreHaltState)


# ---------------------------------------------------------------------------
# Gate 2 — NaN-fail-closed PARITY against the live protocol.Portfolio.
# ---------------------------------------------------------------------------

# (equity_total, peak_equity, daily_open_equity) fixture matrix. Covers the
# benign path AND every non-finite / non-positive sentinel branch in the
# protocol.py properties.
_MONEY_MATRIX = [
    (100_000.0, 100_000.0, 100_000.0),  # flat — no drawdown / loss
    (90_000.0, 100_000.0, 95_000.0),  # 10% drawdown, ~5.3% daily loss
    (110_000.0, 100_000.0, 100_000.0),  # above peak/open — clamp to 0
    (NAN, 100_000.0, 100_000.0),  # non-finite equity → sentinel 1.0
    (100_000.0, NAN, 100_000.0),  # non-finite peak → drawdown sentinel 1.0
    (100_000.0, 100_000.0, NAN),  # non-finite daily-open → daily sentinel 1.0
    (100_000.0, 0.0, 100_000.0),  # non-positive peak → drawdown 0.0 branch
    (100_000.0, 100_000.0, 0.0),  # non-positive daily-open → daily 0.0 branch
    (100_000.0, -50.0, 100_000.0),  # negative peak → 0.0 branch
    (INF, 100_000.0, 100_000.0),  # +inf equity → non-finite → sentinel
    (100_000.0, INF, 100_000.0),  # +inf peak → non-finite → sentinel
]


@pytest.mark.parametrize("equity_total,peak_equity,daily_open_equity", _MONEY_MATRIX)
def test_drawdown_pct_parity_with_protocol(
    equity_total: float, peak_equity: float, daily_open_equity: float
) -> None:
    live = _live_portfolio(
        equity_total=equity_total, peak_equity=peak_equity, daily_open_equity=daily_open_equity
    )
    core = _core_portfolio(
        equity_total=equity_total, peak_equity=peak_equity, daily_open_equity=daily_open_equity
    )
    assert _eq_nan(core.drawdown_pct, live.drawdown_pct), (
        f"drawdown_pct parity broke: core={core.drawdown_pct!r} live={live.drawdown_pct!r}"
    )


@pytest.mark.parametrize("equity_total,peak_equity,daily_open_equity", _MONEY_MATRIX)
def test_daily_loss_pct_parity_with_protocol(
    equity_total: float, peak_equity: float, daily_open_equity: float
) -> None:
    live = _live_portfolio(
        equity_total=equity_total, peak_equity=peak_equity, daily_open_equity=daily_open_equity
    )
    core = _core_portfolio(
        equity_total=equity_total, peak_equity=peak_equity, daily_open_equity=daily_open_equity
    )
    assert _eq_nan(core.daily_loss_pct, live.daily_loss_pct), (
        f"daily_loss_pct parity broke: core={core.daily_loss_pct!r} live={live.daily_loss_pct!r}"
    )


# Position fixtures for current_position_pct parity (asset, qty, mark_price, equity).
_POSITION_MATRIX = [
    ("AAPL", 100.0, 200.0, 100_000.0),  # 20% long
    ("AAPL", -50.0, 200.0, 100_000.0),  # 5% short (signed)
    ("AAPL", NAN, 200.0, 100_000.0),  # non-finite qty → NaN sentinel
    ("AAPL", 100.0, NAN, 100_000.0),  # non-finite mark → NaN sentinel
    ("AAPL", 100.0, 200.0, NAN),  # non-finite equity → NaN sentinel
    ("AAPL", 100.0, 200.0, 0.0),  # non-positive equity → NaN sentinel
    ("AAPL", 100.0, 200.0, INF),  # +inf equity → non-finite → NaN sentinel
]


@pytest.mark.parametrize("asset,qty,mark_price,equity_total", _POSITION_MATRIX)
def test_current_position_pct_with_position_parity(
    asset: str, qty: float, mark_price: float, equity_total: float
) -> None:
    pos = Position(
        asset=asset,
        qty=qty,
        avg_entry_price=190.0,
        mark_price=mark_price,
        unrealized_pnl=0.0,
        realized_fees=0.0,
    )
    live = _live_portfolio(
        equity_total=equity_total,
        peak_equity=100_000.0,
        daily_open_equity=100_000.0,
        positions={asset: pos},
    )
    core = _core_portfolio(
        equity_total=equity_total,
        peak_equity=100_000.0,
        daily_open_equity=100_000.0,
        positions={asset: pos},
    )
    assert _eq_nan(core.current_position_pct(asset), live.current_position_pct(asset)), (
        f"current_position_pct parity broke: core={core.current_position_pct(asset)!r} "
        f"live={live.current_position_pct(asset)!r}"
    )


def test_current_position_pct_no_position_is_zero_parity() -> None:
    """No position for the asset → 0.0 on BOTH (finite-equity branch)."""
    live = _live_portfolio(equity_total=100_000.0, peak_equity=100_000.0, daily_open_equity=100_000.0)
    core = _core_portfolio(equity_total=100_000.0, peak_equity=100_000.0, daily_open_equity=100_000.0)
    assert core.current_position_pct("MSFT") == live.current_position_pct("MSFT") == 0.0


def test_current_position_pct_no_position_nonfinite_equity_parity() -> None:
    """No position but non-finite equity → protocol returns NaN (fail-closed);
    the core read-interface must reproduce that EXACTLY."""
    live = _live_portfolio(equity_total=NAN, peak_equity=100_000.0, daily_open_equity=100_000.0)
    core = _core_portfolio(equity_total=NAN, peak_equity=100_000.0, daily_open_equity=100_000.0)
    assert _eq_nan(core.current_position_pct("MSFT"), live.current_position_pct("MSFT"))


# ---------------------------------------------------------------------------
# Gate 3 — GateDecision: frozen + round-trips the FULL halt triple.
# ---------------------------------------------------------------------------


def test_gate_decision_is_frozen() -> None:
    d = GateDecision(target_position_pct=0.10, reason="ok")
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.target_position_pct = 0.20  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.halt = True  # type: ignore[misc]


def test_gate_decision_defaults_match_action() -> None:
    """A non-halt decision defaults exactly like protocol.Action: halt=False,
    halt_scope=None, halt_until=None, signal_id=None."""
    d = GateDecision(target_position_pct=0.05, reason="signal_passed")
    assert d.target_position_pct == 0.05
    assert d.reason == "signal_passed"
    assert d.signal_id is None
    assert d.halt is False
    assert d.halt_scope is None
    assert d.halt_until is None


def test_gate_decision_round_trips_drawdown_halt_triple() -> None:
    """Rule 1 (drawdown): halt=True, halt_scope=(account, asset_class, None),
    halt_until=None (explicit-resume). The triple must survive verbatim — this
    is the verdict that collapsing onto pdr_core.Proposal would DROP."""
    d = GateDecision(
        target_position_pct=0.0,
        reason="drawdown_circuit_breaker_0.1600",
        halt=True,
        halt_scope=("alpaca-paper", "equity", None),
        halt_until=None,
    )
    assert d.halt is True
    assert d.halt_scope == ("alpaca-paper", "equity", None)
    assert d.halt_until is None
    assert d.target_position_pct == 0.0


def test_gate_decision_round_trips_daily_loss_halt_until() -> None:
    """Rule 2 (daily-loss): halt_until carries a session-open timestamp (ISO str
    or pd.Timestamp — typed Any so the core imports no pandas). It must round-trip
    verbatim so the daily-loss auto-clear is not silently dropped."""
    until = "2026-06-13T00:00:00+00:00"
    d = GateDecision(
        target_position_pct=0.0,
        reason="daily_loss_circuit_breaker_0.0600",
        halt=True,
        halt_scope=("alpaca-paper", "equity", None),
        halt_until=until,
    )
    assert d.halt is True
    assert d.halt_scope == ("alpaca-paper", "equity", None)
    assert d.halt_until == until


def test_gate_decision_carries_signal_id() -> None:
    d = GateDecision(target_position_pct=0.10, reason="ok", signal_id="sig-123")
    assert d.signal_id == "sig-123"
