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
      (3) robustness-not-peak: plateau_stable is True;
      (4) bounded: every proposed_weight in [FLOOR, CAP] (asserted; a violation is a hard error).
    Propose-only (5) is structural: this function never applies anything; it only annotates.

    External-truth guarantee is STRUCTURAL: this signature takes only floats/bool. There is no
    path by which a proposal's own verdict tier or reason text feeds back into the grading number;
    the proposer cannot author the signal that grades it (D80.3 #1).
    """
    # (4) bounded — a violation is a hard error (the silence-only clamp must hold by construction).
    for p in proposal_set.proposals:
        assert WEIGHT_FLOOR <= p.proposed_weight <= WEIGHT_CAP, (
            f"proposed_weight {p.proposed_weight} for {p.factor_id!r} outside "
            f"[{WEIGHT_FLOOR}, {WEIGHT_CAP}] — silence-only invariant violated"
        )

    beats_prior_best = holdout_dsr > prior_best_dsr   # (2) STRICT — a tie reverts.
    eval_passed = bool(beats_prior_best and plateau_stable)   # (3) robustness-not-peak ANDed in.

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
