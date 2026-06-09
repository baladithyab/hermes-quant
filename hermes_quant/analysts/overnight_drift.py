"""hermes_quant.analysts.overnight_drift — OvernightDriftAnalyst (ADR-0089).

A ZERO-TURNOVER conviction modulator on hold-through-close daily positions.

Grounded in docs/research/2026-06-08-r-overnight-drift-anomaly.md + the 2026-06-08
spike: the overnight (close[t-1]->open[t]) vs intraday (open[t]->close[t]) return
split is real and persistent, but the cross-sectional long-short form is destroyed
by cost. The ONLY net-harvestable form is structural — an interday system that
holds through the close earns the overnight drift for free.

So this analyst does NOT propose a round-trip sleeve. It measures each name's
TRAILING ROLLING overnight-minus-intraday spread and emits an AnalystView that
NUDGES the daily long thesis: a name that systematically earns its return overnight
is a better hold-through-close candidate (flattening into the close would forfeit
the premium); an intraday-driven name gets no positive nudge. It enters BMA as a
PEER view — never an override; subject to the same dissent-aware capping as every
analyst. Zero added turnover by construction (it modulates conviction on holds; it
never asks to trade the open/close round-trip).

ADR-0089 invariants enforced here:
  * Asof-honest, no new feed: overnight/intraday are computed ONLY from ctx.bars
    open+close columns (all <= asof by the engine's no-lookahead contract). The
    trailing window EXCLUDES the still-forming current bar's forward information —
    it uses completed (close[t-1]->open[t]->close[t]) pairs only.
  * Adaptive, not assumed: the spike showed the per-name tilt is regime/period-
    dependent (intraday dominated high-beta names in 2023-24, the OPPOSITE of the
    longer-horizon meme-cohort tilt). So this uses a TRAILING ROLLING spread
    recomputed per name, never a static "meme = overnight" cohort assumption.
  * Default-OFF + eval-gated: the analyst only joins the loadout behind
    HERMES_QUANT_OVERNIGHT_DRIFT=1 (wired in advisor._build_default_analysts). With
    the flag absent the analyst is never constructed and behavior is byte-identical.

Calibration mirrors ClassicalTA (ADR-0002 + ADR-0009 §P0-2): confidence_raw is the
analyst's pre-calibration score; confidence is calibrated via ColdStartCalibrator
(max(0, raw - 0.20)) until a fitted calibrator exists.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from hermes_quant.calibrators import ColdStartCalibrator
from hermes_quant.protocol import AnalystView, MarketContext

logger = logging.getLogger(__name__)


class OvernightDriftAnalyst:
    """Trailing overnight-minus-intraday spread → a hold-through-close conviction nudge.

    Discoverable via the analysts loadout when HERMES_QUANT_OVERNIGHT_DRIFT=1.

    Parameters
    ----------
    horizon:
        View horizon. Default "1d" — this is a daily hold-through-close modulator.
    lookback_window:
        Number of trailing COMPLETED daily bars used to estimate the rolling
        overnight-minus-intraday spread. Default 60 (~3 trading months) — long
        enough to be stable, short enough to adapt to regime (ADR-0089 D-4).
    min_history_bars:
        Minimum bars before emitting a non-abstain view. Default 61 (need
        lookback_window+1 closes to form lookback_window overnight/intraday pairs).
    spread_to_conf_scale:
        Maps the annualized spread to a raw confidence via tanh. Default 8.0 — a
        ~12.5% annualized spread saturates toward conf~0.85. Tuned so a modest but
        real overnight tilt produces a usable, non-degenerate confidence.
    min_abs_spread:
        Annualized |spread| below this is treated as no-signal (flat/abstain) —
        anti-noise floor so a name with no real tilt does not vote. Default 0.02
        (2% annualized).
    long_only_nudge:
        When True (default, per ADR-0089), the analyst only emits a LONG nudge for
        positive-overnight-tilt names (the harvestable, hold-through-close case). A
        negative tilt yields ABSTAIN, not a short — the research supports the
        long/hold premium, not a tradeable short of intraday-driven names.
    """

    name = "overnight-drift"
    timeframes = ["1d"]  # daily hold-through-close modulator only
    asset_classes = ["equity", "etf"]  # cash equities/ETFs; crypto trades 24/7 (no overnight gap)
    enabled = True

    def __init__(
        self,
        *,
        horizon: str = "1d",
        lookback_window: int = 60,
        min_history_bars: int = 61,
        spread_to_conf_scale: float = 8.0,
        min_abs_spread: float = 0.02,
        long_only_nudge: bool = True,
    ) -> None:
        self.horizon = horizon
        self.lookback_window = int(lookback_window)
        self.min_history_bars = int(min_history_bars)
        self.spread_to_conf_scale = float(spread_to_conf_scale)
        self.min_abs_spread = float(min_abs_spread)
        self.long_only_nudge = bool(long_only_nudge)

        self.calibrator = ColdStartCalibrator()
        self._n_views_emitted = 0
        self._last_view_at: pd.Timestamp | None = None
        self._error_count = 0

    # ------------------------------------------------------------------

    def analyze(self, ctx: MarketContext) -> AnalystView | None:
        """Emit a hold-through-close conviction nudge from the trailing spread.

        Returns None (abstain) when: insufficient history, missing open/close,
        non-finite computation, or |spread| below the anti-noise floor (and, in
        long-only mode, when the tilt is negative).
        """
        try:
            bars = getattr(ctx, "bars", None)
            if bars is None or len(bars) < self.min_history_bars:
                return None
            if "open" not in bars.columns or "close" not in bars.columns:
                return None

            # SCOPE GUARD (ADR-0089): this analyst is a DAILY EQUITY/ETF
            # hold-through-close modulator. recommend_multi_horizon() runs every
            # analyst for each requested horizon (default ('1d','1w')) and
            # injected providers can drive other asset classes — so we MUST
            # abstain outside our declared scope rather than treating, e.g., a
            # weekly-resampled open/close as an "overnight" spread (which it is
            # not). The class-level timeframes/asset_classes are advertised
            # metadata; this enforces them at the analyze() seam.
            tf = getattr(ctx, "timeframe", None)
            ac = getattr(ctx, "asset_class", None)
            if tf is not None and tf not in self.timeframes:
                return None
            if ac is not None and ac not in self.asset_classes:
                return None

            opens = bars["open"].to_numpy(dtype=float)
            closes = bars["close"].to_numpy(dtype=float)

            # ASOF-HONEST: use only COMPLETED bars. Pair bar t's open with bar
            # t-1's close (overnight) and bar t's close (intraday). All indices are
            # <= asof (the engine filters bars to asof). We DROP the current
            # still-forming bar's contribution by computing on closed pairs only —
            # the last usable pair ends at the last fully-observed close.
            n = len(closes)
            # overnight[t] = open[t]/close[t-1] - 1 ; intraday[t] = close[t]/open[t] - 1
            # both defined for t in 1..n-1
            prev_close = closes[:-1]
            cur_open = opens[1:]
            cur_close = closes[1:]

            # Guard against zero/negative/NaN prices (corrupt bar).
            valid = (
                np.isfinite(prev_close)
                & np.isfinite(cur_open)
                & np.isfinite(cur_close)
                & (prev_close > 0)
                & (cur_open > 0)
            )
            overnight = np.where(valid, cur_open / np.where(prev_close > 0, prev_close, np.nan) - 1.0, np.nan)
            intraday = np.where(valid, cur_close / np.where(cur_open > 0, cur_open, np.nan) - 1.0, np.nan)

            # Trailing window: the last `lookback_window` valid pairs.
            on_tail = overnight[-self.lookback_window :]
            id_tail = intraday[-self.lookback_window :]
            on_tail = on_tail[np.isfinite(on_tail)]
            id_tail = id_tail[np.isfinite(id_tail)]
            if len(on_tail) < max(20, self.lookback_window // 2) or len(id_tail) < max(20, self.lookback_window // 2):
                return None  # not enough clean pairs to estimate a stable spread

            ann = 252.0
            on_ann = float(np.mean(on_tail) * ann)
            id_ann = float(np.mean(id_tail) * ann)
            spread = on_ann - id_ann  # the conviction-modulator signal
            if not np.isfinite(spread):
                return None

            # Anti-noise floor: a name with no real tilt does not vote.
            if abs(spread) < self.min_abs_spread:
                return None

            # Direction: positive overnight tilt -> LONG nudge (better
            # hold-through-close candidate). In long-only mode (default), a
            # negative tilt abstains rather than shorting (ADR-0089: the premium
            # is on the long/hold side; the short of intraday-driven names is NOT
            # the supported, net-harvestable form).
            if spread > 0:
                direction = 1
            else:
                if self.long_only_nudge:
                    return None
                direction = -1

            # confidence_raw from the spread magnitude via tanh squash.
            confidence_raw = float(np.tanh(self.spread_to_conf_scale * abs(spread)))
            confidence_raw = float(np.clip(confidence_raw, 0.0, 1.0))

            # magnitude: the overnight contribution as a per-day fraction (the
            # premium being protected), clipped to a sane band. This is a
            # conviction nudge, not a forecast — keep magnitude modest.
            magnitude = float(np.clip(abs(np.mean(on_tail)), 0.0001, 0.02))

            try:
                calibrated = float(self.calibrator.calibrate(confidence_raw))
            except Exception:  # noqa: BLE001 — cold-start fallback, never block
                calibrated = max(0.0, confidence_raw - 0.20)

            view = AnalystView(
                analyst=self.name,
                direction=direction,
                magnitude=magnitude,
                confidence=calibrated,
                confidence_raw=confidence_raw,
                horizon=self.horizon,
                rationale=(
                    f"overnight {on_ann:+.1%} vs intraday {id_ann:+.1%} ann "
                    f"(spread {spread:+.1%}, n={len(on_tail)}) — "
                    f"{'hold-through-close nudge' if direction > 0 else 'intraday-driven'}"
                )[:256],
                metadata={
                    "overnight_ann": on_ann,
                    "intraday_ann": id_ann,
                    "spread_ann": spread,
                    "n_pairs": int(len(on_tail)),
                    "lookback_window": self.lookback_window,
                    "zero_turnover": True,  # ADR-0089 D-2: modulates holds, never round-trips
                },
            )
            self._n_views_emitted += 1
            self._last_view_at = getattr(ctx, "asof", None)
            return view
        except Exception as exc:  # noqa: BLE001 — one bad analyst can't kill the fan-out
            self._error_count += 1
            logger.warning("overnight-drift analyze failed: %s", exc)
            return None

    def health(self) -> dict:
        return {
            "name": self.name,
            "n_views_emitted": self._n_views_emitted,
            "last_view_at": (self._last_view_at.isoformat() if self._last_view_at is not None else None),
            "error_count": self._error_count,
            "calibrator_status": self.calibrator.status(),
        }
