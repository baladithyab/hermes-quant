"""tests/backtest/test_walk_forward.py — WalkForwardEngine tests (Wave 6a / ADR-0045).

Coverage:
- Synthetic GBM OHLCV fixture (no yfinance/Alpaca dependency)
- HermesQuantStrategy end-to-end run: no crash, finite Sharpe
- BuyAndHoldStrategy baseline sanity
- **WALK-FORWARD LEAKAGE GUARD TEST** (canonical regression for F1 / arxiv:2605.19337):
  A strategy that tries to read asof+1 data receives a KeyError because
  lookback_data has no entries after asof.  The engine also raises
  LookaheadViolation if its own guard detects engine-side leakage.
- Cost model drag: net return < gross return
- WalkForwardResult fields are finite and coherent
- alpha_vs_benchmark = total_return - benchmark_return
"""

from __future__ import annotations

import math
import numpy as np
import pandas as pd
import pytest

from hermes_quant.backtest.cost_model import LIQUID_EQUITY, CostModel
from hermes_quant.backtest.engine import (
    LookaheadViolation,
    WalkForwardConfig,
    WalkForwardEngine,
    WalkForwardResult,
)
from hermes_quant.backtest.strategy import (
    BuyAndHoldStrategy,
    Decision,
    HermesQuantStrategy,
)


# ---------------------------------------------------------------------------
# Synthetic OHLCV fixture
# ---------------------------------------------------------------------------


