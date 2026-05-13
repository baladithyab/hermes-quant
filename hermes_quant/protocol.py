"""hermes_quant.protocol — Core type contracts for analysts, aggregators, and the risk gate.

Authoritative source for:
  - MarketContext (input to analysts)
  - AnalystView (output from analysts)
  - AggregatedSignal (output from aggregators, input to risk gate)
  - Action (output from risk gate, input to signal bus)
  - RealizedOutcome (per-analyst settlement input)
  - EpisodeOutcome (cross-sectional aggregator settlement input — ADR-0009 P1-10)
  - Analyst, Aggregator, RiskGate Protocols

Anchor ADRs: 0002 (analyst protocol), 0003 (aggregator), 0004 (risk gate),
              0009 (Phase-4 amendments).

Versioning rule: fields are added only, never renamed/removed before a major
version bump. New fields have sensible defaults. Consumers ignore unknown fields.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

import pandas as pd

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

Direction = Literal[-1, 0, 1]
"""-1 = short, 0 = flat, +1 = long."""

Timeframe = Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
"""Bar timeframe. Sub-minute timeframes deferred to v0.2."""

AssetClass = Literal["crypto", "equity", "etf", "fx", "option"]
"""Asset class. 'option' deferred to v0.2 (requires Greeks-aware sizer per ADR-0009 §P2-options)."""


# ---------------------------------------------------------------------------
# MarketContext — input to analysts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MarketContext:
    """Per-tick context handed to every analyst.

    The bars DataFrame is canonical OHLCV with timestamp as a COLUMN (not index)
    to avoid subtle indexing bugs across analysts that use .iloc.

    Required columns: ['timestamp', 'open', 'high', 'low', 'close', 'volume']
    Optional columns: ['amount']  # quote-currency volume; populated when source provides

    Validation: all bars are UTC, ascending, deduplicated. The data layer enforces
    this before MarketContext is constructed.
    """

    asset: str                       # e.g., "BTC/USDT", "AAPL"
    timeframe: str                   # one of Timeframe
    asset_class: str                 # one of AssetClass
    exchange: str | None             # for crypto: "binance", "kraken", ...; None for yfinance equity
    bars: pd.DataFrame               # canonical OHLCV
    last_close: float                # bars.iloc[-1]["close"] cached for convenience
    last_volume: float               # bars.iloc[-1]["volume"]
    asof: pd.Timestamp               # decision timestamp, UTC
    extras: Mapping[str, Any] = field(default_factory=dict)
    """Provider-specific extras (orderbook, news, regime, ...). Read-only Mapping
    (an immutable proxy view) — analysts must NOT mutate. Per ADR-0009 §P1 fix
    for the original ADR-0002 mutability concern."""


# ---------------------------------------------------------------------------
# AnalystView — output from analysts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AnalystView:
    """A single analyst's view at a decision timestamp.

    Per ADR-0002 + ADR-0009 §P0-2:
      - confidence is a CALIBRATED probability of directional correctness in [0, 1]
      - confidence_raw is the analyst's pre-calibration score (for debugging + calibrator training)
      - Until a fitted calibrator exists with N >= 200 samples, confidence = max(0, raw - 0.20)
    """

    analyst: str                     # name of the emitting analyst
    direction: Direction
    magnitude: float                 # expected return as fraction (e.g., 0.012 = 1.2%)
    confidence: float                # CALIBRATED probability in [0, 1]
    confidence_raw: float            # raw, uncalibrated score (for debugging + calibrator training)
    horizon: str                     # "5m" | "1h" | "1d" — over what window the view holds
    rationale: str | None = None     # optional human-readable explanation; truncated to 256 chars
    metadata: Mapping[str, Any] | None = None
    """Provider-specific extras (CIs, sub-scores, ...). JSON-serialized + capped at
    1024 chars when written to signal bus."""


# ---------------------------------------------------------------------------
# AggregatedSignal — output from aggregators, input to risk gate
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AggregatedSignal:
    """Aggregator's combined view, post-disagreement-aware sizing.

    Per ADR-0003 + ADR-0009 §P0-2:
      - confidence is a CALIBRATED probability (same isotonic-regression approach as analysts)
      - components is the tuple of contributing AnalystViews — required for stacking/RL training
        (per ADR-0009 §P1-10 EpisodeOutcome)
    """

    asset: str
    timeframe: str
    asset_class: str
    asof: pd.Timestamp
    direction: Direction
    magnitude: float
    confidence: float                # CALIBRATED
    confidence_raw: float
    horizon: str
    components: tuple[AnalystView, ...]   # frozen — required for joint-state replay
    aggregator: str                  # which aggregator emitted this ("bma", "stacking", "rl", ...)
    metadata: Mapping[str, Any] | None = None


# ---------------------------------------------------------------------------
# MarketState — input to risk gate (transaction costs, volatility, etc.)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MarketState:
    """Per-asset cost + risk environment at decision time.

    All values are rolling estimates from real fills (after 30 days of operation)
    or conservative bootstrap defaults (per ADR-0009 §P1-12).
    """

    asset: str
    asof: pd.Timestamp
    volatility: float                # per-period stdev of LOG returns (NOT variance)
    """For Kelly sizing: f* = μ * p / σ²  (per ADR-0009 §P0-1 fix)."""
    commission: float                # fraction (e.g., 0.001 = 10 bps round-trip)
    spread: float                    # fraction (round-trip, e.g., 0.0008 = 8 bps)
    slippage_estimate: float         # fraction; defaults: crypto=12 bps, equity=5 bps, illiquid=25 bps
    funding_cost: float = 0.0        # per-period; perps only
    borrow_cost: float = 0.0         # per-period; shorts only
    tz: str = "UTC"                  # for daily-loss session reset


# ---------------------------------------------------------------------------
# Portfolio — REAL portfolio state, sourced from executions.jsonl
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Position:
    """An open position with mark-to-market accounting."""
    asset: str
    qty: float                       # signed; positive = long, negative = short
    avg_entry_price: float
    mark_price: float
    unrealized_pnl: float
    realized_fees: float


@dataclass(frozen=True)
class Portfolio:
    """Per-(account, asset_class) portfolio partition.

    Per ADR-0009 §P0-3 + §P1-9:
      - State is sourced from executions.jsonl (broker reality), NOT from internal P&L log
      - Partitioned per (account, asset_class) — drawdown halts scope to the partition
      - All P&L metrics are mark-to-market (include unrealized)
    """

    account_id: str                  # e.g., "alpaca-paper", "binance-spot"
    asset_class: str
    asof: pd.Timestamp
    positions: Mapping[str, Position]
    cash: float
    equity_total: float              # cash + sum(positions.mark_value)
    realized_pnl_total: float
    realized_fees_total: float
    peak_equity: float               # rolling peak for drawdown calc
    daily_open_equity: float         # set at session open

    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity_total) / self.peak_equity)

    @property
    def daily_loss_pct(self) -> float:
        if self.daily_open_equity <= 0:
            return 0.0
        return max(0.0, (self.daily_open_equity - self.equity_total) / self.daily_open_equity)

    def current_position_pct(self, asset: str) -> float:
        """Position size as fraction of total equity. 0 if no position."""
        pos = self.positions.get(asset)
        if pos is None or self.equity_total <= 0:
            return 0.0
        return (pos.qty * pos.mark_price) / self.equity_total

    def is_halted(self, halt_state: HaltState, asset: str | None = None) -> bool:
        """True if (account, asset_class, asset?) is in halt."""
        return halt_state.is_halted(self.account_id, self.asset_class, asset)


# ---------------------------------------------------------------------------
# HaltState — durable halt registry (ADR-0009 §P0-4)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HaltRecord:
    account_id: str
    asset_class: str
    asset: str | None                # None = all assets in class
    reason: str
    halted_at: pd.Timestamp
    halted_until: pd.Timestamp | None  # None = until explicit resume
    halt_epoch: int                   # monotonic per (account, asset_class, asset)


@runtime_checkable
class HaltState(Protocol):
    """Read-only access to the halt registry. Backed by SQLite.

    Halts are NEVER cleared by trading signals (ADR-0009 §P0-4). They're cleared
    only by:
      - `hermes quant resume` CLI command (with confirmation)
      - `halted_until` timestamp passing (auto-clear, daily-loss breakers only)
    """

    def is_halted(self, account_id: str, asset_class: str, asset: str | None = None) -> bool: ...
    def active_halts(self) -> list[HaltRecord]: ...


# ---------------------------------------------------------------------------
# Action — output from risk gate
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Action:
    """The risk gate's emit. None means silence (do nothing)."""

    target_position_pct: float       # signed; e.g., 0.10 = 10% NAV long, -0.05 = 5% NAV short
    reason: str                      # human-readable justification
    signal_id: str | None = None     # links to AggregatedSignal that drove this
    halt: bool = False               # if True, also enter halt for halt_scope
    halt_scope: tuple[str, str, str | None] | None = None  # (account, asset_class, asset?)
    halt_until: pd.Timestamp | None = None  # for daily-loss auto-clear


