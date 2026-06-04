"""hermes_quant.governance.analyst_admission — DSR/walk-forward OOS admission gate
for analysts joining the committee (seed 908e, anti-overfit lane L3).

Factors already clear an eval-gate before any live weight
(:func:`hermes_quant.factors.weight_proposer.evaluate_against_holdout`): a weight-set
is admitted iff its held-out DSR STRICTLY beats the prior-best checkpoint AND the
per-fold Sharpe series is plateau-stable (robustness-not-peak, ADR-0080 §D80.3).
ANALYSTS joined the BMA committee with NO such check — they were appended
unconditionally in ``advisor._build_default_analysts``.

This module mirrors the FACTOR contract for analysts. It deliberately does NOT
invent a second mechanism:

  * the DSR is the SAME instrument — :func:`hermes_quant.evaluation.dsr.deflated_sharpe`;
  * the decision is the SAME contract — ``admitted = (holdout_dsr > prior_best_dsr)
    AND plateau_stable`` (STRICT >, a tie reverts);
  * the OOS evidence is the per-fold Sharpe series the walk_forward_replay instrument
    already produces (``WalkForwardBacktestResult.folds[*].result.sharpe``);
  * the prior-best checkpoint persists per-analyst, mirroring weight_proposer's
    load/save_prior_best_dsr.

External-truth is STRUCTURAL (the no-lookahead guarantee): :func:`evaluate_analyst_admission`
takes only floats + a bool. There is no analyst-object path by which an analyst's
own emitted view could feed back into the number that grades it.

Advisory/governance plane only: this module imports NO risk gate, kill switch, or
sizing ladder, performs NO network I/O, and reads NO environment variable. It is a
decision-support gate — the operator/promotion path admits; this only annotates.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# DSR is meaningless below this (evaluation/dsr.py raises < 30); mirror it here so
# the scorer fails conservative rather than letting the ValueError escape.
MIN_OBSERVATIONS: int = 30
# Robustness-not-peak: max relative cross-fold Sharpe dispersion (coefficient of
# variation). Mirrors factors.weight_proposer.PLATEAU_CV_MAX — a consistently-strong
# edge has high per-fold Sharpes but LOW CV; a one-window spike has high CV.
PLATEAU_CV_MAX: float = 1.5

_DEFAULT_DIR = Path.home() / ".hermes" / "quant" / "analysts"
_PRIOR_BEST_FILE = "admission-prior-best.json"
# Persisted admission verdicts the committee-build wiring reads when the
# (default-OFF) admission flag is enabled. Written by the operator/eval path after
# scoring a candidate's OOS evidence; the live committee never computes this inline.
_DEFAULT_DECISIONS_PATH = _DEFAULT_DIR / "admission-decisions.json"

# The committee-build wiring boundary reads this flag (default-OFF). The gate module
# itself reads NO env — the flag lives only at the boundary, mirroring
# factors.weight_proposer (whose HERMES_QUANT_FACTOR_WEIGHT_PROPOSER lives at the
# cron boundary only). Named here as DATA for the wiring to consult.
ADMISSION_FLAG = "HERMES_QUANT_ANALYST_ADMISSION"


@dataclass(frozen=True)
class AnalystAdmissionDecision:
    """Result of :func:`evaluate_analyst_admission`. ``admitted`` mirrors the factor
    eval-gate contract: STRICTLY beats prior-best DSR AND plateau-stable."""

    analyst_id: str
    holdout_dsr: float
    prior_best_dsr: float
    plateau_stable: bool
    beats_prior_best: bool
    admitted: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "analyst_id": self.analyst_id,
            "holdout_dsr": self.holdout_dsr,
            "prior_best_dsr": self.prior_best_dsr,
            "plateau_stable": self.plateau_stable,
            "beats_prior_best": self.beats_prior_best,
            "admitted": self.admitted,
            "reason": self.reason,
        }


def score_analyst_oos(
    fold_sharpes: list[float],
    *,
    n_observations: int,
    n_trials: int = 1,
    plateau_cv_max: float = PLATEAU_CV_MAX,
) -> tuple[float, bool]:
    """Score a candidate analyst's OUT-OF-SAMPLE evidence into ``(holdout_dsr,
    plateau_stable)`` — the two scalars :func:`evaluate_analyst_admission` consumes.

    ``fold_sharpes`` is the per-fold OOS Sharpe series (e.g.
    ``[f.result.sharpe for f in walk_forward_replay(...).folds]``). ``holdout_dsr`` is
    the deflated Sharpe of the mean OOS Sharpe; ``plateau_stable`` is True iff the
    cross-fold dispersion is bounded AND a majority of folds keep the mean's sign
    (robustness-not-peak, mirroring score_holdout).

    Conservative-fail ``(-inf, False)`` on insufficient data (< MIN_OBSERVATIONS, or
    < 2 folds) so a thin-sample candidate is HELD rather than admitted on noise.
    Pure: no network, no env. Reuses :func:`evaluation.dsr.deflated_sharpe`.
    """
    import numpy as np

    from hermes_quant.evaluation.dsr import deflated_sharpe

    if n_observations < MIN_OBSERVATIONS:
        return float("-inf"), False
    if len(fold_sharpes) < 2:
        # Cannot establish a robustness plateau on a single fold -> conservative.
        return float("-inf"), False

    mean_sharpe = float(np.mean(fold_sharpes))
    try:
        dsr = deflated_sharpe(mean_sharpe, n_trials=n_trials, n_observations=n_observations)
    except ValueError:
        return float("-inf"), False

    std_fold = float(np.std(fold_sharpes, ddof=1))
    cv = std_fold / abs(mean_sharpe) if mean_sharpe != 0.0 else float("inf")
    same_sign = sum(1 for s in fold_sharpes if (s > 0) == (mean_sharpe > 0))
    plateau_stable = bool(
        np.isfinite(cv)
        and cv <= plateau_cv_max
        and same_sign > len(fold_sharpes) / 2
    )
    return dsr, plateau_stable


def evaluate_analyst_admission(
    analyst_id: str,
    *,
    holdout_dsr: float,
    prior_best_dsr: float,
    plateau_stable: bool,
) -> AnalystAdmissionDecision:
    """Apply the factor eval-gate contract to an analyst (ADR-0080 §D80.3, mirrored).

    ``admitted`` iff BOTH:
      (1) STRICTLY beats prior-best on held-out DSR: ``holdout_dsr > prior_best_dsr``
          (a tie reverts — checkpoint-fallback); AND
      (2) robustness-not-peak: ``plateau_stable`` is True.

    STRUCTURAL external-truth guarantee: the signature takes only floats/bool. There
    is no path by which the analyst's own emitted views feed back into the grading
    number — the analyst cannot author the signal that grades it (the no-lookahead
    invariant for this gate).
    """
    beats_prior_best = holdout_dsr > prior_best_dsr  # STRICT — a tie reverts.
    admitted = bool(beats_prior_best and plateau_stable)

    if admitted:
        reason = (
            f"admitted: holdout_dsr={holdout_dsr:.4f} > prior_best={prior_best_dsr:.4f} "
            f"AND plateau_stable"
        )
    elif not beats_prior_best:
        reason = (
            f"held: holdout_dsr={holdout_dsr:.4f} <= prior_best={prior_best_dsr:.4f} "
            f"(does not strictly beat the prior-best checkpoint)"
        )
    else:
        reason = (
            f"held: holdout_dsr={holdout_dsr:.4f} beats prior_best but folds are not "
            f"plateau-stable (robustness-not-peak: single-window spike rejected)"
        )

    return AnalystAdmissionDecision(
        analyst_id=analyst_id,
        holdout_dsr=holdout_dsr,
        prior_best_dsr=prior_best_dsr,
        plateau_stable=plateau_stable,
        beats_prior_best=beats_prior_best,
        admitted=admitted,
        reason=reason,
    )


def admit_to_committee(
    candidates: list[Any],
    *,
    decisions: dict[str, AnalystAdmissionDecision],
) -> list[Any]:
    """Filter ``candidates`` to those whose admission decision is ``admitted=True``.

    FAIL-CLOSED: a candidate with NO decision in ``decisions`` is EXCLUDED (an analyst
    that was never gated must never join the committee). Order of the admitted
    candidates is preserved. Each candidate is matched by its ``.name`` attribute (the
    :class:`hermes_quant.protocol.Analyst` identity field).
    """
    admitted: list[Any] = []
    for analyst in candidates:
        name = getattr(analyst, "name", None)
        decision = decisions.get(name) if name is not None else None
        if decision is None:
            logger.info(
                "analyst_admission: holding %r — no admission decision (fail-closed)",
                name,
            )
            continue
        if decision.admitted:
            admitted.append(analyst)
        else:
            logger.info("analyst_admission: holding %r — %s", name, decision.reason)
    return admitted


# ---------------------------------------------------------------------------
# Per-analyst prior-best checkpoint (mirror of weight_proposer load/save).
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def load_prior_best_dsr(analyst_id: str, *, path: Path | None = None) -> float:
    """Read the prior-best held-out DSR for ``analyst_id`` (checkpoint-fallback).

    Missing file or missing analyst -> ``-inf`` (first run: any plateau-stable finite
    DSR strictly beats it). Mirrors weight_proposer.load_prior_best_dsr.
    """
    p = path or (_DEFAULT_DIR / _PRIOR_BEST_FILE)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return float("-inf")
    try:
        return float(data.get(analyst_id, {}).get("held_out_dsr", float("-inf")))
    except (TypeError, ValueError, AttributeError):
        return float("-inf")


def save_prior_best_dsr(analyst_id: str, dsr: float, *, path: Path | None = None) -> Path:
    """Persist the new prior-best held-out DSR for ``analyst_id`` after a passing
    admission (checkpoint-fallback baseline). Per-analyst keyed; other analysts'
    checkpoints are preserved. Mirrors weight_proposer.save_prior_best_dsr.
    """
    p = path or (_DEFAULT_DIR / _PRIOR_BEST_FILE)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    data[analyst_id] = {
        "held_out_dsr": float(dsr),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    _atomic_write(p, json.dumps(data, sort_keys=True))
    return p


# ---------------------------------------------------------------------------
# Persisted decisions loader + committee-build wiring (default-OFF).
# ---------------------------------------------------------------------------


def load_admission_decisions(
    path: Path | None = None,
) -> dict[str, AnalystAdmissionDecision]:
    """Load persisted per-analyst admission verdicts into
    ``{analyst_id: AnalystAdmissionDecision}``.

    The file is written by the operator/eval path after scoring a candidate's OOS
    evidence (the live committee never scores inline). Missing/unreadable file -> ``{}``;
    combined with the fail-closed :func:`admit_to_committee`, that means NO analyst
    joins when the admission flag is on but no decisions exist (never a silent
    no-op). A malformed individual entry is skipped (it cannot admit).
    """
    p = path or _DEFAULT_DECISIONS_PATH
    try:
        raw = json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    decisions: dict[str, AnalystAdmissionDecision] = {}
    for analyst_id, rec in raw.items():
        if not isinstance(rec, dict):
            continue
        try:
            decisions[analyst_id] = AnalystAdmissionDecision(
                analyst_id=analyst_id,
                holdout_dsr=float(rec.get("holdout_dsr", float("-inf"))),
                prior_best_dsr=float(rec.get("prior_best_dsr", float("-inf"))),
                plateau_stable=bool(rec.get("plateau_stable", False)),
                beats_prior_best=bool(rec.get("beats_prior_best", False)),
                admitted=bool(rec.get("admitted", False)),
                reason=str(rec.get("reason", "")),
            )
        except (TypeError, ValueError):
            continue  # a malformed entry cannot admit (fail-closed)
    return decisions


def apply_admission_gate(
    candidates: list[Any],
    *,
    enabled: bool,
    decisions_path: Path | None = None,
) -> list[Any]:
    """Committee-build wiring (seed 908e). DEFAULT-OFF.

    When ``enabled`` is False, returns ``candidates`` UNCHANGED (byte-identical to
    today — silence-by-default, no live disturbance). When True, filters them through
    :func:`admit_to_committee` using the persisted admission decisions: an analyst
    whose decision is not ``admitted`` (or that has no decision — fail-closed) is
    dropped.

    The ``enabled`` flag is resolved by the CALLER (the wiring boundary reads
    :data:`ADMISSION_FLAG`); this function takes a bool so it stays env-free and
    unit-testable, mirroring the factor proposer's pure core.
    """
    if not enabled:
        return candidates
    decisions = load_admission_decisions(decisions_path)
    return admit_to_committee(candidates, decisions=decisions)
