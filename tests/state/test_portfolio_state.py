"""Comprehensive tests for hermes_quant.state.portfolio_state (ADR-0041 wave 1c).

Test coverage:
  1. Empty state.db + 0 executions → empty positions, no cash row
  2. 1 long fill → 1 position row, cash decreased
  3. 1 long + 1 close → quantity=0 (flat), cash returns to start (modulo P&L)
  4. Multiple symbols, multiple fills → correct aggregation
  5. Idempotency: reconstruct_from() twice → same state
  6. Watermark: watermark advances after replay
  7. Watermark: apply_execution updates watermark
  8. PaperReactor integration: execute() → get_positions reflects new state
  9. Sign convention: short fill → negative quantity
 10. Sign convention: closing a short with a long → quantity 0
 11. Direction flip: short → long in one fill
 12. Multiple accounts partitioned correctly
 13. Initial cash bootstrapping from env var
 14. Cash delta: long fill decreases cash by fill_size_pct × fill_price
 15. Partial close: residual lot avg_entry_price unchanged
 16. Full rebuild clears old stale positions
 17. Malformed JSONL lines are skipped, not fatal
 18. _update_position unit tests
 19. apply_execution is failure-isolated (swallows DB errors)
 20. schema columns and indexes presence
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hermes_quant.state.portfolio_state import (
    PortfolioState,
    ReconstructionResult,
    _update_position,
)
from hermes_quant.state.positions import CashState, Position

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(
    asset: str = "AAPL",
    asset_class: str = "equity",
    fill_size_pct: float = 0.05,
    fill_price: float = 150.0,
    asof: str = "2026-05-27T10:00:00.000000Z",
    account_id: str = "paper-default",
    proposal_id: str = "prop_test",
) -> dict[str, Any]:
    """Minimal execution record dict (subset of ExecutionRecord fields)."""
    return {
        "proposal_id": proposal_id,
        "signal_id": None,
        "asset": asset,
        "asset_class": asset_class,
        "timeframe": "1d",
        "asof_decision": asof,
        "asof_execution": asof,
        "target_position_pct": fill_size_pct,
        "decision_price": fill_price,
        "fill_price": fill_price,
        "fill_size_pct": fill_size_pct,
        "reactor_name": "paper",
        "human_in_the_loop": True,
        "approver_user_id": None,
        "reactor_metadata": {"paper": True},
        "account_id": account_id,
    }


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    """Write a list of dicts to a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


@pytest.fixture()
def ps(tmp_path: Path) -> PortfolioState:
    """Fresh PortfolioState backed by isolated tmp_path DB."""
    return PortfolioState(state_db_path=tmp_path / "state.db")


@pytest.fixture()
def executions_path(tmp_path: Path) -> Path:
    return tmp_path / "executions.jsonl"


# ---------------------------------------------------------------------------
# 1. Empty state: 0 executions → empty positions, no cash row
# ---------------------------------------------------------------------------


class TestEmptyState:
    def test_empty_db_empty_executions(self, ps: PortfolioState, executions_path: Path):
        """reconstruct_from() on empty JSONL → empty positions, no cash row."""
        executions_path.touch()
        result = ps.reconstruct_from(executions_path)
        assert result.executions_processed == 0
        assert result.positions_written == 0
        assert ps.get_positions("paper-default") == {}
        assert ps.get_cash("paper-default") is None

    def test_nonexistent_executions_path(self, ps: PortfolioState, tmp_path: Path):
        """reconstruct_from() on non-existent JSONL → empty result (no crash)."""
        result = ps.reconstruct_from(tmp_path / "does_not_exist.jsonl")
        assert result.executions_processed == 0

    def test_get_positions_no_data_returns_empty(self, ps: PortfolioState):
        """get_positions on fresh DB returns empty dict."""
        assert ps.get_positions("paper-default") == {}

    def test_get_cash_no_data_returns_none(self, ps: PortfolioState):
        """get_cash on fresh DB returns None."""
        assert ps.get_cash("paper-default") is None


# ---------------------------------------------------------------------------
# 2. 1 long fill → position row created, cash decreased
# ---------------------------------------------------------------------------


