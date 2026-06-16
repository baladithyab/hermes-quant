"""hermes_quant.factors.weight_proposer — W4 factor-verdict → candidate BMA-weight proposer.

SILENCE-ONLY, PROPOSE-ONLY (capability-map §4 W4 / ADR-0080 §D80.1, §D80.5).

Generalizes the catalyst/profitability.py seed (per-relation-class verdict → raise/prune)
to the whole factor surface: a FactorOracle 4-tier verdict (premium/standard/experimental/
rejected) maps to a CANDIDATE weight diff. premium↑ within WEIGHT_CAP; rejected→silence-toward-0.

The output is an ADVISORY-PLANE artifact only. Nothing in the trading hot path reads it.
Promotion to any live weight is the operator/eval-gated promotion action (ADR-0052), never this
module. Mirrors graph_mining.py honesty rails: PROPOSE only, never auto-mutate a curated
artifact, confidence multiplier silence-only (<= cap, never amplifies).

External-truth: the eval gate scores a proposed set on realized OOS DSR / walk-forward from
market bars (evaluation/dsr.py + backtest/walk_forward.py). Never an LLM self-score; never the
proposer's own verdict re-ingested as truth (ADR-0080 §D80.3).

This module reads NO environment variable and performs NO network I/O at import or call time;
the default-OFF flag (HERMES_QUANT_FACTOR_WEIGHT_PROPOSER) lives at the cron boundary only
(mirrors profitability.py, which is pure). It imports NONE of: risk.gate, governance.kill_switch,
or the discrete sizing ladder — it is structurally confined to the advisory plane.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# --- Hard caps (the silence-only rail; never widened by the loop) -----------
WEIGHT_CAP: float = 1.0          # a factor's candidate weight is clamped to [0, CAP]. NEVER amplifies above.
WEIGHT_FLOOR: float = 0.0        # rejected → silence-toward-0.
MAX_STEP_PER_CYCLE: float = 0.10  # bounded per-cycle change (SkillOpt textual learning-rate analog).
MIN_OBSERVATIONS: int = 30       # DSR is meaningless below this (dsr.py raises < 30); mirror it here.
# Absolute promotability floor on the held-out DSR (ADR-0080 §D80.3). The eval gate's
# checkpoint-fallback baseline is -inf on the first run (load_prior_best_dsr missing -> -inf),
# so STRICTLY-beats-prior-best alone admits ANY finite DSR — including a consistent LOSER whose
# n_trials=1 DSR underflows toward 0.0 (Φ of an extreme-negative z) and which is plateau-stable
# (a consistent loser has low cross-fold Sharpe CV + sign-consistent folds, so the robustness
# check does NOT reject it). 0.5 is the no-edge midpoint of the n_trials=1 PSR (Φ(0) for a
# zero-Sharpe set): a POSITIVE-Sharpe edge is required to clear it. Without this floor the cron
# would write advisory candidates for a guaranteed money-loser and ratchet the baseline to ~0.
MIN_PROMOTABLE_DSR: float = 0.5
# Tier → target weight (the proposal direction). premium gets the most headroom; rejected → 0.
_TIER_TARGET: dict[str, float] = {
    "premium": 1.00,
    "standard": 0.60,
    "experimental": 0.30,
    "rejected": 0.00,    # silence-toward-0
}

_DEFAULT_DIR = Path.home() / ".hermes" / "quant" / "factors"
_CANDIDATES_FILE = "weight-candidates.json"
_REJECTED_BUFFER = "weight-rejected-buffer.jsonl"
_PRIOR_BEST_FILE = "weight-prior-best.json"


@dataclass(frozen=True)
class FactorWeightProposal:
    factor_id: str
    current_weight: float
    proposed_weight: float
    verdict_tier: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "factor_id": self.factor_id,
            "current_weight": round(self.current_weight, 4),
            "proposed_weight": round(self.proposed_weight, 4),
            "verdict_tier": self.verdict_tier,
            "reason": self.reason,
        }


@dataclass
class FactorWeightProposalSet:
    proposals: list[FactorWeightProposal] = field(default_factory=list)
    generated_at: str = ""
    # eval-gate provenance — filled by evaluate_against_holdout, read by the operator/promotion gate.
    held_out_dsr: float | None = None
    held_out_sharpe_delta: float | None = None
    prior_best_dsr: float | None = None
    beats_prior_best: bool | None = None
    plateau_stable: bool | None = None
    eval_passed: bool = False

    def to_dict(self) -> dict:
        """Full serialization incl. eval provenance."""
        return {
            "generated_at": self.generated_at,
            "eval_passed": self.eval_passed,
            "held_out_dsr": self.held_out_dsr,
            "held_out_sharpe_delta": self.held_out_sharpe_delta,
            "prior_best_dsr": self.prior_best_dsr,
            "beats_prior_best": self.beats_prior_best,
            "plateau_stable": self.plateau_stable,
            "proposals": [p.to_dict() for p in self.proposals],
        }


def _clamp(w: float) -> float:
    """Silence-only clamp: [WEIGHT_FLOOR, WEIGHT_CAP]. NEVER returns > WEIGHT_CAP."""
    return max(WEIGHT_FLOOR, min(WEIGHT_CAP, w))


def _n_periods(verdict) -> int:
    """Read n_periods off a FactorVerdict (or a plain dict-ish shim) defensively."""
    panel = getattr(verdict, "ic_panel", None)
    if panel is None and isinstance(verdict, dict):
        panel = verdict.get("ic_panel")
    if isinstance(panel, dict):
        try:
            return int(panel.get("n_periods", 0) or 0)
        except (TypeError, ValueError):
            return 0
    # Fall back to an attribute if a panel object was supplied.
    n = getattr(panel, "n_periods", 0)
    try:
        return int(n or 0)
    except (TypeError, ValueError):
        return 0


def _tier(verdict) -> str:
    """Read the verdict tier off a FactorVerdict (or dict-ish shim)."""
    tier = getattr(verdict, "tier", None)
    if tier is None and isinstance(verdict, dict):
        tier = verdict.get("tier")
    return str(tier) if tier is not None else "rejected"


def propose_weights(
    verdicts: dict,                       # {factor_id: FactorVerdict} from FactorOracle.evaluate_all
    current_weights: dict[str, float] | None = None,
) -> FactorWeightProposalSet:
    """Map 4-tier verdicts → a CANDIDATE weight diff. Pure, deterministic, silence-only.

    For each factor: target = _TIER_TARGET[tier]; proposed = current + clamp(step toward target,
    |step| <= MAX_STEP_PER_CYCLE); then _clamp to [FLOOR, CAP]. rejected drives toward 0.
    A factor with n_periods < MIN_OBSERVATIONS is left at current_weight (INSUFFICIENT — no move),
    mirroring profitability.py INSUFFICIENT_SAMPLE.
    """
    cur = current_weights or {}
    proposals: list[FactorWeightProposal] = []
    for factor_id in sorted(verdicts):
        verdict = verdicts[factor_id]
        tier = _tier(verdict)
        current = float(cur.get(factor_id, 0.0))
        n_periods = _n_periods(verdict)

        if n_periods < MIN_OBSERVATIONS:
            # INSUFFICIENT_SAMPLE: no move; the live number isn't trustworthy yet.
            proposals.append(
                FactorWeightProposal(
                    factor_id=factor_id,
                    current_weight=current,
                    proposed_weight=_clamp(current),
                    verdict_tier=tier,
                    reason=(
                        f"insufficient observations (n_periods={n_periods} "
                        f"< MIN_OBSERVATIONS={MIN_OBSERVATIONS}); no move"
                    ),
                )
            )
            continue

        target = _TIER_TARGET.get(tier, 0.0)
        # Bounded step toward the tier target (SkillOpt textual learning-rate analog).
        delta = target - current
        step = max(-MAX_STEP_PER_CYCLE, min(MAX_STEP_PER_CYCLE, delta))
        proposed = _clamp(current + step)

        if tier == "rejected":
            reason = f"rejected verdict → silence-toward-0 (step {step:+.4f})"
        elif proposed > current:
            reason = f"{tier} verdict → raise toward {target:.2f} within cap (step {step:+.4f})"
        elif proposed < current:
            reason = f"{tier} verdict → reduce toward {target:.2f} (step {step:+.4f})"
        else:
            reason = f"{tier} verdict → already at target {target:.2f}; no move"

        proposals.append(
            FactorWeightProposal(
                factor_id=factor_id,
                current_weight=current,
                proposed_weight=proposed,
                verdict_tier=tier,
                reason=reason,
            )
        )

    return FactorWeightProposalSet(
        proposals=proposals,
        generated_at=datetime.now(UTC).isoformat(),
    )


# --- Held-out OOS scoring (external-truth: realized forward returns from market bars) --------
# The cron passes the OOS *tail* the proposer never saw, plus a `compute_factor` callable
# (AlphaZoo.compute). We build the PROPOSED-weight factor composite as a long/short position
# series, realize it against next-bar returns (signal at t → return t→t+1, no lookahead). The
# per-factor z-score normalization is CAUSAL/expanding (bar t uses only bars <= t — no
# within-holdout lookahead from a full-window mean/std), then score the OOS Sharpe → DSR, and read
# robustness from cross-fold Sharpe jitter (NOT the in-sample peak — the AMZN-weight lesson). This
# module performs NO network I/O; bars are supplied.
HOLDOUT_FOLDS: int = 4               # contiguous OOS sub-windows for the cross-fold jitter check.
# plateau_stable iff the RELATIVE cross-fold Sharpe dispersion (coefficient of variation =
# stdev/|mean|) is bounded AND a majority of folds keep the OOS sign. A relative cap (not an
# absolute one) is what robustness means here: a consistently-strong edge has large per-fold
# Sharpes but LOW relative dispersion, whereas a single-window spike has high dispersion — the
# AMZN-weight lesson (select on a stable plateau, never the in-sample peak).
PLATEAU_CV_MAX: float = 1.5
_ANNUALIZATION: float = 252.0 ** 0.5


def _composite_position(
    proposal_set: FactorWeightProposalSet,
    holdout_bars,
    compute_factor,
):
    """Build the PROPOSED-weight factor composite as a per-bar long/short position in [-1, 1].

    For each factor with a non-zero proposed weight: z-score its values with a CAUSAL/expanding
    normalization (bar t uses only bars <= t, never the full window — no within-holdout lookahead),
    weight by `proposed_weight`, and sum. The composite's sign is the position direction (positive
    factor predicts positive forward return — the IC convention in ic_panel.py). Returns a pd.Series
    aligned to holdout_bars, or None if nothing weighted.
    """
    import numpy as np
    import pandas as pd

    combined: pd.Series | None = None
    total_w = 0.0
    for p in proposal_set.proposals:
        w = float(p.proposed_weight)
        if w <= 0.0:
            continue  # silence-toward-0 factors contribute nothing
        try:
            series = compute_factor(p.factor_id, holdout_bars)
        except Exception:  # noqa: BLE001 — a single bad factor must not crash the OOS score
            continue
        series = pd.Series(series).astype(float)
        # CAUSAL (expanding) z-score: bar t is normalized using ONLY bars <= t. A full-window
        # mean/std would make bar t's z (and hence its np.sign() position at the return below)
        # depend on bars t+1..T — a within-holdout lookahead that contaminates the realized OOS
        # Sharpe → DSR → plateau that gate eval_passed. min_periods=2 mirrors the std(ddof=0)
        # degeneracy guard; bars with a non-finite or zero expanding std contribute 0 (masked),
        # which preserves the prior "constant/degenerate factor contributes no signal" behaviour
        # per-bar instead of dropping the whole factor.
        em = series.expanding(min_periods=2).mean()
        es = series.expanding(min_periods=2).std(ddof=0)
        es_valid = es.where(np.isfinite(es) & (es != 0.0))  # NaN where std is non-finite or zero
        if not es_valid.notna().any():
            continue  # degenerate / constant factor contributes no signal on any bar
        z = ((series - em) / es_valid).where(es_valid.notna())
        contribution = z.fillna(0.0) * w
        combined = contribution if combined is None else combined.add(contribution, fill_value=0.0)
        total_w += w

    if combined is None or total_w == 0.0:
        return None
    # Bounded position in [-1, 1]: sign of the weighted composite (discrete-direction, not size —
    # the sizing ladder is OUTSIDE this loop and untouched here).
    return np.sign(combined).clip(-1.0, 1.0)


def _strategy_returns(position, holdout_bars):
    """Realize the position against NEXT-bar returns (no lookahead: signal at t, return t→t+1)."""
    import pandas as pd

    closes = pd.Series(holdout_bars["close"]).astype(float)
    next_ret = closes.pct_change().shift(-1)  # return earned over t→t+1, attributed to bar t
    aligned = pd.concat([position.rename("pos"), next_ret.rename("ret")], axis=1).dropna()
    return (aligned["pos"] * aligned["ret"]).dropna()


def _sharpe(returns) -> float:
    """Annualized Sharpe of a per-bar return series (0 if degenerate)."""
    import numpy as np

    if len(returns) < 2:
        return 0.0
    std = float(returns.std(ddof=1))
    if not np.isfinite(std) or std == 0.0:
        return 0.0
    return float(returns.mean()) / std * _ANNUALIZATION


def score_holdout(
    proposal_set: FactorWeightProposalSet,
    holdout_bars,
    compute_factor,
    *,
    n_folds: int = HOLDOUT_FOLDS,
    plateau_cv_max: float = PLATEAU_CV_MAX,
) -> tuple[float, float, bool]:
    """Genuine walk-forward score of the PROPOSED weight set on a held-out OOS tail.

    Returns ``(holdout_dsr, holdout_sharpe, plateau_stable)``:
      - ``holdout_dsr``   : DeFlated Sharpe (P[not-false-discovery]) of the OOS strategy return.
      - ``holdout_sharpe``: annualized OOS Sharpe of the proposed composite.
      - ``plateau_stable``: True iff the per-fold Sharpe jitter (stdev across contiguous OOS
        folds) is small — robustness, NOT the in-sample peak. A sharp single-window spike that
        does not repeat across folds is jitter-unstable → False (the AMZN-weight lesson).

    Conservative-fail (-inf, 0.0, False) on insufficient/degenerate data so the cron buffers
    rather than promotes. Pure: no network, no env; `compute_factor` is injected by the caller.
    """
    from hermes_quant.evaluation.dsr import deflated_sharpe

    position = _composite_position(proposal_set, holdout_bars, compute_factor)
    if position is None:
        return float("-inf"), 0.0, False

    rets = _strategy_returns(position, holdout_bars)
    n_obs = len(rets)
    if n_obs < MIN_OBSERVATIONS:
        return float("-inf"), 0.0, False

    sharpe = _sharpe(rets)
    try:
        dsr = deflated_sharpe(sharpe, n_trials=1, n_observations=n_obs)
    except ValueError:
        return float("-inf"), 0.0, False

    # --- Robustness-not-peak: cross-fold Sharpe jitter over contiguous OOS sub-windows. ---
    fold_sharpes: list[float] = []
    folds = max(2, int(n_folds))
    if n_obs >= folds * MIN_OBSERVATIONS // 2 and folds >= 2:
        size = n_obs // folds
        if size >= 2:
            for k in range(folds):
                lo = k * size
                hi = n_obs if k == folds - 1 else (k + 1) * size
                fold_sharpes.append(_sharpe(rets.iloc[lo:hi]))
    if len(fold_sharpes) < 2:
        plateau_stable = False  # cannot establish robustness on < 2 folds → conservative
    else:
        import numpy as np

        mean_fold = float(np.mean(fold_sharpes))
        std_fold = float(np.std(fold_sharpes, ddof=1))
        # Relative dispersion (coefficient of variation). A consistently-strong edge has high
        # per-fold Sharpes but LOW CV; a one-window spike has high CV. Robustness, NOT the peak.
        cv = std_fold / abs(mean_fold) if mean_fold != 0.0 else float("inf")
        # Majority of folds must keep the OOS sign (an edge that only appears in one window is not
        # a plateau). Both conditions are robustness checks, never the in-sample peak.
        same_sign = sum(1 for s in fold_sharpes if (s > 0) == (sharpe > 0))
        plateau_stable = bool(
            np.isfinite(cv)
            and cv <= plateau_cv_max
            and same_sign > len(fold_sharpes) / 2
        )

    return dsr, sharpe, plateau_stable


def evaluate_against_holdout(
    proposal_set: FactorWeightProposalSet,
    *,
    holdout_dsr: float,                   # realized OOS DSR of the PROPOSED weight set (caller computes from bars)
    holdout_sharpe_delta: float,          # OOS Sharpe delta vs benchmark of the proposed set
    prior_best_dsr: float,                # the prior-best checkpoint's held-out DSR (checkpoint-fallback baseline)
    plateau_stable: bool,                 # jitter-stable plateau across folds (robustness-not-peak)
) -> FactorWeightProposalSet:
    """Apply the universal eval-gate contract (ADR-0080 §D80.3). Sets eval_passed.

    eval_passed iff ALL of:
      (1) held-out scored (holdout_dsr from market data — caller guarantees external-truth);
      (2) STRICTLY beats prior-best on held-out: holdout_dsr > prior_best_dsr (checkpoint-fallback —
          if not, revert: the returned set keeps proposals but eval_passed=False so the operator
          does NOT promote, and the set is appended to the rejected buffer);
      (2b) absolute floor: holdout_dsr >= MIN_PROMOTABLE_DSR (the no-edge midpoint). On the FIRST
          run prior_best is -inf, so (2) alone admits ANY finite DSR — including a consistent
          LOSER (negative Sharpe -> DSR ~ 0.0) that is plateau-stable. The floor requires a
          positive-Sharpe edge regardless of the baseline (without it the cron would write
          advisory candidates for a guaranteed money-loser and ratchet the baseline to ~0);
      (3) robustness-not-peak: plateau_stable is True;
      (4) bounded: every proposed_weight in [FLOOR, CAP] (asserted; a violation is a hard error).
    Propose-only (5) is structural: this function never applies anything; it only annotates.

    External-truth guarantee is STRUCTURAL: this signature takes only floats/bool. There is no
    path by which a proposal's own verdict tier or reason text feeds back into the grading number;
    the proposer cannot author the signal that grades it (D80.3 #1).
    """
    # (4) bounded — a violation is a hard error (the silence-only clamp must hold by construction).
    # Explicit raise (NOT a bare assert) so the bounds check survives `python -O` (asserts are
    # stripped under -O; this invariant guards money-software and must never be optimized away).
    for p in proposal_set.proposals:
        if not (WEIGHT_FLOOR <= p.proposed_weight <= WEIGHT_CAP):
            raise ValueError(
                f"proposed_weight {p.proposed_weight} for {p.factor_id!r} outside "
                f"[{WEIGHT_FLOOR}, {WEIGHT_CAP}] — silence-only invariant violated"
            )

    beats_prior_best = holdout_dsr > prior_best_dsr   # (2) STRICT — a tie reverts.
    # (2b) absolute floor: a no-edge / losing set must fail even when prior_best is the first-run
    # -inf baseline. >= is inclusive so a set exactly at the no-edge midpoint is NOT promotable
    # (it must clear the floor with a positive-Sharpe edge). NaN holdout_dsr fails closed: any
    # comparison with NaN is False, so beats_prior_best and the floor both reject it.
    eval_passed = bool(
        beats_prior_best and plateau_stable and holdout_dsr >= MIN_PROMOTABLE_DSR
    )   # (3) robustness-not-peak ANDed in.

    proposal_set.held_out_dsr = holdout_dsr
    proposal_set.held_out_sharpe_delta = holdout_sharpe_delta
    proposal_set.prior_best_dsr = prior_best_dsr
    proposal_set.beats_prior_best = beats_prior_best
    proposal_set.plateau_stable = plateau_stable
    proposal_set.eval_passed = eval_passed
    return proposal_set


def _atomic_write(path: Path, text: str) -> None:
    """Atomic-rename write (AGENTS.md state.json idiom). Best-effort dir creation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_candidates(proposal_set: FactorWeightProposalSet, *, path: Path | None = None) -> Path:
    """Write the ADVISORY-PLANE candidate diff JSON. Atomic write. Never touches live config."""
    p = path or (_DEFAULT_DIR / _CANDIDATES_FILE)
    _atomic_write(p, json.dumps(proposal_set.to_dict(), indent=2, sort_keys=True))
    logger.debug("weight_proposer: wrote %d candidate(s) → %s", len(proposal_set.proposals), p)
    return p


def append_rejected(proposal_set: FactorWeightProposalSet, *, path: Path | None = None) -> None:
    """SkillOpt rejected-edit buffer: a set that fails the gate is recorded so it is not re-proposed."""
    p = path or (_DEFAULT_DIR / _REJECTED_BUFFER)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(proposal_set.to_dict(), sort_keys=True) + "\n")
    logger.debug("weight_proposer: appended rejected set → %s", p)


def load_prior_best_dsr(*, path: Path | None = None) -> float:
    """Read the prior-best checkpoint DSR for checkpoint-fallback.

    Missing → -inf (first run: any held-out pass strictly beats it).
    """
    p = path or (_DEFAULT_DIR / _PRIOR_BEST_FILE)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return float("-inf")
    try:
        return float(data.get("held_out_dsr", float("-inf")))
    except (TypeError, ValueError):
        return float("-inf")


def save_prior_best_dsr(dsr: float, *, path: Path | None = None) -> Path:
    """Persist the new prior-best checkpoint DSR after a passing promotion-candidate.

    Only the cron calls this, and only when eval_passed is True (checkpoint-fallback baseline).
    """
    p = path or (_DEFAULT_DIR / _PRIOR_BEST_FILE)
    _atomic_write(
        p,
        json.dumps(
            {"held_out_dsr": float(dsr), "updated_at": datetime.now(UTC).isoformat()},
            sort_keys=True,
        ),
    )
    return p