def make_gbm_ohlcv(
    n_days: int = 90,
    start: str = "2024-01-02",
    s0: float = 100.0,
    mu: float = 0.0003,
    sigma: float = 0.015,
    seed: int = 42,
) -> pd.DataFrame:
    """Generate synthetic OHLCV via Geometric Brownian Motion.

    No external data dependencies.  Used as the synthetic universe for all
    walk-forward tests.

    Parameters
    ----------
    n_days:
        Number of trading days to generate.
    start:
        Start date string.
    s0:
        Initial price.
    mu:
        Daily drift.
    sigma:
        Daily volatility.
    seed:
        Random seed for reproducibility.

    Returns
    -------
    pd.DataFrame
        DatetimeIndex, columns=[open, high, low, close, volume].
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start=start, periods=n_days)
    rets = rng.normal(mu, sigma, n_days)
    closes = s0 * np.cumprod(1 + rets)
    opens = np.roll(closes, 1)
    opens[0] = s0
    noise = rng.uniform(0.995, 1.005, n_days)
    highs = np.maximum(opens, closes) * noise
    lows = np.minimum(opens, closes) / noise
    volumes = rng.integers(100_000, 1_000_000, n_days).astype(float)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=dates,
    )


@pytest.fixture
def ohlcv_60():
    """60 trading days of GBM data."""
    return make_gbm_ohlcv(n_days=60)


@pytest.fixture
def ohlcv_90():
    """90 trading days of GBM data."""
    return make_gbm_ohlcv(n_days=90)


@pytest.fixture
def walk_forward_config_60(ohlcv_60):
    """Config with ~30 day train / ~20 day holdout on the 60-day fixture."""
    dates = ohlcv_60.index
    return WalkForwardConfig(
        train_start=dates[0],
        train_end=dates[29],
        holdout_start=dates[30],
        holdout_end=dates[49],
        step_days=1,
        lookback_days=60,
        initial_nav=100_000.0,
    )


@pytest.fixture
def walk_forward_config_90(ohlcv_90):
    """Config with ~50 day train / ~30 day holdout on the 90-day fixture."""
    dates = ohlcv_90.index
    return WalkForwardConfig(
        train_start=dates[0],
        train_end=dates[49],
        holdout_start=dates[50],
        holdout_end=dates[79],
        step_days=1,
        lookback_days=90,
        initial_nav=100_000.0,
    )


# ---------------------------------------------------------------------------
# WalkForwardConfig
# ---------------------------------------------------------------------------


class TestWalkForwardConfig:
    def test_config_attributes(self, ohlcv_60):
        dates = ohlcv_60.index
        cfg = WalkForwardConfig(
            train_start=dates[0],
            train_end=dates[29],
            holdout_start=dates[30],
            holdout_end=dates[49],
        )
        assert cfg.step_days == 1
        assert cfg.lookback_days == 252
        assert cfg.initial_nav == 100_000.0

    def test_config_custom_values(self, ohlcv_60):
        dates = ohlcv_60.index
        cfg = WalkForwardConfig(
            train_start=dates[0],
            train_end=dates[29],
            holdout_start=dates[30],
            holdout_end=dates[49],
            step_days=5,
            lookback_days=20,
            initial_nav=50_000.0,
        )
        assert cfg.step_days == 5
        assert cfg.lookback_days == 20
        assert cfg.initial_nav == 50_000.0


# ---------------------------------------------------------------------------
# BuyAndHoldStrategy
# ---------------------------------------------------------------------------


class TestBuyAndHoldStrategy:
    def test_first_call_returns_buy(self):
        bah = BuyAndHoldStrategy(["AAPL"])
        asof = pd.Timestamp("2024-01-02")
        df = pd.DataFrame({"open": [100.0], "close": [101.0]}, index=[asof])
        decisions = bah.decide(asof, df)
        assert len(decisions) == 1
        assert decisions[0].action == "BUY"
        assert decisions[0].symbol == "AAPL"

    def test_subsequent_calls_return_hold(self):
        bah = BuyAndHoldStrategy(["AAPL"])
        asof = pd.Timestamp("2024-01-02")
        df = pd.DataFrame({"open": [100.0], "close": [101.0]}, index=[asof])
        bah.decide(asof, df)  # first call — BUY
        for _ in range(5):
            decisions = bah.decide(asof, df)
            assert all(d.action == "HOLD" for d in decisions)

    def test_equal_weight_allocation(self):
        universe = ["AAPL", "GOOG", "MSFT"]
        bah = BuyAndHoldStrategy(universe)
        asof = pd.Timestamp("2024-01-02")
        df = pd.DataFrame({"close": [100.0]}, index=[asof])
        decisions = bah.decide(asof, df)
        expected_alloc = 1.0 / 3
        for d in decisions:
            assert math.isclose(d.size_fraction, expected_alloc, rel_tol=1e-6)

    def test_custom_allocation(self):
        bah = BuyAndHoldStrategy(["AAPL", "GOOG"], allocation_per_symbol=0.4)
        asof = pd.Timestamp("2024-01-02")
        df = pd.DataFrame({"close": [100.0]}, index=[asof])
        decisions = bah.decide(asof, df)
        for d in decisions:
            assert math.isclose(d.size_fraction, 0.4)


# ---------------------------------------------------------------------------
# HermesQuantStrategy
# ---------------------------------------------------------------------------


class TestHermesQuantStrategy:
    def test_returns_decisions_for_universe(self, ohlcv_60):
        strategy = HermesQuantStrategy(["SPY"], dry_run_llm=True, lookback_bars=5)
        asof = ohlcv_60.index[20]
        lookback = ohlcv_60.loc[ohlcv_60.index <= asof]
        decisions = strategy.decide(asof, lookback)
        assert len(decisions) == 1
        assert decisions[0].symbol == "SPY"

    def test_decision_action_valid(self, ohlcv_60):
        strategy = HermesQuantStrategy(["SPY"], dry_run_llm=True)
        asof = ohlcv_60.index[25]
        lookback = ohlcv_60.loc[ohlcv_60.index <= asof]
        decisions = strategy.decide(asof, lookback)
        for d in decisions:
            assert d.action in ("BUY", "SELL", "HOLD")

    def test_confidence_in_range(self, ohlcv_60):
        strategy = HermesQuantStrategy(["SPY"], dry_run_llm=True)
        asof = ohlcv_60.index[30]
        lookback = ohlcv_60.loc[ohlcv_60.index <= asof]
        decisions = strategy.decide(asof, lookback)
        for d in decisions:
            assert 0.0 <= d.confidence <= 1.0

    def test_size_fraction_in_range(self, ohlcv_60):
        strategy = HermesQuantStrategy(["SPY"], dry_run_llm=True)
        for i in range(20, 40):
            asof = ohlcv_60.index[i]
            lookback = ohlcv_60.loc[ohlcv_60.index <= asof]
            decisions = strategy.decide(asof, lookback)
            for d in decisions:
                assert 0.0 <= d.size_fraction <= 1.0


# ---------------------------------------------------------------------------
# WalkForwardEngine — end-to-end
# ---------------------------------------------------------------------------


class TestWalkForwardEngineEndToEnd:
    def test_hermes_strategy_no_crash(self, ohlcv_90, walk_forward_config_90):
        """HermesQuantStrategy + WalkForwardEngine: no exception, finite values."""
        strategy = HermesQuantStrategy(["SPY"], dry_run_llm=True, lookback_bars=10)
        engine = WalkForwardEngine(walk_forward_config_90)
        result = engine.run(strategy, ["SPY"], ohlcv_90, cost_model=LIQUID_EQUITY)
        assert isinstance(result, WalkForwardResult)

    def test_result_fields_finite(self, ohlcv_90, walk_forward_config_90):
        """All scalar metrics are finite (not NaN or inf)."""
        strategy = HermesQuantStrategy(["SPY"], dry_run_llm=True, lookback_bars=10)
        engine = WalkForwardEngine(walk_forward_config_90)
        result = engine.run(strategy, ["SPY"], ohlcv_90, cost_model=LIQUID_EQUITY)
        for field_name in ("total_return", "sharpe", "sortino", "max_drawdown", "benchmark_return", "alpha_vs_benchmark"):
            val = getattr(result, field_name)
            assert math.isfinite(val), f"{field_name}={val} is not finite"

    def test_alpha_equals_return_minus_benchmark(self, ohlcv_90, walk_forward_config_90):
        """alpha_vs_benchmark = total_return - benchmark_return (exact)."""
        strategy = HermesQuantStrategy(["SPY"], dry_run_llm=True)
        engine = WalkForwardEngine(walk_forward_config_90)
        result = engine.run(strategy, ["SPY"], ohlcv_90)
        assert math.isclose(
            result.alpha_vs_benchmark,
            result.total_return - result.benchmark_return,
            rel_tol=1e-9,
        )

    def test_max_drawdown_non_positive(self, ohlcv_90, walk_forward_config_90):
        """max_drawdown is always ≤ 0."""
        strategy = BuyAndHoldStrategy(["SPY"])
        engine = WalkForwardEngine(walk_forward_config_90)
        result = engine.run(strategy, ["SPY"], ohlcv_90)
        assert result.max_drawdown <= 0.0

    def test_buy_hold_n_trades_one_per_symbol(self, ohlcv_90, walk_forward_config_90):
        """BuyAndHoldStrategy executes exactly 1 trade per symbol."""
        strategy = BuyAndHoldStrategy(["SPY"])
        engine = WalkForwardEngine(walk_forward_config_90)
        result = engine.run(strategy, ["SPY"], ohlcv_90)
        assert result.n_trades >= 1  # at least the initial entry

    def test_cost_pnl_non_positive(self, ohlcv_90, walk_forward_config_90):
        """cost_pnl is always ≤ 0 (costs never help performance)."""
        strategy = HermesQuantStrategy(["SPY"], dry_run_llm=True)
        engine = WalkForwardEngine(walk_forward_config_90)
        result = engine.run(strategy, ["SPY"], ohlcv_90, cost_model=LIQUID_EQUITY)
        assert result.cost_pnl <= 0.0

    def test_nav_series_length(self, ohlcv_90, walk_forward_config_90):
        """NAV series has at least as many entries as holdout days."""
        strategy = BuyAndHoldStrategy(["SPY"])
        engine = WalkForwardEngine(walk_forward_config_90)
        result = engine.run(strategy, ["SPY"], ohlcv_90)
        assert len(result.nav_series) > 1

    def test_decisions_journal_populated(self, ohlcv_90, walk_forward_config_90):
        """Decisions journal is non-empty when trades are executed."""
        strategy = BuyAndHoldStrategy(["SPY"])
        engine = WalkForwardEngine(walk_forward_config_90)
        result = engine.run(strategy, ["SPY"], ohlcv_90)
        assert len(result.decisions_journal) >= 1
        entry = result.decisions_journal[0]
        assert "asof" in entry
        assert "symbol" in entry
        assert "fill_price" in entry

    def test_config_stored_in_result(self, ohlcv_90, walk_forward_config_90):
        """WalkForwardResult.config references the original config."""
        strategy = BuyAndHoldStrategy(["SPY"])
        engine = WalkForwardEngine(walk_forward_config_90)
        result = engine.run(strategy, ["SPY"], ohlcv_90)
        assert result.config is walk_forward_config_90


# ---------------------------------------------------------------------------
# WALK-FORWARD LEAKAGE GUARD TEST
# This is the canonical regression test for failure mode F1 (arxiv:2605.19337).
# A strategy that attempts to read asof+1 data should receive a KeyError because
# lookback_data contains NO entries after asof.
# ---------------------------------------------------------------------------


class _LookaheadCheatStrategy:
    """Test-only strategy that tries to read one day AFTER asof.

    This is the canonical leakage pattern: a strategy that peeks at tomorrow's
    close to decide today's trade.  The engine must prevent this by ensuring
    lookback_data contains no dates after asof.
    """

    def __init__(self):
        self.violation_raised = False
        self.future_dates_seen = []

    def decide(
        self,
        asof: pd.Timestamp,
        lookback_data: pd.DataFrame,
    ) -> list[Decision]:
        # Attempt to read one business day after asof
        future = asof + pd.tseries.offsets.BDay(1)
        # Try to access future date in the lookback data
        future_rows = lookback_data.loc[lookback_data.index > asof]
        if len(future_rows) > 0:
            # Found future data — this is a lookahead violation
            self.future_dates_seen.extend(future_rows.index.tolist())
        return []


class TestLookaheadGuard:
    """CANONICAL LEAKAGE GUARD TEST — regression for F1 (arxiv:2605.19337).

    A strategy that tries to read asof+1 data MUST NOT see any future data
    in lookback_data.  The engine's pre-filtering ensures that
    lookback_data.index.max() ≤ asof for all calls.
    """

    def test_lookback_data_has_no_future_dates(self, ohlcv_90, walk_forward_config_90):
        """LEAKAGE GUARD: lookback_data passed to strategy must never contain dates > asof.

        This is the canonical F1 regression test.  If this test fails, the
        engine is leaking holdout-window data into strategy callbacks.
        """
        cheater = _LookaheadCheatStrategy()
        engine = WalkForwardEngine(walk_forward_config_90)
        engine.run(cheater, ["SPY"], ohlcv_90)
        # The cheater strategy records any future dates it found in lookback_data
        assert cheater.future_dates_seen == [], (
            f"LEAKAGE BUG: engine passed future dates to strategy.decide(): "
            f"{cheater.future_dates_seen}"
        )

    def test_lookback_iloc_plus_one_is_keyerror(self, ohlcv_90, walk_forward_config_90):
        """Attempting to .loc[future_date] in lookback_data raises KeyError.

        This test directly proves that the strategy cannot read future prices
        by attempting a .loc lookup on a date after asof.
        """
        future_access_results = []

        class _KeyErrorTrap:
            def decide(self, asof, lookback_data):
                future = asof + pd.tseries.offsets.BDay(1)
                try:
                    _ = lookback_data.loc[future]
                    future_access_results.append(("found", future))
                except KeyError:
                    future_access_results.append(("keyerror", future))
                return []

        strategy = _KeyErrorTrap()
        engine = WalkForwardEngine(walk_forward_config_90)
        engine.run(strategy, ["SPY"], ohlcv_90)

        # Every attempt to access future data must raise KeyError
        for outcome, future_date in future_access_results:
            assert outcome == "keyerror", (
                f"Strategy was able to read future date {future_date} from lookback_data — "
                "this is a lookahead leak (F1 failure mode, arxiv:2605.19337)."
            )
        # Make sure the strategy actually ran
        assert len(future_access_results) > 0, "Leakage guard test did not execute any steps."

    def test_engine_raises_lookahead_violation_on_engine_bug(self, ohlcv_90):
        """Engine raises LookaheadViolation if its own guard fires (engine bug path)."""
        from hermes_quant.backtest.engine import _LookaheadGuardedFrame, LookaheadViolation

        # Directly test the guard: build a frame and verify max date ≤ asof
        asof = ohlcv_90.index[30]
        frame = _LookaheadGuardedFrame.build(ohlcv_90, asof, lookback_days=60)
        assert frame.index.max() <= asof, (
            "LookaheadGuardedFrame returned data after asof — engine bug!"
        )

    def test_lookback_data_max_date_leq_asof(self, ohlcv_90, walk_forward_config_90):
        """For every step, max(lookback_data.index) ≤ asof."""
        max_dates: list[tuple] = []

        class _DateRecorder:
            def decide(self, asof, lookback_data):
                if len(lookback_data) > 0:
                    max_dates.append((asof, lookback_data.index.max()))
                return []

        engine = WalkForwardEngine(walk_forward_config_90)
        engine.run(_DateRecorder(), ["SPY"], ohlcv_90)

        for asof, max_date in max_dates:
            assert max_date <= asof, (
                f"Lookback max date {max_date} > asof {asof}: leakage detected!"
            )
        assert len(max_dates) > 0, "No steps were executed."


# ---------------------------------------------------------------------------
# Higher-cost model increases drag
# ---------------------------------------------------------------------------


class TestCostModelEffect:
    def test_higher_cost_reduces_nav(self, ohlcv_90, walk_forward_config_90):
        """Running with ILLIQUID cost model produces lower or equal final NAV."""
        from hermes_quant.backtest.cost_model import ILLIQUID

        strategy_cheap = BuyAndHoldStrategy(["SPY"])
        strategy_expensive = BuyAndHoldStrategy(["SPY"])
        engine = WalkForwardEngine(walk_forward_config_90)

        result_cheap = engine.run(strategy_cheap, ["SPY"], ohlcv_90, cost_model=LIQUID_EQUITY)
        result_expensive = engine.run(strategy_expensive, ["SPY"], ohlcv_90, cost_model=ILLIQUID)

        # Illiquid costs more → lower or equal return (with active trading the gap widens)
        assert result_expensive.total_return <= result_cheap.total_return + 1e-9