class TestSingleLongFill:
    def test_single_long_fill_via_reconstruct(
        self, ps: PortfolioState, executions_path: Path
    ):
        """1 long fill → 1 position row with correct quantity and cash debit."""
        rec = _make_record(
            asset="AAPL",
            fill_size_pct=0.05,
            fill_price=200.0,
            account_id="paper-default",
        )
        _write_jsonl(executions_path, [rec])
        result = ps.reconstruct_from(executions_path)

        assert result.executions_processed == 1
        positions = ps.get_positions("paper-default")
        assert len(positions) == 1
        pos = positions[("equity", "AAPL")]
        assert pos.symbol == "AAPL"
        assert pos.quantity == pytest.approx(0.05)
        assert pos.avg_entry_price == pytest.approx(200.0)
        assert pos.is_long

    def test_single_long_fill_cash_debited(
        self, ps: PortfolioState, executions_path: Path
    ):
        """Cash decreases by fill_size_pct × fill_price after long fill."""
        initial = 100_000.0
        rec = _make_record(fill_size_pct=0.05, fill_price=200.0)
        _write_jsonl(executions_path, [rec])
        ps.reconstruct_from(executions_path)
        cash = ps.get_cash("paper-default")
        assert cash is not None
        expected_balance = initial - 0.05 * 200.0
        assert cash.balance_usd == pytest.approx(expected_balance)

    def test_single_long_fill_via_apply(self, ps: PortfolioState):
        """apply_execution() with 1 long fill creates position row."""
        rec = _make_record(fill_size_pct=0.10, fill_price=100.0)
        ps.apply_execution(rec)
        positions = ps.get_positions("paper-default")
        assert ("equity", "AAPL") in positions
        pos = positions[("equity", "AAPL")]
        assert pos.quantity == pytest.approx(0.10)
        assert pos.avg_entry_price == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 3. Long fill + full close → quantity ≈ 0, cash restored (minus P&L)
# ---------------------------------------------------------------------------


class TestLongThenClose:
    def test_full_close_quantity_zero(self, ps: PortfolioState, executions_path: Path):
        """Open + full close → position quantity ≈ 0 (filtered from get_positions)."""
        open_rec = _make_record(
            fill_size_pct=0.05,
            fill_price=100.0,
            asof="2026-05-27T10:00:00.000000Z",
        )
        close_rec = _make_record(
            fill_size_pct=-0.05,
            fill_price=110.0,
            asof="2026-05-27T11:00:00.000000Z",
        )
        _write_jsonl(executions_path, [open_rec, close_rec])
        ps.reconstruct_from(executions_path)
        positions = ps.get_positions("paper-default")
        # No open positions after full close
        assert ("equity", "AAPL") not in positions

    def test_full_close_cash_restored(self, ps: PortfolioState, executions_path: Path):
        """Cash after open + close = initial - (open_cost) + (close_proceeds)."""
        initial = 100_000.0
        open_cost = 0.05 * 100.0  # 5.0
        close_proceeds = 0.05 * 110.0  # 5.5 (cash INCREASES on close sell)
        expected_cash = initial - open_cost + close_proceeds

        open_rec = _make_record(
            fill_size_pct=0.05,
            fill_price=100.0,
            asof="2026-05-27T10:00:00.000000Z",
        )
        close_rec = _make_record(
            fill_size_pct=-0.05,
            fill_price=110.0,
            asof="2026-05-27T11:00:00.000000Z",
        )
        _write_jsonl(executions_path, [open_rec, close_rec])
        ps.reconstruct_from(executions_path)
        cash = ps.get_cash("paper-default")
        assert cash is not None
        assert cash.balance_usd == pytest.approx(expected_cash, rel=1e-9)

    def test_full_close_via_apply(self, ps: PortfolioState):
        """apply_execution open + close → flat position."""
        ps.apply_execution(_make_record(fill_size_pct=0.05, fill_price=100.0))
        ps.apply_execution(
            _make_record(
                fill_size_pct=-0.05,
                fill_price=110.0,
                asof="2026-05-27T11:00:00.000000Z",
            )
        )
        positions = ps.get_positions("paper-default")
        assert ("equity", "AAPL") not in positions


# ---------------------------------------------------------------------------
# 4. Multiple symbols, multiple fills
# ---------------------------------------------------------------------------


