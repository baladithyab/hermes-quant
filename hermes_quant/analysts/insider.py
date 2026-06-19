"""hermes_quant.analysts.insider — insider + 13-F ownership analyst (aegis-ob3).

ADR-0100 (ob3): a NEW analyst that reads asof-honest INSIDER transactions
(``OpenBBInsider``) and 13-F INSTITUTIONAL holdings (``OpenBBInstitutional``)
and emits a deterministic ``AnalystView`` from the net insider buy/sell flow
combined with the institutional position change. It mirrors
``FundamentalsAnalyst`` / ``EstimatesAnalyst``: equity-only, source-backed,
NEVER trains (the calibrator learns; the analyst's scoring table is constant),
and abstains (Protocol-clean None) whenever the data is absent / non-finite /
ambiguous. The LLM NEVER decides — this is pure arithmetic over the asof-honest
ownership frames.

Signal
------
  * INSIDER: net buy/sell = sum(securities_transacted * sign), where sign is +1
    for an acquisition ('A') and -1 for a disposal ('D'). Net buying is bullish;
    net selling is bearish. The strength scales with net flow / gross flow (how
    one-sided the cluster is).
  * 13-F (institutional): the net institutional share CHANGE across holders.
    Rising institutional ownership is bullish; falling is bearish.

The two sub-signals vote (plurality direction); a tie / no surviving sub-signal
-> abstain (silence-by-default). Confidence scales with signal strength, clipped
to a conservative band and calibrated.

DEFAULT-OFF (both flags required)
---------------------------------
Gated on BOTH ``HERMES_QUANT_INSIDER_ANALYST=1`` (the per-analyst toggle for
this NEW analyst) AND ``HERMES_QUANT_OPENBB=1`` (the OpenBB vendor gate the
``OpenBBInsider`` / ``OpenBBInstitutional`` sources ride). Checked FIRST. With
either unset, ``analyze`` returns None WITHOUT touching the providers — so no
openbb import is attempted and the committee is byte-identical to a build that
never registered this analyst.

This analyst is intentionally NOT registered in
``[project.entry-points."hermes_quant.analysts"]`` while it is default-OFF
(ob3): the strongest byte-identical guarantee is that it is never discovered /
instantiated. The flag gate inside ``analyze`` is the second line of defense for
any explicit instantiation.

NOTE on form4 coexistence: ``OpenBBInsider`` feeds the SAME ``filing``-kind
evidence series BESIDE ``evidence/adapters/form4.py``; this analyst is a
SEPARATE consumer of the OpenBB ownership source and does not replace or modify
form4's EDGAR ingestion.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

import numpy as np
import pandas as pd

from hermes_quant.calibrators import ColdStartCalibrator
from hermes_quant.data.openbb_insider import (
    OPENBB_ENABLE_FLAG,
    OpenBBInsider,
    OpenBBInstitutional,
)
from hermes_quant.pdr_core import is_option_asset_class
from hermes_quant.protocol import (
    AnalystView,
    CalibratorNotReady,
    Direction,
    MarketContext,
    RealizedOutcome,
)

logger = logging.getLogger(__name__)

# Per-analyst default-OFF gate for the NEW insider analyst (ob3). Quoted-literal
# default so the flag-inventory scanner counts it.
INSIDER_ANALYST_FLAG = "HERMES_QUANT_INSIDER_ANALYST"

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


def _insider_analyst_enabled() -> bool:
    """True iff BOTH gates are on: the per-analyst toggle AND the OpenBB vendor.

    Read at call time so the env flip takes effect without re-import (and so the
    ``enabled`` property reflects the live flag state for discovery/health).
    Mirrors estimates._estimates_analyst_enabled.
    """
    analyst_on = os.environ.get(INSIDER_ANALYST_FLAG, "0") not in (
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


class InsiderAnalyst:
    """Insider + 13-F ownership analyst (ADR-0100 ob3).

    Equity-only. Reads ``OpenBBInsider`` (net insider buy/sell) and
    ``OpenBBInstitutional`` (net 13-F position change) and emits a deterministic
    AnalystView from the combined sign + strength. Default-OFF
    (``HERMES_QUANT_INSIDER_ANALYST`` AND ``HERMES_QUANT_OPENBB``).
    """

    name = "insider"
    timeframes = ["1d", "1w", "1M", "1Q"]
    asset_classes = ["equity"]  # ETF/crypto/FX abstain via Protocol-None

    # Per-horizon magnitude envelope (mirror FundamentalsAnalyst /
    # EstimatesAnalyst._MAGNITUDE_BY_HORIZON).
    _MAGNITUDE_BY_HORIZON: dict[str, float] = {
        "1d": 0.005,
        "1w": 0.015,
        "1M": 0.040,
        "1Q": 0.080,
    }

    # Confidence-clip envelope (mirror the conservative [0.20, 0.80] band). Both
    # ownership signals are noisy / lagged, so the same conservative band.
    _RAW_CONF_CLIP_LO = 0.20
    _RAW_CONF_CLIP_HI = 0.80

    # A net-flow imbalance below this (|net| / gross) is "balanced" — no
    # actionable cluster -> the sub-signal abstains.
    _MIN_IMBALANCE = 0.10

    @property
    def enabled(self) -> bool:
        """Class/instance flag reflecting the live default-OFF gate.

        A property (not a constant) so discovery / health observe the live env
        state. With the flags unset this is False — the analyst is off.
        """
        return _insider_analyst_enabled()

    def __init__(
        self,
        *,
        horizon: str = "1M",
        insider_provider: Any | None = None,
        institutional_provider: Any | None = None,
    ):
        self.horizon = horizon
        # The providers are constructed lazily-safe: OpenBBInsider /
        # OpenBBInstitutional do NOT import openbb at construction (only at the
        # obb property on fetch), so building them when the flags are off is a
        # byte-identical no-op. Tests inject stubs.
        self.insider_provider = insider_provider or OpenBBInsider()
        self.institutional_provider = institutional_provider or OpenBBInstitutional()
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
    # Sub-signal scorers (pure arithmetic over the asof-honest frames)
    # ------------------------------------------------------------------
    @staticmethod
    def _net_insider_imbalance(df: pd.DataFrame) -> tuple[int, float] | None:
        """Net insider buy/sell imbalance from the mapped insider frame.

        Returns (direction, imbalance) where imbalance = net / gross in [0, 1],
        or None when the frame is absent / has no finite share counts.
        """
        if df is None or len(df) == 0 or "securities_transacted" not in df.columns:
            return None
        signed = 0.0
        gross = 0.0
        any_finite = False
        for _, row in df.iterrows():
            qty = _coerce_float(row.get("securities_transacted"))
            if not np.isfinite(qty) or qty <= 0:
                continue
            any_finite = True
            ad = str(row.get("acquisition_or_disposal") or "").strip().upper()[:1]
            sign = 1.0 if ad == "A" else (-1.0 if ad == "D" else 0.0)
            signed += sign * qty
            gross += qty
        if not any_finite or gross <= 0:
            return None
        imbalance = signed / gross  # in [-1, 1]
        direction = 1 if imbalance > 0 else (-1 if imbalance < 0 else 0)
        return direction, abs(imbalance)

    @staticmethod
    def _net_institutional_change(df: pd.DataFrame) -> tuple[int, float] | None:
        """Net 13-F institutional share CHANGE from the mapped frame.

        Returns (direction, strength) where strength = |net change| / total
        shares held in [0, 1], or None when absent / no finite change.
        """
        if df is None or len(df) == 0 or "change" not in df.columns:
            return None
        net_change = 0.0
        total_shares = 0.0
        any_finite = False
        for _, row in df.iterrows():
            chg = _coerce_float(row.get("change"))
            shares = _coerce_float(row.get("shares"))
            if not np.isfinite(chg):
                continue
            any_finite = True
            net_change += chg
            if np.isfinite(shares) and shares > 0:
                total_shares += shares
        if not any_finite:
            return None
        denom = total_shares if total_shares > 0 else abs(net_change)
        if denom <= 0:
            return None
        strength = abs(net_change) / denom
        direction = 1 if net_change > 0 else (-1 if net_change < 0 else 0)
        return direction, float(min(strength, 1.0))

    # ------------------------------------------------------------------
    # Public API (Analyst Protocol)
    # ------------------------------------------------------------------
    def analyze(self, ctx: MarketContext) -> AnalystView | None:
        """ADR-0100 ob3 entrypoint.

        Returns None when:
          - DEFAULT-OFF (either flag unset) — abstain WITHOUT touching the
            providers (byte-identical committee, no openbb import). Checked
            FIRST.
          - asset class is not 'equity' (Protocol-clean abstain).
          - no ownership data / no surviving sub-signal (silence-by-default).
          - signals tie (silence).
          - the only signal is below the actionable imbalance threshold.
        """
        # DEFAULT-OFF gate FIRST: do not even touch the providers when off.
        if not _insider_analyst_enabled():
            return None
        try:
            uni = self._classify_symbol_universe(ctx.asset, ctx.asset_class)
            if uni != "equity":
                return None

            asof = ctx.asof
            if asof.tzinfo is None:
                asof = asof.tz_localize("UTC")

            insider_df = self._safe_read(
                self.insider_provider, "read_insider", ctx.asset, asof
            )
            inst_df = self._safe_read(
                self.institutional_provider,
                "read_institutional",
                ctx.asset,
                asof,
            )

            sub: list[tuple[str, int, float]] = []  # (label, direction, strength)

            ins = self._net_insider_imbalance(insider_df)
            if ins is not None and ins[0] != 0 and ins[1] >= self._MIN_IMBALANCE:
                sub.append(("insider_net_flow", ins[0], ins[1]))

            inst = self._net_institutional_change(inst_df)
            if inst is not None and inst[0] != 0 and inst[1] >= self._MIN_IMBALANCE:
                sub.append(("institutional_change", inst[0], inst[1]))

            if not sub:
                return None  # no surviving sub-signal -> silence

            longs = [s for s in sub if s[1] == 1]
            shorts = [s for s in sub if s[1] == -1]
            if len(longs) > len(shorts):
                composite_dir: Direction = 1
                contributing = longs
            elif len(shorts) > len(longs):
                composite_dir = -1
                contributing = shorts
            else:
                return None  # tie -> silence

            strength = float(np.mean([s[2] for s in contributing]))
            # FINITE-GUARD: a non-finite aggregate strength can never drive a
            # view (defeats every comparison gate).
            if not np.isfinite(strength) or strength <= 0:
                return None

            mag_scale = float(np.clip(strength, 0.0, 1.0))
            magnitude = self._mag(mag_scale)

            raw = float(
                np.clip(strength, self._RAW_CONF_CLIP_LO, self._RAW_CONF_CLIP_HI)
            )
            try:
                calibrated = self.calibrator.calibrate(raw)
            except CalibratorNotReady:
                # Match FundamentalsAnalyst's Beta(α=2,β=5) warm-start fallback.
                calibrated = (raw + 2.0) / 8.0

            view = AnalystView(
                analyst=self.name,
                direction=composite_dir,
                magnitude=magnitude,
                confidence=float(calibrated),
                confidence_raw=raw,
                horizon=self.horizon,
                rationale=(
                    f"ownership: {','.join(s[0] for s in contributing)} "
                    f"dir={composite_dir} strength={strength:.2f}"
                ),
                metadata={
                    "sub_signals": [
                        {"label": s[0], "direction": s[1], "strength": s[2]}
                        for s in sub
                    ],
                    "n_insider_rows": int(len(insider_df))
                    if insider_df is not None
                    else 0,
                    "n_institutional_rows": int(len(inst_df))
                    if inst_df is not None
                    else 0,
                },
            )
            self._n_views_emitted += 1
            self._last_view_at = asof
            return view

        except Exception as exc:  # noqa: BLE001
            self._error_count += 1
            logger.exception("insider analyze failed: %s", exc)
            return None

    @staticmethod
    def _safe_read(
        provider: Any, method: str, ticker: str, asof: pd.Timestamp
    ) -> pd.DataFrame | None:
        """Call provider.<method>(ticker, as_of=asof); None on any failure.

        Silence-by-default: a provider error (flag-off, openbb-missing,
        transient) must not break the committee — the sub-signal simply
        abstains.
        """
        fn = getattr(provider, method, None)
        if fn is None:
            return None
        try:
            return fn(ticker, as_of=asof)
        except Exception as exc:  # noqa: BLE001
            logger.debug("insider: %s failed for %s: %s", method, ticker, exc)
            return None

    def update(self, outcome: RealizedOutcome) -> None:
        """Feed realized outcome to the calibrator only (NEVER trains weights).

        Mirrors FundamentalsAnalyst.update / EstimatesAnalyst.update / ADR-0018 §D8.
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


__all__ = ["InsiderAnalyst", "INSIDER_ANALYST_FLAG", "OPENBB_ENABLE_FLAG"]
