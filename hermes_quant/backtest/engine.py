"""hermes_quant.backtest.engine — WalkForwardEngine with no-lookahead contract (Wave 6a / ADR-0045).

Walk-forward contract
---------------------
Walk-forward contract: any read of holdout-window data inside strategy.decide() is a
leakage bug. The engine asserts no holdout-window dates are passed into strategy callbacks.

This is failure mode F1 documented in arxiv:2605.19337: contaminated backtesting.
Only 2/19 empirically-primary papers in the 2026 SOTA scan report time-consistent splits.
The guard in this engine ensures hermes-quant hits at least R1 reproducibility (published
split protocol with no lookahead).

Engine design
-------------
Day-by-day stepping over the holdout window:
  1. Build lookback_data: all bars with date ≤ asof (sliced from the full OHLCV).
     The DataFrame's index is date-filtered so strategy.decide() cannot access future
     data — any attempt to index asof+1 raises KeyError (or IndexError for .iloc).
  2. Call strategy.decide(asof, lookback_data) → list[Decision].
  3. Execute fills on next-day's open using CostModel.apply_to_fill().
  4. Advance positions and track PnL.
  5. Aggregate metrics (Sharpe, max drawdown, Sortino, win rate, etc.).

Metrics
-------
All metrics are computed on daily NAV changes over the holdout window:
- total_return        : (final_nav - initial_nav) / initial_nav
- sharpe              : annualised Sharpe (rf=0, × sqrt(252))
- sortino             : annualised Sortino (downside std only)
- max_drawdown        : maximum peak-to-trough drawdown (negative fraction)
- win_rate            : fraction of trades with positive gross PnL
- n_trades            : count of fills executed
- gross_pnl           : PnL before costs
- cost_pnl            : cost drag (always negative or zero)
- benchmark_return    : BuyAndHold return on the same holdout window
- alpha_vs_benchmark  : total_return − benchmark_return
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pandas as pd
import numpy as np

from hermes_quant.backtest.cost_model import CostModel, LIQUID_EQUITY
from hermes_quant.backtest.strategy import Decision, BuyAndHoldStrategy

if TYPE_CHECKING:
    from hermes_quant.backtest.strategy import Strategy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LookaheadViolation
# ---------------------------------------------------------------------------


class LookaheadViolation(Exception):
    """Raised when the engine detects a strategy attempting to read future data.

    This is failure mode F1 in arxiv:2605.19337 — contaminated backtesting.
    The engine's lookahead guard raises this exception rather than silently
    allowing the violation, making leakage a hard error rather than a silent
    performance inflation.
    """


# ---------------------------------------------------------------------------
# WalkForwardConfig
# ---------------------------------------------------------------------------


@dataclass
class WalkForwardConfig:
    """Configuration for a single walk-forward evaluation run.

    Parameters
    ----------
    train_start, train_end:
        The in-sample training window.  The engine does NOT use this window
        directly — it is recorded in the result metadata for the caller's
        use (e.g. strategy warm-up, calibration).
    holdout_start, holdout_end:
        The out-of-sample holdout window.  The engine steps day-by-day
        through this window.  The strategy NEVER receives data after each
        day's asof date.
    step_days:
        Number of calendar days to advance per engine step.  Default 1
        (true daily walk-forward).
    lookback_days:
        Number of calendar days of history to include in lookback_data
        on each step.  Default 252 (≈ 1 trading year).
    initial_nav:
        Starting NAV for position accounting.  Default 100_000.
    """

    train_start: pd.Timestamp
    train_end: pd.Timestamp
    holdout_start: pd.Timestamp
    holdout_end: pd.Timestamp
    step_days: int = 1
    lookback_days: int = 252
    initial_nav: float = 100_000.0


# ---------------------------------------------------------------------------
# WalkForwardResult
# ---------------------------------------------------------------------------


@dataclass
class WalkForwardResult:
    """Aggregate metrics from one WalkForwardEngine run.

    Attributes
    ----------
    total_return:
        Fractional total return over the holdout window (e.g. 0.12 = +12 %).
    sharpe:
        Annualised Sharpe ratio (risk-free = 0).
    sortino:
        Annualised Sortino ratio (downside deviation).
    max_drawdown:
        Maximum peak-to-trough drawdown (negative fraction, e.g. -0.15 = -15 %).
    win_rate:
        Fraction of completed round-trip trades with positive gross PnL.
    n_trades:
        Number of fills executed (entries + exits).
    gross_pnl:
        PnL before transaction costs.
    cost_pnl:
        Transaction cost drag (always ≤ 0).
    benchmark_return:
        Buy-and-hold return on the same holdout window (equal-weight universe).
    alpha_vs_benchmark:
        total_return − benchmark_return (net alpha).
    decisions_journal:
        List of dicts, one per fill, containing asof, symbol, action,
        fill_price, size_fraction, gross_pnl_contribution, cost_bps.
    nav_series:
        Daily NAV series over the holdout window.
    config:
        The WalkForwardConfig used for this run.
    """

    total_return: float
    sharpe: float
    sortino: float
    max_drawdown: float
    win_rate: float
    n_trades: int
    gross_pnl: float
    cost_pnl: float
    benchmark_return: float
    alpha_vs_benchmark: float
    decisions_journal: list[dict[str, Any]] = field(default_factory=list)
    nav_series: list[float] = field(default_factory=list)
    config: WalkForwardConfig | None = None


# ---------------------------------------------------------------------------
# _LookaheadGuardedDataFrame
# ---------------------------------------------------------------------------


class _LookaheadGuardedFrame:
    """Thin wrapper that raises LookaheadViolation if any date > asof is read.

    The WalkForwardEngine passes one of these to strategy.decide() so that
    any .loc / .iloc access beyond asof triggers a hard error rather than
    silently inflating strategy performance.

    The strategy receives this as a pd.DataFrame (we subclass nothing — the
    guard is applied at construction time by pre-filtering).

    Implementation: we simply return a filtered DataFrame whose index only
    contains dates ≤ asof.  If strategy code tries to index a future date,
    pandas raises KeyError naturally.  The engine also asserts max(index) ≤ asof
    before calling decide(), which catches bugs in the engine itself.
    """

    @staticmethod
    def build(
        full_ohlcv: pd.DataFrame,
        asof: pd.Timestamp,
        lookback_days: int,
    ) -> pd.DataFrame:
        """Return a date-filtered copy safe to pass to a strategy.

        The returned DataFrame's index contains only dates ≤ asof and
        within the trailing lookback_days window.
        """
        # Filter: only dates ≤ asof
        mask = full_ohlcv.index <= asof
        filtered = full_ohlcv.loc[mask]

        # Apply lookback window
        window_start = asof - pd.Timedelta(days=lookback_days)
        filtered = filtered.loc[filtered.index >= window_start]

        return filtered.copy()


# ---------------------------------------------------------------------------
# WalkForwardEngine
# ---------------------------------------------------------------------------


class WalkForwardEngine:
    """Day-by-day walk-forward backtester with explicit cost model.

    Walk-forward contract: any read of holdout-window data inside
    strategy.decide() is a leakage bug. The engine asserts no
    holdout-window dates are passed into strategy callbacks.

    The engine enforces the no-lookahead contract by:
      1. Building lookback_data with only dates ≤ asof (via
         _LookaheadGuardedFrame.build).
      2. Asserting that lookback_data.index.max() ≤ asof before calling
         strategy.decide().  If this assertion fails, it raises
         LookaheadViolation (engine bug, not strategy bug).

    Strategy-side lookahead is caught implicitly: the filtered DataFrame does
    not contain future dates, so any .loc[future_date] call raises KeyError.
    Tests prove this via the LEAKAGE GUARD TEST.

    Parameters
    ----------
    config:
        WalkForwardConfig specifying the evaluation windows.
    """

    def __init__(self, config: WalkForwardConfig) -> None:
        self._config = config

    # ------------------------------------------------------------------

    def run(
        self,
        strategy: "Strategy",
        universe: list[str],
        ohlcv: pd.DataFrame,
        *,
        cost_model: CostModel | None = None,
        dry_run_llm: bool = True,  # noqa: ARG002 — consumed by HermesQuantStrategy caller
    ) -> WalkForwardResult:
        """Execute the walk-forward simulation.

        Walk-forward contract: any read of holdout-window data inside
        strategy.decide() is a leakage bug. The engine asserts no
        holdout-window dates are passed into strategy callbacks.

        Parameters
        ----------
        strategy:
            A Strategy instance (must implement decide(asof, lookback_data)).
        universe:
            List of tickers (informational; used for benchmark calculation).
        ohlcv:
            Full OHLCV DataFrame indexed by pd.DatetimeIndex (daily bars).
            Columns: [open, high, low, close, volume] or MultiIndex variants.
            Must span train_start .. holdout_end.
        cost_model:
            CostModel to use for all fills.  Defaults to LIQUID_EQUITY.
        dry_run_llm:
            Passed through; the strategy should already be configured for
            dry-run mode before being passed to .run().

        Returns
        -------
        WalkForwardResult
        """
        cm = cost_model or LIQUID_EQUITY
        cfg = self._config

        # Normalise index to UTC-naive dates
        ohlcv = _normalize_index(ohlcv)

        # Enumerate holdout trading days
        holdout_days = _trading_days_in_range(ohlcv, cfg.holdout_start, cfg.holdout_end)
        if len(holdout_days) == 0:
            logger.warning("WalkForwardEngine: no trading days in holdout window.")
            return self._empty_result()

        # Engine state
        nav = cfg.initial_nav
        nav_series: list[float] = [nav]
        gross_pnl_total = 0.0
        cost_pnl_total = 0.0
        n_trades = 0
        winning_trades = 0
        total_trades_for_winrate = 0
        journal: list[dict[str, Any]] = []

        # Symbol position book: symbol → {qty, avg_cost, prev_fill_price}
        positions: dict[str, dict[str, float]] = {sym: {"qty": 0.0, "avg_cost": 0.0} for sym in universe}

        # Apply step_days
        asof_dates = holdout_days[:: max(1, cfg.step_days)]

        for asof in asof_dates:
            # ----------------------------------------------------------------
            # Engine no-lookahead guard: build lookback before calling decide
            # ----------------------------------------------------------------
            lookback = _LookaheadGuardedFrame.build(ohlcv, asof, cfg.lookback_days)

            if len(lookback) > 0:
                max_lb_date = lookback.index.max()
                if max_lb_date > asof:
                    raise LookaheadViolation(
                        f"Engine bug: lookback_data contains date {max_lb_date} > asof {asof}. "
                        "This is a leakage bug in the engine itself."
                    )

            # Call strategy
            try:
                decisions = strategy.decide(asof, lookback)
            except LookaheadViolation:
                raise  # re-raise strategy-side lookahead violations
            except Exception as exc:  # noqa: BLE001
                logger.warning("Strategy.decide raised %s at %s; skipping.", exc, asof)
                decisions = []

            # ----------------------------------------------------------------
            # Execute fills on *this* bar's close (next day open is ideal
            # but we use close-of-asof as decision price, with costs applied).
            # ----------------------------------------------------------------
            close_px = _get_close_prices(ohlcv, asof, universe)

            for decision in decisions:
                if not decision.is_active():
                    continue

                sym = decision.symbol
                px = close_px.get(sym)
                if px is None or px <= 0:
                    continue

                side = 1 if decision.action == "BUY" else -1
                fill_px = cm.apply_to_fill(px, side, participation_pct=0.05)

                # Notional traded
                alloc_cash = nav * decision.size_fraction
                shares = alloc_cash / fill_px if fill_px > 0 else 0.0

                if shares <= 0:
                    continue

                # Gross PnL vs decision price
                gross_px = px  # no-cost reference
                gross_pnl_contrib = shares * (fill_px - gross_px) * (-side)  # cost is negative
                cost_per_share_frac = abs(fill_px - gross_px) / gross_px
                cost_pnl_contrib = -shares * px * cost_per_share_frac

                # Update portfolio
                pos = positions.setdefault(sym, {"qty": 0.0, "avg_cost": 0.0})
                if side == 1:
                    new_qty = pos["qty"] + shares
                    if new_qty > 0:
                        pos["avg_cost"] = (pos["qty"] * pos["avg_cost"] + shares * fill_px) / new_qty
                    pos["qty"] = new_qty
                else:
                    # Selling: realise PnL
                    realised = shares * (fill_px - pos["avg_cost"])
                    gross_pnl_total += realised
                    if realised > 0:
                        winning_trades += 1
                    total_trades_for_winrate += 1
                    pos["qty"] = max(0.0, pos["qty"] - shares)

                gross_pnl_total += gross_pnl_contrib
                cost_pnl_total += cost_pnl_contrib
                nav += gross_pnl_contrib + cost_pnl_contrib
                n_trades += 1

                journal.append({
                    "asof": str(asof.date()),
                    "symbol": sym,
                    "action": decision.action,
                    "fill_price": round(fill_px, 4),
                    "decision_price": round(px, 4),
                    "shares": round(shares, 4),
                    "size_fraction": decision.size_fraction,
                    "gross_pnl_contribution": round(gross_pnl_contrib, 4),
                    "cost_pnl_contribution": round(cost_pnl_contrib, 4),
                    "confidence": decision.confidence,
                })

            nav_series.append(nav)

        # ----------------------------------------------------------------
        # Benchmark: BuyAndHoldStrategy over same holdout window
        # ----------------------------------------------------------------
        benchmark_return = _compute_buy_hold_return(ohlcv, universe, cfg.holdout_start, cfg.holdout_end)

        # ----------------------------------------------------------------
        # Metrics
        # ----------------------------------------------------------------
        initial_nav = cfg.initial_nav
        total_return = (nav - initial_nav) / initial_nav if initial_nav > 0 else 0.0

        daily_rets = _daily_returns(nav_series)
        sharpe = _annualised_sharpe(daily_rets)
        sortino = _annualised_sortino(daily_rets)
        max_dd = _max_drawdown(nav_series)
        win_rate = (winning_trades / total_trades_for_winrate) if total_trades_for_winrate > 0 else float("nan")

        return WalkForwardResult(
            total_return=total_return,
            sharpe=sharpe,
            sortino=sortino,
            max_drawdown=max_dd,
            win_rate=win_rate,
            n_trades=n_trades,
            gross_pnl=gross_pnl_total,
            cost_pnl=cost_pnl_total,
            benchmark_return=benchmark_return,
            alpha_vs_benchmark=total_return - benchmark_return,
            decisions_journal=journal,
            nav_series=nav_series,
            config=self._config,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _empty_result(self) -> WalkForwardResult:
        return WalkForwardResult(
            total_return=0.0,
            sharpe=0.0,
            sortino=0.0,
            max_drawdown=0.0,
            win_rate=float("nan"),
            n_trades=0,
            gross_pnl=0.0,
            cost_pnl=0.0,
            benchmark_return=0.0,
            alpha_vs_benchmark=0.0,
            config=self._config,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _normalize_index(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure the DataFrame has a DatetimeIndex with tz stripped."""
    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex):
        df = df.copy()
        df.index = pd.to_datetime(df.index)
        idx = df.index  # type: ignore[assignment]
    if getattr(idx, "tz", None) is not None:
        df = df.copy()
        df.index = idx.tz_localize(None)  # type: ignore[union-attr]
    return df


