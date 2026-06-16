"""hermes_quant.eval.stockbench — STOCKBENCH-style smoke harness.

Provides a contamination-safe evaluation of any strategy against a
buy-and-hold baseline over a specified post-knowledge-cutoff window.

References:
    P3 — "STOCKBENCH: Can LLM Agents Trade Stocks Profitably In Real-world
          Markets?" (ICLR 2026 under review, OpenReview XUBKgiO29d)
    F1 — Contaminated Backtesting (Look-Ahead Bias via Training Data)
    C4 — Walk-Forward Evaluation with Transaction-Cost Models

Contamination guard
~~~~~~~~~~~~~~~~~~~
The window MUST start on or after the knowledge cutoff date (configurable via
env var HERMES_QUANT_KNOWLEDGE_CUTOFF, default 2025-01-01).  If window_start
is earlier, a ContaminationError is raised and contamination_guard_fired is
set True on any partial result.

Do NOT default this to 2024-04 or any other fixed cutoff tied to a specific
model version.  Operators running frontier models with later cutoffs should
set the env var accordingly.
"""

from __future__ import annotations

import logging
import math
import os
import warnings
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol, Sequence

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Knowledge-cutoff guard
# ---------------------------------------------------------------------------

_DEFAULT_KNOWLEDGE_CUTOFF = date(2025, 1, 1)


def _get_knowledge_cutoff() -> date:
    """Return the effective knowledge cutoff from env or default."""
    raw = os.environ.get("HERMES_QUANT_KNOWLEDGE_CUTOFF", "")
    if raw.strip():
        try:
            return date.fromisoformat(raw.strip())
        except ValueError:
            warnings.warn(
                f"HERMES_QUANT_KNOWLEDGE_CUTOFF={raw!r} is not a valid ISO date; "
                f"using default {_DEFAULT_KNOWLEDGE_CUTOFF}",
                stacklevel=3,
            )
    return _DEFAULT_KNOWLEDGE_CUTOFF


class ContaminationError(ValueError):
    """Raised when the evaluation window precedes the LLM knowledge cutoff.

    This is a hard error, not a warning, because using pre-cutoff data
    invalidates any result — the model has already seen the test period.
    """


# ---------------------------------------------------------------------------
# Strategy / data-source protocols (thin interfaces so real and stub impls
# both satisfy the harness without inheriting from a base class)
# ---------------------------------------------------------------------------


class StrategyProtocol(Protocol):
    """Minimal interface a strategy must expose to the harness."""

    def decide(
        self,
        ticker: str,
        as_of: date,
        price_history: np.ndarray,
    ) -> float:
        """Return a target position weight in [-1, +1] for `ticker` on `as_of`.

        A return value of 0 means flat/hold; +1 is full long; -1 is full short.
        The harness accepts fractional values for partial sizing.
        """
        ...


