"""hermes_quant.memory.meta_retro — Layer 5: monthly meta-retro (T3, ADR-0080 / ADR-0081 §3).

The deterministic monthly meta-retro engine (the MISSING T3 tier). Pure functions over
JSONL inputs; NO network, NO LLM in the SCORING path (an LLM may only phrase a
candidate-hypothesis *claim* string — it never grades anything). All ordering is sorted
for reproducibility; `asof` is always injected (no `datetime.now()` in the scoring path).

It READS the now-live W2 weekly belief digests (`beliefs.jsonl`), the `research_debate`
audit rows (O7, write-only today — `stage.py:345`), `promotion_event` rows (W2's O3
producer), and the immutable reflection corpus (external truth). It WRITES exactly three
advisory-plane artifacts:
  1. a meta-retro report -> meta_retros.jsonl (append-only, recommendations-only);
  2. CANDIDATE hypotheses -> HypothesisRegistry, registered status="open" (never run);
  3. persona-calibration TELEMETRY -> inside the report row (telemetry_only=True).

And it applies the deterministic weekly->monthly belief promote/expire (ADR-0081 §4).

PROPOSE-ONLY + ADVISORY-PLANE-ONLY (capability-map §5 / ADR-0080 D80.1). This module
NEVER imports or references the risk gate, the hard limits, the discrete sizing ladder,
the kill-switch, the seed catalyst YAML, or any aggregator. The persona-calibration it
emits is TELEMETRY — `deliberative.py`/`bma.py` never read it. The ONLY path from a
candidate or a persona-delta to live policy runs through HypothesisRunner (W6) +
PromotionOrchestrator + operator sign-off. W3 closes a loop by PRODUCING evidence, not
by APPLYING it.

Closes O7 (research_debate audit rows, write-only today) + O8 (RunCards, display-only).
Flag: HERMES_QUANT_MONTHLY_META_RETRO (default-OFF; off-state is byte-identical).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hermes_quant.memory.decisions import MEMORY_HOME  # ~/.hermes/quant/memory
from hermes_quant.memory.weekly_retro import BELIEFS_PATH, _belief_from_row, _parse_dt
from hermes_quant.research.hypothesis_novelty import check_novelty

logger = logging.getLogger(__name__)

META_RETROS_PATH = MEMORY_HOME / "meta_retros.jsonl"  # append-only, NEW advisory artifact
CURRENT_SCHEMA_VERSION: int = 1

ENV_FLAG = "HERMES_QUANT_MONTHLY_META_RETRO"

# RD-Agent failure-tag rubric (capability-map §3 / ADR-0080): why did a hypothesis or
# belief-category fail — was the IDEA wrong (abandon, do not re-propose) or was the
# EXECUTION wrong (sizing/timing — a retry candidate is admissible)?
FAILURE_TAG_APPROACH = "approach"               # the thesis itself was wrong -> NO candidate
FAILURE_TAG_IMPLEMENTATION = "implementation"   # execution/sizing/timing -> retry candidate OK

# Heuristic split point between "approach" (deep negative — the idea is wrong) and
# "implementation" (mild negative — the idea may be right, the execution was off).
# A small, jitter-stable band (NOT a decimal-optimized peak — MT3 / AMZN-weight rule).
_APPROACH_ALPHA_FLOOR: float = -0.02

_write_lock = threading.Lock()


def _flag_on() -> bool:
    """Copy of the canonical multi-value idiom (regime_aware_confidence.py:26)."""
    return os.environ.get(ENV_FLAG, "0") in ("1", "true", "True", "yes", "on")


# ---------------------------------------------------------------------------
# inputs / outputs (all READ-ONLY against existing seams; all external-truth)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PersonaCalibration:
    """Per-persona realized-calibration TELEMETRY. PROPOSED weight only — never applied.

    The grade is ALWAYS realized alpha (external truth); the debate `confidence` field is
    used ONLY to identify the stance, never re-read as the truth that grades it.
    """

    role: str                        # "bull_researcher" | "bear_researcher" | "judge"
    n_calls: int                     # debate rows this persona appeared in (joined to a resolved trade)
    n_correct: int                   # times the persona's stance matched realized alpha sign
    hit_rate: float                  # n_correct / n_calls (0 when n_calls == 0)
    mean_alpha_when_followed: float  # external truth: mean alpha when this stance was the judge call
    proposed_weight_delta: float     # advisory ONLY; centred at 0; clamped to [-0.10, +0.10]
    telemetry_only: bool = True      # ALWAYS True in W3 — flips only after >=M months agree (W6/operator)


@dataclass(frozen=True)
class LessonCategoryTrend:
    """Which lesson_category repeats across the trailing weeks (FINCON over-episode)."""

    lesson_category: str
    weeks_present: int               # of the trailing N weekly belief sets, how many contained it
    cumulative_support_n: int
    mean_alpha_evidence: float       # external truth (mean over the backing weekly beliefs)
    repeats: bool                    # weeks_present >= repeat_threshold
    failure_tag: str | None          # FAILURE_TAG_* when mean_alpha_evidence < 0, else None


@dataclass(frozen=True)
class CandidateHypothesis:
    """A PROPOSED, novelty-gated hypothesis. Registered status='open' only — never run."""

    claim: str
    null_hypothesis: str
    rationale: str
    source_lesson_category: str
    support_n: int
    novelty_max_sim: float           # from the hypothesis_novelty gate
    failure_tag: str | None          # implementation-vs-approach (RD-Agent rubric)


@dataclass
class MetaRetroReport:
    schema_version: int
    meta_retro_id: str               # SHA-stable over (asof_month, config_hash)
    asof: str                        # ISO-8601 UTC distillation tick
    window_start: str
    window_end: str
    config_hash: str                 # SHA-256 over the deterministic config — the REPRODUCIBILITY gate
    lesson_category_trends: list[dict[str, Any]]
    persona_calibration: list[dict[str, Any]]
    candidate_hypotheses: list[dict[str, Any]]
    beliefs_promoted: list[str]      # belief_ids weekly->monthly
    beliefs_expired: list[str]       # belief_ids expired this tick
    promotion_readiness_flips: int   # count of weekly_retro_promotion_readiness=True promotion_events in window
    telemetry_only: bool = True      # INVARIANT: the whole report is advisory


# ---------------------------------------------------------------------------
# reproducibility handle
# ---------------------------------------------------------------------------


def _config_hash(
    window_days: int,
    repeat_threshold: int,
    novelty_threshold: float,
    max_candidates: int,
    weekly_to_monthly_half_life_days: float,
) -> str:
    """SHA-256 over the sorted config dict — the reproducibility handle.

    Re-running the same month with the same config + same input corpus MUST yield the
    same meta_retro_id and the same candidate set (RunCard.strategy_config_hash idiom,
    run_card.py:105)."""
    payload = json.dumps(
        {
            "window_days": window_days,
            "repeat_threshold": repeat_threshold,
            "novelty_threshold": novelty_threshold,
            "max_candidates": max_candidates,
            "weekly_to_monthly_half_life_days": weekly_to_monthly_half_life_days,
            "schema_version": CURRENT_SCHEMA_VERSION,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _make_meta_retro_id(asof: datetime, config_hash: str) -> str:
    """SHA-stable id over (asof_month, config_hash). Stable across re-runs of the
    same month + config (the reproducibility contract, §3)."""
    month = asof.astimezone(UTC).strftime("%Y%m")
    h = hashlib.sha1(f"{month}|{config_hash}".encode()).hexdigest()[:12]
    return f"meta_{month}_{h}"


# ---------------------------------------------------------------------------
# READ helpers (all external-truth, all Oracle-guarded)
# ---------------------------------------------------------------------------


def _stance_sign(recommendation: str | None) -> int:
    """Map a PortfolioRating final_recommendation to a directional sign.

    BUY/OVERWEIGHT -> +1 (bullish), SELL/UNDERWEIGHT -> -1 (bearish), HOLD/None -> 0.
    A standalone sign map (deliberately NOT importing the PortfolioRating intensity
    helper from the debate schema), so meta_retro carries NO dependency on the
    debate-decision contract and NO link to any risk-gate intensity mapping.
    """
    if not recommendation:
        return 0
    rec = str(recommendation).strip().upper()
    if rec in ("BUY", "OVERWEIGHT"):
        return 1
    if rec in ("SELL", "UNDERWEIGHT"):
        return -1
    return 0


def _load_debate_rows(asof: datetime, window_start: datetime) -> list[dict[str, Any]]:
    """READ research_debate audit rows in [window_start, asof). Oracle guard: only rows
    whose asof < the distillation tick (no FUTURE debate informs the month)."""
    from hermes_quant.governance import audit_log

    rows: list[dict[str, Any]] = []
    for e in audit_log.read(since=window_start, kinds=["research_debate"]):
        if e.asof < asof:  # strict: evt.asof < asof (gate condition 4b)
            rows.append(e.payload)
    return rows


def _count_promotion_readiness_flips(asof: datetime, window_start: datetime) -> int:
    """READ promotion_event rows; count those with weekly_retro_promotion_readiness=True in
    [window_start, asof). A meta signal — did the weekly tier keep clearing? W3 does NOT
    write this field (W2 owns O3); it only counts. Oracle-guarded (evt.asof < asof)."""
    from hermes_quant.governance import audit_log

    n = 0
    for e in audit_log.read(since=window_start, kinds=["promotion_event"]):
        if e.asof < asof and bool(e.payload.get("weekly_retro_promotion_readiness")):
            n += 1
    return n


def load_weekly_beliefs(asof: datetime, *, window_days: int, path: Path | None = None) -> list[dict]:
    """READ trailing-window tier='weekly' status='active' belief rows resolvable as of `asof`.

    Belief-level Oracle guard inherited from W2 (ADR-0081 §1): a belief is eligible only if
    its oracle_provenance.tau_observable_max < asof. Replays the append-only log so a later
    'expired' row supersedes an earlier 'active' one (mirrors weekly_retro.materialize_active).
    """
    p = path or BELIEFS_PATH
    if not p.exists():
        return []
    asof = asof.replace(tzinfo=UTC) if asof.tzinfo is None else asof.astimezone(UTC)
    window_start = asof - timedelta(days=window_days)

    latest: dict[str, dict] = {}
    with open(p) as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("beliefs.jsonl: skipping malformed row")
                continue
            bid = row.get("belief_id")
            if not bid:
                continue
            latest[bid] = row  # append-only write order: last row is current state

    eligible: list[dict] = []
    for row in latest.values():
        if str(row.get("status", "active")) != "active":
            continue
        if str(row.get("tier", "weekly")) != "weekly":
            continue
        prov = row.get("oracle_provenance", {}) or {}
        tau_max = _parse_dt(prov.get("tau_observable_max"))
        if tau_max is None or tau_max >= asof:
            continue  # surfaces an outcome not knowable at this asof -> EXCLUDE (Oracle guard)
        dist = _parse_dt(row.get("asof_distilled"))
        if dist is not None and dist < window_start:
            continue  # outside the trailing monthly window
        eligible.append(row)
    return eligible


def _default_realized_alpha_lookup() -> Callable[[str], float | None]:
    """Build the default proposal_id -> realized-alpha lookup, backed by the immutable
    reflection corpus joined through the decision log (external truth, never an LLM score).

    Join chain (read-only): proposal_id is the debate's stable id; the decision log maps
    decision_id <-> ticker; reflections carry decision_id + alpha_return. When a proposal_id
    cannot be resolved to a reflection, returns None (that debate row is simply not scored).
    """
    from hermes_quant.memory.reflector import REFLECTIONS_PATH

    # decision_id -> realized alpha (last resolution wins; append-only corpus).
    alpha_by_decision: dict[str, float] = {}
    if REFLECTIONS_PATH.exists():
        with open(REFLECTIONS_PATH) as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                did = str(row.get("decision_id", ""))
                if not did:
                    continue
                try:
                    alpha_by_decision[did] = float(row.get("alpha_return", 0.0) or 0.0)
                except (TypeError, ValueError):
                    continue

    def _lookup(proposal_id: str) -> float | None:
        if not proposal_id:
            return None
        # Direct decision_id match, else a substring join (proposal_ids embed the
        # decision_id suffix in this codebase's id scheme).
        if proposal_id in alpha_by_decision:
            return alpha_by_decision[proposal_id]
        # Iterate in a stable, file-order-independent order so the substring
        # join's tie-break (when >1 decision_id substring-matches) is
        # deterministic. Without sorted() the "first match wins" outcome would
        # depend on the reflections-file line order (insertion order).
        for did in sorted(alpha_by_decision):
            if did and (did in proposal_id or proposal_id in did):
                return alpha_by_decision[did]
        return None

    return _lookup


# ---------------------------------------------------------------------------
# the deterministic engine (NO LLM in the scoring path)
# ---------------------------------------------------------------------------


def compute_persona_calibration(
    debate_rows: list[dict[str, Any]],
    realized_alpha_by_proposal: Callable[[str], float | None],
) -> list[PersonaCalibration]:
    """Join each debate row's stance/recommendation to realized alpha (external truth).

    A persona is 'correct' when its stance sign matches realized alpha sign on the resolved
    trade. The debate `confidence` field is used ONLY to identify stance presence — the
    grade is ALWAYS realized alpha (the agent never authors the signal that grades it).

    proposed_weight_delta is centred at 0, clamped to [-0.10, +0.10], and is TELEMETRY ONLY
    (PersonaCalibration.telemetry_only stays True). It is NEVER read by any aggregator.
    """
    # Per-role accumulators. The judge persona is graded on the final_recommendation sign;
    # bull/bear personas on whether their stance aligned with realized alpha.
    stats: dict[str, dict[str, float]] = {}

    def _acc(role: str) -> dict[str, float]:
        return stats.setdefault(role, {"n": 0.0, "correct": 0.0, "alpha_sum": 0.0})

    for row in debate_rows:
        proposal_id = str(row.get("proposal_id", ""))
        alpha = realized_alpha_by_proposal(proposal_id)
        if alpha is None:
            continue  # unresolved trade — not scored (no future-peeking, no fabrication)
        alpha_sign = 1 if alpha > 0 else (-1 if alpha < 0 else 0)

        # --- judge persona: graded on the final recommendation sign ---
        judge_sign = _stance_sign(row.get("final_recommendation"))
        if judge_sign != 0:
            a = _acc("judge")
            a["n"] += 1
            a["alpha_sum"] += alpha
            if judge_sign == alpha_sign and alpha_sign != 0:
                a["correct"] += 1

        # --- bull persona: bullish-by-construction; correct when alpha > 0 ---
        if row.get("bull_turns_summary"):
            a = _acc("bull_researcher")
            a["n"] += 1
            a["alpha_sum"] += alpha
            if alpha_sign > 0:
                a["correct"] += 1

        # --- bear persona: bearish-by-construction; correct when alpha < 0 ---
        if row.get("bear_turns_summary"):
            a = _acc("bear_researcher")
            a["n"] += 1
            a["alpha_sum"] += alpha
            if alpha_sign < 0:
                a["correct"] += 1

    out: list[PersonaCalibration] = []
    for role in sorted(stats):  # sorted for reproducibility
        a = stats[role]
        n = int(a["n"])
        n_correct = int(a["correct"])
        hit_rate = (n_correct / n) if n else 0.0
        mean_alpha = (a["alpha_sum"] / n) if n else 0.0
        # Proposed delta: centred at 0 from hit_rate vs the 0.5 coin-flip baseline,
        # scaled small and CLAMPED to [-0.10, +0.10]. TELEMETRY ONLY.
        raw_delta = (hit_rate - 0.5) * 0.20
        proposed = max(-0.10, min(0.10, round(raw_delta, 6)))
        out.append(
            PersonaCalibration(
                role=role,
                n_calls=n,
                n_correct=n_correct,
                hit_rate=round(hit_rate, 6),
                mean_alpha_when_followed=round(mean_alpha, 6),
                proposed_weight_delta=proposed,
                telemetry_only=True,
            )
        )
    return out


def compute_lesson_trends(
    weekly_beliefs: list[dict[str, Any]],
    repeat_threshold: int,
) -> list[LessonCategoryTrend]:
    """FINCON over-episode: which lesson_category appears in >= repeat_threshold of the
    trailing weekly belief sets. Tags failure approach-vs-implementation when alpha < 0.

    'weeks_present' counts DISTINCT distillation ticks (asof_distilled days) in which the
    category appeared — the FINCON "recurs across weeks" signal, robust to multiple beliefs
    per week. mean_alpha_evidence is the external-truth mean over the backing beliefs.
    """
    by_cat: dict[str, dict[str, Any]] = {}
    for row in weekly_beliefs:
        cat = str(row.get("lesson_category", "unknown"))
        bucket = by_cat.setdefault(cat, {"weeks": set(), "support": 0, "alphas": []})
        dist = _parse_dt(row.get("asof_distilled"))
        week_key = dist.strftime("%Y%m%d") if dist else str(row.get("asof_distilled", ""))
        bucket["weeks"].add(week_key)
        bucket["support"] += int(row.get("support_n", 0) or 0)
        try:
            bucket["alphas"].append(float(row.get("alpha_evidence", 0.0) or 0.0))
        except (TypeError, ValueError):
            pass

    out: list[LessonCategoryTrend] = []
    for cat in sorted(by_cat):  # sorted for reproducibility
        bucket = by_cat[cat]
        weeks_present = len(bucket["weeks"])
        alphas = bucket["alphas"]
        mean_alpha = (sum(alphas) / len(alphas)) if alphas else 0.0
        repeats = weeks_present >= repeat_threshold
        failure_tag: str | None = None
        if mean_alpha < 0:
            # Deep negative => the IDEA was wrong (approach). Mild negative => the idea may
            # be right but execution/sizing/timing was off (implementation, retry-admissible).
            failure_tag = (
                FAILURE_TAG_APPROACH
                if mean_alpha < _APPROACH_ALPHA_FLOOR
                else FAILURE_TAG_IMPLEMENTATION
            )
        out.append(
            LessonCategoryTrend(
                lesson_category=cat,
                weeks_present=weeks_present,
                cumulative_support_n=int(bucket["support"]),
                mean_alpha_evidence=round(mean_alpha, 6),
                repeats=repeats,
                failure_tag=failure_tag,
            )
        )
    return out


def _template_claim(trend: LessonCategoryTrend) -> tuple[str, str, str]:
    """Deterministic, NO-LLM claim/null/rationale phrasing (CI/default path).

    The claim is a falsifiable, AST-purity-safe statement (plain prose; no code, no
    eval-able expression embedded). Fully reproducible: same trend -> same strings.
    """
    cat = trend.lesson_category
    if trend.failure_tag == FAILURE_TAG_IMPLEMENTATION:
        claim = (
            f"Adjusting execution timing/sizing on the {cat} setup will recover positive "
            f"alpha over a held-out backtest (the thesis held; execution lagged)."
        )
        null = f"Execution adjustment on {cat} yields no alpha improvement (alpha <= 0)."
        rationale = (
            f"The {cat} category recurred across {trend.weeks_present} weekly belief sets "
            f"with mean alpha {trend.mean_alpha_evidence:+.2%} (implementation-tagged): a "
            f"retry candidate, not an abandonment."
        )
    else:
        claim = (
            f"The {cat} setup carries persistent positive alpha and warrants a dedicated "
            f"factor/playbook over a held-out backtest."
        )
        null = f"The {cat} setup carries no exploitable alpha (alpha <= 0)."
        rationale = (
            f"The {cat} category recurred across {trend.weeks_present} weekly belief sets "
            f"with mean alpha {trend.mean_alpha_evidence:+.2%} and cumulative support "
            f"n={trend.cumulative_support_n}."
        )
    return claim, null, rationale


def synthesize_candidate_hypotheses(
    trends: list[LessonCategoryTrend],
    existing_claims: list[str],
    *,
    novelty_threshold: float,
    max_candidates: int,
    llm_claim_writer: Callable[[LessonCategoryTrend], tuple[str, str, str]] | None = None,
) -> list[CandidateHypothesis]:
    """Emit <= max_candidates candidates from REPEATING, positive-or-implementation-tagged
    trends. Each candidate claim passes through hypothesis_novelty.check_novelty against
    existing_claims; rejected if max_sim >= novelty_threshold.

    RD-Agent rubric: an APPROACH-tagged trend (the idea was wrong) yields NO candidate; a
    positive or IMPLEMENTATION-tagged trend MAY. When llm_claim_writer is None (CI/default),
    a deterministic template phrases the claim (no LLM, fully reproducible). The LLM, if
    present, writes ONLY the claim/null/rationale strings — it SCORES nothing (external-
    truth-only rail).
    """
    # Candidate-eligible trends: must repeat AND not be an abandon-the-idea (approach) failure.
    eligible = [
        t
        for t in trends
        if t.repeats and t.failure_tag != FAILURE_TAG_APPROACH
    ]
    # Deterministic ordering: strongest evidence first (|alpha| * support), tie-break by
    # category name so the candidate set is byte-identical across runs.
    eligible.sort(
        key=lambda t: (abs(t.mean_alpha_evidence) * t.cumulative_support_n, t.lesson_category),
        reverse=True,
    )

    # Build a mutable claim library so candidates dedup against EACH OTHER too (no two
    # near-identical candidates emitted in one pass).
    claim_library: list[str] = list(existing_claims)
    out: list[CandidateHypothesis] = []
    for trend in eligible:
        if len(out) >= max_candidates:
            break
        writer = llm_claim_writer or _template_claim
        claim, null, rationale = writer(trend)
        result = check_novelty(claim, claim_library, threshold=novelty_threshold)
        if not result.passes:
            logger.debug(
                "meta_retro: candidate for %s rejected by novelty gate (%s)",
                trend.lesson_category,
                result.reason,
            )
            continue
        out.append(
            CandidateHypothesis(
                claim=claim,
                null_hypothesis=null,
                rationale=rationale,
                source_lesson_category=trend.lesson_category,
                support_n=trend.cumulative_support_n,
                novelty_max_sim=result.max_sim,
                failure_tag=trend.failure_tag,
            )
        )
        claim_library.append(claim)
    return out


def apply_weekly_to_monthly(
    beliefs: list[dict[str, Any]],
    asof: datetime,
    trends: list[LessonCategoryTrend],
    *,
    weekly_to_monthly_half_life_days: float,
) -> tuple[list[dict], list[str], list[str]]:
    """Deterministic FINMEM promote/expire (ADR-0081 §4), NON-LLM.

      - a weekly belief whose lesson_category REPEATS (trend.repeats) is PROMOTED to
        tier='monthly' (a NEW monthly row, longer half_life_days);
      - weekly beliefs whose category did NOT recur AND whose recency < eps OR
        importance < threshold are EXPIRED (a NEW status='expired' row).

    Oracle provenance is COPIED FORWARD unchanged (never re-tagged as ground truth).
    Returns (new_belief_rows, promoted_belief_ids, expired_belief_ids). The rows are
    NEW append-only rows; the original 'active' rows are never mutated in place.
    """
    asof = asof.replace(tzinfo=UTC) if asof.tzinfo is None else asof.astimezone(UTC)
    asof_iso = asof.isoformat()
    repeats = {t.lesson_category for t in trends if t.repeats}

    new_rows: list[dict] = []
    promoted_ids: list[str] = []
    expired_ids: list[str] = []

    # Deterministic order so the output rows + id lists are byte-identical across runs.
    for row in sorted(beliefs, key=lambda r: str(r.get("belief_id", ""))):
        b = _belief_from_row(row)
        cat = b.lesson_category
        if cat in repeats:
            # PROMOTE weekly -> monthly. A NEW row with a monthly belief_id (stable over
            # tier|role|category|asof). Oracle provenance copied forward UNCHANGED.
            h = hashlib.sha1(
                f"monthly|{b.role}|{cat}|{asof_iso}".encode()
            ).hexdigest()[:12]
            monthly_id = f"bel_monthly_{b.role}_{cat}_{h}"
            monthly = {
                **asdict(b),
                "belief_id": monthly_id,
                "tier": "monthly",
                "half_life_days": float(weekly_to_monthly_half_life_days),
                "asof_distilled": asof_iso,
                "status": "active",
                "oracle_provenance": dict(b.oracle_provenance),  # copied forward unchanged
            }
            new_rows.append(monthly)
            promoted_ids.append(monthly_id)
        else:
            # Did not recur. Expire iff recency below epsilon OR importance below threshold.
            from hermes_quant.memory.weekly_retro import RECENCY_EXPIRE_EPSILON

            if b.recency < RECENCY_EXPIRE_EPSILON or b.importance <= 0.0:
                expired = {
                    **asdict(b),
                    "status": "expired",
                    "asof_distilled": asof_iso,
                    "oracle_provenance": dict(b.oracle_provenance),  # unchanged
                }
                new_rows.append(expired)
                expired_ids.append(b.belief_id)

    return new_rows, sorted(promoted_ids), sorted(expired_ids)


def _append_belief_rows(rows: list[dict], path: Path) -> None:
    """Append belief rows (monthly/expired) to beliefs.jsonl. fsync after write."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        with open(path, "a", buffering=1) as fh:
            for r in rows:
                fh.write(json.dumps(r, sort_keys=True, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def _append_report(report: MetaRetroReport, path: Path) -> None:
    """Append the meta-retro report row to meta_retros.jsonl (append-only). fsync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(report), sort_keys=True, default=str) + "\n"
    with _write_lock:
        with open(path, "a", buffering=1) as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())


def _existing_registry_claims(registry: Any | None) -> list[str]:
    """Read every claim already in the hypothesis registry (open + running + resolved), so
    a candidate dedups against ALL prior hypotheses, not just open ones."""
    if registry is None:
        return []
    claims: list[str] = []
    try:
        seen: set[str] = set()
        for row in registry._iter_rows():  # noqa: SLF001 — read-only replay, same module family
            if row.get("kind") != "hypothesis":
                continue
            hid = row.get("hypothesis_id", "")
            if hid in seen:
                continue
            seen.add(hid)
            claim = row.get("claim")
            if claim:
                claims.append(str(claim))
    except Exception as exc:  # noqa: BLE001
        logger.warning("meta_retro: could not read existing registry claims (%s)", exc)
    return claims


def _register_candidates(
    candidates: list[CandidateHypothesis],
    registry: Any,
    *,
    asof: datetime,
) -> None:
    """Register each candidate as Hypothesis(status='open', author='quant-monthly-meta-retro').

    PROPOSE-ONLY: status is ALWAYS 'open'; a human/HypothesisRunner (W6) must move it
    open->running. The success/falsification criteria are AST-purity-safe numeric
    expressions (the registry's _purity_check_criterion gate accepts them automatically).
    """
    from hermes_quant.research.hypothesis import Hypothesis, HypothesisIDCollision

    for c in candidates:
        hyp = Hypothesis(
            author="quant-monthly-meta-retro",
            claim=c.claim[:512],
            null_hypothesis=c.null_hypothesis[:512],
            success_criteria=["vs_buyhold_alpha > 0.0", "sharpe >= 0.3"],
            falsification_criteria=["vs_buyhold_alpha <= 0.0"],
            experiment_design=(
                "Walk-forward held-out backtest of the source lesson_category setup; "
                f"RD-Agent failure-tag={c.failure_tag or 'positive'}. "
                f"{c.rationale[:1500]}"
            ),
            duration_target_days=90,
            scope={
                "source_lesson_category": c.source_lesson_category,
                "support_n": c.support_n,
                "novelty_max_sim": c.novelty_max_sim,
                "origin": "monthly_meta_retro",
            },
            related_adrs=["ADR-0080", "ADR-0081"],
            status="open",
        )
        try:
            registry.register(hyp)
        except HypothesisIDCollision:
            logger.debug("meta_retro: candidate id collision; skipping duplicate")


def run_meta_retro(
    asof: datetime,
    *,
    window_days: int = 28,
    repeat_threshold: int = 2,
    novelty_threshold: float = 0.85,
    max_candidates: int = 5,
    weekly_to_monthly_half_life_days: float = 90.0,
    realized_alpha_by_proposal: Callable[[str], float | None] | None = None,
    register_candidates: bool = False,
    llm_claim_writer: Callable[[LessonCategoryTrend], tuple[str, str, str]] | None = None,
    beliefs_path: Path | None = None,
    meta_retros_path: Path | None = None,
    registry: Any | None = None,
) -> MetaRetroReport:
    """The full monthly pass. PURE + DETERMINISTIC given (asof, config, input corpus).

    Writes the report to meta_retros.jsonl (append-only). When register_candidates=True,
    registers candidates as Hypothesis(status='open', author='quant-monthly-meta-retro').
    NEVER auto-promotes; NEVER touches a limit. realized_alpha_by_proposal defaults to a
    reflections.jsonl-backed lookup (external truth).

    Reproducibility contract (eval gate condition 1): two calls with the same config over
    the same immutable corpus return reports whose (meta_retro_id, config_hash, sorted
    candidate claims, beliefs_promoted, beliefs_expired) are byte-identical.
    """
    asof = asof.replace(tzinfo=UTC) if asof.tzinfo is None else asof.astimezone(UTC)
    window_start = asof - timedelta(days=window_days)
    bpath = beliefs_path or BELIEFS_PATH
    mpath = meta_retros_path or META_RETROS_PATH

    cfg_hash = _config_hash(
        window_days,
        repeat_threshold,
        novelty_threshold,
        max_candidates,
        weekly_to_monthly_half_life_days,
    )
    meta_retro_id = _make_meta_retro_id(asof, cfg_hash)

    # --- external-truth alpha lookup (default reflections-backed) ---
    alpha_lookup = realized_alpha_by_proposal or _default_realized_alpha_lookup()

    # --- READ inputs (Oracle-guarded) ---
    debate_rows = _load_debate_rows(asof, window_start)
    weekly_beliefs = load_weekly_beliefs(asof, window_days=window_days, path=bpath)
    promotion_flips = _count_promotion_readiness_flips(asof, window_start)

    # --- compute (deterministic, no LLM in the scoring path) ---
    persona = compute_persona_calibration(debate_rows, alpha_lookup)
    trends = compute_lesson_trends(weekly_beliefs, repeat_threshold)

    # Existing claims for the novelty gate (read all prior registry hypotheses).
    existing_claims = _existing_registry_claims(registry)
    candidates = synthesize_candidate_hypotheses(
        trends,
        existing_claims,
        novelty_threshold=novelty_threshold,
        max_candidates=max_candidates,
        llm_claim_writer=llm_claim_writer,
    )

    # --- deterministic weekly->monthly promote/expire (ADR-0081 §4) ---
    new_belief_rows, promoted_ids, expired_ids = apply_weekly_to_monthly(
        weekly_beliefs,
        asof,
        trends,
        weekly_to_monthly_half_life_days=weekly_to_monthly_half_life_days,
    )

    # --- WRITE advisory artifacts ---
    _append_belief_rows(new_belief_rows, bpath)
    if register_candidates and candidates:
        if registry is None:
            from hermes_quant.research.hypothesis import HypothesisRegistry

            registry = HypothesisRegistry()
        _register_candidates(candidates, registry, asof=asof)

    report = MetaRetroReport(
        schema_version=CURRENT_SCHEMA_VERSION,
        meta_retro_id=meta_retro_id,
        asof=asof.isoformat(),
        window_start=window_start.isoformat(),
        window_end=asof.isoformat(),
        config_hash=cfg_hash,
        lesson_category_trends=[asdict(t) for t in trends],
        persona_calibration=[asdict(p) for p in persona],
        candidate_hypotheses=[asdict(c) for c in candidates],
        beliefs_promoted=promoted_ids,
        beliefs_expired=expired_ids,
        promotion_readiness_flips=promotion_flips,
        telemetry_only=True,
    )
    _append_report(report, mpath)
    return report
