"""hermes_quant.analysts.estimates — forward-analyst-estimates analyst (aegis-ob2).

ADR-0100 (ob2): a NEW analyst that reads forward analyst ESTIMATES from
``OpenBBEstimates`` and emits a deterministic ``AnalystView`` from the
estimate-REVISION direction/magnitude. It mirrors ``FundamentalsAnalyst``:
equity-only, parquet/SDK-cache-backed, NEVER trains (the calibrator learns;
the analyst's scoring table is constant), and abstains (Protocol-clean None)
whenever the data is absent / non-finite / ambiguous.

Signal
------
Forward analyst estimates are revised over time. A consensus forward EPS
revised UP across recent publish dates is a bullish revision; revised DOWN is
bearish. The view direction is the sign of the latest revision; the magnitude
scales with the revision size (per-horizon envelope); the confidence scales
with the revision size, clipped to a conservative band and calibrated. The LLM
NEVER decides — this is pure arithmetic over the asof-honest estimate frame.

DEFAULT-OFF (both flags required)
---------------------------------
Gated on BOTH ``HERMES_QUANT_ESTIMATES_ANALYST=1`` (the per-analyst toggle for
this NEW analyst) AND ``HERMES_QUANT_OPENBB=1`` (the OpenBB vendor gate the
``OpenBBEstimates`` source rides). With either unset, ``analyze`` returns None
WITHOUT touching the provider — so no openbb import is attempted and the
committee is byte-identical to a build that never registered this analyst.

This analyst is intentionally NOT registered in
``[project.entry-points."hermes_quant.analysts"]`` while it is default-OFF
(ob2): the strongest byte-identical guarantee is that it is never discovered /
instantiated. The flag gate inside ``analyze`` is the second line of defense
for any explicit instantiation.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

import numpy as np
import pandas as pd

from hermes_quant.calibrators import ColdStartCalibrator
from hermes_quant.data.openbb_fundamentals import OPENBB_ENABLE_FLAG, OpenBBEstimates
from hermes_quant.pdr_core import is_option_asset_class
from hermes_quant.protocol import (
    AnalystView,
    CalibratorNotReady,
    Direction,
    MarketContext,
    RealizedOutcome,
)

logger = logging.getLogger(__name__)

# Per-analyst default-OFF gate for the NEW estimates analyst (ob2). Quoted-literal
# default so the flag-inventory scanner counts it.
ESTIMATES_ANALYST_FLAG = "HERMES_QUANT_ESTIMATES_ANALYST"

SymbolUniverse = Literal["equity", "etf", "crypto", "fx", "unknown"]


def _coerce_float(x: Any) -> float:
    """Return float(x), or NaN if missing / non-finite (finite-guard)."""
    if x is None:
        return float("nan")
    try:
        f = float(x)
    except (TypeError, ValueError, OverflowError):
        return float("nan")
    return f if np.isfinite(f) else float("nan")


def _estimates_analyst_enabled() -> bool:
    """True iff BOTH gates are on: the per-analyst toggle AND the OpenBB vendor.

    Read at call time so the env flip takes effect without re-import (and so the
    class-level ``enabled`` attribute below reflects the live flag state for
    discovery/health).
    """
    analyst_on = os.environ.get(ESTIMATES_ANALYST_FLAG, "0") not in (
        "",
        "0",
        "false",
        "False",
    )
    openbb_on = os.environ.get(OPENBB_ENABLE_FLAG, "0") not in (
        "",
        "0",
        "false",
        "False",
    )
    return analyst_on and openbb_on


class EstimatesAnalyst:
    """Forward-estimate-revision analyst (ADR-0100 ob2).

    Equity-only. Reads ``OpenBBEstimates`` (asof-honest forward estimates) and
    emits a deterministic AnalystView from the latest estimate-revision sign +
    size. Default-OFF (``HERMES_QUANT_ESTIMATES_ANALYST`` AND
    ``HERMES_QUANT_OPENBB``).
    """

    name = "estimates"
    timeframes = ["1d", "1w", "1M", "1Q"]
    asset_classes = ["equity"]  # ETF/crypto/FX abstain via Protocol-None

    # Per-horizon magnitude envelope (mirror FundamentalsAnalyst._MAGNITUDE_BY_HORIZON).
    _MAGNITUDE_BY_HORIZON: dict[str, float] = {
        "1d": 0.005,
        "1w": 0.015,
        "1M": 0.040,
        "1Q": 0.080,
    }

    # Confidence-clip envelope (mirror FundamentalsAnalyst §D3 band). Estimates
    # are forward-looking + noisy, so the same conservative [0.20, 0.80] band.
    _RAW_CONF_CLIP_LO = 0.20
    _RAW_CONF_CLIP_HI = 0.80

    # Revision threshold: a |revision ratio - 1| below this is "in line" (no
    # actionable signal -> abstain). Mirrors the FundamentalsAnalyst earnings
    # outlook 5% band.
    _MIN_REVISION = 0.02
    # Revision size that saturates raw confidence at the clip ceiling (10%).
    _REVISION_SATURATION = 0.10

    @property
    def enabled(self) -> bool:
        """Class/instance flag reflecting the live default-OFF gate.

        A property (not a constant) so discovery / health observe the live env
        state. With the flags unset this is False — the analyst is off.
        """
        return _estimates_analyst_enabled()

    def __init__(
        self,
        *,
        horizon: str = "1M",
        provider: Any | None = None,
    ):
        self.horizon = horizon
        # The provider is constructed lazily-safe: OpenBBEstimates does NOT
        # import openbb at construction (only at the obb property on fetch), so
        # building it when the flags are off is a byte-identical no-op. Tests
        # inject a stub.
        self.provider = provider or OpenBBEstimates()
        self.calibrator = ColdStartCalibrator()
        self._n_views_emitted = 0
        self._last_view_at: pd.Timestamp | None = None
        self._error_count = 0

    # ------------------------------------------------------------------
    # Symbol-class classification (mirror FundamentalsAnalyst §D4)
    # ------------------------------------------------------------------
    @staticmethod
    def _classify_symbol_universe(
        asset: str, asset_class: str | None = None
    ) -> SymbolUniverse:
        if not isinstance(asset, str) or not asset.strip():
            return "unknown"
        if asset_class is not None:
            if is_option_asset_class(asset_class):
                return "unknown"
            if asset_class in ("equity", "etf", "crypto", "fx"):
                return asset_class  # type: ignore[return-value]
        if "/" in asset:
            return "crypto"
        if asset.endswith("=X"):
            return "fx"
        return "equity"

    def _mag(self, scale: float = 1.0) -> float:
        return self._MAGNITUDE_BY_HORIZON.get(self.horizon, 0.040) * scale

    # ------------------------------------------------------------------
    # Public API (Analyst Protocol)
    # ------------------------------------------------------------------
    def analyze(self, ctx: MarketContext) -> AnalystView | None:
        """ADR-0100 ob2 entrypoint.

        Returns None when:
          - DEFAULT-OFF (either flag unset) — abstain WITHOUT touching the
            provider (byte-identical committee, no openbb import).
          - asset class is not 'equity' (Protocol-clean abstain).
          - no estimates / <2 revision points (silence-by-default).
          - latest estimate non-finite (finite-guard).
          - revision below the actionable threshold (in-line -> silence).
        """
        # DEFAULT-OFF gate FIRST: do not even touch the provider when off.
        if not _estimates_analyst_enabled():
            return None
        try:
            uni = self._classify_symbol_universe(ctx.asset, ctx.asset_class)
            if uni != "equity":
                return None

            asof = ctx.asof
            if asof.tzinfo is None:
                asof = asof.tz_localize("UTC")

            try:
                est = self.provider.read_estimates(ctx.asset, as_of=asof)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "estimates: provider.read_estimates failed for %s: %s",
                    ctx.asset,
                    exc,
                )
                return None
            if est is None or len(est) < 2 or "eps_avg" not in est.columns:
                return None

            # Order by publish date ascending; the revision is the move from the
            # prior published estimate to the latest.
            est = est.sort_values("date").reset_index(drop=True)
            latest = _coerce_float(est.iloc[-1]["eps_avg"])
            prior = _coerce_float(est.iloc[-2]["eps_avg"])
            # FINITE-GUARD: a non-finite latest/prior eps_avg can never drive a
            # view (a NaN/inf revision ratio defeats every comparison gate).
            if not np.isfinite(latest) or not np.isfinite(prior):
                return None
            if prior <= 0 or latest <= 0:
                # A non-positive consensus EPS makes the ratio sign-flipped /
                # meaningless (loss-makers). Abstain.
                return None

            revision = (latest - prior) / abs(prior)
            if not np.isfinite(revision) or abs(revision) < self._MIN_REVISION:
                return None  # in-line revision -> silence-by-default

            composite_dir: Direction = 1 if revision > 0 else -1
            # Magnitude scales with revision size (capped by per-horizon envelope).
            mag_scale = float(
                np.clip(abs(revision) / self._REVISION_SATURATION, 0.0, 1.0)
            )
            magnitude = self._mag(mag_scale)

            # Raw confidence: revision size mapped into the conservative band.
            raw = float(
                np.clip(
                    abs(revision) / self._REVISION_SATURATION,
                    self._RAW_CONF_CLIP_LO,
                    self._RAW_CONF_CLIP_HI,
                )
            )

            try:
                calibrated = self.calibrator.calibrate(raw)
            except CalibratorNotReady:
                # Match FundamentalsAnalyst's Beta(α=2,β=5) warm-start fallback.
                calibrated = (raw + 2.0) / 8.0

            latest_date = est.iloc[-1]["date"]
            prior_date = est.iloc[-2]["date"]
            view = AnalystView(
                analyst=self.name,
                direction=composite_dir,
                magnitude=magnitude,
                confidence=float(calibrated),
                confidence_raw=raw,
                horizon=self.horizon,
                rationale=(
                    f"fwd EPS revision {prior:.2f} -> {latest:.2f} "
                    f"({revision:+.1%}) over {prior_date}..{latest_date}"
                ),
                metadata={
                    "eps_avg_latest": latest,
                    "eps_avg_prior": prior,
                    "revision": revision,
                    "latest_publish_date": str(latest_date),
                    "prior_publish_date": str(prior_date),
                },
            )
            self._n_views_emitted += 1
            self._last_view_at = asof
            return view

        except Exception as exc:  # noqa: BLE001
            self._error_count += 1
            logger.exception("estimates analyze failed: %s", exc)
            return None

    def update(self, outcome: RealizedOutcome) -> None:
        """Feed realized outcome to the calibrator only (NEVER trains weights).

        Mirrors FundamentalsAnalyst.update / ADR-0018 §D8.
        """
        self.calibrator.fit(
            [outcome.view.confidence_raw], [outcome.direction_correct]
        )

    def health(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "n_views_emitted": self._n_views_emitted,
            "last_view_at": (
                self._last_view_at.isoformat() if self._last_view_at else None
            ),
            "error_count": self._error_count,
            "calibrator_status": self.calibrator.status(),
            "horizon": self.horizon,
        }


__all__ = ["EstimatesAnalyst", "ESTIMATES_ANALYST_FLAG", "OPENBB_ENABLE_FLAG"]
