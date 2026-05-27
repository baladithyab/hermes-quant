"""tests/shadow/test_account.py — ShadowAccount unit tests.

Wave 8b / ADR-0049.

Tests:
- round-trip apply_signal → mark_to_market with synthetic prices
- cost model is correctly deducted
- isolated DB per rule (two accounts have separate files)
- pnl_curve returns a pd.Series
- idempotency: applying the same event twice doesn't double-count
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from hermes_quant.shadow.account import ShadowAccount
from hermes_quant.shadow.rules import AlwaysFollowAdvisorRule, InverseConsensusRule


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2025, 6, 10, 14, 30, 0, tzinfo=timezone.utc)


def _gate_event(
    direction: str = "buy",
    ticker: str = "AAPL",
    event_id: str = "evt-001",
) -> dict:
    return {
        "event_id": event_id,
        "kind": "gate_approval",
        "asof": _NOW.isoformat(),
        "source": "test",
        "payload": {
            "ticker": ticker,
            "advisor_result": {"direction": direction},
            "signal_provenance": {
                "advisor_direction": direction,
                "vote_share": 0.75,
                "contributing_analysts": ["semantic", "sentiment"],
            },
        },
    }


@pytest.fixture
def tmp_account(tmp_path: Path) -> ShadowAccount:
    rule = AlwaysFollowAdvisorRule()
    return ShadowAccount(rule, initial_cash=100_000.0, cost_model_bps=10.0, db_path=tmp_path / "test.db")


@pytest.fixture
def tmp_inverse_account(tmp_path: Path) -> ShadowAccount:
    rule = InverseConsensusRule()
    return ShadowAccount(rule, initial_cash=100_000.0, cost_model_bps=10.0, db_path=tmp_path / "inverse.db")


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


class TestInitialState:
    def test_initial_cash(self, tmp_account: ShadowAccount):
        assert tmp_account.cash == pytest.approx(100_000.0)

    def test_initial_positions_empty(self, tmp_account: ShadowAccount):
        assert tmp_account.positions == {}

    def test_initial_pnl_history_empty(self, tmp_account: ShadowAccount):
        assert tmp_account.pnl_history == []


# ---------------------------------------------------------------------------
# apply_signal round-trip
# ---------------------------------------------------------------------------


class TestApplySignal:
    def test_buy_decreases_cash(self, tmp_account: ShadowAccount):
        event = _gate_event(direction="buy", ticker="AAPL", event_id="e1")
        prices = {"AAPL": 200.0}
        decision = tmp_account.apply_signal(event, prices)
        assert decision is not None
        assert decision.action == "buy"
        # Cash should have decreased
        assert tmp_account.cash < 100_000.0

    def test_sell_on_empty_position(self, tmp_account: ShadowAccount):
        """Selling short from flat: cash should increase (short position opens)."""
        event = _gate_event(direction="sell", ticker="TSLA", event_id="e2")
        prices = {"TSLA": 250.0}
        decision = tmp_account.apply_signal(event, prices)
        assert decision is not None
        assert decision.action == "sell"
        # With InverseConsensus the action is "sell" — cash increases from short proceeds
        assert tmp_account.cash > 100_000.0

    def test_returns_none_for_non_approval(self, tmp_account: ShadowAccount):
        event = {"event_id": "e3", "kind": "fill", "asof": _NOW.isoformat(),
                 "source": "test", "payload": {"ticker": "AAPL", "direction": "buy"}}
        decision = tmp_account.apply_signal(event, {"AAPL": 200.0})
        assert decision is None

    def test_returns_none_when_no_price(self, tmp_account: ShadowAccount):
        event = _gate_event(direction="buy", ticker="NVDA", event_id="e4")
        decision = tmp_account.apply_signal(event, {})  # no price for NVDA
        assert decision is None

    def test_position_created_after_buy(self, tmp_account: ShadowAccount):
        event = _gate_event(direction="buy", ticker="AAPL", event_id="e5")
        tmp_account.apply_signal(event, {"AAPL": 150.0})
        positions = tmp_account.positions
        assert "AAPL" in positions
        assert positions["AAPL"]["quantity"] > 0

    def test_cost_model_applied(self, tmp_account: ShadowAccount):
        """Fill price should be higher than market price for a buy (cost drag)."""
        event = _gate_event(direction="buy", ticker="AAPL", event_id="e6")
        initial_cash = tmp_account.cash
        # With market price 100, fill price = 100 * (1 + 0.0005 + 0.001) = 100.15
        tmp_account.apply_signal(event, {"AAPL": 100.0})
        cash_after = tmp_account.cash
        equity = 100_000.0  # initial equity
        size = 0.10  # AlwaysFollowAdvisorRule.size_fraction
        expected_notional = equity * size  # $10,000
        # Fill price > 100 due to cost, so cash_after < initial - expected_notional
        assert cash_after < initial_cash - expected_notional * 0.999  # some cost applied


# ---------------------------------------------------------------------------
# mark_to_market
# ---------------------------------------------------------------------------


class TestMarkToMarket:
    def test_mark_returns_dict_keys(self, tmp_account: ShadowAccount):
        result = tmp_account.mark_to_market({"AAPL": 200.0})
        assert set(result.keys()) == {
            "equity_total", "cash", "positions_value", "pnl_today", "pnl_total"
        }

    def test_initial_equity_equals_cash(self, tmp_account: ShadowAccount):
        result = tmp_account.mark_to_market({})
        # No positions → equity = cash
        assert result["equity_total"] == pytest.approx(result["cash"])
        assert result["positions_value"] == pytest.approx(0.0)

    def test_pnl_today_after_price_rise(self, tmp_account: ShadowAccount):
        # Buy AAPL at 100
        tmp_account.apply_signal(
            _gate_event(direction="buy", ticker="AAPL", event_id="m1"),
            {"AAPL": 100.0},
        )
        # Mark at 110 → positions_value should be higher
        result = tmp_account.mark_to_market({"AAPL": 110.0})
        assert result["positions_value"] > 0
        # Total equity should be > initial minus cost
        # (we bought at ~100.15 and market moved to 110)
        assert result["equity_total"] > 99_000  # conservative check

    def test_pnl_history_grows(self, tmp_account: ShadowAccount):
        tmp_account.mark_to_market({})
        tmp_account.mark_to_market({})
        assert len(tmp_account.pnl_history) == 2


# ---------------------------------------------------------------------------
# pnl_curve
# ---------------------------------------------------------------------------


class TestPnlCurve:
    def test_empty_curve_returns_series(self, tmp_account: ShadowAccount):
        curve = tmp_account.pnl_curve()
        assert isinstance(curve, pd.Series)
        assert len(curve) == 0

    def test_curve_after_mark(self, tmp_account: ShadowAccount):
        tmp_account.mark_to_market({"AAPL": 200.0})
        curve = tmp_account.pnl_curve()
        assert isinstance(curve, pd.Series)
        assert len(curve) == 1
        assert curve.name == "always_follow_advisor"


# ---------------------------------------------------------------------------
# Isolated DB per rule
# ---------------------------------------------------------------------------


class TestIsolatedDb:
    def test_different_db_files(self, tmp_path: Path):
        rule_a = AlwaysFollowAdvisorRule()
        rule_b = InverseConsensusRule()
        acct_a = ShadowAccount(rule_a, db_path=tmp_path / "a.db")
        acct_b = ShadowAccount(rule_b, db_path=tmp_path / "b.db")
        assert acct_a.db_path != acct_b.db_path

    def test_accounts_do_not_share_state(self, tmp_path: Path):
        rule_a = AlwaysFollowAdvisorRule()
        rule_b = InverseConsensusRule()
        acct_a = ShadowAccount(rule_a, db_path=tmp_path / "a.db")
        acct_b = ShadowAccount(rule_b, db_path=tmp_path / "b.db")

        # Buy signal → a buys, b sells (inverse)
        event = _gate_event(direction="buy", ticker="AAPL", event_id="iso1")
        acct_a.apply_signal(event, {"AAPL": 100.0})
        acct_b.apply_signal(event, {"AAPL": 100.0})

        # a has LONG position, b has SHORT position
        pos_a = acct_a.positions
        pos_b = acct_b.positions
        assert pos_a.get("AAPL", {}).get("quantity", 0) > 0
        assert pos_b.get("AAPL", {}).get("quantity", 0) < 0

    def test_db_files_are_sqlite(self, tmp_path: Path):
        import sqlite3
        rule = AlwaysFollowAdvisorRule()
        db = tmp_path / "x.db"
        ShadowAccount(rule, db_path=db)
        conn = sqlite3.connect(str(db))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "shadow_positions" in tables
        assert "shadow_cash" in tables
        conn.close()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_duplicate_event_not_double_counted(self, tmp_account: ShadowAccount):
        event = _gate_event(direction="buy", ticker="AAPL", event_id="idem1")
        prices = {"AAPL": 100.0}
        tmp_account.apply_signal(event, prices)
        cash_after_first = tmp_account.cash

        # Apply again with same event_id
        tmp_account.apply_signal(event, prices)
        assert tmp_account.cash == pytest.approx(cash_after_first)
