"""hermes_quant.factors.factor_oracle — AlphaBench-style Factor Forecasting Oracle.

Bridges AlphaZoo registration to production-readiness scoring.

The FactorOracle evaluates every registered alpha factor against real OHLCV
bars via a walk-forward IC panel, then maps the resulting metrics to one of
four production-readiness tiers:

    premium      — icir ≥ 0.5 AND hit_rate ≥ 0.60 AND ic_mean ≥ 0.05
    standard     — icir ≥ 0.3 AND hit_rate ≥ 0.55 AND ic_mean ≥ 0.02
    experimental — icir ≥ 0.1 AND hit_rate ≥ 0.50
    rejected     — anything below experimental

Every evaluation result is appended to an APPEND-ONLY JSONL file at::

    ~/.hermes/quant/factors/factor_verdicts.jsonl

Re-evaluating a factor adds a NEW row (verdict history is preserved).
Reading returns the LATEST verdict for a given factor_id.

Integration with ICDedupGate
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
When ``ic_dedup_gate`` is supplied, the oracle runs the factor returns through
the dedup gate before scoring.  A near-duplicate factor is automatically
downgraded to ``rejected`` and the dedup reason is prepended to ``reasons``.

References
~~~~~~~~~~
    AlphaBench (CityU, 2024) — Factor Forecasting Oracle (FFO) pattern.
    R&D-Agent (NeurIPS 2025, arXiv:2505.15155) — ICIR thresholds §4.2.
    WorldQuant — 4-tier signal grading (premium/standard/research/reject).
    ADR-0055 — FactorOracle and production-readiness tiers.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pandas as pd
from pydantic import BaseModel, Field

from hermes_quant.factors.ic_panel import ICPanel, compute_ic_panel

if TYPE_CHECKING:
    from hermes_quant.factors.alpha_zoo import AlphaZoo
    from hermes_quant.factors.ic_dedup import ICDedupGate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default storage path (overridable in tests via env var)
# ---------------------------------------------------------------------------
_DEFAULT_DIR = Path(
    os.environ.get(
        "HERMES_QUANT_ALPHA_ZOO_DIR",
        Path.home() / ".hermes" / "quant" / "factors",
    )
)

_VERDICTS_FILENAME = "factor_verdicts.jsonl"


# ---------------------------------------------------------------------------
# ProductionReadinessThresholds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _TierThresholds:
    """Numeric thresholds for a single tier."""

    min_icir: float
    min_hit_rate: float
    min_ic_mean: float


@dataclass
class ProductionReadinessThresholds:
    """Configurable thresholds for the 4-tier production-readiness system.

    Three pre-built named profiles are provided as class attributes:
        PREMIUM      — conservative; suitable for live capital allocation.
        STANDARD     — moderate; suitable for paper trading / shadow mode.
        EXPERIMENTAL — permissive; suitable for research pipeline inclusion.

    Custom thresholds can be passed directly to the constructor.

    Attributes:
        premium:      Thresholds that must ALL be met for premium tier.
        standard:     Thresholds that must ALL be met for standard tier.
        experimental: Thresholds that must ALL be met for experimental tier.
                      Anything below is "rejected".
    """

    premium: _TierThresholds = None  # type: ignore[assignment]
    standard: _TierThresholds = None  # type: ignore[assignment]
    experimental: _TierThresholds = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.premium is None:
            self.premium = _TierThresholds(
                min_icir=0.5, min_hit_rate=0.60, min_ic_mean=0.05
            )
        if self.standard is None:
            self.standard = _TierThresholds(
                min_icir=0.3, min_hit_rate=0.55, min_ic_mean=0.02
            )
        if self.experimental is None:
            self.experimental = _TierThresholds(
                min_icir=0.1, min_hit_rate=0.50, min_ic_mean=float("-inf")
            )

    def assign_tier(
        self, panel: ICPanel
    ) -> tuple[Literal["premium", "standard", "experimental", "rejected"], list[str]]:
        """Assign a tier and reasons for a given :class:`ICPanel`.

        Returns
        -------
        tier:    The tier string.
        reasons: Up to 5 human-readable explanations.
        """
        ic_mean = panel.ic_mean
        icir = panel.icir
        hit_rate = panel.hit_rate

        # Guard NaN — always rejected
        if not all(
            isinstance(x, float) and (x == x)  # NaN != NaN
            for x in [ic_mean, icir, hit_rate]
        ):
            return "rejected", ["NaN metrics — insufficient or degenerate data"]

        # ---- Premium ----
        p = self.premium
        if icir >= p.min_icir and hit_rate >= p.min_hit_rate and ic_mean >= p.min_ic_mean:
            reasons = [
                f"icir={icir:.4f} ≥ premium min {p.min_icir}",
                f"hit_rate={hit_rate:.4f} ≥ premium min {p.min_hit_rate}",
                f"ic_mean={ic_mean:.4f} ≥ premium min {p.min_ic_mean}",
            ]
            return "premium", reasons

        # ---- Standard ----
        s = self.standard
        if icir >= s.min_icir and hit_rate >= s.min_hit_rate and ic_mean >= s.min_ic_mean:
            reasons = [
                f"icir={icir:.4f} ≥ standard min {s.min_icir}",
                f"hit_rate={hit_rate:.4f} ≥ standard min {s.min_hit_rate}",
                f"ic_mean={ic_mean:.4f} ≥ standard min {s.min_ic_mean}",
            ]
            return "standard", reasons

        # ---- Experimental ----
        e = self.experimental
        if icir >= e.min_icir and hit_rate >= e.min_hit_rate:
            reasons = [
                f"icir={icir:.4f} ≥ experimental min {e.min_icir}",
                f"hit_rate={hit_rate:.4f} ≥ experimental min {e.min_hit_rate}",
            ]
            # Add shortfall notes
            if icir < s.min_icir:
                reasons.append(
                    f"icir={icir:.4f} below standard min {s.min_icir}"
                )
            return "experimental", reasons[:5]

        # ---- Rejected ----
        reasons: list[str] = []
        if icir < e.min_icir:
            reasons.append(f"icir={icir:.4f} below experimental min {e.min_icir}")
        if hit_rate < e.min_hit_rate:
            reasons.append(
                f"hit_rate={hit_rate:.4f} below experimental min {e.min_hit_rate}"
            )
        reasons.append("does not meet minimum experimental thresholds")
        return "rejected", reasons[:5]


# ---------------------------------------------------------------------------
# FactorVerdict Pydantic v2 model
# ---------------------------------------------------------------------------


class FactorVerdict(BaseModel):
    """Production-readiness verdict for a single alpha factor.

    Attributes:
        factor_id:        Unique factor identifier (from AlphaZoo).
        name:             Human-readable factor name.
        ic_panel:         Serialised :class:`ICPanel` metrics dict.
        production_ready: True when tier is premium or standard.
        tier:             One of premium | standard | experimental | rejected.
        reasons:          Up to 5 explanations for the tier assignment.
        reviewed_at:      ISO-8601 UTC timestamp of this evaluation.
    """

    factor_id: str
    name: str
    ic_panel: dict
    production_ready: bool
    tier: Literal["premium", "standard", "experimental", "rejected"]
    reasons: list[str] = Field(default_factory=list, max_length=5)
    reviewed_at: str = Field(default="")

    model_config = {"extra": "forbid"}

    def model_post_init(self, __context: object) -> None:  # type: ignore[override]
        if not self.reviewed_at:
            object.__setattr__(
                self,
                "reviewed_at",
                datetime.now(timezone.utc).isoformat(),
            )

    @property
    def ic_panel_obj(self) -> ICPanel:
        """Deserialise the ic_panel dict back to an :class:`ICPanel`."""
        return ICPanel.from_dict(self.ic_panel)


# ---------------------------------------------------------------------------
# FactorOracle
# ---------------------------------------------------------------------------


class FactorOracle:
    """AlphaBench-style Factor Forecasting Oracle.

    Evaluates registered alpha factors against OHLCV bars, computes IC panels
    via walk-forward windows, maps metrics to production-readiness tiers, and
    persists every verdict to an append-only JSONL log.

    Args:
        alpha_zoo:      Populated :class:`~hermes_quant.factors.alpha_zoo.AlphaZoo`.
        ic_dedup_gate:  Optional :class:`~hermes_quant.factors.ic_dedup.ICDedupGate`
                        — when supplied, near-duplicate factors are flagged and
                        downgraded to ``rejected``.
        thresholds:     Custom :class:`ProductionReadinessThresholds`.  Defaults
                        to the built-in conservative thresholds.
        verdicts_dir:   Override directory for ``factor_verdicts.jsonl``.
    """

    def __init__(
        self,
        alpha_zoo: "AlphaZoo",
        *,
        ic_dedup_gate: "ICDedupGate | None" = None,
        thresholds: ProductionReadinessThresholds | None = None,
        verdicts_dir: str | Path | None = None,
    ) -> None:
        self._zoo = alpha_zoo
        self._dedup = ic_dedup_gate
        self._thresholds = thresholds or ProductionReadinessThresholds()

        vdir = (
            Path(verdicts_dir)
            if verdicts_dir is not None
            else _DEFAULT_DIR
        )
        vdir.mkdir(parents=True, exist_ok=True)
        self._verdicts_path = vdir / _VERDICTS_FILENAME

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _append_verdict(self, verdict: FactorVerdict) -> None:
        """Append a single verdict to the JSONL log (APPEND-ONLY)."""
        with open(self._verdicts_path, "a", encoding="utf-8") as fh:
            fh.write(verdict.model_dump_json() + "\n")
        logger.debug(
            "FactorOracle: verdict appended for %r → %s",
            verdict.factor_id,
            verdict.tier,
        )

    def _latest_verdict(self, factor_id: str) -> FactorVerdict | None:
        """Read the LATEST verdict for *factor_id* from the JSONL log.

        Scans the entire file linearly (file is typically small).
        Returns None if no verdict exists for this factor.
        """
        if not self._verdicts_path.exists():
            return None
        latest: FactorVerdict | None = None
        with open(self._verdicts_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if not isinstance(data, dict):
                        continue
                    if data.get("factor_id") == factor_id:
                        latest = FactorVerdict.model_validate(data)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("FactorOracle: malformed verdict line: %s", exc)
        return latest

    # ------------------------------------------------------------------
    # Core evaluation
    # ------------------------------------------------------------------

    def evaluate(
        self,
        factor_id: str,
        bars: pd.DataFrame,
        *,
        fwd_horizon_days: int = 5,
    ) -> FactorVerdict:
        """Evaluate a single factor and persist the verdict.

        Parameters
        ----------
        factor_id:
            Must be registered in the :class:`AlphaZoo`.
        bars:
            OHLCV DataFrame with DatetimeIndex and at least ``close`` and
            ``volume`` columns.  Minimum length: ``ic_panel.window`` (60)
            observations beyond the forward-horizon shift.
        fwd_horizon_days:
            Forward-return horizon.  Default 5 (next-week).

        Returns
        -------
        FactorVerdict
            The evaluation result (also persisted to JSONL).

        Raises
        ------
        KeyError
            If *factor_id* is not registered in the zoo.
        """
        factor = self._zoo.read(factor_id)
        if factor is None:
            raise KeyError(f"FactorOracle: factor {factor_id!r} not registered in AlphaZoo")

        # ---- Compute factor series ----
        try:
            factor_series = self._zoo.compute(factor_id, bars)
            factor_series.name = factor_id
        except Exception as exc:  # noqa: BLE001
            verdict = FactorVerdict(
                factor_id=factor_id,
                name=factor.name,
                ic_panel=ICPanel(
                    factor_id=factor_id,
                    ic_mean=float("nan"),
                    ic_std=float("nan"),
                    icir=float("nan"),
                    hit_rate=float("nan"),
                    turnover=float("nan"),
                    n_periods=0,
                    fwd_horizon_days=fwd_horizon_days,
                ).to_dict(),
                production_ready=False,
                tier="rejected",
                reasons=[f"factor compute() failed: {exc}"],
                reviewed_at=datetime.now(timezone.utc).isoformat(),
            )
            self._append_verdict(verdict)
            return verdict

        # ---- Build forward returns ----
        fwd_returns = (
            bars["close"].pct_change(fwd_horizon_days).shift(-fwd_horizon_days)
        )
        fwd_returns.name = "fwd_returns"

        # ---- ICDedupGate check (optional) ----
        dedup_reason: str | None = None
        if self._dedup is not None:
            factor_returns = factor_series.dropna().values
            dedup_result = self._dedup.check(factor_returns)
            if not dedup_result.passes:
                dedup_reason = f"ICDedupGate: {dedup_result.reason}"
                logger.info(
                    "FactorOracle: %r rejected by ICDedupGate: %s",
                    factor_id,
                    dedup_result.reason,
                )

        # ---- Compute IC panel ----
        try:
            panel = compute_ic_panel(
                factor_series,
                fwd_returns,
                factor_id=factor_id,
                fwd_horizon_days=fwd_horizon_days,
            )
        except ValueError as exc:
            verdict = FactorVerdict(
                factor_id=factor_id,
                name=factor.name,
                ic_panel=ICPanel(
                    factor_id=factor_id,
                    ic_mean=float("nan"),
                    ic_std=float("nan"),
                    icir=float("nan"),
                    hit_rate=float("nan"),
                    turnover=float("nan"),
                    n_periods=0,
                    fwd_horizon_days=fwd_horizon_days,
                ).to_dict(),
                production_ready=False,
                tier="rejected",
                reasons=[f"insufficient data: {exc}"],
                reviewed_at=datetime.now(timezone.utc).isoformat(),
            )
            self._append_verdict(verdict)
            return verdict

        # ---- Dedup override ----
        if dedup_reason is not None:
            reasons = [dedup_reason]
            verdict = FactorVerdict(
                factor_id=factor_id,
                name=factor.name,
                ic_panel=panel.to_dict(),
                production_ready=False,
                tier="rejected",
                reasons=reasons[:5],
                reviewed_at=datetime.now(timezone.utc).isoformat(),
            )
            self._append_verdict(verdict)
            return verdict

        # ---- Tier assignment ----
        tier, reasons = self._thresholds.assign_tier(panel)
        production_ready = tier in ("premium", "standard")

        verdict = FactorVerdict(
            factor_id=factor_id,
            name=factor.name,
            ic_panel=panel.to_dict(),
            production_ready=production_ready,
            tier=tier,
            reasons=reasons[:5],
            reviewed_at=datetime.now(timezone.utc).isoformat(),
        )
        self._append_verdict(verdict)
        return verdict

    def evaluate_all(
        self,
        bars: pd.DataFrame,
        *,
        fwd_horizon_days: int = 5,
    ) -> dict[str, FactorVerdict]:
        """Evaluate every registered factor.

        Returns
        -------
        dict[factor_id, FactorVerdict]
        """
        results: dict[str, FactorVerdict] = {}
        for factor in self._zoo.list_all():
            try:
                verdict = self.evaluate(
                    factor.factor_id,
                    bars,
                    fwd_horizon_days=fwd_horizon_days,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "FactorOracle.evaluate_all: error for %r: %s",
                    factor.factor_id,
                    exc,
                )
                continue
            results[factor.factor_id] = verdict
        return results

    def rank(
        self,
        bars: pd.DataFrame,
        *,
        fwd_horizon_days: int = 5,
    ) -> list[tuple[str, FactorVerdict]]:
        """Evaluate all factors and return them sorted by ICIR descending.

        Parameters
        ----------
        bars:              OHLCV DataFrame.
        fwd_horizon_days:  Forward-return horizon.

        Returns
        -------
        List of (factor_id, FactorVerdict) tuples, sorted by icir desc.
        """
        verdicts = self.evaluate_all(bars, fwd_horizon_days=fwd_horizon_days)

        def _icir_key(item: tuple[str, FactorVerdict]) -> float:
            icir = item[1].ic_panel.get("icir", float("-inf"))
            if icir != icir:  # NaN
                return float("-inf")
            return float(icir)

        return sorted(verdicts.items(), key=_icir_key, reverse=True)

    def latest_verdict(self, factor_id: str) -> FactorVerdict | None:
        """Return the latest persisted verdict for *factor_id*, or None."""
        return self._latest_verdict(factor_id)
