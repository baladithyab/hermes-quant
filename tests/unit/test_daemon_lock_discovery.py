"""Tests for daemon/discovery.py + daemon/portfolio_loader.py.

Vestigial-daemon-spine deletion: this file originally also covered
``daemon/lock.py`` (TestDaemonLock — the singleton daemon flock). That lock
only protected the long-lived daemon entry point (``daemon/main.py``), which
was vestigial; the live spine is cron scripts that call advisor.recommend +
reactors directly. ``daemon/lock.py`` and ``daemon/main.py`` were removed, and
TestDaemonLock with them. The discovery + portfolio-reconstruction coverage
(both KEPT utilities) stays.
"""

from __future__ import annotations

import pandas as pd
import pytest

from hermes_quant.daemon.discovery import (
    discover_aggregators,
    discover_analysts,
    discover_data_providers,
    instantiate_aggregator,
    instantiate_analysts,
    instantiate_data_provider,
)
from hermes_quant.daemon.portfolio_loader import reconstruct_portfolio
from hermes_quant.daemon.signal_bus import emit_execution_record

# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    """These tests require entry points to be registered (via pip install -e).

    Discovery may return empty if the package isn't installed; we just
    verify the API contract.
    """

    def test_discover_returns_dict(self):
        d = discover_analysts()
        assert isinstance(d, dict)

    def test_discover_aggregators_returns_dict(self):
        d = discover_aggregators()
        assert isinstance(d, dict)

    def test_discover_aggregators_no_dangling_stacking_warning(self, caplog):
        """Regression (FIX A1): the `stacking` aggregator entry point was
        declared in pyproject.toml but aggregators/stacking.py never landed,
        so discover_aggregators() logged a "failed to load entry point
        stacking ... No module named ..." warning on every daemon boot.

        Assert (a) no warning mentions a failed/missing 'stacking' load, and
        (b) every discovered aggregator name resolves to an importable module
        (i.e. the entry-point set is exactly the modules that exist).

        Skips gracefully if the package isn't installed (no entry points).
        """
        import importlib

        with caplog.at_level("WARNING", logger="hermes_quant.daemon.discovery"):
            discovered = discover_aggregators()

        for record in caplog.records:
            msg = record.getMessage().lower()
            assert not ("failed to load entry point" in msg and "stacking" in msg), (
                "dangling 'stacking' entry point regressed: " + record.getMessage()
            )

        if not discovered:
            pytest.skip("package not installed; no aggregator entry points registered")

        # Every discovered class must come from a module that actually imports.
        assert "stacking" not in discovered
        for name, cls in discovered.items():
            module = importlib.import_module(cls.__module__)
            assert module is not None, name

    def test_discover_data_providers_returns_dict(self):
        d = discover_data_providers()
        assert isinstance(d, dict)

    def test_instantiate_unknown_returns_none_warning(self):
        a = instantiate_aggregator("nonexistent")
        assert a is None
        p = instantiate_data_provider("nonexistent")
        assert p is None

    def test_instantiate_analysts_unknown_skipped(self):
        out = instantiate_analysts(enabled_names=["nonexistent"])
        assert out == []


# ---------------------------------------------------------------------------
# PortfolioLoader
# ---------------------------------------------------------------------------


class TestPortfolioReconstruction:
    def _exec(
        self,
        side: str,
        qty: float,
        fill: float,
        *,
        asset: str = "BTC/USDT",
        account_id: str = "alpaca-paper",
        asset_class: str = "crypto",
        fees: float = 0.0,
    ) -> dict:
        return {
            "schema_version": 1,
            "exec_id": f"exec-{side}-{qty}-{fill}",
            "asof": pd.Timestamp.utcnow().isoformat(),
            "asset": asset,
            "side": side,
            "qty": qty,
            "fill_price": fill,
            "decision_price": fill,
            "fees": fees,
            "account_id": account_id,
            "asset_class": asset_class,
        }

    def test_empty_executions_returns_initial_cash(self, tmp_path):
        bus = tmp_path / "execs.jsonl"
        p = reconstruct_portfolio(
            "alpaca-paper",
            "crypto",
            initial_cash=100_000.0,
            bus_path=bus,
        )
        assert p.cash == 100_000.0
        assert p.positions == {}
        assert p.equity_total == 100_000.0

    def test_buy_then_close_realizes_pnl(self, tmp_path):
        bus = tmp_path / "execs.jsonl"
        # Buy 1 BTC at 60k
        emit_execution_record(self._exec("buy", 1.0, 60_000.0), path=bus)
        # Sell 1 BTC at 62k
        emit_execution_record(self._exec("sell", 1.0, 62_000.0), path=bus)

        p = reconstruct_portfolio(
            "alpaca-paper",
            "crypto",
            initial_cash=100_000.0,
            bus_path=bus,
        )
        # Position closed
        assert "BTC/USDT" not in p.positions
        # Realized PnL = 2000 (bought at 60k, sold at 62k)
        assert p.realized_pnl_total == pytest.approx(2_000.0)

    def test_short_then_cover_realizes_positive_pnl(self, tmp_path):
        """cs00 regression: shorting then covering CHEAPER must realize a POSITIVE
        P&L. The v0.1.1 full-close formula carried a spurious trailing
        `* (1 if old_qty > 0 else -1)` factor that INVERTED the short branch
        (sell 10@100 then buy 10@90 booked -100 instead of +100). A short cover
        is a full close (new_qty == 0), which the v0.1.1 gate permits, so this
        stream reaches the fixed branch.
        """
        bus = tmp_path / "execs.jsonl"
        # Sell 10 BTC at 100 (open short), then buy 10 BTC at 90 (cover cheaper).
        emit_execution_record(self._exec("sell", 10.0, 100.0), path=bus)
        emit_execution_record(self._exec("buy", 10.0, 90.0), path=bus)

        p = reconstruct_portfolio(
            "alpaca-paper",
            "crypto",
            initial_cash=100_000.0,
            bus_path=bus,
        )
        # Position fully closed.
        assert "BTC/USDT" not in p.positions
        # Shorted at 100, covered at 90 => +100 realized profit (was -100 buggy).
        assert p.realized_pnl_total == pytest.approx(100.0)

    def test_open_position_marked_to_market(self, tmp_path):
        bus = tmp_path / "execs.jsonl"
        emit_execution_record(self._exec("buy", 1.0, 60_000.0), path=bus)
        p = reconstruct_portfolio(
            "alpaca-paper",
            "crypto",
            initial_cash=100_000.0,
            bus_path=bus,
            mark_prices={"BTC/USDT": 65_000.0},
        )
        assert "BTC/USDT" in p.positions
        pos = p.positions["BTC/USDT"]
        assert pos.qty == 1.0
        assert pos.avg_entry_price == 60_000.0
        assert pos.mark_price == 65_000.0
        assert pos.unrealized_pnl == pytest.approx(5_000.0)

    def test_filters_by_account_and_class(self, tmp_path):
        bus = tmp_path / "execs.jsonl"
        emit_execution_record(
            self._exec("buy", 1.0, 60_000.0, account_id="alpaca-paper"),
            path=bus,
        )
        emit_execution_record(
            self._exec("buy", 1.0, 60_000.0, account_id="binance-spot"),
            path=bus,
        )
        # Only alpaca-paper records should affect this portfolio
        p = reconstruct_portfolio(
            "alpaca-paper",
            "crypto",
            initial_cash=100_000.0,
            bus_path=bus,
        )
        assert p.positions["BTC/USDT"].qty == 1.0  # only one buy

    def test_drawdown_pct(self, tmp_path):
        bus = tmp_path / "execs.jsonl"
        # Buy 1 BTC at 60k, immediately mark at 50k (loss)
        emit_execution_record(self._exec("buy", 1.0, 60_000.0), path=bus)
        p = reconstruct_portfolio(
            "alpaca-paper",
            "crypto",
            initial_cash=100_000.0,
            bus_path=bus,
            mark_prices={"BTC/USDT": 50_000.0},
        )
        # equity = (100k - 60k cash) + 1*50k mark = 90k
        # peak ≥ initial 100k
        assert p.drawdown_pct >= 0.05

    def test_fees_subtract_from_cash(self, tmp_path):
        bus = tmp_path / "execs.jsonl"
        emit_execution_record(self._exec("buy", 1.0, 60_000.0, fees=10.0), path=bus)
        p = reconstruct_portfolio(
            "alpaca-paper",
            "crypto",
            initial_cash=100_000.0,
            bus_path=bus,
            mark_prices={"BTC/USDT": 60_000.0},
        )
        # cash = 100k - 60k - 10 = 39990
        assert p.cash == pytest.approx(100_000.0 - 60_000.0 - 10.0)
        assert p.realized_fees_total == 10.0

    def test_malformed_record_skipped(self, tmp_path):
        bus = tmp_path / "execs.jsonl"
        # Write a malformed record (missing fields) directly
        good = self._exec("buy", 1.0, 60_000.0)
        emit_execution_record(good, path=bus)
        # Append a malformed record manually (missing asset)
        bad = {
            "schema_version": 1,
            "side": "buy",
            "account_id": "alpaca-paper",
            "asset_class": "crypto",
        }
        emit_execution_record(bad, path=bus)
        # Reconstruction skips the bad one
        p = reconstruct_portfolio(
            "alpaca-paper",
            "crypto",
            initial_cash=100_000.0,
            bus_path=bus,
        )
        # Good record processed
        assert "BTC/USDT" in p.positions

    # Phase-8 P1-α regression (synthesis 2026-05-13): the v0.1.1 portfolio
    # reconstruction GATES OFF partial closes and direction flips because
    # the existing branch logic has known sign-convention bugs (caught by
    # both Claude P1 and DeepSeek P0). The gate raises NotImplementedError
    # rather than silently corrupting equity/drawdown computations.
    # v0.1.2 will land the rewrite + 8 explicit case tests.

    def test_partial_close_raises_not_implemented(self, tmp_path):
        """Selling 0.5 BTC of a 1 BTC long must raise (gate active)."""
        import pytest as _pytest

        bus = tmp_path / "execs.jsonl"
        emit_execution_record(self._exec("buy", 1.0, 60_000.0), path=bus)
        emit_execution_record(self._exec("sell", 0.5, 62_000.0), path=bus)

        with _pytest.raises(NotImplementedError, match="Phase-8 P1-α"):
            reconstruct_portfolio(
                "alpaca-paper",
                "crypto",
                initial_cash=100_000.0,
                bus_path=bus,
            )

    def test_direction_flip_raises_not_implemented(self, tmp_path):
        """Selling 1.5 BTC of a 1 BTC long (long → short flip) must raise."""
        import pytest as _pytest

        bus = tmp_path / "execs.jsonl"
        emit_execution_record(self._exec("buy", 1.0, 60_000.0), path=bus)
        emit_execution_record(self._exec("sell", 1.5, 62_000.0), path=bus)

        with _pytest.raises(NotImplementedError, match="Phase-8 P1-α"):
            reconstruct_portfolio(
                "alpaca-paper",
                "crypto",
                initial_cash=100_000.0,
                bus_path=bus,
            )

    def test_buy_then_full_close_still_works(self, tmp_path):
        """Sanity: clean full-close path NOT affected by the gate."""
        bus = tmp_path / "execs.jsonl"
        emit_execution_record(self._exec("buy", 1.0, 60_000.0), path=bus)
        emit_execution_record(self._exec("sell", 1.0, 62_000.0), path=bus)

        p = reconstruct_portfolio(
            "alpaca-paper",
            "crypto",
            initial_cash=100_000.0,
            bus_path=bus,
        )
        assert "BTC/USDT" not in p.positions
        assert p.realized_pnl_total == pytest.approx(2_000.0)

    def test_scale_in_same_direction_still_works(self, tmp_path):
        """Sanity: scaling into a long (buy + buy) is the same-direction
        path, NOT gated."""
        bus = tmp_path / "execs.jsonl"
        emit_execution_record(self._exec("buy", 1.0, 60_000.0), path=bus)
        emit_execution_record(self._exec("buy", 0.5, 61_000.0), path=bus)

        p = reconstruct_portfolio(
            "alpaca-paper",
            "crypto",
            initial_cash=200_000.0,
            bus_path=bus,
            mark_prices={"BTC/USDT": 62_000.0},
        )
        assert "BTC/USDT" in p.positions
        assert p.positions["BTC/USDT"].qty == pytest.approx(1.5)
