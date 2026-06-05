"""Offline gate for promoting ResearchDebate dissent quality (B41-e).

This module is advisory-plane / eval-only. It proves, over a fixed offline
corpus, that debate-ON improves downstream realized decision quality versus the
legacy committee:

    r_debate = debate_action * realized_forward_return
    r_legacy = legacy_committee_action * realized_forward_return
    d        = r_debate - r_legacy

It flips no flags. In particular, ``HERMES_QUANT_RESEARCH_DEBATE`` and
``HERMES_QUANT_REDTEAM_TURN`` remain operator-controlled and default-OFF.

The shape mirrors ``llm_beats_fallback_gate``: five criteria, all must pass,
with explicit finite guards so a non-finite input cannot slip past a threshold
comparison.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from hermes_quant.eval.llm_beats_fallback_gate import (
    CriterionResult,
    GateVerdict,
    _downside_deviation,
    _lag1_autocovariance,
    _max_drawdown,
    _round,
)
from hermes_quant.evaluation.cv import PurgedWalkForward
from hermes_quant.evaluation.dsr import deflated_sharpe
from hermes_quant.evaluation.lookahead import shuffle_timestamps_test

EFFECT_REAL = "EFFECT_REAL"
REGIME_BREADTH = "REGIME_BREADTH"
OOS_REPRODUCIBLE = "OOS_REPRODUCIBLE"
CONTAMINATION_CLEAN = "CONTAMINATION_CLEAN"
HARMLESS = "HARMLESS"

_DRAWDOWN_REGIME = "drawdown"
_AXIS = "research_debate_dissent"
DEFAULT_ALLOWED_ACTIONS: tuple[float, ...] = (
    -0.20,
    -0.15,
    -0.10,
    -0.05,
    0.0,
    0.05,
    0.10,
    0.15,
    0.20,
)


@dataclass(frozen=True)
class DebateDissentEpisode:
    """One offline decision-quality observation for the debate promotion gate.

    ``debate_action`` and ``legacy_committee_action`` are signed NAV fractions
    on the deterministic sizing ladder by default. ``debate_confidence`` and
    ``legacy_committee_confidence`` are included because non-finite confidences
    are corpus corruption even though confidence is not itself a sizing lever.
    """

    asof: pd.Timestamp
    observable_asof: pd.Timestamp
    regime: str
    debate_action: float
    legacy_committee_action: float
    realized_forward_return: float
    debate_confidence: float
    legacy_committee_confidence: float
    knowledge_cutoff: pd.Timestamp | None = None


@dataclass(frozen=True)
class DebateDissentGateConfig:
    """Conservative, fail-closed thresholds for ``DebateDissentGate``."""

    dsr_floor: float = 0.95
    min_beaten_regimes: int = 2
    require_drawdown_regime: bool = True
    fold_rate_floor: float = 0.60
    n_splits: int = 5
    shuffle_alpha: float = 0.05
    n_shuffles: int = 199
    shuffle_seed: int = 42
    downside_dev_tol: float = 0.10
    min_observations: int = 30
    allowed_actions: tuple[float, ...] = DEFAULT_ALLOWED_ACTIONS


class DebateDissentGate:
    """PromotionGate-shaped verdict for ResearchDebate dissent quality."""

    def __init__(self, config: DebateDissentGateConfig | None = None) -> None:
        self.config = config or DebateDissentGateConfig()

    def check(self, episodes: list[DebateDissentEpisode]) -> GateVerdict:
        """Evaluate the corpus and return an advisory verdict.

        Empty, malformed, non-finite, or domain-invalid corpora return a failing
        verdict rather than raising. This is an offline gate; a bad measurement
        is not evidence for promotion.
        """
        s = self._series(episodes)
        criteria = [
            self._effect_real(s),
            self._regime_breadth(s),
            self._oos_reproducible(s),
            self._contamination_clean(s),
            self._harmless(s),
        ]
        reasons = [f"{c.name}: {c.reason}" for c in criteria if not c.passed]
        passed = not reasons

        d = s["d"]
        mean_delta = float(d.mean()) if d.size and s["finite_ok"] else float("nan")
        metrics = {
            "axis": _AXIS,
            "n_episodes": int(d.size),
            "mean_delta": _round(mean_delta),
            "finite_ok": bool(s["finite_ok"]),
            "input_violations": tuple(s["input_violations"]),
        }

        if passed:
            suggested_action = (
                "All 5 criteria passed: debate-ON beats the legacy committee on "
                "realized downstream decision quality OOS, across regimes including "
                "drawdown, with a real DSR-backed effect, clean contamination checks, "
                "and no excess risk. Advisory only: do not auto-flip any flag."
            )
        elif len(reasons) == 1:
            suggested_action = (
                f"One criterion failed ({reasons[0]}). ResearchDebate dissent is "
                "not cleared for production default; keep the flags shadow-only/OFF."
            )
        else:
            suggested_action = (
                f"{len(reasons)} criteria failed. ResearchDebate dissent is not "
                "cleared for production default; keep the flags shadow-only/OFF."
            )

        return GateVerdict(
            axis=_AXIS,
            passed=passed,
            reasons=reasons,
            suggested_action=suggested_action,
            metrics=metrics,
            criteria=criteria,
        )

    def _series(self, episodes: list[DebateDissentEpisode]) -> dict:
        ordered = sorted(episodes, key=lambda e: e.asof)
        violations = self._input_violations(ordered)
        debate = np.array([e.debate_action for e in ordered], dtype=float)
        legacy = np.array([e.legacy_committee_action for e in ordered], dtype=float)
        ret = np.array([e.realized_forward_return for e in ordered], dtype=float)
        r_debate = debate * ret
        r_legacy = legacy * ret
        return {
            "ordered": ordered,
            "debate": debate,
            "legacy": legacy,
            "ret": ret,
            "r_debate": r_debate,
            "r_legacy": r_legacy,
            "d": r_debate - r_legacy,
            "timestamps": [e.asof for e in ordered],
            "regimes": [e.regime for e in ordered],
            "finite_ok": not violations,
            "input_violations": violations,
        }

    def _input_violations(self, ordered: list[DebateDissentEpisode]) -> list[str]:
        cfg = self.config
        violations: list[str] = []

        if cfg.min_observations < 1:
            violations.append("min_observations must be >= 1")
        if cfg.n_splits < 1:
            violations.append("n_splits must be >= 1")
        if cfg.n_shuffles < 1:
            violations.append("n_shuffles must be >= 1")
        for name, value in (
            ("dsr_floor", cfg.dsr_floor),
            ("fold_rate_floor", cfg.fold_rate_floor),
            ("shuffle_alpha", cfg.shuffle_alpha),
            ("downside_dev_tol", cfg.downside_dev_tol),
        ):
            if not _is_finite_float(value):
                violations.append(f"{name} is not finite")

        allowed = {float(a) for a in cfg.allowed_actions}
        if not allowed or any(not math.isfinite(a) for a in allowed):
            violations.append("allowed_actions must be a non-empty finite ladder")

        for i, ep in enumerate(ordered):
            if pd.isna(ep.asof):
                violations.append(f"episode {i}: asof is NaT/NaN")
            if pd.isna(ep.observable_asof):
                violations.append(f"episode {i}: observable_asof is NaT/NaN")
            if ep.knowledge_cutoff is not None and pd.isna(ep.knowledge_cutoff):
                violations.append(f"episode {i}: knowledge_cutoff is NaT/NaN")
            if not isinstance(ep.regime, str) or not ep.regime.strip():
                violations.append(f"episode {i}: regime is empty")
            for name, value in (
                ("debate_action", ep.debate_action),
                ("legacy_committee_action", ep.legacy_committee_action),
                ("realized_forward_return", ep.realized_forward_return),
                ("debate_confidence", ep.debate_confidence),
                ("legacy_committee_confidence", ep.legacy_committee_confidence),
            ):
                if not _is_finite_float(value):
                    violations.append(f"episode {i}: {name} is not finite")
            if _is_finite_float(ep.debate_action) and float(ep.debate_action) not in allowed:
                violations.append(
                    f"episode {i}: debate_action={ep.debate_action} is outside "
                    "the discrete action ladder"
                )
            if _is_finite_float(ep.legacy_committee_action) and (
                float(ep.legacy_committee_action) not in allowed
            ):
                violations.append(
                    f"episode {i}: legacy_committee_action={ep.legacy_committee_action} "
                    "is outside the discrete action ladder"
                )
            for name, value in (
                ("debate_confidence", ep.debate_confidence),
                ("legacy_committee_confidence", ep.legacy_committee_confidence),
            ):
                if _is_finite_float(value) and not (0.0 <= float(value) <= 1.0):
                    violations.append(f"episode {i}: {name}={value} is outside [0, 1]")

        return violations

    def _input_failure(self, name: str, s: dict) -> CriterionResult | None:
        if s["finite_ok"]:
            return None
        return CriterionResult(
            name,
            False,
            "corpus/config contains non-finite or domain-invalid inputs; "
            "refusing to promote on a degenerate measurement",
            {
                "finite_ok": False,
                "input_violations": tuple(s["input_violations"]),
            },
        )

    def _effect_real(self, s: dict) -> CriterionResult:
        if (failed := self._input_failure(EFFECT_REAL, s)) is not None:
            return failed

        cfg = self.config
        d = s["d"]
        n = d.size
        mean_d = float(d.mean()) if n else float("nan")

        if n < cfg.min_observations:
            return CriterionResult(
                EFFECT_REAL,
                False,
                f"only {n} episodes < min_observations={cfg.min_observations}: "
                "insufficient power for a Deflated-Sharpe false-discovery hedge",
                {"n": n, "mean_delta": _round(mean_d), "deflated_sharpe": None},
            )

        std = float(d.std(ddof=1))
        if not math.isfinite(mean_d) or not math.isfinite(std):
            return CriterionResult(
                EFFECT_REAL,
                False,
                "mean/std(delta) is not finite; refusing to compare against thresholds",
                {"n": n, "mean_delta": _round(mean_d), "std_delta": _round(std)},
            )

        if std == 0.0:
            dsr = 1.0 if mean_d > 0 else 0.0
            sharpe = math.inf if mean_d > 0 else (-math.inf if mean_d < 0 else 0.0)
        else:
            sharpe = mean_d / std
            try:
                dsr = deflated_sharpe(
                    observed_sharpe=sharpe,
                    n_trials=1,
                    n_observations=n,
                    skew=_sample_skew(d),
                    kurtosis=_sample_kurtosis(d),
                )
            except (ValueError, ZeroDivisionError):
                dsr = float("nan")

        metrics = {
            "n": n,
            "mean_delta": _round(mean_d),
            "per_obs_sharpe": _round(sharpe),
            "deflated_sharpe": _round(dsr),
            "dsr_floor": cfg.dsr_floor,
        }
        if not (math.isfinite(mean_d) and mean_d > 0):
            return CriterionResult(
                EFFECT_REAL,
                False,
                f"mean(delta)={mean_d:+.5f} is not finite and > 0; debate-ON "
                "does not beat the legacy committee on average realized quality",
                metrics,
            )
        if not (math.isfinite(dsr) and math.isfinite(cfg.dsr_floor) and dsr >= cfg.dsr_floor):
            return CriterionResult(
                EFFECT_REAL,
                False,
                f"deflated_sharpe(delta)={dsr:.4f} < floor={cfg.dsr_floor:.4f}; "
                "the useful-dissent edge is not effect-real",
                metrics,
            )
        return CriterionResult(EFFECT_REAL, True, "", metrics)

    def _regime_breadth(self, s: dict) -> CriterionResult:
        if (failed := self._input_failure(REGIME_BREADTH, s)) is not None:
            return failed

        cfg = self.config
        order: list[str] = []
        buckets: dict[str, list[float]] = {}
        for regime, delta in zip(s["regimes"], s["d"], strict=True):
            if regime not in buckets:
                buckets[regime] = []
                order.append(regime)
            buckets[regime].append(float(delta))

        per_regime_mean = {regime: float(np.mean(buckets[regime])) for regime in order}
        beaten = [
            regime
            for regime in order
            if math.isfinite(per_regime_mean[regime]) and per_regime_mean[regime] > 0
        ]
        metrics = {
            "per_regime_mean_delta": {r: _round(per_regime_mean[r]) for r in order},
            "beaten_regimes": beaten,
            "min_beaten_regimes": cfg.min_beaten_regimes,
            "drawdown_beaten": _DRAWDOWN_REGIME in beaten,
            "drawdown_present": _DRAWDOWN_REGIME in order,
        }
        if len(beaten) < cfg.min_beaten_regimes:
            return CriterionResult(
                REGIME_BREADTH,
                False,
                f"debate-ON beats the legacy committee in only {len(beaten)} "
                f"regime(s) {beaten} < required {cfg.min_beaten_regimes}",
                metrics,
            )
        if cfg.require_drawdown_regime and _DRAWDOWN_REGIME not in beaten:
            detail = (
                "drawdown is present but not beaten"
                if _DRAWDOWN_REGIME in order
                else "no drawdown regime is present"
            )
            return CriterionResult(
                REGIME_BREADTH,
                False,
                "debate-ON does not beat the legacy committee in the drawdown "
                f"regime ({detail})",
                metrics,
            )
        return CriterionResult(REGIME_BREADTH, True, "", metrics)

    def _oos_reproducible(self, s: dict) -> CriterionResult:
        if (failed := self._input_failure(OOS_REPRODUCIBLE, s)) is not None:
            return failed

        cfg = self.config
        df = pd.DataFrame({"timestamp": s["timestamps"], "_d": s["d"]})
        try:
            splits = list(PurgedWalkForward(n_splits=cfg.n_splits).split(df))
        except ValueError as exc:
            return CriterionResult(
                OOS_REPRODUCIBLE,
                False,
                f"walk-forward could not run ({exc}); cannot prove OOS reproducibility",
                {"n": int(df.shape[0]), "n_splits": cfg.n_splits, "fold_rate": None},
            )

        beats = 0
        total = 0
        for sp in splits:
            mask = (df["timestamp"] >= sp.test_start) & (df["timestamp"] <= sp.test_end)
            fold = df.loc[mask, "_d"]
            if fold.empty:
                continue
            total += 1
            fold_mean = float(fold.mean())
            if math.isfinite(fold_mean) and fold_mean > 0:
                beats += 1
        fold_rate = beats / total if total else float("nan")
        metrics = {
            "fold_rate": _round(fold_rate),
            "beats": beats,
            "folds": total,
            "fold_rate_floor": cfg.fold_rate_floor,
        }
        if not (total > 0 and math.isfinite(fold_rate) and fold_rate >= cfg.fold_rate_floor):
            return CriterionResult(
                OOS_REPRODUCIBLE,
                False,
                f"out-of-sample fold-rate={fold_rate if total else float('nan'):.4f} "
                f"< floor={cfg.fold_rate_floor:.4f} ({beats}/{total} folds beat "
                "legacy); useful dissent does not reproduce OOS",
                metrics,
            )
        return CriterionResult(OOS_REPRODUCIBLE, True, "", metrics)

    def _contamination_clean(self, s: dict) -> CriterionResult:
        if (failed := self._input_failure(CONTAMINATION_CLEAN, s)) is not None:
            return failed

        cfg = self.config
        ordered = s["ordered"]
        structural_ok = True
        structural_reason: str | None = None
        for ep in ordered:
            if not (ep.observable_asof > ep.asof):
                structural_ok = False
                structural_reason = (
                    f"episode asof={ep.asof.isoformat()} has observable_asof="
                    f"{ep.observable_asof.isoformat()} not strictly after it"
                )
                break

        knowledge_ok = True
        knowledge_reason: str | None = None
        for ep in ordered:
            if ep.knowledge_cutoff is not None and not (ep.observable_asof > ep.knowledge_cutoff):
                knowledge_ok = False
                knowledge_reason = (
                    f"episode asof={ep.asof.isoformat()} realized at "
                    f"{ep.observable_asof.isoformat()} at-or-before "
                    f"knowledge_cutoff={ep.knowledge_cutoff.isoformat()}"
                )
                break

        shuffle_p = float("nan")
        shuffle_real = float("nan")
        statistical_ok = False
        if s["d"].size >= 3 and math.isfinite(cfg.shuffle_alpha):
            frame = pd.DataFrame(
                {
                    "timestamp": pd.to_datetime(s["timestamps"], utc=True),
                    "_delta": s["d"],
                }
            )
            res = shuffle_timestamps_test(
                lambda f: _lag1_autocovariance(f["_delta"].values),
                frame,
                n_shuffles=cfg.n_shuffles,
                alpha=cfg.shuffle_alpha,
                seed=cfg.shuffle_seed,
            )
            shuffle_p = float(res.p_value)
            shuffle_real = float(res.real_score)
            statistical_ok = math.isfinite(shuffle_p) and shuffle_p <= cfg.shuffle_alpha

        metrics = {
            "structural_ok": structural_ok,
            "knowledge_cutoff_ok": knowledge_ok,
            "statistical_ok": statistical_ok,
            "shuffle_p_value": _round(shuffle_p),
            "shuffle_alpha": cfg.shuffle_alpha,
            "shuffle_real_autocov": _round(shuffle_real),
            "n_shuffles": cfg.n_shuffles,
        }
        if not structural_ok:
            return CriterionResult(
                CONTAMINATION_CLEAN,
                False,
                f"structural look-ahead: {structural_reason}",
                metrics,
            )
        if not knowledge_ok:
            return CriterionResult(
                CONTAMINATION_CLEAN,
                False,
                f"knowledge-cutoff contamination: {knowledge_reason}",
                metrics,
            )
        if not statistical_ok:
            return CriterionResult(
                CONTAMINATION_CLEAN,
                False,
                f"shuffle p_value={shuffle_p:.4f} > alpha={cfg.shuffle_alpha:.4f}; "
                "the edge is scattered/oracle-like rather than regime-contiguous",
                metrics,
            )
        return CriterionResult(CONTAMINATION_CLEAN, True, "", metrics)

    def _harmless(self, s: dict) -> CriterionResult:
        if (failed := self._input_failure(HARMLESS, s)) is not None:
            return failed

        cfg = self.config
        r_debate = s["r_debate"]
        r_legacy = s["r_legacy"]
        dd_debate = _downside_deviation(r_debate)
        dd_legacy = _downside_deviation(r_legacy)
        if dd_legacy == 0.0:
            ratio = 1.0 if dd_debate == 0.0 else math.inf
        else:
            ratio = dd_debate / dd_legacy
        mdd_debate = _max_drawdown(r_debate)
        mdd_legacy = _max_drawdown(r_legacy)
        metrics = {
            "downside_dev_debate": _round(dd_debate),
            "downside_dev_legacy": _round(dd_legacy),
            "downside_dev_ratio": _round(ratio),
            "downside_dev_tol": cfg.downside_dev_tol,
            "debate_max_drawdown": _round(mdd_debate),
            "legacy_max_drawdown": _round(mdd_legacy),
        }

        dev_trips = (
            not math.isfinite(ratio)
            or not math.isfinite(cfg.downside_dev_tol)
            or ratio > 1.0 + cfg.downside_dev_tol
        )
        drawdown_trips = (
            not math.isfinite(mdd_debate)
            or not math.isfinite(mdd_legacy)
            or mdd_debate < mdd_legacy
        )
        if dev_trips or drawdown_trips:
            legs: list[str] = []
            if dev_trips:
                legs.append(
                    f"downside-deviation ratio={ratio:.3f} > "
                    f"1+tol={1.0 + cfg.downside_dev_tol:.3f}"
                )
            if drawdown_trips:
                legs.append(
                    f"debate max-drawdown={mdd_debate:+.5f} worse than "
                    f"legacy={mdd_legacy:+.5f}"
                )
            return CriterionResult(
                HARMLESS,
                False,
                "edge bought with excess risk: " + "; ".join(legs),
                metrics,
            )
        return CriterionResult(HARMLESS, True, "", metrics)


def _is_finite_float(value: float) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _sample_skew(x: np.ndarray) -> float:
    n = x.size
    if n < 3:
        return 0.0
    mean = x.mean()
    sd = x.std(ddof=0)
    if sd == 0.0:
        return 0.0
    return float(np.mean(((x - mean) / sd) ** 3))


def _sample_kurtosis(x: np.ndarray) -> float:
    n = x.size
    if n < 4:
        return 3.0
    mean = x.mean()
    sd = x.std(ddof=0)
    if sd == 0.0:
        return 3.0
    return float(np.mean(((x - mean) / sd) ** 4))
