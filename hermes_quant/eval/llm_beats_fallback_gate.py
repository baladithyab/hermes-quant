"""hermes_quant.eval.llm_beats_fallback_gate — Gate-3 keystone (lane W2B, B41-b).

The OFFLINE, DETERMINISTIC eval gate that proves an LLM decision stage actually
beats its deterministic fallback on REALIZED decision quality over a fixed
corpus. This is the "Gate 3" that ADR-4665 §7.2 flags as ABSENT for every LLM
decision stage.

POSTURE (hard constraints — this module is advisory-plane / eval-only):
  * It does NOT change any decision path and does NOT flip any flag. It returns a
    :class:`GateVerdict` (pass/fail per axis) a human reads. Default state: the
    gates exist but nothing auto-promotes. The deterministic risk gate (ADR-0004)
    remains the final authority.
  * It is ADDITIVE — it lives BESIDE ``promotion_gate.py`` (which it deliberately
    does not modify) and is imported by other code; nothing here mutates the
    existing promotion machinery.
  * It is DETERMINISTIC + REPRODUCIBLE — same corpus → same verdict. No
    wall-clock, no network, no RNG without a pinned seed (the only stochastic
    primitive, ``lookahead.shuffle_timestamps_test``, is driven with a fixed
    seed). All times are UTC.
  * No look-ahead: the contamination guard is part of the gate's own contract.

It REUSES the existing evaluation primitives rather than reinventing them:
  * ``evaluation.dsr.deflated_sharpe``      — EFFECT_REAL (false-discovery hedge).
  * ``evaluation.cv.PurgedWalkForward``     — OOS_REPRODUCIBLE (walk-forward
                                              fold-rate, mirroring promotion_gate's
                                              ``positive_excess_fold_rate`` floor).
  * ``evaluation.lookahead.shuffle_timestamps_test`` — CONTAMINATION_CLEAN (the
                                              statistical leg of the look-ahead
                                              guard).

THE UNIFYING MODEL
==================
Every episode carries the LLM action, the deterministic fallback's action (same
units), and the realized forward return that BOTH would have earned. Realized
decision quality is ``action * realized_forward_return``:

    r_llm[i] = llm_action[i] * ret[i]
    r_fb[i]  = fallback_action[i] * ret[i]
    d[i]     = r_llm[i] - r_fb[i]          (the delta series the gate scores)

A RiskCommittee approval ∈ {0, 1} (approve earns the trade's forward return;
reject = 0 = silence = flat). A Trader position ∈ [-1, 1] (signed exposure). ONE
engine scores both; the two axes (:class:`RiskCommitteeAxis`,
:class:`TraderAxis`) are thin wrappers that differ ONLY in (i) the label, (ii)
action-domain validation, and (iii) — committee only — an approval-precision
read-out. The criteria all operate on ``d``.

THE FIVE CRITERIA (ALL must pass — fail-closed)
===============================================
1. EFFECT_REAL         ``mean(d) > 0`` AND ``deflated_sharpe(d) >= dsr_floor``.
2. REGIME_BREADTH      LLM beats fallback (per-regime ``mean(d) > 0``) in
                       ``>= min_beaten_regimes`` regimes INCLUDING the drawdown
                       regime.
3. OOS_REPRODUCIBLE    ``PurgedWalkForward`` over the episodes; the fraction of
                       OOS test folds whose ``mean(d) > 0`` is ``>= fold_rate_floor``.
4. CONTAMINATION_CLEAN structural ``observable_asof`` STRICTLY ``>`` ``asof`` for
                       every episode; the realized outcome lies AFTER any LLM
                       ``knowledge_cutoff``; AND a shuffle test on the lag-1
                       autocovariance of ``d`` reads CLEAN.
5. HARMLESS            the edge is not bought with excess risk: the LLM's
                       downside-deviation ratio is within tolerance AND its
                       max-drawdown is not materially worse than the fallback's.

THE CONTAMINATION POLARITY (two load-bearing inversions — read carefully)
=========================================================================
``lookahead.shuffle_timestamps_test`` permutes the ``timestamp`` column and
re-sorts, so feeding it a ``{timestamp, delta}`` frame is a permutation test on
the TEMPORAL ORDERING of ``d``. Its ``.passed`` property is ``p_value > alpha``
— the INVERSE of ``validation.py``'s ``significant = p_value <= alpha`` — and the
canonical no-lookahead CI gate itself does not trust ``.passed`` (it calls it
flaky and asserts only structural fields). So we IGNORE ``.passed`` and read
``p_value`` directly under one documented convention:

    CLEAN  ⟺  shuffle p_value <= alpha

The score function is the LAG-1 AUTOCOVARIANCE of ``d`` (NOT validation.py's
``_timing_pnl`` sign-of-previous statistic). Rationale:
  * A GENUINE regime edge is temporally CONTIGUOUS — the LLM beats the fallback
    through a whole drawdown block — so ``d`` is a clustered run with HIGH lag-1
    autocovariance. Shuffling the order collapses it → the real score is a strong
    high outlier → small p → CLEAN.
  * A MEMORIZED / oracle edge is SCATTERED — the LLM "magically" dodges losers on
    i.i.d.-random episodes — so ``d``'s positive mass has NO temporal structure →
    autocovariance ≈ 0 → shuffle-invariant → large p → CONTAMINATION fires.
  * ``_timing_pnl`` would be wrong here: a genuine all-positive clustered edge is
    sign-uniform, hence ORDER-INVARIANT under that sign-of-prev statistic, and
    would FALSE-fire contamination. Autocovariance keys on magnitude CLUSTERING,
    which is exactly the genuine-vs-memorized discriminator.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from hermes_quant.evaluation.cv import PurgedWalkForward
from hermes_quant.evaluation.dsr import deflated_sharpe
from hermes_quant.evaluation.lookahead import shuffle_timestamps_test

# ---------------------------------------------------------------------------
# Criterion name constants (stable string keys — other code matches on these)
# ---------------------------------------------------------------------------
EFFECT_REAL = "EFFECT_REAL"
REGIME_BREADTH = "REGIME_BREADTH"
OOS_REPRODUCIBLE = "OOS_REPRODUCIBLE"
CONTAMINATION_CLEAN = "CONTAMINATION_CLEAN"
HARMLESS = "HARMLESS"

_DRAWDOWN_REGIME = "drawdown"


# ---------------------------------------------------------------------------
# Episode — the unit of the corpus
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Episode:
    """One logged decision episode for an LLM stage and its deterministic fallback.

    Attributes:
        asof: Decision time (UTC). The instant the stage produced its action.
        observable_asof: When the realized forward return became observable (UTC).
            MUST be STRICTLY after ``asof`` — that gap is the forward horizon, and
            an at-or-before value is a structural look-ahead (the contamination
            guard fails the whole corpus on it).
        regime: Market-regime label for this episode (e.g. "trend", "drawdown").
            REGIME_BREADTH requires the LLM to beat the fallback in the drawdown
            regime plus at least one other.
        llm_action: The LLM stage's action. Committee approval ∈ {0,1}; trader
            position ∈ [-1,1]. (The owning axis validates the domain.)
        fallback_action: The deterministic fallback's action, SAME UNITS as
            ``llm_action`` (so ``d`` is a like-for-like decision-quality delta).
        realized_forward_return: The return BOTH actions would have earned over
            the forward horizon. Signed; the realized outcome of the decision.
        knowledge_cutoff: Optional LLM training-knowledge cutoff (UTC). When set,
            the realized outcome (``observable_asof``) MUST lie strictly after it
            — otherwise the model may have trained on the answer (contamination).
    """

    asof: pd.Timestamp
    observable_asof: pd.Timestamp
    regime: str
    llm_action: float
    fallback_action: float
    realized_forward_return: float
    knowledge_cutoff: pd.Timestamp | None = None


# ---------------------------------------------------------------------------
# Config — fail-closed conservative defaults, all overridable
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GateConfig:
    """Thresholds for the gate. Defaults are CONSERVATIVE / fail-closed.

    Attributes:
        dsr_floor: Minimum Deflated-Sharpe of ``d`` for EFFECT_REAL (default 0.95
            — high confidence the edge is not a false discovery). Inclusive.
        min_beaten_regimes: Minimum count of regimes the LLM must beat for
            REGIME_BREADTH (default 2). The drawdown regime must always be among
            them (see ``require_drawdown_regime``).
        require_drawdown_regime: When True (default) REGIME_BREADTH additionally
            requires the drawdown regime to be beaten — drawdown survival is the
            load-bearing case for money-software.
        fold_rate_floor: Minimum fraction of walk-forward OOS folds whose
            ``mean(d) > 0`` for OOS_REPRODUCIBLE (default 0.60 — mirrors
            ``promotion_gate``'s ``positive_excess_fold_rate`` floor). Inclusive.
        n_splits: Walk-forward fold count (default 5, matching PurgedWalkForward).
        shuffle_alpha: Significance threshold for the contamination shuffle test
            (default 0.05). CLEAN ⟺ ``p_value <= shuffle_alpha``.
        n_shuffles: Shuffle count for the contamination test (default 199). The
            minimum reachable p-value is ``1/(n_shuffles+1)``, so this must be
            large enough that ``1/(n_shuffles+1) <= shuffle_alpha`` (199 → 0.005).
        shuffle_seed: Pinned RNG seed for the shuffle test (determinism).
        downside_dev_tol: Tolerance on the LLM/fallback downside-deviation ratio
            for HARMLESS (default 0.10 — the LLM may carry at most 10% more
            downside deviation than the fallback). Inclusive of the tolerance.
        min_observations: Minimum episodes required to even attempt EFFECT_REAL's
            DSR (default 30, matching dsr.py's guard). Below it, EFFECT_REAL fails
            closed rather than raising.
    """

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


# ---------------------------------------------------------------------------
# Per-criterion + verdict result types (PromotionDecision-shaped)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CriterionResult:
    """The outcome of one of the five criteria."""

    name: str
    passed: bool
    reason: str  # human-readable; "" when passed
    metrics: dict = field(default_factory=dict)


@dataclass(frozen=True)
class GateVerdict:
    """PromotionDecision-shaped verdict for one axis.

    Attributes:
        axis: "risk_committee" | "trader".
        passed: True iff ALL five criteria passed.
        reasons: Human-readable strings, one per FAILED criterion (empty on pass).
        suggested_action: High-level recommendation for the operator.
        metrics: Flat dict of headline numbers (incl., for the committee axis, the
            approval-precision read-out).
        criteria: The full per-criterion breakdown (all five, pass or fail).
    """

    axis: str
    passed: bool
    reasons: list[str]
    suggested_action: str
    metrics: dict
    criteria: list[CriterionResult]

    @property
    def failed_criteria(self) -> list[str]:
        """Names of the criteria that failed, in evaluation order."""
        return [c.name for c in self.criteria if not c.passed]


# ---------------------------------------------------------------------------
# NaN-safe float helpers (determinism: repr must be byte-stable across runs)
# ---------------------------------------------------------------------------
def _round(x: float, ndigits: int = 10) -> float:
    """Round for a byte-stable repr; NaN/inf pass through unchanged.

    The verdict's ``repr`` is the determinism contract (tests compare
    ``repr(v1) == repr(v2)``). Rounding floats to a fixed precision pins any
    last-ULP nondeterminism in the underlying numpy reductions without changing
    a single pass/fail outcome (the thresholds are far from 1e-10 boundaries).
    """
    if x is None or not math.isfinite(x):
        return x
    return round(float(x), ndigits)


def _lag1_autocovariance(x: np.ndarray) -> float:
    """Lag-1 autocovariance of a 1-D series (population normalization, /n).

    The contamination score function. Keys on temporal magnitude CLUSTERING: a
    contiguous run of similar deltas (a genuine regime edge) yields a high value
    that collapses under a timestamp shuffle; a scattered (memorized) edge yields
    ≈ 0, which is shuffle-invariant. See the module docstring's polarity note.
    """
    x = np.asarray(x, dtype=float).ravel()
    if x.size < 3:
        return 0.0
    x = x - x.mean()
    return float(np.dot(x[:-1], x[1:]) / x.size)


def _downside_deviation(r: np.ndarray) -> float:
    """Root-mean-square of the negative part of a realized-return series."""
    r = np.asarray(r, dtype=float)
    if r.size == 0:
        return 0.0
    neg = np.minimum(r, 0.0)
    return float(np.sqrt(np.mean(neg**2)))


def _max_drawdown(r: np.ndarray) -> float:
    """Max drawdown of the additive equity curve of a per-episode return series.

    Returns a value <= 0 (0.0 = no drawdown). Additive (cumsum) rather than
    compounded — the per-episode returns are decision-quality increments, not a
    reinvested book, so the additive curve is the faithful risk read.
    """
    r = np.asarray(r, dtype=float)
    if r.size == 0:
        return 0.0
    equity = np.cumsum(r)
    peak = np.maximum.accumulate(equity)
    return float(np.min(equity - peak))


# ---------------------------------------------------------------------------
# The unified scoring engine
# ---------------------------------------------------------------------------
class _LLMBeatsFallbackEngine:
    """One engine that scores the five criteria on a corpus of episodes.

    The two public axes are thin wrappers over this; the engine itself is
    axis-agnostic (it works purely on ``d = r_llm - r_fb``).
    """

    def __init__(self, config: GateConfig | None = None) -> None:
        self.config = config or GateConfig()

    # -- the realized decision-quality series --------------------------------
    @staticmethod
    def _series(episodes: list[Episode]) -> dict:
        """Compute the realized decision-quality arrays from the corpus.

        Episodes are ordered by ``asof`` (UTC) so the delta series is in true
        decision order — the lag-1 autocovariance and walk-forward folds both
        depend on that ordering being chronological.
        """
        ordered = sorted(episodes, key=lambda e: e.asof)
        ret = np.array([e.realized_forward_return for e in ordered], dtype=float)
        llm = np.array([e.llm_action for e in ordered], dtype=float)
        fb = np.array([e.fallback_action for e in ordered], dtype=float)
        r_llm = llm * ret
        r_fb = fb * ret
        return {
            "ordered": ordered,
            "ret": ret,
            "llm": llm,
            "fb": fb,
            "r_llm": r_llm,
            "r_fb": r_fb,
            "d": r_llm - r_fb,
            "timestamps": [e.asof for e in ordered],
            "regimes": [e.regime for e in ordered],
        }

    # -- sample moments (numpy-only; mirror validation.py's definitions) -----
    @staticmethod
    def _sample_skew(x: np.ndarray) -> float:
        """Sample skewness (population definition). 0.0 when n<3 or zero-variance."""
        n = x.size
        if n < 3:
            return 0.0
        m = x.mean()
        sd = x.std(ddof=0)
        if sd == 0:
            return 0.0
        return float(np.mean(((x - m) / sd) ** 3))

    @staticmethod
    def _sample_kurtosis(x: np.ndarray) -> float:
        """Sample kurtosis, NON-excess (normal == 3.0), matching dsr.py's param.
        3.0 when n<4 or zero-variance."""
        n = x.size
        if n < 4:
            return 3.0
        m = x.mean()
        sd = x.std(ddof=0)
        if sd == 0:
            return 3.0
        return float(np.mean(((x - m) / sd) ** 4))

    # -- criterion 1: EFFECT_REAL --------------------------------------------
    def _effect_real(self, s: dict) -> CriterionResult:
        d = s["d"]
        n = d.size
        mean_d = float(d.mean()) if n else float("nan")
        cfg = self.config

        if n < cfg.min_observations:
            return CriterionResult(
                EFFECT_REAL,
                False,
                f"only {n} episodes < min_observations={cfg.min_observations}: "
                "insufficient power for a Deflated-Sharpe false-discovery hedge",
                {"n": n, "mean_delta": _round(mean_d), "deflated_sharpe": None},
            )

        std = float(d.std(ddof=1))
        if std == 0.0:
            # No dispersion: a constant delta. A constant POSITIVE edge has an
            # infinite Sharpe (no downside variance) — treat as a pass on the DSR
            # leg; only the mean>0 leg gates. mean<=0 is caught below.
            dsr = 1.0 if mean_d > 0 else 0.0
            sharpe = math.inf if mean_d > 0 else (-math.inf if mean_d < 0 else 0.0)
        else:
            sharpe = mean_d / std
            skew = self._sample_skew(d)
            kurt = self._sample_kurtosis(d)  # non-excess (normal == 3.0)
            try:
                dsr = deflated_sharpe(
                    observed_sharpe=sharpe,
                    n_trials=1,
                    n_observations=n,
                    skew=skew,
                    kurtosis=kurt,
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

        if not (mean_d > 0):
            return CriterionResult(
                EFFECT_REAL,
                False,
                f"mean(delta)={mean_d:+.5f} is not > 0 — the LLM does not beat the "
                "fallback on average realized decision quality",
                metrics,
            )
        if math.isnan(dsr) or dsr < cfg.dsr_floor:
            return CriterionResult(
                EFFECT_REAL,
                False,
                f"deflated_sharpe(delta)={dsr:.4f} < floor={cfg.dsr_floor:.4f} — the "
                "edge is not statistically distinguishable from a false discovery",
                metrics,
            )
        return CriterionResult(EFFECT_REAL, True, "", metrics)

    # -- criterion 2: REGIME_BREADTH -----------------------------------------
    def _regime_breadth(self, s: dict) -> CriterionResult:
        cfg = self.config
        d = s["d"]
        regimes = s["regimes"]
        # Per-regime mean delta, in first-seen order for a stable report.
        order: list[str] = []
        buckets: dict[str, list[float]] = {}
        for rg, dd in zip(regimes, d, strict=True):
            if rg not in buckets:
                buckets[rg] = []
                order.append(rg)
            buckets[rg].append(float(dd))
        per_regime_mean = {rg: float(np.mean(buckets[rg])) for rg in order}
        beaten = [rg for rg in order if per_regime_mean[rg] > 0]

        metrics = {
            "per_regime_mean_delta": {rg: _round(per_regime_mean[rg]) for rg in order},
            "beaten_regimes": beaten,
            "min_beaten_regimes": cfg.min_beaten_regimes,
            "drawdown_beaten": _DRAWDOWN_REGIME in beaten,
            "drawdown_present": _DRAWDOWN_REGIME in order,
        }

        if len(beaten) < cfg.min_beaten_regimes:
            return CriterionResult(
                REGIME_BREADTH,
                False,
                f"LLM beats the fallback in only {len(beaten)} regime(s) "
                f"{beaten} < required {cfg.min_beaten_regimes}",
                metrics,
            )
        if cfg.require_drawdown_regime and _DRAWDOWN_REGIME not in beaten:
            present = _DRAWDOWN_REGIME in order
            detail = (
                "the drawdown regime is present but NOT beaten"
                if present
                else "no drawdown regime is present in the corpus at all"
            )
            return CriterionResult(
                REGIME_BREADTH,
                False,
                f"LLM does not beat the fallback in the drawdown regime — {detail}; "
                "drawdown survival is required",
                metrics,
            )
        return CriterionResult(REGIME_BREADTH, True, "", metrics)

    # -- criterion 3: OOS_REPRODUCIBLE ---------------------------------------
    def _oos_reproducible(self, s: dict) -> CriterionResult:
        cfg = self.config
        df = pd.DataFrame({"timestamp": s["timestamps"], "_d": s["d"]})
        try:
            splits = list(PurgedWalkForward(n_splits=cfg.n_splits).split(df))
        except ValueError as exc:
            # Too few episodes for the requested folds → fail closed, don't raise.
            return CriterionResult(
                OOS_REPRODUCIBLE,
                False,
                f"walk-forward could not run ({exc}); too few episodes for "
                f"{cfg.n_splits} out-of-sample folds — cannot prove reproducibility",
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
            if float(fold.mean()) > 0:
                beats += 1
        fold_rate = (beats / total) if total else float("nan")
        metrics = {
            "fold_rate": _round(fold_rate),
            "beats": beats,
            "folds": total,
            "fold_rate_floor": cfg.fold_rate_floor,
        }
        if total == 0 or math.isnan(fold_rate) or fold_rate < cfg.fold_rate_floor:
            return CriterionResult(
                OOS_REPRODUCIBLE,
                False,
                f"out-of-sample fold-rate={fold_rate if total else float('nan'):.4f} "
                f"< floor={cfg.fold_rate_floor:.4f} ({beats}/{total} folds beat the "
                "fallback) — the edge does not reproduce across walk-forward windows",
                metrics,
            )
        return CriterionResult(OOS_REPRODUCIBLE, True, "", metrics)

    # -- criterion 4: CONTAMINATION_CLEAN ------------------------------------
    def _contamination_clean(self, s: dict) -> CriterionResult:
        cfg = self.config
        ordered = s["ordered"]

        # (4a) Structural: observable_asof STRICTLY after asof for EVERY episode.
        structural_ok = True
        first_struct_violation: str | None = None
        for ep in ordered:
            if not (ep.observable_asof > ep.asof):
                structural_ok = False
                first_struct_violation = (
                    f"episode asof={ep.asof.isoformat()} has "
                    f"observable_asof={ep.observable_asof.isoformat()} not strictly "
                    "after it (forward outcome is not in the future of the decision)"
                )
                break

        # (4b) Knowledge cutoff: the realized outcome must lie AFTER the cutoff.
        knowledge_ok = True
        first_knowledge_violation: str | None = None
        for ep in ordered:
            if ep.knowledge_cutoff is not None and not (ep.observable_asof > ep.knowledge_cutoff):
                knowledge_ok = False
                first_knowledge_violation = (
                    f"episode asof={ep.asof.isoformat()} realized at "
                    f"{ep.observable_asof.isoformat()} is at-or-before its LLM "
                    f"knowledge_cutoff={ep.knowledge_cutoff.isoformat()} — the model "
                    "may have trained on the answer"
                )
                break

        # (4c) Statistical: shuffle test on the lag-1 autocovariance of d.
        # CLEAN ⟺ p_value <= alpha (see module docstring's polarity note). We read
        # p_value directly and DO NOT trust LookaheadTestResult.passed (whose
        # convention is the inverse and which the canonical CI gate calls flaky).
        shuffle_p = float("nan")
        shuffle_real = float("nan")
        statistical_ok = False
        d = s["d"]
        if d.size >= 3:
            frame = pd.DataFrame(
                {
                    "timestamp": pd.to_datetime(s["timestamps"], utc=True),
                    "_delta": d,
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
            statistical_ok = shuffle_p <= cfg.shuffle_alpha

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
                f"structural look-ahead — {first_struct_violation}",
                metrics,
            )
        if not knowledge_ok:
            return CriterionResult(
                CONTAMINATION_CLEAN,
                False,
                f"knowledge-cutoff contamination — {first_knowledge_violation}",
                metrics,
            )
        if not statistical_ok:
            return CriterionResult(
                CONTAMINATION_CLEAN,
                False,
                f"shuffle look-ahead test: p_value={shuffle_p:.4f} > "
                f"alpha={cfg.shuffle_alpha:.4f} — the realized edge is temporally "
                "UNSTRUCTURED (scattered), the signature of a memorized/oracle edge "
                "rather than a genuine regime-contiguous one",
                metrics,
            )
        return CriterionResult(CONTAMINATION_CLEAN, True, "", metrics)

    # -- criterion 5: HARMLESS -----------------------------------------------
    def _harmless(self, s: dict) -> CriterionResult:
        cfg = self.config
        r_llm = s["r_llm"]
        r_fb = s["r_fb"]

        dd_llm = _downside_deviation(r_llm)
        dd_fb = _downside_deviation(r_fb)
        # Ratio convention: 1.0 when both have zero downside (equally harmless);
        # +inf when the LLM has downside the fallback does not (strictly worse).
        if dd_fb == 0.0:
            ratio = 1.0 if dd_llm == 0.0 else math.inf
        else:
            ratio = dd_llm / dd_fb

        mdd_llm = _max_drawdown(r_llm)
        mdd_fb = _max_drawdown(r_fb)

        metrics = {
            "downside_dev_llm": _round(dd_llm),
            "downside_dev_fallback": _round(dd_fb),
            "downside_dev_ratio": _round(ratio),
            "downside_dev_tol": cfg.downside_dev_tol,
            "llm_max_drawdown": _round(mdd_llm),
            "fallback_max_drawdown": _round(mdd_fb),
        }

        # Strictest reading: either leg trips → fail.
        dev_trips = ratio > 1.0 + cfg.downside_dev_tol
        # max-drawdown is <= 0; "materially worse" = strictly more negative.
        dd_trips = mdd_llm < mdd_fb
        if dev_trips or dd_trips:
            legs = []
            if dev_trips:
                legs.append(
                    f"downside-deviation ratio={ratio:.3f} > 1+tol={1.0 + cfg.downside_dev_tol:.3f}"
                )
            if dd_trips:
                legs.append(f"LLM max-drawdown={mdd_llm:+.5f} worse than fallback={mdd_fb:+.5f}")
            return CriterionResult(
                HARMLESS,
                False,
                "edge bought with excess risk — " + "; ".join(legs),
                metrics,
            )
        return CriterionResult(HARMLESS, True, "", metrics)

    # -- orchestration -------------------------------------------------------
    def evaluate(self, episodes: list[Episode], *, axis: str, extra_metrics: dict) -> GateVerdict:
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

        metrics: dict = {
            "axis": axis,
            "n_episodes": int(s["d"].size),
            "mean_delta": _round(float(s["d"].mean()) if s["d"].size else float("nan")),
        }
        metrics.update(extra_metrics)

        if passed:
            suggested_action = (
                f"All 5 criteria passed on the {axis} axis: the LLM stage beats its "
                "deterministic fallback on realized decision quality, the edge is "
                "statistically real, reproduces out-of-sample across >=2 regimes "
                "(incl. drawdown), is contamination-clean, and is not bought with "
                "excess risk. ADVISORY ONLY — surface to the operator; this gate "
                "flips no flag and the deterministic risk gate remains final authority."
            )
        elif len(reasons) == 1:
            suggested_action = (
                f"One criterion failed on the {axis} axis ({reasons[0]}). The LLM "
                "stage is NOT cleared to beat its fallback; do not promote."
            )
        else:
            suggested_action = (
                f"{len(reasons)} criteria failed on the {axis} axis. The LLM stage is "
                "NOT cleared to beat its fallback; do not promote."
            )

        return GateVerdict(
            axis=axis,
            passed=passed,
            reasons=reasons,
            suggested_action=suggested_action,
            metrics=metrics,
            criteria=criteria,
        )


# ---------------------------------------------------------------------------
# The two thin axis wrappers
# ---------------------------------------------------------------------------
class _AxisBase:
    """Common machinery for the two axes. Differs only in label, action-domain
    validation, and (committee only) an approval-precision read-out."""

    axis_name: str = ""

    def __init__(self, config: GateConfig | None = None) -> None:
        self._engine = _LLMBeatsFallbackEngine(config)

    @property
    def config(self) -> GateConfig:
        return self._engine.config

    def _validate_domain(self, episodes: list[Episode]) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _extra_metrics(self, episodes: list[Episode]) -> dict:
        return {}

    def evaluate(self, episodes: list[Episode]) -> GateVerdict:
        if not episodes:
            raise ValueError(f"{self.axis_name} axis: empty corpus — nothing to evaluate")
        self._validate_domain(episodes)
        return self._engine.evaluate(
            episodes, axis=self.axis_name, extra_metrics=self._extra_metrics(episodes)
        )


class RiskCommitteeAxis(_AxisBase):
    """Approval-quality axis: does the LLM risk-committee's APPROVAL beat the
    deterministic risk gate's decisions out-of-sample?

    Action domain: approval ∈ {0, 1} (approve / reject). Reject = 0 = silence =
    flat = earns nothing — the silence-by-default posture (ADR-0004): a rejected
    trade simply doesn't happen, so it earns zero realized return.

    Adds an approval-precision read-out (LLM vs fallback): of the trades each side
    APPROVED, what fraction had a positive realized forward return. Read-out only
    — it does not change pass/fail.
    """

    axis_name = "risk_committee"

    def _validate_domain(self, episodes: list[Episode]) -> None:
        for ep in episodes:
            for who, a in (("llm", ep.llm_action), ("fallback", ep.fallback_action)):
                if a not in (0.0, 1.0):
                    raise ValueError(
                        f"risk_committee axis: {who}_action={a} is not a binary "
                        "approval ∈ {0, 1} (approve=1 / reject=0). A committee "
                        "decision must be binary; fractional actions belong to the "
                        "trader axis."
                    )

    def _extra_metrics(self, episodes: list[Episode]) -> dict:
        return {
            "llm_approval_precision": _round(
                self._approval_precision([e for e in episodes], side="llm")
            ),
            "fallback_approval_precision": _round(
                self._approval_precision([e for e in episodes], side="fallback")
            ),
        }

    @staticmethod
    def _approval_precision(episodes: list[Episode], *, side: str) -> float:
        """Fraction of APPROVED trades with a positive realized forward return.

        NaN when the side approved nothing (no trades to be precise about).
        """
        approved = [
            e for e in episodes if (e.llm_action if side == "llm" else e.fallback_action) == 1.0
        ]
        if not approved:
            return float("nan")
        wins = sum(1 for e in approved if e.realized_forward_return > 0)
        return wins / len(approved)


class TraderAxis(_AxisBase):
    """Proposal-quality axis: does the LLM trader's PROPOSAL beat the deterministic
    trader proposal out-of-sample?

    Action domain: signed position ∈ [-1, 1] (the discrete sizing ladder of
    ADR-0004 lives inside this interval; the axis only enforces the bound, not the
    ladder, so logged real proposals at any granularity replay). The committee's
    {0,1} domain is strictly inside this interval, so a committee corpus is also a
    valid trader corpus.
    """

    axis_name = "trader"

    def _validate_domain(self, episodes: list[Episode]) -> None:
        for ep in episodes:
            for who, a in (("llm", ep.llm_action), ("fallback", ep.fallback_action)):
                if not (-1.0 <= a <= 1.0) or math.isnan(a):
                    raise ValueError(
                        f"trader axis: {who}_action={a} is outside the position "
                        "range [-1, 1]. Continuous exposure must stay within the "
                        "signed unit interval."
                    )


__all__ = [
    "EFFECT_REAL",
    "REGIME_BREADTH",
    "OOS_REPRODUCIBLE",
    "CONTAMINATION_CLEAN",
    "HARMLESS",
    "Episode",
    "GateConfig",
    "CriterionResult",
    "GateVerdict",
    "RiskCommitteeAxis",
    "TraderAxis",
]
