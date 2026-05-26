"""hermes_quant.analysts.microstructure — Microstructure-lite analyst.

Per the founding charter (docs/charter/2026-05-13-hermes-quant-charter.md
§"Layer 1 Analyst Pool"): "microstructure — order book imbalance, queue
position, trade flow toxicity (VPIN)". Order-book features require an L2
data feed which v0.1.2's yfinance/ccxt-spot providers don't expose. So
this analyst implements the SUBSET we can derive from OHLCV alone:

1. **Bollinger %B** — where in the band the close sits, normalized to [0,1].
   Below 0 = breaking down out of band, above 1 = breaking up. We treat
   sustained band-walks (close > 95% for N consecutive bars) as a momentum
   short signal (mean-reversion), and below 5% for N as a long signal.

2. **ATR-relative volatility regime** — ATR(14) / close gives a normalized
   volatility. A spike in ATR with no directional follow-through is a
   trade-flow-toxicity proxy (real microstructure VPIN computes this from
   tick imbalance; we approximate from bar imbalance: |close - open| / range).

3. **Trend quality (Wilder's ADX-lite)** — ratio of |close[N] - close[0]|
   to sum of bar ranges over N. High ratio = clean directional move
   (worth following); low ratio = chop (silence per the charter's
   "rewarded for correct inaction" principle).

The composite signal is a vote: Bollinger + ATR-volatility + ADX-quality
each emit a sub-signal; the composite direction is the SIGN of the unweighted
sum. Magnitude is the mean expected return across votes. Raw confidence is
the agreement fraction (3/3=1.0, 2/3=0.67, 1/3=0.33). Calibration via
ColdStartCalibrator (cold-start shrinkage of 0.20).

Per ADR-0002 + ADR-0009 §P0-2: confidence_raw is preserved on AnalystView
for calibrator training.

This is the SECOND VOICE for BMA aggregation. Two analysts is the minimum
that makes BMA do anything; with one analyst the aggregator is degenerate
(single-vote = single-output). Per the charter, the three-analyst MVP
(ClassicalTA + microstructure + Kronos) is the empirical test of whether
the ensemble pattern adds value at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from hermes_quant.calibrators import ColdStartCalibrator
from hermes_quant.protocol import (
    AnalystView,
    Direction,
    MarketContext,
    RealizedOutcome,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Indicator math (pure functions; no state)
# ---------------------------------------------------------------------------


def percent_b(close: pd.Series, period: int = 20, n_std: float = 2.0) -> float:
    """Bollinger %B = (close - lower) / (upper - lower).

    Returns NaN if insufficient data. <0 = below lower band (oversold),
    >1 = above upper band (overbought).
    """
    if len(close) < period:
        return float("nan")
    sma = close.rolling(period).mean().iloc[-1]
    std = close.rolling(period).std().iloc[-1]
    if std == 0 or np.isnan(std):
        return 0.5  # no volatility -> middle of band
    upper = sma + n_std * std
    lower = sma - n_std * std
    if upper == lower:
        return 0.5
    return float((close.iloc[-1] - lower) / (upper - lower))


def atr_relative(bars: pd.DataFrame, period: int = 14) -> float:
    """ATR(period) / last_close — normalized volatility. NaN if insufficient.

    Higher = more volatile relative to price. ATR uses true range:
    max(high-low, |high-prev_close|, |low-prev_close|).
    """
    if len(bars) < period + 1:
        return float("nan")
    high = bars["high"]
    low = bars["low"]
    close = bars["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    last_close = close.iloc[-1]
    if last_close == 0 or np.isnan(last_close):
        return float("nan")
    return float(atr / last_close)


def trend_quality(close: pd.Series, period: int = 14) -> float:
    """Wilder's-ADX-lite: |close[N] - close[0]| / sum(|close[i] - close[i-1]|).

    Returns 0..1. 1.0 = perfectly directional (every bar moves the same way),
    near 0 = pure chop (random walk equally up and down). NaN if insufficient.
    """
    if len(close) < period + 1:
        return float("nan")
    window = close.iloc[-(period + 1) :]
    net_move = abs(window.iloc[-1] - window.iloc[0])
    bar_moves = window.diff().abs().sum()
    if bar_moves == 0:
        return 0.0
    return float(min(1.0, net_move / bar_moves))


def directional_bar_imbalance(bars: pd.DataFrame, period: int = 20) -> float:
    """Order-flow toxicity proxy: rolling mean of (close-open)/range.

    Per VPIN literature: sustained one-sided imbalance suggests informed
    flow. Range [-1, 1]; near 0 = balanced (low toxicity); +0.5 = consistent
    bullish bars; -0.5 = consistent bearish bars.
    """
    if len(bars) < period:
        return 0.0
    rng = bars["high"] - bars["low"]
    rng_safe = rng.where(rng > 0, 1.0)  # avoid div by zero
    bar_imbalance = (bars["close"] - bars["open"]) / rng_safe
    return float(bar_imbalance.rolling(period).mean().iloc[-1])


# ---------------------------------------------------------------------------
# Sub-signal helpers
# ---------------------------------------------------------------------------


@dataclass
class _SubSignal:
    """Per-rule emission. direction in {-1, 0, +1}, magnitude as fraction,
    raw_confidence in [0, 1]."""

    direction: int
    magnitude: float
    raw_confidence: float
    rule: str


# ---------------------------------------------------------------------------
# MicrostructureLite analyst
# ---------------------------------------------------------------------------


class MicrostructureLite:
    """Microstructure-lite analyst. Per the founding charter §"Layer 1".

    Emits an AnalystView combining three OHLCV-derivable microstructure
    features. Discoverable via `[project.entry-points."hermes_quant.analysts"]`.

    Args:
        horizon: forecast horizon string (e.g. "4h"). Must be one of the
            Timeframe literals from protocol.py.
        bb_period: Bollinger lookback (default 20).
        bb_std: Bollinger band width in stdevs (default 2.0).
        atr_period: ATR lookback (default 14).
        adx_period: trend-quality lookback (default 14).
        toxicity_period: order-flow imbalance lookback (default 20).
        min_history_bars: minimum bars required before emitting; below this
            we return None (per Protocol contract).
    """

    name = "microstructure_lite"
    timeframes = ["5m", "15m", "30m", "1h", "4h", "1d"]
    asset_classes = ["crypto", "equity", "etf", "fx"]

    def __init__(
        self,
        *,
        horizon: str = "4h",
        bb_period: int = 20,
        bb_std: float = 2.0,
        atr_period: int = 14,
        adx_period: int = 14,
        toxicity_period: int = 20,
        min_history_bars: int = 30,
    ):
        self.horizon = horizon
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.atr_period = atr_period
        self.adx_period = adx_period
        self.toxicity_period = toxicity_period
        self.min_history_bars = min_history_bars

        self.calibrator = ColdStartCalibrator()

        self._n_views_emitted = 0
        self._last_view_at: pd.Timestamp | None = None
        self._error_count = 0

    # ------------------- sub-signals -------------------

    def _bollinger_signal(self, close: pd.Series) -> _SubSignal:
        """Bollinger %B mean-reversion: <0.05 long, >0.95 short."""
        b = percent_b(close, period=self.bb_period, n_std=self.bb_std)
        if np.isnan(b):
            return _SubSignal(0, 0.0, 0.0, "bollinger")
        if b < 0.05:
            # Far below band → mean-reversion long
            mag = float(np.clip((0.05 - b) * 0.05, 0.001, 0.03))
            return _SubSignal(1, mag, min(1.0, (0.05 - b) * 5), "bollinger")
        if b > 0.95:
            # Far above band → mean-reversion short
            mag = float(np.clip((b - 0.95) * 0.05, 0.001, 0.03))
            return _SubSignal(-1, mag, min(1.0, (b - 0.95) * 5), "bollinger")
        return _SubSignal(0, 0.0, 0.0, "bollinger")

    def _trend_quality_signal(self, close: pd.Series, bar_imbalance: float) -> _SubSignal:
        """High trend quality + bar imbalance directional → follow it."""
        q = trend_quality(close, period=self.adx_period)
        if np.isnan(q):
            return _SubSignal(0, 0.0, 0.0, "trend_quality")
        # Need quality > 0.6 for confidence; below that, silence
        if q < 0.6:
            return _SubSignal(0, 0.0, 0.0, "trend_quality")
        # Direction follows bar_imbalance sign
        if abs(bar_imbalance) < 0.1:
            # Quality is good but no consistent flow direction — chop on a
            # nominally trending series. Silence.
            return _SubSignal(0, 0.0, 0.0, "trend_quality")
        direction = 1 if bar_imbalance > 0 else -1
        mag = float(np.clip(abs(bar_imbalance) * q * 0.05, 0.001, 0.03))
        conf = float(min(1.0, q * abs(bar_imbalance) * 2))
        return _SubSignal(direction, mag, conf, "trend_quality")

    def _toxicity_signal(self, bars: pd.DataFrame, atr_rel: float) -> _SubSignal:
        """Order-flow toxicity: high ATR + persistent bar imbalance.

        Real VPIN measures buy-vs-sell volume imbalance from tick data.
        OHLCV approximation: persistent (close > open) bars in a high-vol
        regime suggest informed buying flow → follow the direction. The
        signal is quiet when ATR is low (no informed activity to track).
        """
        if np.isnan(atr_rel):
            return _SubSignal(0, 0.0, 0.0, "toxicity")
        # Need elevated vol regime — historical comparison would be better
        # but we don't have a long-term ATR baseline at this stage.
        # Use absolute threshold: <0.5% intraday ATR = quiet, gate.
        if atr_rel < 0.005:
            return _SubSignal(0, 0.0, 0.0, "toxicity")
        imbalance = directional_bar_imbalance(bars, period=self.toxicity_period)
        if abs(imbalance) < 0.15:
            # Volatile but no consistent direction = pure noise; silence
            return _SubSignal(0, 0.0, 0.0, "toxicity")
        direction = 1 if imbalance > 0 else -1
        mag = float(np.clip(atr_rel * abs(imbalance) * 2, 0.001, 0.04))
        conf = float(min(1.0, abs(imbalance) * 3))
        return _SubSignal(direction, mag, conf, "toxicity")

    # ------------------- main emission -------------------

    def analyze(self, ctx: MarketContext) -> AnalystView | None:
        """Per ADR-0002. Returns None if insufficient history or all
        sub-signals silent (per the charter's silence-by-default principle).
        """
        bars = ctx.bars
        if len(bars) < self.min_history_bars:
            return None

        try:
            close = bars["close"]
            imbalance = directional_bar_imbalance(bars, period=self.toxicity_period)
            atr_rel = atr_relative(bars, period=self.atr_period)

            sub_signals = [
                self._bollinger_signal(close),
                self._trend_quality_signal(close, imbalance),
                self._toxicity_signal(bars, atr_rel),
            ]
        except Exception:  # noqa: BLE001 — defensive
            self._error_count += 1
            logger.warning(
                "microstructure_lite: analysis failed for %s",
                ctx.asset,
                exc_info=True,
            )
            return None

        active = [s for s in sub_signals if s.direction != 0]
        if not active:
            # Silence — charter's "rewarded for correct inaction"
            return None

        # Composite: sign of unweighted sum
        net_score = sum(s.direction * s.raw_confidence for s in active)
        if abs(net_score) < 1e-6:
            # Disagreement (active signals canceling) → flat
            return None
        composite_direction: Direction = 1 if net_score > 0 else -1

        # Composite magnitude: weighted average of magnitude across active
        weight_sum = sum(s.raw_confidence for s in active) or 1.0
        composite_magnitude = sum(s.magnitude * s.raw_confidence for s in active) / weight_sum

        # Raw confidence: agreement fraction
        agreeing = sum(1 for s in active if s.direction == composite_direction)
        raw_confidence = agreeing / len(sub_signals)  # full N=3 denominator

        # Calibrate
        try:
            calibrated = self.calibrator.calibrate(raw_confidence)
        except Exception:  # noqa: BLE001
            calibrated = max(0.0, raw_confidence - 0.20)

        view = AnalystView(
            analyst=self.name,
            direction=composite_direction,
            magnitude=composite_magnitude,
            confidence=calibrated,
            confidence_raw=raw_confidence,
            horizon=self.horizon,
            rationale=self._render_rationale(active),
            metadata={
                "bollinger_pct_b": _safe(percent_b(close, self.bb_period, self.bb_std)),
                "atr_relative": _safe(atr_rel),
                "trend_quality": _safe(trend_quality(close, self.adx_period)),
                "bar_imbalance": _safe(imbalance),
                "active_subsignals": [s.rule for s in active],
                "n_active_subsignals": len(active),
            },
        )

        self._n_views_emitted += 1
        self._last_view_at = ctx.asof
        return view

    def update(self, outcome: RealizedOutcome) -> None:
        """Per ADR-0002. Calibrator update happens at settlement loop.
        Stub for v0.1.2 — calibrator.fit happens elsewhere when sample
        threshold reached.
        """
        # No-op for v0.1.2; the settlement loop calls calibrator.fit
        # from its own context with the joined entry+exit window.
        pass

    def health(self) -> dict:
        return {
            "name": self.name,
            "n_views_emitted": self._n_views_emitted,
            "last_view_at": (str(self._last_view_at) if self._last_view_at else None),
            "error_count": self._error_count,
            "calibrated": self.calibrator.is_calibrated,
            "n_calibration_samples": self.calibrator.n_samples,
        }

    @staticmethod
    def _render_rationale(active: list[_SubSignal]) -> str:
        """Human-readable rule summary; capped at 256 chars per Protocol."""
        parts = [f"{s.rule}={s.direction:+d}@{s.raw_confidence:.2f}" for s in active]
        return f"[microstructure] {', '.join(parts)}"[:256]


def _safe(x: float) -> float | None:
    """JSON-friendly NaN handling — return None instead of float('nan')."""
    if np.isnan(x) if isinstance(x, float) else False:
        return None
    try:
        if np.isnan(x):
            return None
    except (TypeError, ValueError):
        pass
    return float(x)