class TestMultipleSymbols:
    def test_two_symbols_separate_positions(
        self, ps: PortfolioState, executions_path: Path
    ):
        """Two distinct symbols produce two separate position rows."""
        records = [
            _make_record(asset="AAPL", fill_size_pct=0.03, fill_price=150.0),
            _make_record(asset="MSFT", fill_size_pct=0.04, fill_price=300.0),
        ]
        _write_jsonl(executions_path, records)
        ps.reconstruct_from(executions_path)
        positions = ps.get_positions("paper-default")
        assert ("equity", "AAPL") in positions
        assert ("equity", "MSFT") in positions
        assert positions[("equity", "AAPL")].quantity == pytest.approx(0.03)
        assert positions[("equity", "MSFT")].quantity == pytest.approx(0.04)

    def test_multiple_fills_same_symbol_aggregates(
        self, ps: PortfolioState, executions_path: Path
    ):
        """Two adds to same symbol → weighted-average entry, combined quantity."""
        records = [
            _make_record(
                asset="AAPL",
                fill_size_pct=0.05,
                fill_price=100.0,
                asof="2026-05-27T10:00:00.000000Z",
            ),
            _make_record(
                asset="AAPL",
                fill_size_pct=0.05,
                fill_price=120.0,
                asof="2026-05-27T11:00:00.000000Z",
            ),
        ]
        _write_jsonl(executions_path, records)
        ps.reconstruct_from(executions_path)
        positions = ps.get_positions("paper-default")
        pos = positions[("equity", "AAPL")]
        assert pos.quantity == pytest.approx(0.10, rel=1e-9)
        expected_avg = (0.05 * 100.0 + 0.05 * 120.0) / 0.10
        assert pos.avg_entry_price == pytest.approx(expected_avg, rel=1e-9)

    def test_reaffirmation_does_not_inflate(
        self, ps: PortfolioState, executions_path: Path, monkeypatch
    ):
        """N re-affirmations of the SAME target fold to ONE intended position.

        Canonical ADR-0091 re-affirmation scenario. Each record carries the SAME
        absolute target (0.05 for AAPL long, -0.2 for BA short) but a DISTINCT
        proposal_id — i.e. the advisor re-affirmed an already-held, unchanged
        target N times.

        CORRECT end state: a single intended position (AAPL 0.05, BA -0.2),
        because every fire after the first is an effective-delta-0 re-affirmation
        of the same target. Cost basis stays at the first-fire fill_price; cash
        moves only once per symbol (the genuine open).

        This was a strict-xfail regression spec (cr09) until the Increment-0
        Option-E shared fold-time normalizer landed (cr09/ra01). With
        HERMES_QUANT_DELTA_NORMALIZER=1 it now PASSES: a re-affirmation folds to
        delta 0 instead of inflating to N*target (AAPL 12 x 0.05 = 0.60;
        BA 6 x -0.2 = -1.20 under the legacy fold). The flag-OFF legacy inflation
        is covered separately in tests/state/test_normalizer_wired_fold.py.
        """
        monkeypatch.setenv("HERMES_QUANT_DELTA_NORMALIZER", "1")
        # AAPL: 12 re-affirmations of the same 0.05 long target (distinct ids).
        aapl_records = [
            _make_record(
                asset="AAPL",
                fill_size_pct=0.05,
                fill_price=100.0,
                asof=f"2026-05-27T10:{minute:02d}:00.000000Z",
                proposal_id=f"prop_aapl_{minute:02d}",
            )
            for minute in range(12)
        ]
        # BA: 6 re-affirmations of the same -0.2 short target (distinct ids).
        ba_records = [
            _make_record(
                asset="BA",
                fill_size_pct=-0.2,
                fill_price=200.0,
                asof=f"2026-05-27T11:{minute:02d}:00.000000Z",
                proposal_id=f"prop_ba_{minute:02d}",
            )
            for minute in range(6)
        ]
        _write_jsonl(executions_path, [*aapl_records, *ba_records])

        result = ps.reconstruct_from(executions_path)
        assert result.executions_processed == 18

        positions = ps.get_positions("paper-default")

        # AAPL stays at the single intended 0.05 long (NOT 12 x 0.05 = 0.60).
        aapl = positions[("equity", "AAPL")]
        assert aapl.quantity == pytest.approx(0.05, rel=1e-9)
        assert aapl.avg_entry_price == pytest.approx(100.0, rel=1e-9)

        # BA stays at the single intended -0.2 short (NOT 6 x -0.2 = -1.20).
        ba = positions[("equity", "BA")]
        assert ba.quantity == pytest.approx(-0.2, rel=1e-9)
        assert ba.avg_entry_price == pytest.approx(200.0, rel=1e-9)

        # Cash moves only once per symbol: one genuine open each, the
        # re-affirmations are delta-0 (no cash impact).
        #   AAPL long  -> cash -= 0.05 * 100.0 = 5.0
        #   BA short   -> cash -= (-0.2) * 200.0 = -40.0 (short credits cash)
        expected_cash = 100_000.0 - (0.05 * 100.0) - (-0.2 * 200.0)
        cash = ps.get_cash("paper-default")
        assert cash is not None
        assert cash.balance_usd == pytest.approx(expected_cash, rel=1e-9)

    def test_multi_symbol_cash_sum(self, ps: PortfolioState, executions_path: Path):
        """Cash decremented by sum of all fill costs."""
        records = [
            _make_record(asset="AAPL", fill_size_pct=0.02, fill_price=100.0),
            _make_record(asset="MSFT", fill_size_pct=0.03, fill_price=200.0),
        ]
        _write_jsonl(executions_path, records)
        ps.reconstruct_from(executions_path)
        expected_cash = 100_000.0 - 0.02 * 100.0 - 0.03 * 200.0
        cash = ps.get_cash("paper-default")
        assert cash is not None
        assert cash.balance_usd == pytest.approx(expected_cash, rel=1e-9)