# ---------------------------------------------------------------------------
# Settlement — outcomes for analyst.update() and aggregator.update()
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RealizedOutcome:
    """Per-analyst settlement input. Each AnalystView gets one of these."""

    view: AnalystView
    asof_view: pd.Timestamp
    asof_settlement: pd.Timestamp
    realized_return: float            # actual return over view.horizon
    direction_correct: bool


@dataclass(frozen=True)
class EpisodeOutcome:
    """Cross-sectional settlement input for aggregators (ADR-0009 §P1-10).

    Required for stacking/RL — the aggregator must see what ALL analysts said
    AT THE SAME TIMESTAMP to learn correlations. RealizedOutcome's per-analyst
    slice is insufficient for joint learning.
    """

    asset: str
    timeframe: str
    asof: pd.Timestamp
    aggregated_signal: AggregatedSignal              # contains components: tuple[AnalystView, ...]
    realized_returns: Mapping[str, float]             # horizon -> return: {"5m": 0.003, "1h": 0.012}
    direction_correct: Mapping[str, bool]
    realized_net_pnl: float | None = None
    """Actual after-fee P&L from executions.jsonl (ADR-0009 §P0-3).
    None if the signal didn't fill (cost gate, halt, etc.)."""


# ---------------------------------------------------------------------------
# Calibrator (per-analyst and per-aggregator)
# ---------------------------------------------------------------------------

