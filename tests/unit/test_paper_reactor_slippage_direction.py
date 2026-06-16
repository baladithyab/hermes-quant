"""PaperReactor / DeterministicEquityReactor slippage-direction wiring (ADR-0070).

These are END-TO-END guards that the reactor threads the CURRENT position into
the slippage model so an exposure-REDUCING fill (trim a long, partially cover a
short) pays the correct opposite-side slippage instead of being credited with a
favorable price.

Defect (pre-fix): apply_slippage keyed its cost direction off the sign of the
ABSOLUTE post-fill target (fill_size_pct, ADR-0091 Option E), with no knowledge
of the current position. Both live reactors pass target_pct=fill_size_pct, so a
+0.20 long trimmed to +0.05 fired direction_sign=+1 -> fill_price ABOVE decision,
which is a FAVORABLE price for what is really a SELL. The settlement loop then
booked realized_return = (fill_price - decision_price)/decision_price > 0,
crediting the trim leg with phantom P&L. v0.2 is the promoted default-ON envelope.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hermes_quant.react.base import ExecutionRecord
from hermes_quant.react.paper import PaperReactor


def _make_proposal(symbol: str = "AAPL") -> Any:
    proposal = MagicMock()
    proposal.proposal_id = "prop_slip_dir_001"
    proposal.symbol = symbol
    proposal.asset_class = "equity"
    proposal.timeframe = "1d"
    proposal.advisor_result = {
        "decision_price": 150.0,
        "as_of": "2026-05-27T10:00:00Z",
    }
    proposal.reactor_metadata = None
    return proposal


class _DummyPos:
    def __init__(self, quantity: float) -> None:
        self.quantity = quantity


def _seed_book(ps_instance: Any, positions: dict[tuple[str, str], _DummyPos]) -> None:
    ps_instance.get_positions = MagicMock(return_value=positions)


class TestPaperReactorSlippageDirection:
    def test_trim_long_fills_below_decision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Trim a +0.20 long to +0.05 (a SELL of ~15% NAV): the recorded fill
        price must be BELOW decision_price (a sell receives a worse price), NOT
        above it as the pre-fix target-sign logic produced."""
        monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.2")
        monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)

        import hermes_quant.state.portfolio_state as ps_mod
        from hermes_quant.state.portfolio_state import PortfolioState as DBPortfolioState

        ps_instance = DBPortfolioState(state_db_path=tmp_path / "state.db")
        _seed_book(ps_instance, {("equity", "AAPL"): _DummyPos(0.20)})

        reactor = PaperReactor(executions_path=tmp_path / "executions.jsonl")
        proposal = _make_proposal("AAPL")

        with patch.object(ps_mod, "_singleton", ps_instance):
            record = reactor.execute(proposal, fill_size_pct=0.05)

        assert isinstance(record, ExecutionRecord)
        assert record.decision_price == pytest.approx(150.0)
        # The headline assertion: a trim (SELL) must fill BELOW decision.
        assert record.fill_price < record.decision_price, (
            f"trim of a long is a SELL; fill must be below decision, "
            f"got fill={record.fill_price} decision={record.decision_price}"
        )
        bd = (record.reactor_metadata or {}).get("slippage_breakdown") or {}
        assert bd.get("total_bps", 0.0) > 0.0  # slippage really fired

    def test_partial_cover_short_fills_above_decision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Partially cover a -0.20 short to -0.05 (a BUY of ~15% NAV): the fill
        price must be ABOVE decision_price (paying up to buy back)."""
        monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.2")
        monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)

        import hermes_quant.state.portfolio_state as ps_mod
        from hermes_quant.state.portfolio_state import PortfolioState as DBPortfolioState

        ps_instance = DBPortfolioState(state_db_path=tmp_path / "state.db")
        _seed_book(ps_instance, {("equity", "AAPL"): _DummyPos(-0.20)})

        reactor = PaperReactor(executions_path=tmp_path / "executions.jsonl")
        proposal = _make_proposal("AAPL")

        with patch.object(ps_mod, "_singleton", ps_instance):
            record = reactor.execute(proposal, fill_size_pct=-0.05)

        assert record.fill_price > record.decision_price, (
            f"covering a short is a BUY; fill must be above decision, "
            f"got fill={record.fill_price} decision={record.decision_price}"
        )

    def test_realized_return_on_trim_is_negative_cost(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end money assertion: the realized_return the settlement loop
        derives from a trim fill must be a COST (<= 0), not phantom positive P&L.

        The settlement loop derives side='buy' from the positive post-fill target
        (fill_size_pct=+0.05 > 0) and computes realized_return =
        (fill_price - decision_price)/decision_price for a buy. With the fix the
        trim fills BELOW decision, so realized_return is negative — a cost — rather
        than the favorable positive value the pre-fix code booked.
        """
        monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.2")
        monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)

        import hermes_quant.state.portfolio_state as ps_mod
        from hermes_quant.state.portfolio_state import PortfolioState as DBPortfolioState

        ps_instance = DBPortfolioState(state_db_path=tmp_path / "state.db")
        _seed_book(ps_instance, {("equity", "AAPL"): _DummyPos(0.20)})

        reactor = PaperReactor(executions_path=tmp_path / "executions.jsonl")
        proposal = _make_proposal("AAPL")

        with patch.object(ps_mod, "_singleton", ps_instance):
            record = reactor.execute(proposal, fill_size_pct=0.05)

        # Mirror the settlement-loop single-fill realized_return formula
        # (daemon/settlement_loop.py: side derived buy from positive fill_size_pct).
        decision_price = record.decision_price
        fill_price = record.fill_price
        realized_return = (fill_price - decision_price) / decision_price
        assert realized_return < 0.0, (
            f"a trim leg must book a slippage COST, not phantom positive P&L; "
            f"realized_return={realized_return}"
        )

    def test_opening_fill_unchanged_above_decision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REGRESSION: a genuine opening long (empty book) still fills ABOVE
        decision (a buy pays up) — the fix is a no-op for opening fills."""
        monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.2")
        monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)

        import hermes_quant.state.portfolio_state as ps_mod
        from hermes_quant.state.portfolio_state import PortfolioState as DBPortfolioState

        ps_instance = DBPortfolioState(state_db_path=tmp_path / "state.db")
        _seed_book(ps_instance, {})  # empty book -> current position 0.0

        reactor = PaperReactor(executions_path=tmp_path / "executions.jsonl")
        proposal = _make_proposal("AAPL")

        with patch.object(ps_mod, "_singleton", ps_instance):
            record = reactor.execute(proposal, fill_size_pct=0.20)

        assert record.fill_price > record.decision_price

    def test_state_read_failure_degrades_to_legacy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Silence-by-default: if the position read raises, the helper returns 0.0
        and the fill still proceeds (never blocked). An opening-shaped fill keeps
        the legacy direction (a +long still fills above decision)."""
        monkeypatch.setenv("HERMES_QUANT_PAPER_SLIPPAGE_MODEL", "v0.2")
        monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)

        import hermes_quant.state.portfolio_state as ps_mod
        from hermes_quant.state.portfolio_state import PortfolioState as DBPortfolioState

        ps_instance = DBPortfolioState(state_db_path=tmp_path / "state.db")
        ps_instance.get_positions = MagicMock(side_effect=RuntimeError("db locked"))

        reactor = PaperReactor(executions_path=tmp_path / "executions.jsonl")
        proposal = _make_proposal("AAPL")

        with patch.object(ps_mod, "_singleton", ps_instance):
            record = reactor.execute(proposal, fill_size_pct=0.20)

        assert isinstance(record, ExecutionRecord)
        assert record.fill_price > record.decision_price  # legacy long-above-decision
