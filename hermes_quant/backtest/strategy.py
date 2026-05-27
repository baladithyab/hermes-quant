"""hermes_quant.backtest.strategy — Strategy protocol + concrete strategies (Wave 6a / ADR-0045).

Strategy protocol
-----------------
A Strategy is any callable with signature::

    def decide(
        asof: pd.Timestamp,
        lookback_data: pd.DataFrame,
    ) -> list[Decision]

``lookback_data`` is a DataFrame indexed by date with columns
[open, high, low, close, volume] and is NEVER allowed to contain entries
after ``asof``.  The WalkForwardEngine enforces this contract (see
walk_forward.py).

Concrete strategies
-------------------
- **HermesQuantStrategy**: wires the existing advisor (classical_ta) →
  TraderNode → RiskCommittee chain.  Uses StubLLMCommittee when
  dry_run_llm=True so no API calls are made.
- **BuyAndHoldStrategy**: always-long baseline.  Allocates 100 % to the
  first symbol on the first bar; holds forever.  The canonical benchmark
  for alpha measurement.

Decision dataclass
------------------
Returned by every strategy.  The WalkForwardEngine reads these to execute
fills via CostModel.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import pandas as pd

from hermes_quant.backtest.stub_llm import StubLLMCommittee
from hermes_quant.agents.trader import TraderNode, TraderAction
from hermes_quant.agents.risk_committee.committee import RiskCommittee

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Decision dataclass
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    """A single trade instruction produced by a strategy.

    Attributes
    ----------
    symbol:
        Ticker symbol.
    action:
        "BUY", "SELL", or "HOLD".
    size_fraction:
        Fraction of available NAV to allocate (0–1).  For HOLD this should
        be 0.0.
    confidence:
        Signal confidence (0–1).
    rationale:
        Human-readable rationale (≤2048 chars).
    metadata:
        Arbitrary strategy-level context passed through to the journal.
    """

    symbol: str
    action: str  # "BUY" | "SELL" | "HOLD"
    size_fraction: float
    confidence: float
    rationale: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_active(self) -> bool:
        """Return True if this decision results in a fill (not HOLD)."""
        return self.action != "HOLD" and self.size_fraction > 0.0


# ---------------------------------------------------------------------------
# Strategy protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Strategy(Protocol):
    """Protocol for all backtest strategies.

    Every concrete strategy must implement ``decide(asof, lookback_data)``.
    The WalkForwardEngine guarantees that ``lookback_data.index`` contains
    no dates after ``asof``.
    """

    def decide(
        self,
        asof: pd.Timestamp,
        lookback_data: pd.DataFrame,
    ) -> list[Decision]:
        """Produce trade decisions as of *asof* using only *lookback_data*.

        Parameters
        ----------
        asof:
            The current simulation date.  The strategy MUST NOT access any
            data after this date — doing so is a lookahead bug (F1 in
            arxiv:2605.19337).  The engine pre-filters lookback_data but this
            contract is the strategy's responsibility too.
        lookback_data:
            OHLCV DataFrame (DatetimeIndex, columns=[open,high,low,close,volume])
            containing only bars up to and including asof.

        Returns
        -------
        list[Decision]
            Zero or more trade decisions.  An empty list means "do nothing."
        """
        ...


# ---------------------------------------------------------------------------
# HermesQuantStrategy
# ---------------------------------------------------------------------------


class HermesQuantStrategy:
    """Backtestable wrapper around the production advisor → trader → risk pipeline.

    This strategy wires together:
    1. A lightweight signal generator (momentum-based direction from returns)
       standing in for the full multi-analyst advisor during dry-run.
    2. TraderNode (deterministic v0.1) to convert the signal to a TraderProposal.
    3. RiskCommittee (deterministic v0.1) to apply the silence-vote gate.

    When ``dry_run_llm=True`` (the default) all LLM calls are replaced by
    StubLLMCommittee — no API keys required, outputs are deterministic.

    Parameters
    ----------
    universe:
        List of ticker symbols this strategy operates on.
    dry_run_llm:
        When True (default), use StubLLMCommittee instead of real LLM calls.
    lookback_bars:
        Number of bars used to compute the momentum signal (default 20).
    """

    def __init__(
        self,
        universe: list[str],
        *,
        dry_run_llm: bool = True,
        lookback_bars: int = 20,
    ) -> None:
        self._universe = universe
        self._dry_run_llm = dry_run_llm
        self._lookback_bars = lookback_bars
        self._stub = StubLLMCommittee()
        self._trader_node = TraderNode()
        self._risk_committee = RiskCommittee(
            llm_caller=self._stub if dry_run_llm else None,
        )

    # ------------------------------------------------------------------

    def decide(
        self,
        asof: pd.Timestamp,
        lookback_data: pd.DataFrame,
    ) -> list[Decision]:
        """Produce decisions for every symbol in the universe.

        Signal: sign of the rolling N-bar return on the close column.
        Confidence: |return| normalised to [0, 1] via tanh(5 × |ret|).
        """
        decisions: list[Decision] = []

        # lookback_data may be multi-symbol (MultiIndex) or single-symbol
        for symbol in self._universe:
            try:
                sym_data = self._extract_symbol_data(lookback_data, symbol)
                if sym_data is None or len(sym_data) < 2:
                    decisions.append(self._hold(symbol, "insufficient data"))
                    continue

                close_col = sym_data["close"]
                closes = (close_col.dropna() if isinstance(close_col, pd.Series) else close_col.iloc[:, 0].dropna())
                if len(closes) < 2:
                    decisions.append(self._hold(symbol, "insufficient close data"))
                    continue

                decision = self._signal_to_decision(symbol, closes, sym_data)
                decisions.append(decision)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "HermesQuantStrategy.decide failed for %s at %s: %s",
                    symbol, asof, exc,
                )
                decisions.append(self._hold(symbol, f"error: {exc}"))

        return decisions

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _extract_symbol_data(
        self, lookback_data: pd.DataFrame, symbol: str
    ) -> pd.DataFrame | None:
        """Extract single-symbol OHLCV slice from a potentially multi-symbol DataFrame."""
        if isinstance(lookback_data.columns, pd.MultiIndex):
            # Multi-symbol: top level = column name, second level = symbol
            try:
                cols = {col: lookback_data[(col, symbol)] for col in ["open", "high", "low", "close", "volume"] if (col, symbol) in lookback_data.columns}
                if not cols:
                    return None
                return pd.DataFrame(cols, index=lookback_data.index)
            except KeyError:
                return None
        else:
            # Single-symbol: use as-is (symbol is informational)
            return lookback_data

    def _signal_to_decision(
        self,
        symbol: str,
        closes: pd.Series,
        sym_data: pd.DataFrame,
    ) -> Decision:
        """Convert a close series to a Decision via the production pipeline."""
        import math as _math

        # Momentum signal: N-bar return
        n = min(self._lookback_bars, len(closes) - 1)
        ret = float(closes.iloc[-1] / closes.iloc[-n] - 1.0) if n > 0 else 0.0

        direction = 1 if ret > 0 else (-1 if ret < 0 else 0)
        raw_conf = _math.tanh(5.0 * abs(ret))  # squash to (0, 1)
        confidence = max(0.1, min(0.9, raw_conf))

        # StubLLM → research plan
        plan = self._stub.research_plan(
            direction=direction,
            confidence=confidence,
            symbol=symbol,
        )

        # Advisor signal dict (minimal — TraderNode gracefully handles missing fields)
        last_close = float(closes.iloc[-1])
        advisor_signal: dict = {
            "direction": direction,
            "confidence": confidence,
            "magnitude": abs(ret),
            "metadata": {
                "last_close": last_close,
                "atr_relative": 0.02,  # synthetic default ~2 % ATR
            },
            "data_quality": {"last_close": last_close},
        }

        # TraderNode
        trader_proposal = self._trader_node(plan, advisor_signal)

        # RiskCommittee
        risk_summary = self._risk_committee.debate(trader_proposal, plan)

        # Compute effective size after risk
        effective_size = trader_proposal.size_fraction * risk_summary.silence_multiplier

        return Decision(
            symbol=symbol,
            action=trader_proposal.action.value,
            size_fraction=effective_size,
            confidence=trader_proposal.confidence,
            rationale=trader_proposal.rationale[:512],
            metadata={
                "direction": direction,
                "ret": ret,
                "silence_multiplier": risk_summary.silence_multiplier,
                "n_risk_rounds": risk_summary.n_rounds,
            },
        )

    @staticmethod
    def _hold(symbol: str, reason: str) -> Decision:
        return Decision(
            symbol=symbol,
            action="HOLD",
            size_fraction=0.0,
            confidence=0.0,
            rationale=f"HOLD: {reason}",
        )


# ---------------------------------------------------------------------------
# BuyAndHoldStrategy
# ---------------------------------------------------------------------------


class BuyAndHoldStrategy:
    """Always-long baseline strategy.

    Allocates an equal share of NAV to every symbol in the universe on the
    first call and holds forever.  Subsequent calls return HOLD.

    This is the canonical buy-and-hold benchmark used to measure alpha.
    A walk-forward result where the strategy cannot beat this benchmark
    net of costs fails the charter gate (ADR-0045).

    Parameters
    ----------
    universe:
        List of ticker symbols to hold.
    allocation_per_symbol:
        Fraction of NAV per symbol.  Defaults to 1/len(universe) (equal weight).
    """

    def __init__(
        self,
        universe: list[str],
        *,
        allocation_per_symbol: float | None = None,
    ) -> None:
        self._universe = universe
        n = len(universe) if universe else 1
        self._alloc = allocation_per_symbol if allocation_per_symbol is not None else 1.0 / n
        self._entered = False

    def decide(
        self,
        asof: pd.Timestamp,  # noqa: ARG002
        lookback_data: pd.DataFrame,  # noqa: ARG002
    ) -> list[Decision]:
        """Buy equal weight on first call; HOLD thereafter."""
        if not self._entered:
            self._entered = True
            return [
                Decision(
                    symbol=sym,
                    action="BUY",
                    size_fraction=self._alloc,
                    confidence=1.0,
                    rationale="BuyAndHoldStrategy: initial entry — holds forever.",
                )
                for sym in self._universe
            ]
        return [
            Decision(
                symbol=sym,
                action="HOLD",
                size_fraction=0.0,
                confidence=1.0,
                rationale="BuyAndHoldStrategy: holding.",
            )
            for sym in self._universe
        ]
