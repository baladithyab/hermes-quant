"""hermes_quant.analysts.classical_ta — RSI + MACD + Bollinger + MA-cross.

A composite classical-TA analyst. Computes 4 sub-signals and combines them
via simple voting; the composite direction is the sign of the unweighted
sum, magnitude is the mean expected return, raw confidence is the
agreement fraction.

Per ADR-0002 + ADR-0009 §P0-2:
- confidence is calibrated; until N>=200 fitted samples exist, use
  ColdStartCalibrator (max(0, raw - 0.20)).
- confidence_raw is preserved on AnalystView for calibrator training.

The sub-signals are intentionally simple — this analyst is the v0.1
anchor that gives us a working signal *before* Kronos is wired (v0.1.2).

Indicators:
- RSI(14): >70 short, <30 long, else flat. Magnitude = abs(RSI-50)/100 ~= 0.2-0.5%.
- MACD(12,26,9): histogram cross. Histogram > 0 long, < 0 short.
- Bollinger(20, 2σ): close < lower long; close > upper short. Mag from band width.
- SMA(20)/SMA(50) cross: SMA20 > SMA50 long, else short. Mag from spread.

Calibration: each sub-signal individually emits a raw confidence in [0,1];
the composite raw is the agreement fraction (4 sub-signals: 4/4 = 1.0,
3/4 = 0.75, 2/4 = 0.5, etc.). Disagreement (no plurality direction) → flat.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from hermes_quant.calibrators import ColdStartCalibrator
from hermes_quant.protocol import (
    AnalystView,
    CalibratorNotReady,
    Direction,
    MarketContext,
    RealizedOutcome,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Indicator math (pure functions; no state)
# ---------------------------------------------------------------------------

def rsi(close: pd.Series, period: int = 14) -> float:
    """Wilder's RSI, last value.

    Edge case: if there's no movement at all (avg_gain==avg_loss==0), RSI
    is undefined; we return 50.0 (neutral). If only avg_loss is zero (all
    upticks), return 100.0 (max bullish).
    """
    if len(close) < period + 1:
        return float("nan")
    delta = close.diff().dropna()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    if avg_loss == 0:
        if avg_gain == 0:
            return 50.0  # no movement at all → neutral
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def macd_histogram(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> float:
    """MACD histogram = (EMA_fast - EMA_slow) - signal_EMA."""
    if len(close) < slow + signal:
        return float("nan")
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return float((macd_line - signal_line).iloc[-1])


def bollinger_bands(
    close: pd.Series, period: int = 20, num_std: float = 2.0
) -> tuple[float, float, float]:
    """Returns (upper, mid, lower) at last bar."""
    if len(close) < period:
        return float("nan"), float("nan"), float("nan")
    mid = close.rolling(period).mean().iloc[-1]
    std = close.rolling(period).std().iloc[-1]
    return float(mid + num_std * std), float(mid), float(mid - num_std * std)


def sma_cross_signal(
    close: pd.Series, fast_p: int = 20, slow_p: int = 50
) -> tuple[Direction, float]:
    """SMA20 vs SMA50 cross. Returns (direction, magnitude_fraction)."""
    if len(close) < slow_p:
        return 0, 0.0
    sma_fast = close.rolling(fast_p).mean().iloc[-1]
    sma_slow = close.rolling(slow_p).mean().iloc[-1]
    last = close.iloc[-1]
    if last <= 0 or np.isnan(sma_fast) or np.isnan(sma_slow):
        return 0, 0.0
    spread = (sma_fast - sma_slow) / last
    direction: Direction = 1 if spread > 0 else (-1 if spread < 0 else 0)
    # Magnitude: clamp spread magnitude to [0.001, 0.05]
    mag = float(np.clip(abs(spread), 0.001, 0.05))
    return direction, mag


# ---------------------------------------------------------------------------
# ClassicalTAAnalyst
# ---------------------------------------------------------------------------

@dataclass
class _SubSignal:
    direction: Direction
    magnitude: float
    raw_confidence: float
    label: str


class ClassicalTAAnalyst:
    """Composite RSI + MACD + Bollinger + MA-cross analyst.

    Per ADR-0002 + ADR-0009 §P0-2: emits AnalystView with calibrated confidence
    + confidence_raw for calibrator training.

    Discoverable via [project.entry-points."hermes_quant.analysts"] = "classical_ta".
    """

    name = "classical-ta"
    timeframes = ["15m", "30m", "1h", "4h", "1d"]
    asset_classes = ["crypto", "equity", "etf", "fx"]
    enabled = True

    # Sub-signal weights — uniform for v0.1.1
    SUB_SIGNAL_LABELS = ("rsi", "macd", "bollinger", "ma_cross")

    def __init__(
        self,
        *,
        horizon: str = "4h",
        rsi_period: int = 14,
        rsi_long_threshold: float = 30.0,
        rsi_short_threshold: float = 70.0,
        bollinger_period: int = 20,
        bollinger_std: float = 2.0,
        sma_fast: int = 20,
        sma_slow: int = 50,
        min_history_bars: int = 60,
    ):
        self.horizon = horizon
        self.rsi_period = rsi_period
        self.rsi_long_threshold = rsi_long_threshold
        self.rsi_short_threshold = rsi_short_threshold
        self.bollinger_period = bollinger_period
        self.bollinger_std = bollinger_std
        self.sma_fast = sma_fast
        self.sma_slow = sma_slow
        self.min_history_bars = min_history_bars

        self.calibrator = ColdStartCalibrator()

        self._n_views_emitted = 0
        self._last_view_at: pd.Timestamp | None = None
        self._error_count = 0

    # ------------------- sub-signals -------------------

    def _rsi_signal(self, close: pd.Series) -> _SubSignal:
        r = rsi(close, period=self.rsi_period)
        if np.isnan(r):
            return _SubSignal(0, 0.0, 0.0, "rsi")
        if r < self.rsi_long_threshold:
            # Oversold → mean-reversion long
            mag = float(np.clip((self.rsi_long_threshold - r) / 100.0, 0.001, 0.05))
            conf = float(np.clip((self.rsi_long_threshold - r) / self.rsi_long_threshold, 0.1, 1.0))
            return _SubSignal(1, mag, conf, "rsi")
        if r > self.rsi_short_threshold:
            mag = float(np.clip((r - self.rsi_short_threshold) / 100.0, 0.001, 0.05))
            conf = float(np.clip((r - self.rsi_short_threshold) / (100 - self.rsi_short_threshold), 0.1, 1.0))
            return _SubSignal(-1, mag, conf, "rsi")
        return _SubSignal(0, 0.0, 0.0, "rsi")

    def _macd_signal(self, close: pd.Series) -> _SubSignal:
        h = macd_histogram(close)
        if np.isnan(h):
            return _SubSignal(0, 0.0, 0.0, "macd")
        last = close.iloc[-1]
        if last <= 0:
            return _SubSignal(0, 0.0, 0.0, "macd")
        # Magnitude relative to last close
        mag_frac = abs(h) / last
        mag = float(np.clip(mag_frac, 0.001, 0.05))
        if h > 0:
            return _SubSignal(1, mag, float(np.clip(mag_frac * 50, 0.1, 1.0)), "macd")
        if h < 0:
            return _SubSignal(-1, mag, float(np.clip(mag_frac * 50, 0.1, 1.0)), "macd")
        return _SubSignal(0, 0.0, 0.0, "macd")

    def _bollinger_signal(self, close: pd.Series) -> _SubSignal:
        upper, mid, lower = bollinger_bands(close, self.bollinger_period, self.bollinger_std)
        if np.isnan(upper):
            return _SubSignal(0, 0.0, 0.0, "bollinger")
        last = close.iloc[-1]
        band_width = (upper - lower)
        if band_width <= 0:
            return _SubSignal(0, 0.0, 0.0, "bollinger")
        # Position within bands (-1 = at lower, +1 = at upper)
        position = (2 * (last - mid) / band_width)
        if last < lower:
            mag = float(np.clip((lower - last) / last, 0.001, 0.05))
            return _SubSignal(1, mag, float(np.clip(abs(position) - 1, 0.1, 1.0)), "bollinger")
        if last > upper:
            mag = float(np.clip((last - upper) / last, 0.001, 0.05))
            return _SubSignal(-1, mag, float(np.clip(abs(position) - 1, 0.1, 1.0)), "bollinger")
        return _SubSignal(0, 0.0, 0.0, "bollinger")

    def _ma_cross_signal(self, close: pd.Series) -> _SubSignal:
        d, mag = sma_cross_signal(close, self.sma_fast, self.sma_slow)
        if d == 0:
            return _SubSignal(0, 0.0, 0.0, "ma_cross")
        # Confidence proportional to spread, capped
        conf = float(np.clip(mag * 20, 0.1, 1.0))
        return _SubSignal(d, mag, conf, "ma_cross")

    # ------------------- public API -------------------

    def analyze(self, ctx: MarketContext) -> AnalystView | None:
        """Per ADR-0002: returns None if insufficient context or flat composite."""
        try:
            if ctx.bars is None or len(ctx.bars) < self.min_history_bars:
                return None
            close = ctx.bars["close"]
            if len(close) < self.min_history_bars:
                return None

            sub_signals = [
                self._rsi_signal(close),
                self._macd_signal(close),
                self._bollinger_signal(close),
                self._ma_cross_signal(close),
            ]

            # Aggregate
            longs = [s for s in sub_signals if s.direction == 1]
            shorts = [s for s in sub_signals if s.direction == -1]
            n_total = len(sub_signals)

            if not longs and not shorts:
                return None  # all flat

            if len(longs) > len(shorts):
                composite_direction: Direction = 1
                contributing = longs
            elif len(shorts) > len(longs):
                composite_direction = -1
                contributing = shorts
            else:
                # Tie — silence (per silence-by-default principle)
                return None

            # Magnitude = mean of contributing magnitudes
            magnitude = float(np.mean([s.magnitude for s in contributing]))

            # Raw confidence = contributing fraction × mean sub-confidence
            agreement = len(contributing) / n_total
            mean_sub_conf = float(np.mean([s.raw_confidence for s in contributing]))
            confidence_raw = float(np.clip(agreement * mean_sub_conf, 0.0, 1.0))

            # Calibrate (cold-start: max(0, raw - 0.20))
            try:
                calibrated = self.calibrator.calibrate(confidence_raw)
            except CalibratorNotReady:
                calibrated = max(0.0, confidence_raw - 0.20)

            view = AnalystView(
                analyst=self.name,
                direction=composite_direction,
                magnitude=magnitude,
                confidence=calibrated,
                confidence_raw=confidence_raw,
                horizon=self.horizon,
                rationale=(
                    f"agreement={len(contributing)}/{n_total} "
                    f"({','.join(s.label for s in contributing)})"
                ),
                metadata={
                    "sub_signals": [
                        {"label": s.label, "direction": s.direction,
                         "magnitude": s.magnitude, "raw_conf": s.raw_confidence}
                        for s in sub_signals
                    ],
                },
            )
            self._n_views_emitted += 1
            self._last_view_at = ctx.asof
            return view
        except Exception as e:  # noqa: BLE001
            self._error_count += 1
            logger.exception("classical-ta analyze failed: %s", e)
            return None

    def update(self, outcome: RealizedOutcome) -> None:
        """Feed realized outcome to the calibrator's training buffer.

        v0.1.1 stores updates but doesn't refit — refit happens via a
        scheduled job (out of scope for v0.1.1; daemon settlement loop will
        accumulate and the calibrator switch to IsotonicCalibrator happens
        when n_samples >= 200).
        """
        # For v0.1.1, just track count
        self.calibrator.fit([outcome.view.confidence_raw], [outcome.direction_correct])

    def health(self) -> dict:
        return {
            "name": self.name,
            "n_views_emitted": self._n_views_emitted,
            "last_view_at": (
                self._last_view_at.isoformat() if self._last_view_at else None
            ),
            "error_count": self._error_count,
            "calibrator_status": self.calibrator.status(),
        }