# ---------------------------------------------------------------------------
# 5. Idempotency: reconstruct_from() twice → same state
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_reconstruct_twice_same_positions(
        self, ps: PortfolioState, executions_path: Path
    ):
        """Running reconstruct_from() twice yields identical state."""
        records = [
            _make_record(asset="AAPL", fill_size_pct=0.05, fill_price=150.0),
            _make_record(asset="TSLA", fill_size_pct=0.03, fill_price=200.0),
        ]
        _write_jsonl(executions_path, records)

        r1 = ps.reconstruct_from(executions_path)
        pos1 = ps.get_positions("paper-default")
        cash1 = ps.get_cash("paper-default")

        r2 = ps.reconstruct_from(executions_path)
        pos2 = ps.get_positions("paper-default")
        cash2 = ps.get_cash("paper-default")

        assert r1.executions_processed == r2.executions_processed
        assert set(pos1.keys()) == set(pos2.keys())
        for key in pos1:
            assert pos1[key].quantity == pytest.approx(pos2[key].quantity)
            assert pos1[key].avg_entry_price == pytest.approx(pos2[key].avg_entry_price)
        assert cash1.balance_usd == pytest.approx(cash2.balance_usd)  # type: ignore[union-attr]

    def test_reconstruct_clears_stale_positions(
        self, ps: PortfolioState, executions_path: Path
    ):
        """Full rebuild clears positions from a previous different state."""
        # First write with AAPL + MSFT
        _write_jsonl(
            executions_path,
            [
                _make_record(asset="AAPL", fill_size_pct=0.05, fill_price=100.0),
                _make_record(asset="MSFT", fill_size_pct=0.03, fill_price=200.0),
            ],
        )
        ps.reconstruct_from(executions_path)
        assert ("equity", "MSFT") in ps.get_positions("paper-default")

        # Now overwrite JSONL with only AAPL
        _write_jsonl(
            executions_path,
            [_make_record(asset="AAPL", fill_size_pct=0.05, fill_price=100.0)],
        )
        ps.reconstruct_from(executions_path)
        positions = ps.get_positions("paper-default")
        assert ("equity", "MSFT") not in positions
        assert ("equity", "AAPL") in positions


# ---------------------------------------------------------------------------
# 6 & 7. Watermark tests
# ---------------------------------------------------------------------------


class TestWatermark:
    def test_watermark_none_before_any_replay(self, ps: PortfolioState):
        """Watermark is None before any replay."""
        assert ps.get_watermark() is None

    def test_watermark_set_after_reconstruct(
        self, ps: PortfolioState, executions_path: Path
    ):
        """Watermark is set to the latest asof after reconstruct_from()."""
        records = [
            _make_record(asof="2026-05-27T10:00:00.000000Z"),
            _make_record(asof="2026-05-27T12:00:00.000000Z"),
        ]
        _write_jsonl(executions_path, records)
        ps.reconstruct_from(executions_path)
        wm = ps.get_watermark()
        assert wm == "2026-05-27T12:00:00.000000Z"

    def test_watermark_updated_by_apply_execution(self, ps: PortfolioState):
        """apply_execution() advances watermark."""
        ps.apply_execution(_make_record(asof="2026-05-27T09:00:00.000000Z"))
        wm1 = ps.get_watermark()
        ps.apply_execution(_make_record(asof="2026-05-27T10:00:00.000000Z"))
        wm2 = ps.get_watermark()
        assert wm1 is not None
        assert wm2 is not None
        assert wm2 >= wm1

    def test_watermark_not_regressed_by_earlier_asof(self, ps: PortfolioState):
        """Watermark should not regress if an older-timestamped record arrives later."""
        ps.apply_execution(_make_record(asof="2026-05-27T12:00:00.000000Z"))
        ps.apply_execution(_make_record(asof="2026-05-27T10:00:00.000000Z"))
        wm = ps.get_watermark()
        # MAX in the SQL ON CONFLICT ensures no regression
        assert wm == "2026-05-27T12:00:00.000000Z"


# ---------------------------------------------------------------------------
# 8. PaperReactor integration
# ---------------------------------------------------------------------------