class PriceSourceProtocol(Protocol):
    """Minimal interface for synthetic or real price feeds."""

    def get_prices(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> np.ndarray:
        """Return a 1-D array of daily adjusted-close prices (start..end inclusive)."""
        ...


# ---------------------------------------------------------------------------
# Built-in synthetic price source (used in tests and smoke harness when
# no real data feed is configured)
# ---------------------------------------------------------------------------


class _SyntheticPriceSource:
    """Deterministic GBM price generator for testing.

    Seeded by ticker name hash so the same ticker always yields the same
    path, making tests reproducible across runs.
    """

    def __init__(self, *, mu: float = 0.0003, sigma: float = 0.015) -> None:
        self.mu = mu
        self.sigma = sigma

    def get_prices(self, ticker: str, start: date, end: date) -> np.ndarray:
        rng = np.random.default_rng(hash(ticker) % (2**32))
        n_days = (end - start).days + 1
        log_returns = rng.normal(self.mu, self.sigma, size=n_days)
        prices = 100.0 * np.exp(np.cumsum(log_returns))
        return prices


# ---------------------------------------------------------------------------
# Built-in stub strategy (buy-and-hold long; also used as benchmark)
# ---------------------------------------------------------------------------


class _BuyAndHoldStrategy:
    """Always-long strategy used as the benchmark baseline."""

    def decide(
        self,
        ticker: str,
        as_of: date,
        price_history: np.ndarray,
    ) -> float:
        return 1.0  # always full long


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

_DEFAULT_UNIVERSE: list[str] = ["AAPL", "MSFT", "NVDA", "GOOG", "META"]


@dataclass
class STOCKBENCHResult:
    """Evaluation metrics from a STOCKBENCH run.

    Attributes:
        universe:               Tickers evaluated.
        window_start:           First date of the evaluation window.
        window_end:             Last date of the evaluation window.
        benchmark:              Benchmark ticker (e.g. 'SPY').
        cumulative_return:      Portfolio cumulative return over the window.
        max_drawdown:           Maximum peak-to-trough drawdown (negative).
        sortino:                Sortino ratio (downside-deviation based).
        n_decisions:            Total position changes (flips / adjustments).
        decisions_per_day_avg:  Average decisions per calendar day.
        vs_buyhold_alpha:       Cumulative return minus buy-and-hold return.
        contamination_guard_fired: True when window_start was pre-cutoff.
        metadata:               Additional detail (per-ticker metrics, etc.).
    """

    universe: list[str]
    window_start: date
    window_end: date
    benchmark: str
    cumulative_return: float
    max_drawdown: float
    sortino: float
    n_decisions: int
    decisions_per_day_avg: float
    vs_buyhold_alpha: float
    contamination_guard_fired: bool
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "universe": self.universe,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "benchmark": self.benchmark,
            "cumulative_return": self.cumulative_return,
            "max_drawdown": self.max_drawdown,
            "sortino": self.sortino,
            "n_decisions": self.n_decisions,
            "decisions_per_day_avg": self.decisions_per_day_avg,
            "vs_buyhold_alpha": self.vs_buyhold_alpha,
            "contamination_guard_fired": self.contamination_guard_fired,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Portfolio math helpers
# ---------------------------------------------------------------------------


def _compute_max_drawdown(equity_curve: np.ndarray) -> float:
    """Return maximum peak-to-trough drawdown (negative float)."""
    if len(equity_curve) < 2:
        return 0.0
    peak = np.maximum.accumulate(equity_curve)
    drawdowns = (equity_curve - peak) / np.where(peak > 0, peak, 1.0)
    return float(np.min(drawdowns))


def _compute_sortino(daily_returns: np.ndarray, *, annualize: bool = True) -> float:
    """Sortino ratio = mean(r) / downside_deviation(r), optionally annualised.

    Downside deviation is the root-mean-square of the *below-target* returns
    about a minimum-acceptable-return (MAR) of 0 — i.e. ``sqrt(mean(min(r,0)²))``
    — NOT ``std(neg)`` about the losers' own mean.  The std-about-own-mean form
    is a fail-open trap: a strategy whose losing days are all the SAME magnitude
    (a fixed stop-loss, or steady down-drift at constant position size) has zero
    dispersion *about its own mean* and would have collapsed below 1e-12 →
    spurious +inf (the BEST possible Sortino) → cleared the promotion gate even
    though the strategy is a net loser.  Measuring RMS about MAR=0 keeps the
    downside deviation large (proportional to the loss magnitude), yielding a
    finite — and correctly negative, for a net-losing strategy — Sortino that
    the gate can reject.

    The +inf return is reserved for the TRUE no-downside case only: when there
    is not a single negative day, downside risk is genuinely zero and +inf
    (unbounded risk-adjusted return) is the correct, intended signal.
    """
    if len(daily_returns) < 2:
        return float("nan")
    mean_r = float(np.mean(daily_returns))
    # RMS of below-MAR(=0) returns about 0, across ALL days (not just losers).
    downside = np.minimum(daily_returns, 0.0)
    downside_dev = math.sqrt(float(np.mean(downside ** 2)))
    if downside_dev < 1e-12:
        # No negative day at all → no downside risk → unbounded Sortino.
        return float("inf")
    ratio = mean_r / downside_dev
    if annualize:
        ratio *= math.sqrt(252)
    return float(ratio)


# ---------------------------------------------------------------------------
# Main harness
# ---------------------------------------------------------------------------


class STOCKBENCHHarness:
    """STOCKBENCH-style smoke harness.

    Evaluates *strategy* over *universe* within *window* and computes
    cumulative return, max drawdown, Sortino, and alpha vs buy-and-hold.

    Contamination guard
    ~~~~~~~~~~~~~~~~~~~
    Raises :class:`ContaminationError` if ``window_start`` is earlier than
    the knowledge cutoff (HERMES_QUANT_KNOWLEDGE_CUTOFF, default 2025-01-01).
    This ensures we never evaluate over data the LLM might have memorised.

    Wave 6a integration
    ~~~~~~~~~~~~~~~~~~~
    The harness is designed as a thin wrapper over any strategy that exposes
    :class:`StrategyProtocol`.  The WalkForwardEngine from Wave 6a is the
    recommended substrate for real evaluations; pass a wrapped engine as the
    strategy.  For unit tests, pass a simple stub.

    Args:
        max_universe_size:  Cap on the number of tickers (default 5, per the
                            STOCKBENCH paper's 5-stock evaluation design).
        price_source:       Data provider implementing PriceSourceProtocol.
                            Defaults to the built-in synthetic GBM source.
        strict_contamination: When True (default) raise ContaminationError on
                              pre-cutoff windows.  When False, emit a warning
                              and set contamination_guard_fired=True.
    """

    MAX_UNIVERSE_SIZE = 5
    DEFAULT_UNIVERSE = list(_DEFAULT_UNIVERSE)

    def __init__(
        self,
        *,
        max_universe_size: int = 5,
        price_source: PriceSourceProtocol | None = None,
        strict_contamination: bool = True,
    ) -> None:
        self.max_universe_size = max_universe_size
        self._price_source: PriceSourceProtocol = (
            price_source if price_source is not None else _SyntheticPriceSource()
        )
        self.strict_contamination = strict_contamination

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(
        self,
        strategy: StrategyProtocol,
        universe: list[str] | None = None,
        window_start: date | None = None,
        window_end: date | None = None,
        *,
        benchmark: str = "SPY",
    ) -> STOCKBENCHResult:
        """Run the evaluation.

        Args:
            strategy:     Strategy to evaluate.
            universe:     List of tickers.  Defaults to 5-stock standard set.
                          Capped at *max_universe_size* (excess tickers silently
                          truncated with a warning).
            window_start: First day of the evaluation window (inclusive).
                          Must be ≥ HERMES_QUANT_KNOWLEDGE_CUTOFF.
            window_end:   Last day of the evaluation window (inclusive).
            benchmark:    Benchmark ticker for buy-and-hold comparison.

        Returns:
            STOCKBENCHResult with all metrics populated.

        Raises:
            ContaminationError: When strict_contamination=True and window_start
                                is earlier than the knowledge cutoff.
        """
        universe = list(universe or self.DEFAULT_UNIVERSE)
        if len(universe) > self.max_universe_size:
            warnings.warn(
                f"STOCKBENCHHarness: universe capped at {self.max_universe_size} "
                f"tickers (was {len(universe)})",
                stacklevel=2,
            )
            universe = universe[: self.max_universe_size]

        # Defaults for window
        if window_end is None:
            window_end = date.today()
        if window_start is None:
            window_start = window_end - timedelta(days=60)

        # --- Contamination guard ---
        # Cross-model review (MoA C1): use `<=` not `<` for symmetry with the
        # Oracle Fallacy guard (`tau_observable < asof` strict-exclusion).
        # A model with knowledge cutoff = 2025-01-01 has indexed anything
        # PUBLISHED on 2025-01-01, so window_start == cutoff is contaminated.
        cutoff = _get_knowledge_cutoff()
        contamination_guard_fired = False
        if window_start <= cutoff:
            contamination_guard_fired = True
            msg = (
                f"STOCKBENCHHarness: window_start {window_start} is earlier than "
                f"the knowledge cutoff {cutoff}.  This evaluation window may be "
                f"contaminated by LLM parametric knowledge (F1 — Contaminated "
                f"Backtesting).  Set HERMES_QUANT_KNOWLEDGE_CUTOFF env var to "
                f"override (current effective cutoff: {cutoff})."
            )
            if self.strict_contamination:
                raise ContaminationError(msg)
            else:
                warnings.warn(msg, stacklevel=2)

        n_days = (window_end - window_start).days + 1

        # --- Per-ticker simulation ---
        ticker_results: dict[str, dict] = {}
        strategy_portfolio_values: list[np.ndarray] = []
        buyhold_portfolio_values: list[np.ndarray] = []
        total_decisions = 0

        buyhold_strategy = _BuyAndHoldStrategy()

        for ticker in universe:
            prices = self._price_source.get_prices(ticker, window_start, window_end)
            n = len(prices)
            if n < 2:
                logger.warning(
                    "STOCKBENCHHarness: ticker %s has <2 price points, skipping", ticker
                )
                continue

            strategy_equity, n_dec = self._simulate_ticker(
                strategy, ticker, prices, window_start
            )
            buyhold_equity, _ = self._simulate_ticker(
                buyhold_strategy, ticker, prices, window_start
            )

            strategy_portfolio_values.append(strategy_equity)
            buyhold_portfolio_values.append(buyhold_equity)
            total_decisions += n_dec

            # Per-ticker alpha
            strat_ret = (strategy_equity[-1] - strategy_equity[0]) / strategy_equity[0]
            bh_ret = (buyhold_equity[-1] - buyhold_equity[0]) / buyhold_equity[0]
            ticker_results[ticker] = {
                "strategy_return": float(strat_ret),
                "buyhold_return": float(bh_ret),
                "alpha": float(strat_ret - bh_ret),
                "n_decisions": n_dec,
            }

        if not strategy_portfolio_values:
            # Degenerate: no valid tickers
            return STOCKBENCHResult(
                universe=universe,
                window_start=window_start,
                window_end=window_end,
                benchmark=benchmark,
                cumulative_return=0.0,
                max_drawdown=0.0,
                sortino=float("nan"),
                n_decisions=0,
                decisions_per_day_avg=0.0,
                vs_buyhold_alpha=0.0,
                contamination_guard_fired=contamination_guard_fired,
                metadata={"error": "no_valid_tickers"},
            )

        # --- Aggregate across tickers (equal-weight portfolio) ---
        min_len = min(v.shape[0] for v in strategy_portfolio_values)
        strat_portfolio = np.mean(
            np.stack([v[:min_len] for v in strategy_portfolio_values], axis=0), axis=0
        )
        bh_portfolio = np.mean(
            np.stack([v[:min_len] for v in buyhold_portfolio_values], axis=0), axis=0
        )

        strat_daily_rets = np.diff(strat_portfolio) / strat_portfolio[:-1]
        bh_daily_rets = np.diff(bh_portfolio) / bh_portfolio[:-1]

        strat_cum_return = float((strat_portfolio[-1] / strat_portfolio[0]) - 1.0)
        bh_cum_return = float((bh_portfolio[-1] / bh_portfolio[0]) - 1.0)

        max_dd = _compute_max_drawdown(strat_portfolio)
        sortino = _compute_sortino(strat_daily_rets)
        vs_buyhold_alpha = strat_cum_return - bh_cum_return

        decisions_per_day = total_decisions / max(n_days, 1)

        return STOCKBENCHResult(
            universe=universe,
            window_start=window_start,
            window_end=window_end,
            benchmark=benchmark,
            cumulative_return=strat_cum_return,
            max_drawdown=max_dd,
            sortino=sortino,
            n_decisions=total_decisions,
            decisions_per_day_avg=decisions_per_day,
            vs_buyhold_alpha=vs_buyhold_alpha,
            contamination_guard_fired=contamination_guard_fired,
            metadata={
                "per_ticker": ticker_results,
                "buyhold_cumulative_return": bh_cum_return,
                "knowledge_cutoff": cutoff.isoformat(),
            },
        )

    # ------------------------------------------------------------------
    # Internal per-ticker simulation
    # ------------------------------------------------------------------

    def _simulate_ticker(
        self,
        strategy: StrategyProtocol,
        ticker: str,
        prices: np.ndarray,
        window_start: date,
    ) -> tuple[np.ndarray, int]:
        """Simulate *strategy* on a single ticker price series.

        Returns:
            (equity_curve, n_decisions) where equity_curve[0] = 100.
        """
        n = len(prices)
        equity = np.empty(n, dtype=float)
        equity[0] = 100.0
        prev_position = 0.0
        n_decisions = 0

        for i in range(1, n):
            as_of = window_start + timedelta(days=i - 1)
            # Provide history up to (not including) current bar
            history = prices[:i]
            try:
                new_position = float(
                    strategy.decide(ticker, as_of, history)
                )
                # Clamp to [-1, +1]
                new_position = max(-1.0, min(1.0, new_position))
            except Exception:  # noqa: BLE001
                logger.exception(
                    "STOCKBENCHHarness: strategy.decide raised for %s on %s; "
                    "defaulting to 0 (flat)",
                    ticker,
                    as_of,
                )
                new_position = 0.0

            if abs(new_position - prev_position) > 1e-6:
                n_decisions += 1

            # Daily return contribution: position × price return
            daily_ret = (prices[i] - prices[i - 1]) / prices[i - 1]
            equity[i] = equity[i - 1] * (1.0 + new_position * daily_ret)
            prev_position = new_position

        return equity, n_decisions