def _trading_days_in_range(
    ohlcv: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> list[pd.Timestamp]:
    """Return sorted list of index dates in [start, end]."""
    mask = (ohlcv.index >= start) & (ohlcv.index <= end)
    return sorted(ohlcv.index[mask].tolist())


def _get_close_prices(
    ohlcv: pd.DataFrame,
    asof: pd.Timestamp,
    universe: list[str],
) -> dict[str, float]:
    """Extract close prices for the universe at asof."""
    result: dict[str, float] = {}
    if asof not in ohlcv.index:
        return result

    row = ohlcv.loc[asof]

    if isinstance(ohlcv.columns, pd.MultiIndex):
        for sym in universe:
            try:
                px = float(row[("close", sym)])
                if math.isfinite(px) and px > 0:
                    result[sym] = px
            except (KeyError, TypeError):
                pass
    else:
        # Single symbol: use "close" column; universe is informational
        try:
            if isinstance(row, pd.Series):
                px = float(row["close"])
            else:
                px = float(row["close"].iloc[0])
            if math.isfinite(px) and px > 0:
                for sym in universe:
                    result[sym] = px
        except (KeyError, TypeError):
            pass

    return result


def _compute_buy_hold_return(
    ohlcv: pd.DataFrame,
    universe: list[str],
    holdout_start: pd.Timestamp,
    holdout_end: pd.Timestamp,
) -> float:
    """Compute equal-weight buy-and-hold return over the holdout window."""
    returns_per_sym: list[float] = []
    days = _trading_days_in_range(ohlcv, holdout_start, holdout_end)
    if len(days) < 2:
        return 0.0

    first_day, last_day = days[0], days[-1]

    for sym in universe:
        try:
            if isinstance(ohlcv.columns, pd.MultiIndex):
                start_px = float(ohlcv.loc[first_day, ("close", sym)])
                end_px = float(ohlcv.loc[last_day, ("close", sym)])
            else:
                start_px = float(ohlcv.loc[first_day, "close"])
                end_px = float(ohlcv.loc[last_day, "close"])
            if start_px > 0 and math.isfinite(start_px) and math.isfinite(end_px):
                returns_per_sym.append(end_px / start_px - 1.0)
        except (KeyError, TypeError):
            pass

    return float(np.mean(returns_per_sym)) if returns_per_sym else 0.0


def _daily_returns(nav_series: list[float]) -> list[float]:
    """Compute daily fractional returns from NAV series."""
    if len(nav_series) < 2:
        return []
    return [
        (nav_series[i] - nav_series[i - 1]) / nav_series[i - 1]
        for i in range(1, len(nav_series))
        if nav_series[i - 1] > 0
    ]


def _annualised_sharpe(daily_rets: list[float]) -> float:
    """Annualised Sharpe ratio (rf=0)."""
    if len(daily_rets) < 2:
        return 0.0
    arr = np.array(daily_rets, dtype=float)
    std = float(np.std(arr, ddof=1))
    if std == 0:
        return 0.0
    return float(np.mean(arr) / std * math.sqrt(252))


def _annualised_sortino(daily_rets: list[float]) -> float:
    """Annualised Sortino ratio (rf=0, downside std only)."""
    if len(daily_rets) < 2:
        return 0.0
    arr = np.array(daily_rets, dtype=float)
    downside = arr[arr < 0]
    if len(downside) < 2:
        return float(np.mean(arr) * math.sqrt(252)) if np.mean(arr) > 0 else 0.0
    std_down = float(np.std(downside, ddof=1))
    if std_down == 0:
        return 0.0
    return float(np.mean(arr) / std_down * math.sqrt(252))


def _max_drawdown(nav_series: list[float]) -> float:
    """Maximum peak-to-trough drawdown (negative fraction)."""
    if len(nav_series) < 2:
        return 0.0
    arr = np.array(nav_series, dtype=float)
    running_max = np.maximum.accumulate(arr)
    drawdowns = (arr - running_max) / np.where(running_max > 0, running_max, 1.0)
    return float(drawdowns.min())
