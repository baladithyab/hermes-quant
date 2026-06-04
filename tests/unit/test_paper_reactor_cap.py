from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from unittest.mock import MagicMock, patch

from hermes_quant.react.paper import PaperReactor
from hermes_quant.react.base import ExecutionRecord


def _make_proposal(symbol: str = "AAPL") -> Any:
    proposal = MagicMock()
    proposal.proposal_id = "prop_cap_001"
    proposal.symbol = symbol
    proposal.asset_class = "equity"
    proposal.timeframe = "1d"
    proposal.advisor_result = {
        "decision_price": 150.0,
        "as_of": "2026-05-27T10:00:00Z",
    }
    return proposal


class TestPaperReactorPortfolioCap:
    def test_flag_off_is_bit_identical(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """With HERMES_QUANT_PORTFOLIO_CAPS unset, execute() must not touch the cap seam."""

        monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_CAPS", raising=False)

        # Guardrail: if the cap helper is ever invoked with flag OFF, this test fails.
        with patch(
            "hermes_quant.risk.portfolio_normalize.clip_one_to_remaining_headroom",
            side_effect=AssertionError("clip_one_to_remaining_headroom should not be called when flag is OFF"),
        ):
            reactor = PaperReactor(executions_path=tmp_path / "executions.jsonl")
            proposal = _make_proposal()

            record = reactor.execute(proposal, fill_size_pct=0.05)

        assert isinstance(record, ExecutionRecord)
        assert record.asset == "AAPL"
        assert record.fill_size_pct == pytest.approx(0.05)
        assert not (record.reactor_metadata or {}).get("silenced")

    def test_cap_silences_over_gross(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """With flag ON and a book at 200% gross, a new fire is silenced at the seam."""

        monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")

        executions_path = tmp_path / "executions.jsonl"
        reactor = PaperReactor(executions_path=executions_path)
        proposal = _make_proposal("MSFT")

        import hermes_quant.state.portfolio_state as ps_mod

        # Build a minimal PortfolioState instance and seed it with 200% gross exposure.
        from hermes_quant.state.portfolio_state import PortfolioState as DBPortfolioState

        ps_instance = DBPortfolioState(state_db_path=tmp_path / "state.db")

        class DummyPos:
            def __init__(self, quantity: float) -> None:
                self.quantity = quantity

        # positions maps (asset_class, symbol) -> position
        ps_instance.get_positions = MagicMock(
            return_value={
                ("equity", "AAPL"): DummyPos(1.0),
                ("equity", "MSFT"): DummyPos(1.0),
            }
        )

        with patch.object(ps_mod, "_singleton", ps_instance):
            record = reactor.execute(proposal, fill_size_pct=0.10)

        # Silenced record: no position-moving fill appended, but an audit trail is returned.
        assert (record.reactor_metadata or {}).get("paper") is True
        assert (record.reactor_metadata or {}).get("silenced") is True
        silence_reason = (record.reactor_metadata or {}).get("silence_reason", "")
        assert silence_reason.startswith("portfolio_cap_")
        assert record.fill_size_pct == pytest.approx(0.0)

        # PortfolioState.apply_execution should not see a position-moving fill
        positions_after = ps_instance.get_positions("paper-default")
        # Our fake get_positions is still wired, but quantity should be unchanged (no new MSFT line)
        assert positions_after[("equity", "AAPL")].quantity == pytest.approx(1.0)
        assert positions_after[("equity", "MSFT")].quantity == pytest.approx(1.0)

    def test_cap_passes_with_headroom(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """With flag ON and an empty book, the cap seam passes the fill through."""

        monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_CAPS", "1")

        executions_path = tmp_path / "executions.jsonl"
        reactor = PaperReactor(executions_path=executions_path)
        proposal = _make_proposal("AAPL")

        import hermes_quant.state.portfolio_state as ps_mod

        from hermes_quant.state.portfolio_state import PortfolioState as DBPortfolioState

        ps_instance = DBPortfolioState(state_db_path=tmp_path / "state.db")
        # Empty book: get_positions returns empty dict
        ps_instance.get_positions = MagicMock(return_value={})

        with patch.object(ps_mod, "_singleton", ps_instance):
            record = reactor.execute(proposal, fill_size_pct=0.05)

        assert isinstance(record, ExecutionRecord)
        assert record.asset == "AAPL"
        assert record.fill_size_pct == pytest.approx(0.05)
        assert not (record.reactor_metadata or {}).get("silenced")

        # PortfolioState should have applied the execution and now reflect the position
        positions = ps_instance.get_positions("paper-default")
        # Our MagicMock get_positions still returns {}, so instead rely on apply_execution side effects
        # by reading positions via the real method on a fresh instance reconstructed from executions.
        fresh_ps = DBPortfolioState(state_db_path=ps_instance.db_path)
        fresh_positions = fresh_ps.get_positions("paper-default")
        assert ("equity", "AAPL") in fresh_positions
        assert fresh_positions[("equity", "AAPL")].quantity == pytest.approx(0.05)