class Calibrator(Protocol):
    """Maps raw confidence scores to calibrated probabilities (ADR-0009 §P0-2).

    Implementations:
      - IsotonicCalibrator (production) — sklearn isotonic regression
      - IdentityCalibrator (testing) — passthrough
      - ColdStartCalibrator (cold-start fallback) — max(0, raw - 0.20)
    """

    n_samples: int
    is_calibrated: bool

    def calibrate(self, raw_score: float) -> float: ...
    def fit(self, raw_scores, direction_correct) -> None: ...
    def status(self) -> dict: ...


# ---------------------------------------------------------------------------
# Analyst, Aggregator, RiskGate Protocols
# ---------------------------------------------------------------------------

@runtime_checkable
class Analyst(Protocol):
    """An analyst module emits AnalystViews from MarketContexts.

    Discovery: registered via [project.entry-points."hermes_quant.analysts"].
    """

    name: str
    timeframes: list[str]
    asset_classes: list[str]
    enabled: bool

    def analyze(self, ctx: MarketContext) -> AnalystView | None: ...
    """Returns None if no view (out-of-scope, insufficient context, etc.)."""

    def health(self) -> dict: ...
    """Surfaces in `quant_doctor`. Must include {n_views_emitted, last_view_at,
    error_count, calibrator_status}."""


@runtime_checkable
class StatefulAnalyst(Analyst, Protocol):
    """Analysts that learn from realized outcomes implement update()."""
    def update(self, outcome: RealizedOutcome) -> None: ...


@runtime_checkable
class Aggregator(Protocol):
    """An aggregator combines AnalystViews into an AggregatedSignal.

    Discovery: registered via [project.entry-points."hermes_quant.aggregators"].
    """

    name: str

    def aggregate(self, views: list[AnalystView],
                  context: MarketContext) -> AggregatedSignal: ...

    def update(self, outcome: EpisodeOutcome) -> None: ...
    """Settlement loop calls this per cross-sectional episode."""


@runtime_checkable
class RiskGate(Protocol):
    """Deterministic risk gate. Final boundary before signal emission.

    Per ADR-0004 + ADR-0009 §P0-5:
      - Circuit breakers FIRST (before signal-flatness check)
      - Halts FIRST (before any other rule)
      - Discrete action steps (anti-leverage-gambling)
      - Hard rules — aggregator (RL or otherwise) cannot bypass
    """

    def gate(self, signal: AggregatedSignal,
             market: MarketState,
             portfolio: Portfolio,
             halt_state: HaltState) -> Action | None: ...
    """Returns None for silence, Action for decision."""


# ---------------------------------------------------------------------------
# DataProvider Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class DataProvider(Protocol):
    """Per ADR-0005. Discovered via [project.entry-points."hermes_quant.data_providers"]."""

    name: str
    asset_classes: list[str]
    timeframes: list[str]
    requires_credentials: bool

    def fetch_bars(self, asset: str, timeframe: str,
                   start: pd.Timestamp, end: pd.Timestamp,
                   *, use_cache: bool = True) -> pd.DataFrame: ...

    def fetch_latest(self, asset: str, timeframe: str,
                     lookback: int = 500) -> pd.DataFrame: ...

    def health(self) -> dict: ...


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class HermesQuantError(Exception):
    """Base for all hermes-quant errors."""


class DataProviderError(HermesQuantError):
    """Transient data provider failure (network, rate limit, etc.). Retry-safe."""


class RateLimitError(DataProviderError):
    """Provider hit rate limit. Back off + fall back to next provider in chain."""


class DataQualityError(HermesQuantError):
    """Bars failed validation gates. Don't retry — log + skip tick."""


class SignalTooLarge(HermesQuantError):
    """Signal record exceeds 4096-byte atomic-write limit. Cap components or rationale."""


class DaemonAlreadyRunning(HermesQuantError):
    """Singleton lock is held by a live PID. Stop the running daemon first."""


class CalibratorNotReady(HermesQuantError):
    """Calibrator hasn't accumulated enough samples. Use cold-start shrinkage."""