class TestPaperReactorIntegration:
    """Verify PaperReactor.execute() calls PortfolioState.apply_execution."""

    def _make_proposal(self, symbol: str = "AAPL") -> Any:
        """Minimal Proposal-like mock for PaperReactor.execute."""
        proposal = MagicMock()
        proposal.proposal_id = "prop_test_001"
        proposal.symbol = symbol
        proposal.asset_class = "equity"
        proposal.timeframe = "1d"
        proposal.advisor_result = {
            "decision_price": 150.0,
            "as_of": "2026-05-27T10:00:00Z",
        }
        return proposal

    def test_paper_reactor_updates_portfolio_state(self, tmp_path: Path):
        """After PaperReactor.execute(), PortfolioState reflects the fill."""
        from hermes_quant.react.paper import PaperReactor
        import hermes_quant.state.portfolio_state as ps_mod

        executions_path = tmp_path / "executions.jsonl"
        db_path = tmp_path / "state.db"
        ps_instance = PortfolioState(state_db_path=db_path)

        # Patch the singleton so PaperReactor uses our test instance
        with patch.object(ps_mod, "_singleton", ps_instance):
            reactor = PaperReactor(executions_path=executions_path)
            proposal = self._make_proposal("AAPL")
            record = reactor.execute(proposal, fill_size_pct=0.05)

        positions = ps_instance.get_positions("paper-default")
        assert ("equity", "AAPL") in positions
        pos = positions[("equity", "AAPL")]
        assert pos.quantity == pytest.approx(0.05)

    def test_paper_reactor_state_failure_does_not_block(self, tmp_path: Path):
        """If PortfolioState.apply_execution raises, execute() still returns."""
        from hermes_quant.react.paper import PaperReactor
        import hermes_quant.state.portfolio_state as ps_mod

        executions_path = tmp_path / "executions.jsonl"

        # Inject a broken PortfolioState that raises on apply_execution
        broken_ps = MagicMock(spec=PortfolioState)
        broken_ps.apply_execution.side_effect = RuntimeError("DB is broken")

        with patch.object(ps_mod, "_singleton", broken_ps):
            reactor = PaperReactor(executions_path=executions_path)
            proposal = self._make_proposal("AAPL")
            # Should NOT raise
            record = reactor.execute(proposal, fill_size_pct=0.05)

        assert record is not None
        assert record.asset == "AAPL"


# ---------------------------------------------------------------------------
# 9 & 10. Sign convention: short fills
# ---------------------------------------------------------------------------


class TestSignConvention:
    def test_short_fill_produces_negative_quantity(
        self, ps: PortfolioState, executions_path: Path
    ):
        """A short fill (fill_size_pct < 0) produces negative position quantity."""
        rec = _make_record(fill_size_pct=-0.05, fill_price=150.0)
        _write_jsonl(executions_path, [rec])
        ps.reconstruct_from(executions_path)
        positions = ps.get_positions("paper-default")
        assert ("equity", "AAPL") in positions
        pos = positions[("equity", "AAPL")]
        assert pos.quantity == pytest.approx(-0.05)
        assert pos.is_short

    def test_short_fill_increases_cash(
        self, ps: PortfolioState, executions_path: Path
    ):
        """Short fill (negative fill_size_pct) INCREASES cash balance."""
        initial = 100_000.0
        rec = _make_record(fill_size_pct=-0.05, fill_price=200.0)
        _write_jsonl(executions_path, [rec])
        ps.reconstruct_from(executions_path)
        cash = ps.get_cash("paper-default")
        assert cash is not None
        # cash += -(-0.05) × 200 = + 10
        expected = initial + 0.05 * 200.0
        assert cash.balance_usd == pytest.approx(expected)

    def test_closing_short_with_long_brings_to_zero(
        self, ps: PortfolioState, executions_path: Path
    ):
        """Short open + covering long fill → flat position."""
        records = [
            _make_record(
                fill_size_pct=-0.05,
                fill_price=100.0,
                asof="2026-05-27T10:00:00.000000Z",
            ),
            _make_record(
                fill_size_pct=0.05,
                fill_price=90.0,
                asof="2026-05-27T11:00:00.000000Z",
            ),
        ]
        _write_jsonl(executions_path, records)
        ps.reconstruct_from(executions_path)
        positions = ps.get_positions("paper-default")
        assert ("equity", "AAPL") not in positions

    def test_closing_short_via_apply(self, ps: PortfolioState):
        """apply_execution: short open + covering long → flat via apply_execution."""
        ps.apply_execution(
            _make_record(fill_size_pct=-0.05, fill_price=100.0)
        )
        positions = ps.get_positions("paper-default")
        assert positions[("equity", "AAPL")].is_short

        ps.apply_execution(
            _make_record(
                fill_size_pct=0.05,
                fill_price=90.0,
                asof="2026-05-27T11:00:00.000000Z",
            )
        )
        positions = ps.get_positions("paper-default")
        assert ("equity", "AAPL") not in positions


# ---------------------------------------------------------------------------
# 11. Direction flip
# ---------------------------------------------------------------------------


