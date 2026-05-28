"""hermes_quant.analysts.fundamentals — equity fundamentals analyst.

Per ADR-0064 + design docs/design/v0.6.1-fundamentals-analyst.md §2:

Six sub-signals derived from yfinance .info / .balance_sheet / .income_stmt
/ .cashflow are aggregated to one AnalystView:

  1. _score_pe_ratio              — P/E vs sector median
  2. _score_pe_forward_direction  — forward < trailing → improving
  3. _score_de                    — debt-to-equity buckets
  4. _score_fcf                   — Free Cash Flow level + YoY growth
  5. _score_revenue_growth        — Revenue YoY
  6. _score_eps_surprise          — actual vs forward consensus proxy

Equity-only — ETF / crypto / FX abstain (Protocol-clean None, NOT a
zero-confidence view per ADR-0064 §D4 / silence-bias-gate footgun).

NEVER trains, only infers (ADR-0018 §D8 generalized): the per-signal
calibration table is class-level constants; analyze() never mutates it.
update() forwards realized outcomes to the calibrator only — same pattern
as ClassicalTAAnalyst / KronosAnalyst.

Confidence clipped to [0.20, 0.80] (ADR-0064 §D3): floor looser than
Kronos's [0.30, 0.85] because partial-data is less obviously wrong than
path-disagreement; ceiling tighter than ClassicalTA's implicit 1.0
because yfinance values can be wrong without warning.

Composite gate: ≥3 surviving sub-signals (out of 6); plurality direction;
ties return None (silence-by-default).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from hermes_quant.calibrators import ColdStartCalibrator
from hermes_quant.data.fundamentals_provider import FundamentalsProvider
from hermes_quant.protocol import (
    AnalystView,
    CalibratorNotReady,
    Direction,
    MarketContext,
    RealizedOutcome,
)

logger = logging.getLogger(__name__)


SymbolUniverse = Literal["equity", "etf", "crypto", "fx", "unknown"]


@dataclass
class _SubSignal:
    direction: Direction
    magnitude: float
    raw_confidence: float
    label: str
    rationale: str = ""


def _coerce_float(x: Any) -> float:
    """Return float(x), or NaN if missing / unparseable."""
    if x is None:
        return float("nan")
    try:
        f = float(x)
        if not np.isfinite(f):
            return float("nan")
        return f
    except (TypeError, ValueError):
        return float("nan")


class FundamentalsAnalyst:
    """Six-signal fundamentals analyst.

    Per ADR-0064:
      - Equity-only (hard gate via _classify_symbol_universe).
      - Reads from FundamentalsProvider's parquet cache (no live yfinance
        on the hot path).
      - Six sub-signals; ≥3 surviving for composite; plurality direction.
      - Raw confidence clipped to [0.20, 0.80].
      - Default horizon = "1M".
    """

    name = "fundamentals"
    timeframes = ["1d", "1w", "1M", "1Q"]
    asset_classes = ["equity"]  # ETF/crypto/FX abstain via Protocol-None
    enabled = True

    SUB_SIGNAL_LABELS = (
        "pe_relative",
        "pe_forward_direction",
        "debt_equity_trend",
        "fcf_growth",
        "revenue_yoy",
        "earnings_surprise",
    )

    # Per-horizon magnitude envelope (ADR-0064 §D6 table)
    _MAGNITUDE_BY_HORIZON: dict[str, float] = {
        "1d": 0.005,
        "1w": 0.015,
        "1M": 0.040,
        "1Q": 0.080,
    }

    # Confidence-clip envelope (ADR-0064 §D3)
    _RAW_CONF_CLIP_LO = 0.20
    _RAW_CONF_CLIP_HI = 0.80

    # Composite gates
    _MIN_SURVIVING_SUBSIGNALS = 3
    _STALENESS_DAYS_HARD_LIMIT = 7  # per-signal abstain if older

    def __init__(
        self,
        *,
        horizon: str = "1M",
        provider: FundamentalsProvider | None = None,
        cache_root: Path | None = None,
    ):
        self.horizon = horizon
        self.provider = provider or FundamentalsProvider(
            cache_root=cache_root
            or (Path.home() / ".hermes" / "quant" / "cache" / "fundamentals")
        )
        self.calibrator = ColdStartCalibrator()
        self._n_views_emitted = 0
        self._last_view_at: pd.Timestamp | None = None
        self._error_count = 0

    # ------------------------------------------------------------------
    # Symbol-class classification (ADR-0064 §D4)
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_symbol_universe(
        asset: str, asset_class: str | None = None
    ) -> SymbolUniverse:
        """Decide whether the analyst should act on this asset.

        Rules (in order):
          1. Empty / non-string asset → 'unknown'.
          2. asset_class explicitly given:
             - 'equity' / 'etf' / 'crypto' / 'fx' → trust upstream.
             - 'option'                          → 'unknown' (deferred).
          3. '/' in asset                          → 'crypto'.
          4. asset endswith '=X'                   → 'fx'.
          5. else                                  → 'equity'.

        Only 'equity' lets analyze() proceed; everything else returns a
        Protocol-clean None (NOT a zero-confidence view).
        """
        if not isinstance(asset, str) or not asset.strip():
            return "unknown"
        if asset_class is not None:
            if asset_class in ("equity", "etf", "crypto", "fx"):
                return asset_class  # type: ignore[return-value]
            if asset_class == "option":
                return "unknown"
            # Unknown asset_class string → fall through to heuristics.
        if "/" in asset:
            return "crypto"
        if asset.endswith("=X"):
            return "fx"
        return "equity"

    # ------------------------------------------------------------------
    # Cache-backed snapshot fetch
    # ------------------------------------------------------------------

    def _fetch_fundamentals(
        self, ticker: str, asof: pd.Timestamp
    ) -> pd.Series | None:
        """Read the latest snapshot from the parquet cache.

        Returns None if:
          - no parquet file (cache miss; cron will populate)
          - parquet is empty
          - latest row is older than _STALENESS_DAYS_HARD_LIMIT days
          - quote_type is 'ETF' (post-fetch ETF detection)
        """
        try:
            snapshot = self.provider.read_latest(ticker, as_of=asof)
        except FileNotFoundError:
            return None
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "fundamentals: provider.read_latest failed for %s: %s", ticker, exc
            )
            return None
        if snapshot is None:
            return None
        # ETF post-check (yfinance may label something an ETF that the
        # heuristic gate above missed).
        qt = snapshot.get("quote_type")
        if isinstance(qt, str) and qt.upper() == "ETF":
            return None
        # Per-row hard staleness (ADR-0064 §D5).
        try:
            fetched_at = pd.Timestamp(snapshot["fetched_at"])
        except (KeyError, ValueError, TypeError):
            return None
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.tz_localize("UTC")
        if asof.tzinfo is None:
            asof = asof.tz_localize("UTC")
        age_days = (asof - fetched_at).days
        if age_days > self._STALENESS_DAYS_HARD_LIMIT:
            return None
        return snapshot

    # ------------------------------------------------------------------
    # Sub-signal scorers (one per row in §D6 calibration table)
    # ------------------------------------------------------------------

    def _mag(self, scale: float = 1.0) -> float:
        """Per-horizon magnitude × per-signal scale factor."""
        return self._MAGNITUDE_BY_HORIZON.get(self.horizon, 0.040) * scale

    def _score_pe_ratio(
        self, snap: pd.Series, sector_median_pe: float | None
    ) -> _SubSignal:
        """Trailing P/E vs sector median bucket (ADR-0064 §D6)."""
        pe = _coerce_float(snap.get("pe_trailing"))
        if np.isnan(pe) or pe <= 0:
            return _SubSignal(0, 0.0, 0.0, "pe_relative", "missing/non-positive")
        if pe > 1000:
            return _SubSignal(0, 0.0, 0.0, "pe_relative", "out of sane range")
        if sector_median_pe is None or sector_median_pe <= 0:
            return _SubSignal(0, 0.0, 0.0, "pe_relative", "no sector benchmark")
        ratio = pe / sector_median_pe
        if ratio < 0.7:
            return _SubSignal(
                +1, self._mag(), 0.80, "pe_relative",
                f"P/E={pe:.1f} vs sector {sector_median_pe:.1f} → cheap",
            )
        if ratio < 0.85:
            return _SubSignal(
                +1, self._mag(0.7), 0.55, "pe_relative",
                f"P/E={pe:.1f} vs sector {sector_median_pe:.1f}",
            )
        if ratio > 1.30:
            return _SubSignal(
                -1, self._mag(), 0.80, "pe_relative",
                f"P/E={pe:.1f} vs sector {sector_median_pe:.1f} → rich",
            )
        if ratio > 1.15:
            return _SubSignal(
                -1, self._mag(0.7), 0.55, "pe_relative",
                f"P/E={pe:.1f} vs sector {sector_median_pe:.1f}",
            )
        return _SubSignal(0, 0.0, 0.0, "pe_relative", "near sector median")

    def _score_pe_forward_direction(self, snap: pd.Series) -> _SubSignal:
        """Forward P/E < trailing → market expects EPS to rise → BUY tilt."""
        pe_t = _coerce_float(snap.get("pe_trailing"))
        pe_f = _coerce_float(snap.get("pe_forward"))
        if np.isnan(pe_t) or np.isnan(pe_f) or pe_t <= 0 or pe_f <= 0:
            return _SubSignal(0, 0.0, 0.0, "pe_forward_direction", "missing")
        if pe_t > 1000 or pe_f > 1000:
            return _SubSignal(0, 0.0, 0.0, "pe_forward_direction", "out of sane range")
        # Forward materially below trailing → improving earnings outlook.
        ratio = pe_f / pe_t
        if ratio < 0.85:
            return _SubSignal(
                +1, self._mag(0.6), 0.55, "pe_forward_direction",
                f"fwd P/E {pe_f:.1f} < trailing {pe_t:.1f}",
            )
        if ratio > 1.15:
            return _SubSignal(
                -1, self._mag(0.6), 0.55, "pe_forward_direction",
                f"fwd P/E {pe_f:.1f} > trailing {pe_t:.1f}",
            )
        return _SubSignal(0, 0.0, 0.0, "pe_forward_direction", "fwd ≈ trailing")

    def _score_de(self, snap: pd.Series) -> _SubSignal:
        """Debt-to-equity bucket (ADR-0064 §D6)."""
        dte = _coerce_float(snap.get("debt_to_equity"))
        if np.isnan(dte) or dte < 0:
            return _SubSignal(0, 0.0, 0.0, "debt_equity_trend", "missing")
        if dte > 50:
            return _SubSignal(0, 0.0, 0.0, "debt_equity_trend", "out of sane range")
        if dte < 0.3:
            return _SubSignal(
                +1, self._mag(0.6), 0.50, "debt_equity_trend",
                f"D/E={dte:.2f} → clean balance sheet",
            )
        if dte > 5.0:
            return _SubSignal(
                -1, self._mag(), 0.85, "debt_equity_trend",
                f"D/E={dte:.2f} → covenant risk",
            )
        if dte > 2.0:
            return _SubSignal(
                -1, self._mag(0.7), 0.70, "debt_equity_trend",
                f"D/E={dte:.2f} → highly levered",
            )
        return _SubSignal(0, 0.0, 0.0, "debt_equity_trend", "moderate leverage")

    def _score_fcf(self, snap: pd.Series) -> _SubSignal:
        """Free cash flow level + YoY growth (ADR-0064 §D6)."""
        fcf = _coerce_float(snap.get("free_cash_flow"))
        fcf_yoy = _coerce_float(snap.get("fcf_yoy"))
        if np.isnan(fcf):
            return _SubSignal(0, 0.0, 0.0, "fcf_growth", "missing")
        # Sanity gate (per ADR §D9 table).
        if abs(fcf) > 1e12:
            return _SubSignal(0, 0.0, 0.0, "fcf_growth", "out of sane range")
        if fcf < 0:
            return _SubSignal(
                -1, self._mag(0.6), 0.55, "fcf_growth",
                f"FCF={fcf:.2e} → cash-burning",
            )
        if np.isnan(fcf_yoy):
            return _SubSignal(0, 0.0, 0.0, "fcf_growth", "no YoY")
        if fcf_yoy > 0.20:
            return _SubSignal(
                +1, self._mag(0.8), 0.65, "fcf_growth",
                f"FCF YoY +{fcf_yoy:.1%}",
            )
        if fcf_yoy < -0.20:
            return _SubSignal(
                -1, self._mag(0.8), 0.65, "fcf_growth",
                f"FCF YoY {fcf_yoy:.1%}",
            )
        return _SubSignal(0, 0.0, 0.0, "fcf_growth", "stable")

    def _score_revenue_growth(self, snap: pd.Series) -> _SubSignal:
        """Revenue YoY (ADR-0064 §D6)."""
        ryoy = _coerce_float(snap.get("revenue_yoy"))
        if np.isnan(ryoy):
            return _SubSignal(0, 0.0, 0.0, "revenue_yoy", "missing")
        if ryoy > 0.15:
            return _SubSignal(
                +1, self._mag(0.7), 0.55, "revenue_yoy",
                f"Rev YoY +{ryoy:.1%}",
            )
        if ryoy < -0.10:
            return _SubSignal(
                -1, self._mag(0.9), 0.75, "revenue_yoy",
                f"Rev YoY {ryoy:.1%} → declining",
            )
        if ryoy < 0.0:
            return _SubSignal(
                -1, self._mag(0.7), 0.60, "revenue_yoy",
                f"Rev YoY {ryoy:.1%}",
            )
        return _SubSignal(0, 0.0, 0.0, "revenue_yoy", "flat")

    def _score_eps_surprise(self, snap: pd.Series) -> _SubSignal:
        """Earnings 'surprise' proxy (actual EPS vs forward EPS).

        v0.6.1 proxy: yfinance does not expose I/B/E/S consensus directly,
        so we use the ratio of trailing EPS to forward EPS as a coarse
        proxy. v0.7 will swap in true actual-vs-consensus from a feed
        that exposes it (Alpha Vantage / FMP).
        """
        actual = _coerce_float(snap.get("eps_trailing"))
        consensus = _coerce_float(snap.get("eps_forward"))
        if np.isnan(actual) or np.isnan(consensus):
            return _SubSignal(0, 0.0, 0.0, "earnings_surprise", "missing")
        if consensus <= 0:
            return _SubSignal(0, 0.0, 0.0, "earnings_surprise", "consensus <= 0")
        ratio = actual / consensus
        if ratio > 1.05:
            return _SubSignal(
                +1, self._mag(0.7), 0.60, "earnings_surprise",
                f"EPS ${actual:.2f} vs fwd ${consensus:.2f} (+{(ratio - 1):.1%})",
            )
        if ratio < 0.95:
            return _SubSignal(
                -1, self._mag(0.8), 0.65, "earnings_surprise",
                f"EPS ${actual:.2f} vs fwd ${consensus:.2f} ({(ratio - 1):.1%})",
            )
        return _SubSignal(0, 0.0, 0.0, "earnings_surprise", "in line")

    # ------------------------------------------------------------------
    # Public API (Analyst Protocol)
    # ------------------------------------------------------------------

    def analyze(self, ctx: MarketContext) -> AnalystView | None:
        """ADR-0002 + ADR-0064 entrypoint.

        Returns None when:
          - asset class is not 'equity' (Protocol-clean abstain)
          - cache miss / hard-staleness
          - <3 surviving sub-signals (partial-data abstain)
          - tied composite (silence-by-default)
        """
        try:
            uni = self._classify_symbol_universe(ctx.asset, ctx.asset_class)
            if uni != "equity":
                return None

            asof = ctx.asof
            if asof.tzinfo is None:
                asof = asof.tz_localize("UTC")

            snap = self._fetch_fundamentals(ctx.asset, asof)
            if snap is None:
                return None

            sector = snap.get("sector")
            sector_median_pe = self.provider.read_sector_median_pe(
                sector if isinstance(sector, str) else None, as_of=asof
            )

            sub_signals = [
                self._score_pe_ratio(snap, sector_median_pe),
                self._score_pe_forward_direction(snap),
                self._score_de(snap),
                self._score_fcf(snap),
                self._score_revenue_growth(snap),
                self._score_eps_surprise(snap),
            ]

            surviving = [s for s in sub_signals if s.direction != 0]
            if len(surviving) < self._MIN_SURVIVING_SUBSIGNALS:
                return None

            longs = [s for s in surviving if s.direction == +1]
            shorts = [s for s in surviving if s.direction == -1]
            if len(longs) > len(shorts):
                composite_dir: Direction = 1
                contributing = longs
            elif len(shorts) > len(longs):
                composite_dir = -1
                contributing = shorts
            else:
                return None  # tie → silence

            magnitude = float(np.mean([s.magnitude for s in contributing]))
            agreement = len(contributing) / len(surviving)
            mean_conf = float(np.mean([s.raw_confidence for s in contributing]))
            confidence_raw = float(
                np.clip(
                    agreement * mean_conf,
                    self._RAW_CONF_CLIP_LO,
                    self._RAW_CONF_CLIP_HI,
                )
            )

            try:
                calibrated = self.calibrator.calibrate(confidence_raw)
            except CalibratorNotReady:
                # Match ClassicalTA's Beta(α=2,β=5) warm-start fallback.
                calibrated = (confidence_raw + 2.0) / 8.0

            try:
                fetched_at = pd.Timestamp(snap["fetched_at"])
                if fetched_at.tzinfo is None:
                    fetched_at = fetched_at.tz_localize("UTC")
                age_days = int((asof - fetched_at).days)
            except Exception:  # noqa: BLE001
                age_days = -1

            view = AnalystView(
                analyst=self.name,
                direction=composite_dir,
                magnitude=magnitude,
                confidence=calibrated,
                confidence_raw=confidence_raw,
                horizon=self.horizon,
                rationale=(
                    f"agreement={len(contributing)}/{len(surviving)} surviving "
                    f"({','.join(s.label for s in contributing)})"
                ),
                metadata={
                    "snapshot_age_days": age_days,
                    "sector": sector,
                    "sector_median_pe": sector_median_pe,
                    "sub_signals": [
                        {
                            "label": s.label,
                            "direction": s.direction,
                            "magnitude": s.magnitude,
                            "raw_conf": s.raw_confidence,
                            "rationale": s.rationale,
                        }
                        for s in sub_signals
                    ],
                },
            )
            self._n_views_emitted += 1
            self._last_view_at = asof
            return view

        except Exception as exc:  # noqa: BLE001
            self._error_count += 1
            logger.exception("fundamentals analyze failed: %s", exc)
            return None

    def update(self, outcome: RealizedOutcome) -> None:
        """Feed realized outcome to the calibrator only.

        Per ADR-0018 §D8 generalized: analyst NEVER mutates its weights /
        sub-signal table from outcomes — only the calibrator learns. This
        method is the analyst's only mutator and forwards to
        `self.calibrator.fit(...)` exactly like ClassicalTA / Kronos.
        """
        self.calibrator.fit(
            [outcome.view.confidence_raw], [outcome.direction_correct]
        )

    def health(self) -> dict:
        return {
            "name": self.name,
            "n_views_emitted": self._n_views_emitted,
            "last_view_at": (
                self._last_view_at.isoformat() if self._last_view_at else None
            ),
            "error_count": self._error_count,
            "calibrator_status": self.calibrator.status(),
            "horizon": self.horizon,
            "cache_root": str(self.provider.cache_root),
        }


__all__ = ["FundamentalsAnalyst"]
