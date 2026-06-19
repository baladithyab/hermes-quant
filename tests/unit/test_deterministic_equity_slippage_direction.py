"""DeterministicEquityReactor slippage-direction wiring (ADR-0070).

The det-equity book tracks positions in TRUE SHARES (reactor_metadata.quantity),
so the reactor converts shares -> NAV-fraction before keying the slippage cost
direction off the traded delta. This guards that an exposure-REDUCING fill (trim
a long / partially cover a short) fills on the COST side, not the favorable side.

The SLIPPED price is what the backend fills at and what lands in fill_price, so a
mis-signed slip flows straight into the booked P&L (settlement_loop realized_return).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import hermes_quant.react.deterministic_equity as det_mod
from hermes_quant.proposals import Proposal
from hermes_quant.react.backend import FillResult
from hermes_quant.react.deterministic_equity import DeterministicEquityReactor


class _FakeBackend:
    """Fills AT the price it is handed (the slipped price) so fill_price reflects slippage."""

    name = "deterministic"

    def __init__(self, *, equity: float = 100_000.0) -> None:
        self._equity = equity

    def account_equity(self) -> float | None:
        return self._equity

    def buying_power(self) -> float | None:
        return self._equity

    def submit_equity(
        self, *, symbol: str, signed_qty: float, decision_price: float, client_order_id: str
    ) -> FillResult:
        return FillResult(
            symbol=symbol,
            filled_avg_price=decision_price,  # backend fills at the (slipped) price passed
            filled_qty=float(signed_qty),
            status="filled",
            position_intent="buy_to_open" if signed_qty > 0 else "sell_to_open",
            order_id=f"det-{client_order_id[:16]}",
            source=self.name,
        )


class _PSWithPositions:
    """Fake PortfolioState exposing get_positions (shares) + swallowing apply_execution."""

    def __init__(self, positions: dict[tuple[str, str], float]) -> None:
        self._positions = positions
        self.applied: list[dict[str, Any]] = []

    def get_positions(self, account_id: str) -> dict[tuple[str, str], Any]:
        class _Pos:
            def __init__(self, q: float) -> None:
                self.quantity = q

        return {k: _Pos(v) for k, v in self._positions.items()}

    def apply_execution(self, record: dict[str, Any]) -> None:
        self.applied.append(record)


def _proposal(*, symbol: str = "AAPL", decision_price: float = 100.0) -> Proposal:
    return Proposal(
        proposal_id=f"prop_2026-06-05T00:00:00_{symbol}_slipdir",
        state="pending",
        symbol=symbol,
        asset_class="equity",
        timeframe="1d",
        created_at="2026-06-05T00:00:00Z",
        expires_at="2026-06-05T01:00:00Z",
        advisor_result={
            "as_of": "2026-06-05T00:00:00Z",
            "decision_price": decision_price,
            "signal_id": "sig-1",
        },
    )


@pytest.fixture(autouse=True)
def _slippage_on(monkeypatch):
    for var in (
        "HERMES_QUANT_ADMISSIBILITY",
        "HERMES_QUANT_PORTFOLIO_CAPS",
        "HERMES_QUANT_BROKER_BACKEND",
        "HERMES_QUANT_ALPACA_PAPER",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_QUANT_REFLECTION", "0")
    monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.2")  # promoted default


def _wire(tmp_path: Path, backend: _FakeBackend, ps: _PSWithPositions, monkeypatch):
    monkeypatch.setattr(det_mod, "select_backend", lambda *a, **kw: backend)
    import hermes_quant.state.portfolio_state as ps_mod

    monkeypatch.setattr(ps_mod, "get_portfolio_state", lambda: ps)
    return DeterministicEquityReactor(executions_path=tmp_path / "executions.jsonl")


def test_trim_long_fills_below_decision(tmp_path, monkeypatch):
    """+20% NAV long (= +200 shares @100 on $100k NAV) trimmed to +5%: a SELL,
    so the recorded fill price must be BELOW decision_price."""
    # +0.20 NAV @ decision 100 on $100k NAV = 200 shares.
    backend = _FakeBackend(equity=100_000.0)
    ps = _PSWithPositions({("equity", "AAPL"): 200.0})
    reactor = _wire(tmp_path, backend, ps, monkeypatch)

    record = reactor.execute(_proposal(decision_price=100.0), fill_size_pct=0.05)

    assert record.decision_price == pytest.approx(100.0)
    assert record.fill_price < record.decision_price, (
        f"det-equity trim of a long is a SELL; fill must be below decision, "
        f"got fill={record.fill_price}"
    )


def test_partial_cover_short_fills_above_decision(tmp_path, monkeypatch):
    """-20% NAV short (= -200 shares) partially covered to -5%: a BUY, so the
    recorded fill price must be ABOVE decision_price."""
    backend = _FakeBackend(equity=100_000.0)
    ps = _PSWithPositions({("equity", "AAPL"): -200.0})
    reactor = _wire(tmp_path, backend, ps, monkeypatch)

    record = reactor.execute(_proposal(decision_price=100.0), fill_size_pct=-0.05)

    assert record.fill_price > record.decision_price, (
        f"det-equity cover of a short is a BUY; fill must be above decision, "
        f"got fill={record.fill_price}"
    )


def test_opening_long_unchanged_above_decision(tmp_path, monkeypatch):
    """REGRESSION: opening a long on an empty book still fills ABOVE decision."""
    backend = _FakeBackend(equity=100_000.0)
    ps = _PSWithPositions({})  # empty book -> current 0.0
    reactor = _wire(tmp_path, backend, ps, monkeypatch)

    record = reactor.execute(_proposal(decision_price=100.0), fill_size_pct=0.20)

    assert record.fill_price > record.decision_price