class TestDirectionFlip:
    def test_long_to_short_flip(self, ps: PortfolioState, executions_path: Path):
        """Long +0.05 then short -0.10 → net -0.05 (short), avg = fill_price of the flip."""
        records = [
            _make_record(
                fill_size_pct=0.05,
                fill_price=100.0,
                asof="2026-05-27T10:00:00.000000Z",
            ),
            _make_record(
                fill_size_pct=-0.10,
                fill_price=110.0,
                asof="2026-05-27T11:00:00.000000Z",
            ),
        ]
        _write_jsonl(executions_path, records)
        ps.reconstruct_from(executions_path)
        positions = ps.get_positions("paper-default")
        pos = positions[("equity", "AAPL")]
        assert pos.quantity == pytest.approx(-0.05, rel=1e-9)
        # After flip, avg_entry_price = fill_price of the flipping trade
        assert pos.avg_entry_price == pytest.approx(110.0)
        assert pos.is_short


# ---------------------------------------------------------------------------
# 12. Multiple accounts
# ---------------------------------------------------------------------------


class TestMultipleAccounts:
    def test_two_accounts_partitioned(
        self, ps: PortfolioState, executions_path: Path
    ):
        """Two accounts have independent positions and cash."""
        records = [
            _make_record(
                account_id="paper-a",
                asset="AAPL",
                fill_size_pct=0.05,
                fill_price=100.0,
            ),
            _make_record(
                account_id="paper-b",
                asset="MSFT",
                fill_size_pct=0.10,
                fill_price=200.0,
            ),
        ]
        _write_jsonl(executions_path, records)
        ps.reconstruct_from(executions_path)

        pos_a = ps.get_positions("paper-a")
        pos_b = ps.get_positions("paper-b")
        assert ("equity", "AAPL") in pos_a
        assert ("equity", "MSFT") not in pos_a
        assert ("equity", "MSFT") in pos_b
        assert ("equity", "AAPL") not in pos_b

        cash_a = ps.get_cash("paper-a")
        cash_b = ps.get_cash("paper-b")
        assert cash_a is not None
        assert cash_b is not None
        assert cash_a.balance_usd != cash_b.balance_usd


# ---------------------------------------------------------------------------
# 13. Initial cash from env var
# ---------------------------------------------------------------------------


