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
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import pandas as pd

from hermes_quant.backtest.stub_llm import StubLLMCommittee
from hermes_quant.agents.trader import TraderNode, TraderAction
from hermes_quant.agents.risk_committee.committee import RiskCommittee

if TYPE_CHECKING:
    from hermes_quant.protocol import MarketContext

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


# ---------------------------------------------------------------------------
# AdvisorStrategy (D3 — makes the analyst-pool / BMA flags measurable)
# ---------------------------------------------------------------------------


class AdvisorStrategy:
    """Backtestable strategy that drives the REAL analyst-pool -> BMA -> risk-gate chain.

    Why this exists
    ---------------
    ``HermesQuantStrategy`` is a momentum stand-in: it never runs the analyst
    pool or the BMA aggregator, so ablating the analyst-pool / aggregator flags
    (the L2 learning-loop cluster — ``HERMES_QUANT_STACKING`` /
    ``L2_POSTERIOR_DECAY`` / ``L2_PER_ANALYST_CALIB`` / ``L2_LESSON_HAIRCUT`` /
    ``L2_POSTERIOR_PERSIST``, all in ``aggregators/bma.py``) through it would show
    a FALSE NULL — the flags can't change a decision the path never makes.

    ``AdvisorStrategy`` closes that gap. On each ``decide(asof, lookback)`` it:
      1. Builds a ``MarketContext`` from ``lookback`` (which the engine has
         already filtered to dates <= asof — no-lookahead).
      2. Runs every injected analyst -> collects ``AnalystView`` s.
      3. Aggregates them through a PERSISTENT ``BMAAggregator`` (reused across
         days so per-analyst Beta posteriors accumulate — the state the
         accumulation-biting L2 flags read).
      4. Gates the signal through the deterministic ``DefaultRiskGate`` against a
         synthetic flat portfolio + bootstrap market state (same posture as
         ``advisor.recommend`` — the size is informational, not a live order).
      5. Emits a ``Decision``.

    Settlement loop (the asof-honest part)
    --------------------------------------
    The ``WalkForwardEngine`` has no settlement hook, but the accumulation-biting
    L2 flags only bite once the BMA has accrued per-analyst skill via
    ``update(EpisodeOutcome)``. So this strategy keeps its OWN settlement loop:
    each day, BEFORE deciding, it settles any pending decision whose outcome is
    now observable — ``observable_asof = decision_asof + horizon_delta`` — using
    ONLY ``lookback`` close prices (all <= asof by the engine's contract). The
    realized direction-correctness feeds ``aggregator.update(...)``. This mirrors
    the live settlement loop's c96e ``observable_asof`` discipline EXACTLY, so the
    flags accrue the same skill they would in production — never peeking past
    asof. ``learn_from_fills=False`` disables the loop (then only the cold-start-
    biting flags ``L2_PER_ANALYST_CALIB`` / ``L2_LESSON_HAIRCUT`` move output).

    Offline + deterministic
    -----------------------
    No LLM, no network: analysts are injected (default = the canonical offline
    committee), the aggregator/gate are deterministic, and there is no RNG. Same
    inputs + same flag -> same decisions.

    Parameters
    ----------
    universe:
        Ticker symbols to decide on (single-symbol is the common case).
    analysts:
        Injected analyst list. When None, the canonical offline committee
        (ClassicalTA + MicrostructureLite + Kronos, each degrading gracefully if
        its optional dep is missing) is built via the advisor's loadout helper.
    aggregator:
        Injected aggregator. When None, a HERMETIC ``BMAAggregator`` is built
        (see "Hermetic by default" below). The SAME instance is reused for the
        whole run so posteriors accumulate.
    risk_gate:
        Injected risk gate. When None, a fresh ``DefaultRiskGate`` is built.
    calibrator:
        Injected calibrator for the DEFAULT aggregator. When None, an
        ``IdentityCalibrator`` (deterministic passthrough) is pinned. Ignored
        when ``aggregator`` is supplied (the caller owns that aggregator's
        calibrator). See "Hermetic by default".
    posterior_store_path:
        Path for the DEFAULT aggregator's L2 posterior persistence. When None, a
        per-instance temp file is used so an ``L2_POSTERIOR_PERSIST`` ablation can
        NEVER write the real production store. Ignored when ``aggregator`` is
        supplied.
    asset_class / timeframe:
        Context fields for the analysts (default equity / 1d).
    learn_from_fills:
        When True (default), run the internal asof-honest settlement loop so the
        accumulation-biting L2 flags accrue skill. When False, skip it.
    min_history_bars:
        Minimum lookback rows before the strategy will emit a non-HOLD decision
        (analyst warm-up). Default 30.

    Hermetic by default (money-software / repro discipline)
    -------------------------------------------------------
    When ``aggregator`` is None, the default ``BMAAggregator`` is built HERMETIC
    so an ablation is reproducible and READ-ONLY regardless of the host machine:

      * **Pinned calibrator.** A stock ``BMAAggregator()`` loads
        ``~/.hermes/quant/calibrators/isotonic.pkl`` if present — so its output
        would depend on whatever private, fitted calibrator the host happens to
        have. That makes an ablation NON-reproducible across machines (and on a
        clean box the cold-start fallback caps confidence at 0.375, silencing
        every signal -> a FALSE NULL for every flag). We pin an
        ``IdentityCalibrator`` (deterministic passthrough): the eval measures the
        FLAG's effect, not the host's calibrator. Inject your own ``calibrator``
        (or a fully-built ``aggregator``) to evaluate against a specific one.
      * **Sandboxed posterior store.** A stock ``BMAAggregator()`` persists L2
        posteriors to the canonical production path when
        ``HERMES_QUANT_L2_POSTERIOR_PERSIST=1`` — so ablating THAT flag through a
        default aggregator would WRITE production state from a "read-only" eval.
        We point the store at a per-instance temp file so settlement can never
        touch the real store.
    """

    def __init__(
        self,
        universe: list[str],
        *,
        analysts: list[Any] | None = None,
        aggregator: Any = None,
        risk_gate: Any = None,
        calibrator: Any = None,
        posterior_store_path: Any = None,
        asset_class: str = "equity",
        timeframe: str = "1d",
        learn_from_fills: bool = True,
        min_history_bars: int = 30,
    ) -> None:
        self._universe = universe
        self._asset_class = asset_class
        self._timeframe = timeframe
        self._learn_from_fills = learn_from_fills
        self._min_history_bars = min_history_bars
        # Owns a temp dir ONLY when we built the default aggregator; cleaned up by
        # GC of the TemporaryDirectory handle stored on self.
        self._owned_tmpdir = None

        if analysts is None:
            from hermes_quant.advisor import _build_default_analysts

            analysts = _build_default_analysts()
        self._analysts = analysts

        if aggregator is None:
            aggregator = self._build_hermetic_aggregator(
                calibrator=calibrator, posterior_store_path=posterior_store_path
            )
        self._aggregator = aggregator

        if risk_gate is None:
            from hermes_quant.risk.gate import DefaultRiskGate

            risk_gate = DefaultRiskGate()
        self._risk_gate = risk_gate

        # Pending decisions awaiting settlement, per symbol:
        #   list of (decision_asof, observable_asof, direction, signal).
        self._pending: dict[str, list[tuple]] = {sym: [] for sym in universe}

    def _build_hermetic_aggregator(self, *, calibrator: Any, posterior_store_path: Any):
        """Build a reproducible, READ-ONLY default ``BMAAggregator`` for ablation.

        See the class docstring's "Hermetic by default" section for WHY. Pins a
        deterministic calibrator (default ``IdentityCalibrator``) and a sandboxed
        posterior-store path (default a per-instance temp file) so the eval is
        machine-independent and can never write the production posterior store.
        """
        import tempfile
        from pathlib import Path

        from hermes_quant.aggregators.bma import BMAAggregator
        from hermes_quant.calibrators import IdentityCalibrator

        if posterior_store_path is None:
            self._owned_tmpdir = tempfile.TemporaryDirectory(prefix="hq-ablation-posteriors-")
            posterior_store_path = Path(self._owned_tmpdir.name) / "posteriors.json"

        agg = BMAAggregator(posterior_store_path=posterior_store_path)
        agg.calibrator = calibrator if calibrator is not None else IdentityCalibrator()
        return agg

    # ------------------------------------------------------------------

    def decide(
        self,
        asof: pd.Timestamp,
        lookback_data: pd.DataFrame,
    ) -> list[Decision]:
        """Run the real advisor chain for every symbol; emit one Decision each."""
        # Settlement FIRST (asof-honest): credit any pending decision now
        # observable, so the BMA posteriors are up to date before we decide.
        if self._learn_from_fills:
            self._settle_due(asof, lookback_data)

        decisions: list[Decision] = []
        for symbol in self._universe:
            try:
                decisions.append(self._decide_one(symbol, asof, lookback_data))
            except Exception as exc:  # noqa: BLE001 — one bad symbol can't kill the run
                logger.warning(
                    "AdvisorStrategy.decide failed for %s at %s: %s", symbol, asof, exc
                )
                decisions.append(self._hold(symbol, f"error: {exc}"))
        return decisions

    # ------------------------------------------------------------------
    # Per-symbol decision
    # ------------------------------------------------------------------

    def _decide_one(
        self, symbol: str, asof: pd.Timestamp, lookback_data: pd.DataFrame
    ) -> Decision:
        from hermes_quant.protocol import AnalystView

        sym_bars = self._symbol_bars(lookback_data, symbol)
        if sym_bars is None or len(sym_bars) < self._min_history_bars:
            return self._hold(symbol, "insufficient history for advisor warmup")

        ctx = self._build_context(symbol, asof, sym_bars)

        views: list[AnalystView] = []
        for analyst in self._analysts:
            try:
                if hasattr(analyst, "analyze"):
                    view = analyst.analyze(ctx)
                elif hasattr(analyst, "observe"):
                    view = analyst.observe(ctx)
                else:
                    continue
            except Exception as exc:  # noqa: BLE001 — one bad analyst can't kill the fan-out
                logger.warning("AdvisorStrategy: analyst %s raised: %s", analyst, exc)
                continue
            if view is not None:
                views.append(view)

        if not views:
            return self._hold(symbol, "no analyst views")

        signal = self._aggregator.aggregate(views, ctx)
        if signal.direction == 0 or signal.confidence <= 0.0:
            self._record_pending(symbol, asof, signal)
            return self._hold(symbol, "aggregator silent/flat")

        action = self._gate(symbol, signal, asof)
        self._record_pending(symbol, asof, signal)

        if action is None or action.target_position_pct == 0.0:
            return self._hold(symbol, "risk gate silenced")

        target = action.target_position_pct
        return Decision(
            symbol=symbol,
            action="BUY" if target > 0 else "SELL",
            size_fraction=abs(target),
            confidence=float(signal.confidence),
            rationale=(action.reason or "advisor")[:512],
            metadata={
                "direction": int(signal.direction),
                "agg_confidence": float(signal.confidence),
                "agg_confidence_raw": float(signal.confidence_raw),
                "target_position_pct": float(target),
                "n_views": signal.metadata.get("n_views") if signal.metadata else None,
            },
        )

    # ------------------------------------------------------------------
    # Settlement loop (asof-honest)
    # ------------------------------------------------------------------

    def _settle_due(self, asof: pd.Timestamp, lookback_data: pd.DataFrame) -> None:
        """Settle pending decisions whose outcome is observable by ``asof``.

        For each pending (decision_asof, observable_asof, direction, signal) with
        observable_asof <= asof, compute whether the realized close move from the
        decision bar to the observable bar agreed with the decision direction —
        using ONLY ``lookback_data`` (all dates <= asof, by the engine's
        contract — never a peek). Feed the result to ``aggregator.update`` so the
        per-analyst Beta posteriors (and the B50 / decay rings) accumulate.
        """
        from hermes_quant.protocol import EpisodeOutcome

        for symbol in self._universe:
            sym_bars = self._symbol_bars(lookback_data, symbol)
            still_pending: list[tuple] = []
            for decision_asof, observable_asof, direction, signal in self._pending.get(
                symbol, []
            ):
                if observable_asof > asof:
                    still_pending.append(
                        (decision_asof, observable_asof, direction, signal)
                    )
                    continue
                realized = self._realized_return(sym_bars, decision_asof, observable_asof)
                if realized is None:
                    # Outcome bar not present in lookback yet — keep waiting.
                    still_pending.append(
                        (decision_asof, observable_asof, direction, signal)
                    )
                    continue
                direction_correct = {
                    v.analyst: (
                        (realized > 0 and v.direction > 0)
                        or (realized < 0 and v.direction < 0)
                    )
                    for v in signal.components
                }
                horizon = signal.horizon if signal.horizon not in ("0m", "") else self._timeframe
                try:
                    self._aggregator.update(
                        EpisodeOutcome(
                            asset=symbol,
                            timeframe=self._timeframe,
                            asof=pd.Timestamp(decision_asof),
                            aggregated_signal=signal,
                            realized_returns={horizon: float(realized)},
                            direction_correct=direction_correct,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 — settlement must not abort the run
                    logger.warning("AdvisorStrategy: aggregator.update raised: %s", exc)
            self._pending[symbol] = still_pending

    def _record_pending(self, symbol: str, asof: pd.Timestamp, signal) -> None:
        """Queue a decision for later asof-honest settlement."""
        if not self._learn_from_fills:
            return
        if not signal.components:
            return  # nothing to credit
        horizon = signal.horizon if signal.horizon not in ("0m", "") else self._timeframe
        observable = pd.Timestamp(asof) + _horizon_to_timedelta(horizon)
        self._pending.setdefault(symbol, []).append(
            (pd.Timestamp(asof), observable, int(signal.direction), signal)
        )

    @staticmethod
    def _realized_return(
        sym_bars: pd.DataFrame | None,
        decision_asof: pd.Timestamp,
        observable_asof: pd.Timestamp,
    ) -> float | None:
        """Close-to-close return from the decision bar to the first bar at/after
        ``observable_asof``. Returns None when the outcome bar isn't in the frame.

        Reads ONLY bars in ``sym_bars`` (dates <= asof by contract), so it can
        never look ahead of the engine's current step.
        """
        if sym_bars is None or len(sym_bars) < 2:
            return None
        idx = sym_bars.index
        dec = pd.Timestamp(decision_asof)
        obs = pd.Timestamp(observable_asof)
        if getattr(idx, "tz", None) is not None:
            if dec.tzinfo is None:
                dec = dec.tz_localize(idx.tz)
            if obs.tzinfo is None:
                obs = obs.tz_localize(idx.tz)
        dec_pos = idx.searchsorted(dec, side="right") - 1
        if dec_pos < 0:
            return None
        obs_pos = idx.searchsorted(obs, side="left")
        if obs_pos >= len(idx):
            return None  # outcome bar not observable yet
        try:
            dec_px = float(sym_bars["close"].iloc[dec_pos])
            obs_px = float(sym_bars["close"].iloc[obs_pos])
        except (KeyError, IndexError, TypeError):
            return None
        if dec_px <= 0:
            return None
        return obs_px / dec_px - 1.0

    # ------------------------------------------------------------------
    # Context + gate helpers
    # ------------------------------------------------------------------

    def _build_context(
        self, symbol: str, asof: pd.Timestamp, sym_bars: pd.DataFrame
    ) -> MarketContext:
        from hermes_quant.protocol import MarketContext

        asof_ts = pd.Timestamp(asof)
        if asof_ts.tzinfo is None:
            asof_ts = asof_ts.tz_localize("UTC")
        return MarketContext(
            asset=symbol,
            timeframe=self._timeframe,
            asset_class=self._asset_class,
            exchange=None,
            bars=sym_bars,
            last_close=float(sym_bars["close"].iloc[-1]),
            last_volume=float(sym_bars["volume"].iloc[-1]),
            asof=asof_ts,
        )

    def _gate(self, symbol: str, signal, asof: pd.Timestamp):
        from hermes_quant.advisor import _bootstrap_market_state, _synthetic_portfolio

        asof_ts = pd.Timestamp(asof)
        if asof_ts.tzinfo is None:
            asof_ts = asof_ts.tz_localize("UTC")
        market = _bootstrap_market_state(symbol, self._asset_class, asof_ts)
        portfolio = _synthetic_portfolio(symbol, self._asset_class, asof_ts)
        try:
            return self._risk_gate.gate(signal, market, portfolio, _NoHaltState())
        except Exception as exc:  # noqa: BLE001
            logger.warning("AdvisorStrategy: risk gate raised: %s", exc)
            return None

    def _symbol_bars(
        self, lookback_data: pd.DataFrame, symbol: str
    ) -> pd.DataFrame | None:
        """Project the engine's lookback frame into a canonical OHLCV frame with a
        ``timestamp`` COLUMN (the analyst/advisor contract), indexed for the
        settlement search.

        The engine passes a DatetimeIndex'd frame; analysts expect a
        ``timestamp`` column. Multi-symbol (MultiIndex columns) is sliced to the
        symbol's OHLCV.
        """
        if isinstance(lookback_data.columns, pd.MultiIndex):
            try:
                cols = {
                    c: lookback_data[(c, symbol)]
                    for c in ["open", "high", "low", "close", "volume"]
                    if (c, symbol) in lookback_data.columns
                }
            except KeyError:
                return None
            if not cols:
                return None
            df = pd.DataFrame(cols, index=lookback_data.index)
        else:
            needed = ["open", "high", "low", "close", "volume"]
            if not all(c in lookback_data.columns for c in needed):
                return None
            df = lookback_data[needed].copy()
        df = df.dropna(subset=["close"])
        if len(df) == 0:
            return None
        df = df.copy()
        df["timestamp"] = df.index
        return df

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
# AdvisorStrategy helpers
# ---------------------------------------------------------------------------


# Horizon -> observability delay. Mirrors aggregators/bma.py::_HORIZON_TO_TIMEDELTA
# so the settlement loop's observable_asof matches the BMA's c96e stamp exactly.
# Unknown horizons fall back to 1 day (a POSITIVE delay — never makes a sample
# observable before its decision, which would be lookahead).
_HORIZON_TIMEDELTA: dict[str, pd.Timedelta] = {
    "5m": pd.Timedelta(minutes=5),
    "15m": pd.Timedelta(minutes=15),
    "30m": pd.Timedelta(minutes=30),
    "1h": pd.Timedelta(hours=1),
    "4h": pd.Timedelta(hours=4),
    "1d": pd.Timedelta(days=1),
    "1w": pd.Timedelta(weeks=1),
    "1M": pd.Timedelta(days=30),
    "1Q": pd.Timedelta(days=90),
}


def _horizon_to_timedelta(horizon: str) -> pd.Timedelta:
    return _HORIZON_TIMEDELTA.get(horizon, pd.Timedelta(days=1))


class _NoHaltState:
    """No-op halt state — backtests have no real halt registry (mirrors the
    advisor's _EmptyHaltState). Returns "never halted" so the gate evaluates the
    signal on its own merits."""

    def is_halted(self, account_id: str, asset_class: str, asset: str | None = None) -> bool:
        return False

    def active_halts(self) -> list:
        return []
