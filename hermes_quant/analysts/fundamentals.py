"""hermes_quant.analysts.fundamentals — equity fundamentals analyst.

Per ADR-0064 + design docs/design/v0.6.1-fundamentals-analyst.md §2:

Six sub-signals derived from yfinance .info / .balance_sheet / .income_stmt
/ .cashflow are aggregated to one AnalystView:

  1. _score_pe_ratio              — P/E vs sector median
  2. _score_pe_forward_direction  — forward < trailing → improving
  3. _score_de                    — debt-to-equity buckets
  4. _score_fcf                   — Free Cash Flow level + YoY growth
  5. _score_revenue_growth        — Revenue YoY
  6. _score_eps_surprise          — forward EPS vs trailing EPS (analyst-revision tilt)

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
from hermes_quant.pdr_core import is_option_asset_class
from hermes_quant.playbook.scorers import NON_EQUITY_QUOTE_TYPES
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
        "earnings_outlook",
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

    # Staleness gates (ADR-0064 §D5; cs40).
    #
    # Two distinct freshness concerns, two distinct gates:
    #
    #   (1) DATUM staleness — is the underlying fundamental too OLD to be
    #       relevant? Fundamentals are quarterly, so a datum is measured
    #       against its fiscal-period basis (report_date preferred, else
    #       period_end), NOT against when the cron happened to cache it. A
    #       quarterly datum is legitimately ~1 quarter old between filings,
    #       and stays the freshest-available datum until the next quarter is
    #       filed (one quarter ≈ 91d) plus the reporting lag (~45d) before
    #       that next quarter is even knowable. The cadence-aware hard limit
    #       (~2 quarters) admits the freshest-available quarter in every
    #       legitimate case while still rejecting a snapshot older than ~half
    #       a fiscal year. This replaces the old 7d fetched_at gate on the
    #       datum-recency axis: post-cs12 the provider's reporting-lag filter
    #       already guarantees a returned row was PUBLIC by as_of, so keying
    #       datum-recency off fetched_at (a 7d cron-cadence value) wrongly
    #       darkened the analyst on any backtest cache not re-snapshotted
    #       daily — the lag (visible at period_end+45d) and the 7d
    #       fetched_at gate (fetched_at ~30d+ old by then) cancelled.
    #
    #   (2) CRON-LIVENESS — only the FALLBACK when a row carries NO fiscal
    #       basis (old-schema / pre-B34 parquets backfilled with NaT). There
    #       the original §D5 7d fetched_at gate stands: if the cron stopped
    #       writing fresh rows for >7d the cache is going stale, abstain.
    #       This preserves the original live behavior for basis-less rows and
    #       loosens nothing for them.
    _STALENESS_DATUM_DAYS_HARD_LIMIT = 190  # ~2 quarters; datum-recency axis
    _STALENESS_FETCHED_AT_DAYS_HARD_LIMIT = 7  # cron-liveness fallback (no basis)
    # Back-compat alias (some callers/tests reference the original name).
    _STALENESS_DAYS_HARD_LIMIT = _STALENESS_FETCHED_AT_DAYS_HARD_LIMIT

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
        if isinstance(self.calibrator, ColdStartCalibrator):
            # ADR-0065 v0.6.1-fix-H3: surface cold-start collapse risk.
            logger.warning(
                "FundamentalsAnalyst: using ColdStartCalibrator. Confidence outputs will be "
                "approximately constant ~0.31 until calibrated. Run "
                "scripts/quant-bootstrap-calibrator.py to seed real calibration."
            )
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
             - option FAMILY ('option' / 'us_option') → 'unknown' (deferred).
          3. '/' in asset                          → 'crypto'.
          4. asset endswith '=X'                   → 'fx'.
          5. else                                  → 'equity'.

        Only 'equity' lets analyze() proceed; everything else returns a
        Protocol-clean None (NOT a zero-confidence view).
        """
        if not isinstance(asset, str) or not asset.strip():
            return "unknown"
        if asset_class is not None:
            # Option FAMILY first: the live host stamps 'us_option' (react.multileg),
            # 'option' is the generic/legacy token. Recognize the FAMILY via
            # pdr_core.is_option_asset_class — a bare `== "option"` would miss the
            # live 'us_option' stamp and fall through to the symbol heuristics, which
            # classify an OCC-21 option symbol (no '/', no '=X') as 'equity' and let
            # analyze() fetch/score fundamentals for a contract symbol as a stock
            # (ac1's contract-layer divergence, here in the analyst).
            if is_option_asset_class(asset_class):
                return "unknown"
            if asset_class in ("equity", "etf", "crypto", "fx"):
                return asset_class  # type: ignore[return-value]
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
          - quote_type is in NON_EQUITY_QUOTE_TYPES (post-fetch non-equity
            detection: ETF / MUTUALFUND / INDEX / CURRENCY / CRYPTOCURRENCY)
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
        # Non-equity quote_type post-check (cs45/cs47). The symbol heuristics
        # above only catch '/' (crypto) and '=X' (fx); a plain ticker with NO
        # asset_class classifies 'equity' and the snapshot is fetched. The
        # provider then writes ANY yfinance quoteType verbatim, so a MUTUALFUND
        # / INDEX / CURRENCY / CRYPTOCURRENCY (or ETF) snapshot would otherwise
        # be scored with equity-specific fundamentals (P/E, D/E, FCF, …) as if
        # it were a stock — a category error feeding an ADR-0004 gate input.
        # Abstain on the FULL canonical non-equity set, single-sourced from the
        # provider-side scorers.NON_EQUITY_QUOTE_TYPES (the set scorers.py uses
        # to skip equity-only earnings lookups) rather than a third inlined copy.
        qt = snapshot.get("quote_type")
        if isinstance(qt, str) and qt.upper() in NON_EQUITY_QUOTE_TYPES:
            return None
        if asof.tzinfo is None:
            asof = asof.tz_localize("UTC")
        # Per-row hard staleness (ADR-0064 §D5; cs40).
        #
        # Prefer the DATUM's fiscal-period basis (report_date, else
        # period_end) and apply the cadence-aware quarterly limit. The
        # provider's reporting-lag filter has already proven the row was
        # PUBLIC by as_of, so the only remaining question is whether the
        # datum is too OLD to be relevant — a quarterly question, not a
        # 7-day cron-cadence one. Only when the row carries NEITHER a
        # report_date NOR a period_end (old-schema / pre-B34 parquets) do we
        # fall back to the original 7d fetched_at cron-liveness gate.
        basis = self._datum_basis(snapshot)
        if basis is not None:
            datum_age_days = (asof - basis).days
            # cs77: bound the datum age BELOW as well as above. A future-dated
            # fiscal basis (corrupt / hand-built / vendor mis-stamped
            # report_date / period_end > asof) makes datum_age_days NEGATIVE,
            # and the old upper-only `> HARD_LIMIT` clause read `negative > 190`
            # as False -> the gate was BYPASSED and a not-yet-knowable datum was
            # scored as a current one (same fail-OPEN-on-future-timestamp class
            # as cs42a/cs53/cs67/cs68/cs69/cs75). The NaN-safe bounded membership
            # test mirrors cs75 (`if not (0 <= age_days <= HARD)`) and cs67
            # (`0 <= age_h < ttl`): a future basis -> negative -> abstain; a
            # NaT/missing-derived nan age fails the test -> abstain. The
            # inclusive `<=` preserves the old strictly-greater upper boundary,
            # so a genuinely-current datum (age in [0, HARD_LIMIT]) is admitted
            # byte-identically.
            if not (0 <= datum_age_days <= self._STALENESS_DATUM_DAYS_HARD_LIMIT):
                return None
            return snapshot
        # Fallback: no fiscal basis — cron-liveness gate on fetched_at.
        try:
            fetched_at = pd.Timestamp(snapshot["fetched_at"])
        except (KeyError, ValueError, TypeError):
            return None
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.tz_localize("UTC")
        age_days = (asof - fetched_at).days
        if age_days > self._STALENESS_FETCHED_AT_DAYS_HARD_LIMIT:
            return None
        return snapshot

    @staticmethod
    def _datum_basis(snapshot: pd.Series) -> pd.Timestamp | None:
        """Return the datum's fiscal-period basis (UTC), or None.

        report_date preferred (when the filing was published), else
        period_end (the fiscal period the datum describes). Returns None
        when both are absent / NaT — the basis-less fallback path. Mirrors
        the provider's _apply_reporting_lag_filter precedence (report_date
        → period_end) so the staleness axis is consistent with the
        knowability axis.
        """
        for col in ("report_date", "period_end"):
            if col not in snapshot.index:
                continue
            raw = snapshot.get(col)
            if raw is None:
                continue
            try:
                ts = pd.Timestamp(raw)
            except (ValueError, TypeError):
                continue
            if ts is pd.NaT or pd.isna(ts):
                continue
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            return ts
        return None

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
        """Earnings outlook proxy (forward EPS vs trailing EPS).

        v0.6.1 proxy: yfinance does not expose I/B/E/S consensus directly,
        so we compare forward consensus EPS to trailing actual EPS as a
        coarse proxy for analyst revisions.

        v0.6.1-fix-M5: direction was inverted in v0.6.1's initial cut.
        Forward EPS is the *forecast*; trailing EPS is the realised value.
        ``forward / trailing > 1`` means analysts expect growth (bullish),
        ``< 1`` means analysts expect contraction (bearish). The previous
        ``trailing / forward`` ratio had the sign flipped. The label is
        also renamed from ``earnings_surprise`` to ``earnings_outlook``
        because this is not a true post-print surprise -- it's an
        ex-ante analyst-revision tilt. v0.7 will swap in true
        actual-vs-consensus from a feed that exposes it (Alpha Vantage / FMP).
        """
        trailing = _coerce_float(snap.get("eps_trailing"))
        forward = _coerce_float(snap.get("eps_forward"))
        if np.isnan(trailing) or np.isnan(forward):
            return _SubSignal(0, 0.0, 0.0, "earnings_outlook", "missing")
        if trailing <= 0:
            # Loss-makers: ratio is meaningless / sign-flipped. Skip.
            return _SubSignal(0, 0.0, 0.0, "earnings_outlook", "trailing <= 0")
        ratio = forward / trailing
        if ratio > 1.05:
            return _SubSignal(
                +1, self._mag(0.7), 0.60, "earnings_outlook",
                f"fwd EPS ${forward:.2f} vs trail ${trailing:.2f} (+{(ratio - 1):.1%})",
            )
        if ratio < 0.95:
            return _SubSignal(
                -1, self._mag(0.8), 0.65, "earnings_outlook",
                f"fwd EPS ${forward:.2f} vs trail ${trailing:.2f} ({(ratio - 1):.1%})",
            )
        return _SubSignal(0, 0.0, 0.0, "earnings_outlook", "in line")

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