class TestInitialCash:
    def test_initial_cash_from_env(
        self, ps: PortfolioState, executions_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """HERMES_QUANT_PAPER_INITIAL_CASH env var sets bootstrap cash."""
        monkeypatch.setenv("HERMES_QUANT_PAPER_INITIAL_CASH", "50000.0")
        rec = _make_record(fill_size_pct=0.01, fill_price=100.0)
        _write_jsonl(executions_path, [rec])
        ps.reconstruct_from(executions_path)
        cash = ps.get_cash("paper-default")
        assert cash is not None
        # initial 50000 - 0.01 × 100 = 49999
        assert cash.balance_usd == pytest.approx(50_000.0 - 0.01 * 100.0)

    def test_invalid_env_falls_back_to_default(
        self,
        ps: PortfolioState,
        executions_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Invalid HERMES_QUANT_PAPER_INITIAL_CASH → fallback to 100_000."""
        monkeypatch.setenv("HERMES_QUANT_PAPER_INITIAL_CASH", "not-a-number")
        executions_path.touch()
        ps.reconstruct_from(executions_path)
        # No fills: no cash row yet (bootstrapped on first fill)
        # Apply one fill to trigger bootstrap
        ps.apply_execution(_make_record(fill_size_pct=0.0, fill_price=100.0))
        cash = ps.get_cash("paper-default")
        # With fill_size_pct=0 cash = initial_cash - 0 × price = initial_cash
        assert cash is not None
        assert cash.balance_usd == pytest.approx(100_000.0)


# ---------------------------------------------------------------------------
# 14. Cash delta precision
# ---------------------------------------------------------------------------


class TestCashDelta:
    def test_cash_delta_long_fill(self, ps: PortfolioState):
        """Cash decreases by exactly fill_size_pct × fill_price for long."""
        ps.apply_execution(_make_record(fill_size_pct=0.07, fill_price=143.21))
        cash = ps.get_cash("paper-default")
        assert cash is not None
        expected = 100_000.0 - 0.07 * 143.21
        assert cash.balance_usd == pytest.approx(expected, rel=1e-9)

    def test_cash_delta_short_fill(self, ps: PortfolioState):
        """Cash increases by |fill_size_pct| × fill_price for short."""
        ps.apply_execution(_make_record(fill_size_pct=-0.04, fill_price=250.0))
        cash = ps.get_cash("paper-default")
        assert cash is not None
        expected = 100_000.0 + 0.04 * 250.0
        assert cash.balance_usd == pytest.approx(expected, rel=1e-9)

    def test_zero_fill_size_no_cash_change(self, ps: PortfolioState):
        """fill_size_pct=0 → cash unchanged from initial."""
        ps.apply_execution(_make_record(fill_size_pct=0.0, fill_price=100.0))
        cash = ps.get_cash("paper-default")
        assert cash is not None
        assert cash.balance_usd == pytest.approx(100_000.0)


# ---------------------------------------------------------------------------
# 15. Partial close: residual lot avg_entry_price unchanged
# ---------------------------------------------------------------------------


class TestPartialClose:
    def test_partial_close_residual_avg_unchanged(
        self, ps: PortfolioState, executions_path: Path
    ):
        """Partial close preserves avg_entry_price of residual lot."""
        records = [
            _make_record(
                fill_size_pct=0.10,
                fill_price=100.0,
                asof="2026-05-27T10:00:00.000000Z",
            ),
            # Close half at 120: residual lot should still have avg=100
            _make_record(
                fill_size_pct=-0.05,
                fill_price=120.0,
                asof="2026-05-27T11:00:00.000000Z",
            ),
        ]
        _write_jsonl(executions_path, records)
        ps.reconstruct_from(executions_path)
        positions = ps.get_positions("paper-default")
        pos = positions[("equity", "AAPL")]
        assert pos.quantity == pytest.approx(0.05, rel=1e-9)
        # ADR-0041 §D7: residual-lot rule — avg_entry stays at original 100.0
        assert pos.avg_entry_price == pytest.approx(100.0, rel=1e-9)


# ---------------------------------------------------------------------------
# 16. Malformed JSONL lines skipped
# ---------------------------------------------------------------------------


class TestMalformedLines:
    def test_malformed_line_skipped(
        self, ps: PortfolioState, executions_path: Path
    ):
        """Malformed JSON lines are skipped; valid records still processed."""
        executions_path.parent.mkdir(parents=True, exist_ok=True)
        with open(executions_path, "w") as fh:
            fh.write("not-valid-json\n")
            fh.write(json.dumps(_make_record(fill_size_pct=0.05, fill_price=100.0)) + "\n")
            fh.write("{broken}\n")

        result = ps.reconstruct_from(executions_path)
        # 1 valid record processed (malformed lines silently skipped)
        assert result.executions_processed == 1
        positions = ps.get_positions("paper-default")
        assert ("equity", "AAPL") in positions

    def test_missing_fields_counted_as_error(
        self, ps: PortfolioState, executions_path: Path
    ):
        """Records that fail field extraction are counted in result.errors."""
        bad = {"fill_size_pct": "not-a-number", "fill_price": 100.0, "asset": "AAPL"}
        _write_jsonl(executions_path, [bad])
        result = ps.reconstruct_from(executions_path)
        assert result.errors  # At least one error logged


# ---------------------------------------------------------------------------
# 17. Schema tests
# ---------------------------------------------------------------------------


class TestSchema:
    def test_positions_table_exists(self, ps: PortfolioState):
        """positions table is created with correct columns."""
        with ps._conn() as conn:
            cols_info = conn.execute("PRAGMA table_info(positions)").fetchall()
        col_names = {c["name"] for c in cols_info}
        assert {"account_id", "asset_class", "symbol", "quantity",
                "avg_entry_price", "last_update_at"}.issubset(col_names)

    def test_cash_table_exists(self, ps: PortfolioState):
        """cash table is created with correct columns."""
        with ps._conn() as conn:
            cols_info = conn.execute("PRAGMA table_info(cash)").fetchall()
        col_names = {c["name"] for c in cols_info}
        assert {"account_id", "balance_usd", "last_update_at", "equity_total"}.issubset(col_names)

    def test_executions_replayed_table_exists(self, ps: PortfolioState):
        """executions_replayed watermark table is present."""
        with ps._conn() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='executions_replayed'"
            ).fetchone()
        assert row is not None

    def test_halts_table_preserved(self, ps: PortfolioState):
        """Creating PortfolioState on a DB that already has halts does NOT drop it."""
        # Insert a synthetic halts row manually
        with ps._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS halts (
                    account_id TEXT NOT NULL,
                    asset_class TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    halted_at TEXT NOT NULL,
                    halted_until TEXT,
                    halt_epoch INTEGER NOT NULL,
                    cleared_at TEXT,
                    cleared_reason TEXT,
                    PRIMARY KEY (account_id, asset_class, asset, halt_epoch)
                ) WITHOUT ROWID;
                """
            )
            conn.execute(
                "INSERT INTO halts VALUES ('acct','equity','AAPL','test','2026-01-01',NULL,1,NULL,NULL)"
            )

        # Re-init schema should NOT drop halts
        ps._init_schema()
        with ps._conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM halts").fetchone()[0]
        assert count == 1

    def test_positions_pk_is_compound(self, ps: PortfolioState):
        """positions PK is (account_id, asset_class, symbol)."""
        with ps._conn() as conn:
            cols = conn.execute("PRAGMA table_info(positions)").fetchall()
        pk_cols = sorted([c["name"] for c in cols if c["pk"] > 0])
        assert pk_cols == sorted(["account_id", "asset_class", "symbol"])


# ---------------------------------------------------------------------------
# 18. _update_position unit tests
# ---------------------------------------------------------------------------


class TestUpdatePosition:
    def test_open_new_long(self):
        new_qty, new_avg = _update_position(0.0, 0.0, 0.05, 100.0)
        assert new_qty == pytest.approx(0.05)
        assert new_avg == pytest.approx(100.0)

    def test_add_to_long_weighted_average(self):
        new_qty, new_avg = _update_position(0.05, 100.0, 0.05, 120.0)
        assert new_qty == pytest.approx(0.10)
        assert new_avg == pytest.approx(110.0)

    def test_full_close_long(self):
        new_qty, new_avg = _update_position(0.05, 100.0, -0.05, 110.0)
        assert abs(new_qty) < 1e-12
        assert new_avg == pytest.approx(0.0)

    def test_partial_close_long_residual_avg_unchanged(self):
        new_qty, new_avg = _update_position(0.10, 100.0, -0.04, 130.0)
        assert new_qty == pytest.approx(0.06, rel=1e-9)
        assert new_avg == pytest.approx(100.0)  # residual-lot rule

    def test_open_new_short(self):
        new_qty, new_avg = _update_position(0.0, 0.0, -0.05, 100.0)
        assert new_qty == pytest.approx(-0.05)
        assert new_avg == pytest.approx(100.0)

    def test_add_to_short_weighted_average(self):
        new_qty, new_avg = _update_position(-0.05, 100.0, -0.05, 80.0)
        assert new_qty == pytest.approx(-0.10)
        # weighted avg: (0.05×100 + 0.05×80) / 0.10 = 90
        assert new_avg == pytest.approx(90.0)

    def test_full_close_short(self):
        new_qty, new_avg = _update_position(-0.05, 100.0, 0.05, 80.0)
        assert abs(new_qty) < 1e-12
        assert new_avg == pytest.approx(0.0)

    def test_direction_flip_long_to_short(self):
        """Long 0.05 → short after -0.10 fill → net -0.05, avg = fill_price."""
        new_qty, new_avg = _update_position(0.05, 100.0, -0.10, 115.0)
        assert new_qty == pytest.approx(-0.05, rel=1e-9)
        assert new_avg == pytest.approx(115.0)

    def test_direction_flip_short_to_long(self):
        """Short -0.05 → long after +0.10 fill → net +0.05, avg = fill_price."""
        new_qty, new_avg = _update_position(-0.05, 100.0, 0.10, 90.0)
        assert new_qty == pytest.approx(0.05, rel=1e-9)
        assert new_avg == pytest.approx(90.0)


# ---------------------------------------------------------------------------
# 19. Failure isolation for apply_execution
# ---------------------------------------------------------------------------


class TestApplyExecutionFailureIsolation:
    def test_corrupt_db_apply_execution_does_not_raise(self, tmp_path: Path):
        """apply_execution swallows exceptions; callers are never blocked."""
        ps = PortfolioState(state_db_path=tmp_path / "state.db")

        # Simulate a broken DB by patching _apply_execution_unsafe
        with patch.object(ps, "_apply_execution_unsafe", side_effect=RuntimeError("oops")):
            # Must NOT raise
            ps.apply_execution(_make_record())


# ---------------------------------------------------------------------------
# 20. accounts_seen in ReconstructionResult
# ---------------------------------------------------------------------------


class TestReconstructionResult:
    def test_accounts_seen_populated(
        self, ps: PortfolioState, executions_path: Path
    ):
        """ReconstructionResult.accounts_seen contains all account_ids seen."""
        records = [
            _make_record(account_id="acc-a"),
            _make_record(account_id="acc-b"),
            _make_record(account_id="acc-a"),  # duplicate — counted once
        ]
        _write_jsonl(executions_path, records)
        result = ps.reconstruct_from(executions_path)
        assert result.accounts_seen == {"acc-a", "acc-b"}

    def test_executions_processed_count(
        self, ps: PortfolioState, executions_path: Path
    ):
        """executions_processed matches number of valid records."""
        records = [_make_record() for _ in range(5)]
        _write_jsonl(executions_path, records)
        result = ps.reconstruct_from(executions_path)
        assert result.executions_processed == 5
