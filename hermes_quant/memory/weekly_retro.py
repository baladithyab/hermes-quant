"""hermes_quant.memory.weekly_retro — Layer 4: weekly CVRF distillation (ADR-0081).

Gated by HERMES_QUANT_WEEKLY_RETRO=1 at the CRON layer. The library functions are
pure + deterministic + network-free (safe in CI); the flag gate lives in
ops/scripts/quant-weekly-retro.py and in the llm_committee injection site, mirroring
how reflector.py is library-pure and the flag lives in the reactor/_paper_reflection_hook.

PROPOSE-ONLY. Writes ONLY to the advisory plane: beliefs.jsonl (a rebuildable view of
the immutable reflections.jsonl) and a promotion_event audit row. NEVER touches the
risk gate, hard limits, the sizing ladder, or the kill-switch (capability-map §5).

This module IMPLEMENTS ADR-0081 §1 (belief schema), §2 (weekly distillation), and §4
(deterministic FINMEM promote/expire). It closes O2 (the missing weekly distillation
tier) and O3 (the dangling `weekly_retro_promotion_readiness` gate field consumed at
governance/promotion.py:158).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hermes_quant.memory.decisions import MEMORY_HOME

logger = logging.getLogger(__name__)

BELIEFS_PATH = MEMORY_HOME / "beliefs.jsonl"
REFLECTIONS_PATH = MEMORY_HOME / "reflections.jsonl"   # read-only input
CURRENT_BELIEF_SCHEMA_VERSION: int = 1

# --- Tunables (jitter-tested, NOT decimal-optimized — MT3 / AMZN-weight rule) ---
BELIEF_BUDGET_PER_ROLE: int = 3        # Reflexion Ω≈1-3; FINCON small-set. Per-role cap N.
MIN_SUPPORT_N: int = 3                  # a belief needs >=3 backing reflections (no single-trade beliefs)
HALF_LIFE_DAYS = {"weekly": 14.0, "monthly": 60.0}   # weekly decays faster than monthly
RECENCY_EXPIRE_EPSILON: float = 0.10    # recency < eps -> expire
IMPORTANCE_BONUS_K: float = 1.0         # +K on a pivotal positive-alpha event
TRAILING_WINDOW_DAYS: int = 7           # the "weekly" window of reflections to distill

# Injection-target roles (today only PM/RM read the lessons block,
# llm_committee.py:295). The store carries all roles for forward-compat (W3/W7).
INJECTION_ROLES: tuple[str, ...] = ("portfolio_manager", "research_manager")

_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Belief dataclass (ADR-0081 §1, verbatim)
# ---------------------------------------------------------------------------


@dataclass
class Belief:
    """A distilled, decaying, provenance-tagged verbal delta (one beliefs.jsonl row)."""

    schema_version: int          # CURRENT_BELIEF_SCHEMA_VERSION = 1
    belief_id: str               # SHA-stable over (tier, role, lesson_category, ticker, asof_distilled)
    tier: str                    # "weekly" | "monthly" — sets the half-life
    role: str                    # the ONE role this is propagated to (selective propagation)
    lesson_category: str         # the LessonCategory enum value it generalizes
    verbal_delta: str            # ≤1-2 sentences; "what to do differently"
    alpha_evidence: float        # mean realized ALPHA of the winners split (external truth)
    support_n: int               # number of reflections backing it
    half_life_days: float        # by tier (weekly shorter, monthly longer)
    access_counter: int          # FINMEM counter; +1 each time surfaced into a prompt
    importance: float            # FINMEM importance; +K on a pivotal profitable event
    recency: float               # decay value in (0,1]; reset to 1.0 on access
    oracle_provenance: dict      # {"source","tau_observable_max","decision_ids"}
    asof_distilled: str          # ISO-8601 UTC of the distillation tick
    status: str                  # "active" | "expired" (append-only; expiry is a NEW row)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_dt(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    except (ValueError, TypeError):
        return None


def _ensure_utc(asof: datetime) -> datetime:
    return asof.replace(tzinfo=UTC) if asof.tzinfo is None else asof.astimezone(UTC)


def _make_belief_id(tier: str, role: str, lesson_category: str, ticker: str,
                    asof_distilled: str) -> str:
    """SHA-stable belief id over (tier, role, lesson_category, ticker, asof_distilled).

    ticker is included so two patterns mined in the same distillation tick for the
    same (role, category) on different tickers do not collide (ADR-0081 §1 keys on
    lesson_category, and §2 groups by category+ticker).
    """
    h = hashlib.sha1(
        f"{tier}|{role}|{lesson_category}|{ticker.upper()}|{asof_distilled}".encode()
    ).hexdigest()[:12]
    return f"bel_{tier}_{role}_{ticker.upper()}_{h}"


def _alpha(belief_tier: str, days_since_distilled: float) -> float:
    """FINMEM recency multiplier: alpha = 0.5 ** (days / half_life_days)."""
    hl = HALF_LIFE_DAYS.get(belief_tier, HALF_LIFE_DAYS["weekly"])
    if hl <= 0:
        return 0.0
    return 0.5 ** (max(0.0, days_since_distilled) / hl)


def _eviction_score(b: Belief) -> float:
    """Lowest access_counter * recency * importance is evicted first (ADR-0081 §4)."""
    return float(b.access_counter) * float(b.recency) * float(b.importance)


def _belief_from_row(row: dict[str, Any]) -> Belief:
    return Belief(
        schema_version=int(row.get("schema_version", CURRENT_BELIEF_SCHEMA_VERSION)),
        belief_id=str(row.get("belief_id", "")),
        tier=str(row.get("tier", "weekly")),
        role=str(row.get("role", "portfolio_manager")),
        lesson_category=str(row.get("lesson_category", "unknown")),
        verbal_delta=str(row.get("verbal_delta", "")),
        alpha_evidence=float(row.get("alpha_evidence", 0.0) or 0.0),
        support_n=int(row.get("support_n", 0) or 0),
        half_life_days=float(row.get("half_life_days", HALF_LIFE_DAYS["weekly"])),
        access_counter=int(row.get("access_counter", 0) or 0),
        importance=float(row.get("importance", 1.0) or 0.0),
        recency=float(row.get("recency", 1.0) or 0.0),
        oracle_provenance=dict(row.get("oracle_provenance", {}) or {}),
        asof_distilled=str(row.get("asof_distilled", "")),
        status=str(row.get("status", "active")),
    )


# ---------------------------------------------------------------------------
# I/O (append-only; beliefs.jsonl is a rebuildable projection)
# ---------------------------------------------------------------------------


def _append_beliefs(beliefs: list[Belief], path: Path) -> None:
    """Append belief rows (active OR expired) to beliefs.jsonl. fsync after write."""
    if not beliefs:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        with open(path, "a", buffering=1) as fh:
            for b in beliefs:
                fh.write(json.dumps(asdict(b), sort_keys=True, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def load_reflections(asof: datetime, *, path: Path | None = None,
                     window_days: int = TRAILING_WINDOW_DAYS) -> list[dict]:
    """Load trailing-window reflection rows RESOLVABLE as of `asof`.

    Applies the Oracle guard FIRST (same rule as retriever.py:351-362): a row is
    eligible only if its tau_observable < asof. This is the lookahead-honesty rail —
    the distiller must never read an outcome that was not knowable at the distillation
    tick. Rows with tau_observable >= asof are excluded BEFORE any grouping.
    """
    p = path or REFLECTIONS_PATH
    asof = _ensure_utc(asof)
    window_start = asof - timedelta(days=window_days)
    if not p.exists():
        return []
    eligible: list[dict] = []
    with open(p) as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("reflections.jsonl: skipping malformed row")
                continue
            if not isinstance(row, dict):
                continue
            tau = _parse_dt(row.get("tau_observable"))
            if tau is None or tau >= asof:
                # not yet knowable at the distillation tick — EXCLUDE (Oracle guard)
                continue
            res = _parse_dt(row.get("asof_resolution"))
            if res is not None and res < window_start:
                continue  # outside the trailing weekly window
            eligible.append(row)
    return eligible


def load_belief_rows(*, path: Path | None = None) -> list[dict]:
    """Stream every belief row (active + expired). Malformed rows skipped + logged."""
    p = path or BELIEFS_PATH
    if not p.exists():
        return []
    rows: list[dict] = []
    with open(p) as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("beliefs.jsonl: skipping malformed row")
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def materialize_active(rows: list[dict], asof: datetime) -> list[Belief]:
    """Replay the append-only belief log into the CURRENT active set as of `asof`.

    A belief_id is active iff its LATEST row has status='active'. A later 'expired'
    row supersedes it. Mirrors decisions.read_pending() event-replay
    (decisions.py:186-197). Also applies the belief-level Oracle guard
    (oracle_provenance.tau_observable_max < asof).
    """
    asof = _ensure_utc(asof)
    latest: dict[str, Belief] = {}
    for row in rows:
        bid = row.get("belief_id")
        if not bid:
            continue
        # Append-only log is in write order; the last row for an id is its state.
        latest[bid] = _belief_from_row(row)

    active: list[Belief] = []
    for b in latest.values():
        if b.status != "active":
            continue
        tau_max = _parse_dt(b.oracle_provenance.get("tau_observable_max"))
        if tau_max is None or tau_max >= asof:
            # belief surfaces an outcome not knowable at this asof — EXCLUDE
            continue
        active.append(b)
    return active


# ---------------------------------------------------------------------------
# CVRF distillation (ADR-0081 §2)
# ---------------------------------------------------------------------------


def split_winners_losers(reflections: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split by realized ALPHA (alpha_return), NOT raw P&L. winners: alpha_return > 0."""
    winners = [r for r in reflections if float(r.get("alpha_return", 0.0) or 0.0) > 0.0]
    losers = [r for r in reflections if float(r.get("alpha_return", 0.0) or 0.0) <= 0.0]
    return winners, losers


def group_by_pattern(reflections: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Group by (lesson_category, ticker). Key is (lesson_category, ticker.upper())."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in reflections:
        key = (str(r.get("lesson_category", "unknown")), str(r.get("ticker", "")).upper())
        groups.setdefault(key, []).append(r)
    return groups


def _verbal_delta(ticker: str, category: str, win_alpha: float, lose_alpha: float,
                  n_win: int, n_lose: int) -> str:
    """Deterministic template-built belief text (NO LLM)."""
    if n_win >= n_lose:
        return (
            f"On {ticker} {category}: the winning split realized {win_alpha:+.1%} mean alpha "
            f"({n_win} trades); favor this setup and cut losers ({lose_alpha:+.1%}, {n_lose}) faster."
        )
    return (
        f"On {ticker} {category}: losers dominated ({lose_alpha:+.1%} mean alpha over {n_lose} "
        f"trades vs {win_alpha:+.1%} over {n_win}); size down or avoid this setup."
    )


def distill_beliefs(reflections: list[dict], *, asof: datetime,
                    role: str = "portfolio_manager",
                    budget: int = BELIEF_BUDGET_PER_ROLE,
                    min_support: int = MIN_SUPPORT_N) -> list[Belief]:
    """CVRF lower-half: conceptualize winners-vs-losers into <=budget verbal belief-deltas.

    Deterministic, NO LLM. For each (lesson_category, ticker) group with >= min_support
    backing reflections, compute mean winner-alpha vs mean loser-alpha and emit a
    template-built verbal_delta. Attach alpha_evidence (= mean alpha of the winning
    split), support_n, oracle_provenance (tau_observable_max + decision_ids).
    Rank groups by |alpha_evidence| * support_n; keep top `budget` per role.
    """
    asof = _ensure_utc(asof)
    asof_iso = asof.isoformat()
    groups = group_by_pattern(reflections)

    candidates: list[tuple[float, Belief]] = []
    for (category, ticker), rows in groups.items():
        if len(rows) < min_support:
            continue  # no single-trade (or thin-support) beliefs
        winners, losers = split_winners_losers(rows)
        win_alpha = (
            sum(float(r.get("alpha_return", 0.0) or 0.0) for r in winners) / len(winners)
            if winners else 0.0
        )
        lose_alpha = (
            sum(float(r.get("alpha_return", 0.0) or 0.0) for r in losers) / len(losers)
            if losers else 0.0
        )
        # alpha_evidence = mean alpha of the winning split (external truth, never raw P&L).
        alpha_evidence = win_alpha

        tau_values = [_parse_dt(r.get("tau_observable")) for r in rows]
        tau_values = [t for t in tau_values if t is not None]
        tau_max = max(tau_values).isoformat() if tau_values else asof_iso
        decision_ids = sorted(
            {str(r.get("decision_id", "")) for r in rows if r.get("decision_id")}
        )

        belief = Belief(
            schema_version=CURRENT_BELIEF_SCHEMA_VERSION,
            belief_id=_make_belief_id("weekly", role, category, ticker, asof_iso),
            tier="weekly",
            role=role,
            lesson_category=category,
            verbal_delta=_verbal_delta(
                ticker, category, win_alpha, lose_alpha, len(winners), len(losers)
            ),
            alpha_evidence=round(alpha_evidence, 6),
            support_n=len(rows),
            half_life_days=HALF_LIFE_DAYS["weekly"],
            access_counter=0,
            importance=1.0,
            recency=1.0,
            oracle_provenance={
                "source": "agent_reflection",
                "tau_observable_max": tau_max,
                "decision_ids": decision_ids,
            },
            asof_distilled=asof_iso,
            status="active",
        )
        rank = abs(alpha_evidence) * len(rows)
        candidates.append((rank, belief))

    # Rank descending; deterministic tie-break by belief_id for reproducibility.
    candidates.sort(key=lambda rb: (rb[0], rb[1].belief_id), reverse=True)
    return [b for _, b in candidates[:budget]]


# ---------------------------------------------------------------------------
# FINMEM deterministic promote/expire (ADR-0081 §4 — NON-LLM, pure arithmetic)
# ---------------------------------------------------------------------------


def decay_and_promote(active: list[Belief], new: list[Belief], *, asof: datetime,
                      budget: int = BELIEF_BUDGET_PER_ROLE
                      ) -> tuple[list[Belief], list[Belief]]:
    """Apply the FINMEM rule. Returns (kept_active, newly_expired).

    Per belief, per tick:
      - DECAY:   recency *= alpha(tier), where alpha = 0.5 ** (days_since_distilled
                 / half_life_days).
      - PROMOTE: a belief whose pattern RECURS in `new` gets access_counter += 1,
                 recency = 1.0; a pivotal positive-alpha recurrence gets importance += K
                 and is upgraded weekly->monthly (slower decay).
      - EXPIRE:  emit a status='expired' row when recency < RECENCY_EXPIRE_EPSILON
                 OR per-role active count > budget (evict LOWEST
                 access_counter * recency * importance first).
    No LLM participates. Same corpus + asof => same active set (reproducible).
    """
    asof = _ensure_utc(asof)
    # Pattern-recurrence unit is (role, lesson_category) — the FINCON per-role,
    # per-category recurrence key. A belief whose pattern recurs in `new` is promoted.
    #
    # distill_beliefs groups by (lesson_category, ticker), so a SINGLE week can emit
    # two same-(role, lesson_category) new beliefs for different tickers (e.g. AAPL
    # momentum alpha=+0.05 and TSLA momentum alpha<=0, both inside the top-budget set).
    # The positive-alpha promotion gate below must reflect the WHOLE category's net
    # evidence, NOT an arbitrary last-iterated survivor: a plain dict keyed on
    # (role, lesson_category) would be last-writer-wins, letting an unrelated ticker's
    # alpha sign decide a live belief's weekly->monthly promotion (3x stickier). We
    # therefore AGGREGATE the recurring new beliefs per (role, lesson_category) by
    # support-weighted mean alpha — making the gate order-invariant under any
    # permutation of `new`. Non-finite alpha_evidence is dropped from the weighting
    # (NaN/inf must never silently flip the gate; cf. the finite-guard family).
    by_key: dict[tuple[str, str], list[Belief]] = {}
    for nb in new:
        by_key.setdefault((nb.role, nb.lesson_category), []).append(nb)

    new_alpha_by_key: dict[tuple[str, str], float] = {}
    for key, group in by_key.items():
        finite = [g for g in group if math.isfinite(float(g.alpha_evidence))]
        if not finite:
            # All non-finite: cannot establish positive evidence -> do not promote.
            new_alpha_by_key[key] = 0.0
            continue
        total_support = sum(max(0, int(g.support_n)) for g in finite)
        if total_support > 0:
            new_alpha_by_key[key] = (
                sum(float(g.alpha_evidence) * max(0, int(g.support_n)) for g in finite)
                / total_support
            )
        else:
            # No support weight to differentiate; fall back to the plain mean so the
            # gate is still order-invariant (never depends on iteration order).
            new_alpha_by_key[key] = sum(float(g.alpha_evidence) for g in finite) / len(finite)

    kept: list[Belief] = []
    expired: list[Belief] = []

    for b in active:
        dist = _parse_dt(b.asof_distilled)
        days = (asof - dist).total_seconds() / 86400.0 if dist else 0.0
        decayed = Belief(**asdict(b))
        decayed.recency = round(b.recency * _alpha(b.tier, days), 8)

        key = (b.role, b.lesson_category)
        # ar99 idempotency / double-fire guard (ADR-0081 §4): the cron passes
        # asof=now(), so a duplicate same-week firing (POSIX DOM/DOW OR-fire, manual
        # re-run, or a retry after partial failure) re-distills the SAME trailing
        # reflections into a NEW belief_id carrying the SAME backing decisions. Keyed
        # only on (role, lesson_category), that was treated as a fresh "recurrence"
        # and double-promoted (access_counter+1, importance+K, weekly->monthly) on
        # ZERO new evidence.
        #
        # The duplicate-run signature is PROVABLE only when BOTH the active belief and
        # the freshly-distilled beliefs carry decision_ids (the real distill_beliefs
        # path sets them — :346): a same-week re-run yields fresh decision_ids that are
        # a SUBSET of the active belief's prior set (no genuinely-new decision). When
        # the fresh beliefs introduce a decision_id NOT already backing the active
        # belief, it is genuine new evidence and promotes as before. When decision_ids
        # are ABSENT on either side (legacy beliefs / un-provenanced fixtures), we
        # CANNOT prove a duplicate, so we fall back to the prior category-recurrence
        # behavior (promote) rather than silently suppress a real cross-ticker
        # recurrence — fail-toward-the-legacy-semantics, not toward over-suppression.
        recurrence_present = key in new_alpha_by_key
        recurrence_is_genuine = recurrence_present  # default: legacy (no provenance to prove otherwise)
        if recurrence_present:
            prior_ids = set(b.oracle_provenance.get("decision_ids") or [])
            fresh_ids: set = set()
            for nb in by_key.get(key, []):
                fresh_ids |= set(nb.oracle_provenance.get("decision_ids") or [])
            # Only when we have provenance on BOTH sides can we distinguish a genuine
            # recurrence from a same-evidence re-distillation. If we can, require a
            # truly-new decision_id; otherwise keep the legacy promote.
            if prior_ids and fresh_ids:
                recurrence_is_genuine = bool(fresh_ids - prior_ids)
        if recurrence_is_genuine:
            # PROMOTE on recurrence (FINMEM access-counter promotion).
            decayed.access_counter = b.access_counter + 1
            decayed.recency = 1.0
            decayed.asof_distilled = asof.isoformat()
            if new_alpha_by_key[key] > 0:
                decayed.importance = b.importance + IMPORTANCE_BONUS_K
                if decayed.tier == "weekly":
                    decayed.tier = "monthly"
                    decayed.half_life_days = HALF_LIFE_DAYS["monthly"]

        if decayed.recency < RECENCY_EXPIRE_EPSILON:
            decayed.status = "expired"
            expired.append(decayed)
        else:
            kept.append(decayed)

    # Add genuinely-new beliefs (no recurring active match) to the kept set.
    active_keys = {(b.role, b.lesson_category) for b in active}
    for nb in new:
        if (nb.role, nb.lesson_category) not in active_keys:
            kept.append(nb)

    # Per-role budget enforcement: evict lowest-score active beliefs.
    by_role: dict[str, list[Belief]] = {}
    for b in kept:
        by_role.setdefault(b.role, []).append(b)
    final_kept: list[Belief] = []
    for _role, beliefs in by_role.items():
        if len(beliefs) <= budget:
            final_kept.extend(beliefs)
            continue
        ranked = sorted(beliefs, key=lambda b: (_eviction_score(b), b.belief_id))
        n_evict = len(beliefs) - budget
        for b in ranked[:n_evict]:
            ev = Belief(**asdict(b))
            ev.status = "expired"
            expired.append(ev)
        final_kept.extend(ranked[n_evict:])

    return final_kept, expired


def access_touch(belief_id: str, *, path: Path | None = None) -> None:
    """FINMEM access-counter bump: called by the retriever when a belief is surfaced
    into a prompt. Appends an 'active' row with access_counter+1, recency=1.0.
    Best-effort, never raises (prompt rendering must never break on a belief-store write)."""
    try:
        p = path or BELIEFS_PATH
        rows = load_belief_rows(path=p)
        latest: Belief | None = None
        for row in rows:
            if row.get("belief_id") == belief_id:
                latest = _belief_from_row(row)
        if latest is None or latest.status != "active":
            return
        bumped = Belief(**asdict(latest))
        bumped.access_counter = latest.access_counter + 1
        bumped.recency = 1.0
        _append_beliefs([bumped], p)
    except Exception as exc:  # noqa: BLE001
        logger.warning("weekly_retro.access_touch failed for %s (%s); non-blocking",
                       belief_id, exc)


# ---------------------------------------------------------------------------
# O3 closer + top-level entry point
# ---------------------------------------------------------------------------


@dataclass
class WeeklyRetroResult:
    asof: str
    n_reflections_read: int
    beliefs_distilled: int
    beliefs_expired: int
    active_belief_count: int          # post-pass total across all roles
    under_budget: bool                # active_belief_count <= BELIEF_BUDGET_PER_ROLE * n_roles
    promotion_readiness_emitted: bool # True iff the promotion_event was written
    transitions: list[str] = field(default_factory=list)  # cron silence-contract one-liners


def emit_promotion_readiness(result: WeeklyRetroResult, asof: datetime) -> None:
    """Close O3. Emit ONE promotion_event audit row whose payload sets
    weekly_retro_promotion_readiness=True — IFF the pass completed AND under_budget.

    This is the SINGLE missing producer for the gate field consumed at promotion.py:158.
    Wire shape EXACTLY matches the existing test seed (tests/governance/test_promotion.py:39-50).

    NOTE: this writes ONE field the gate reads; it does NOT relax any gate threshold.
    Passing remains necessary-not-sufficient (operator sign-off unchanged).
    """
    if not result.under_budget:
        return
    from hermes_quant.governance import audit_log

    audit_log.append(
        audit_log.GovernanceEvent(
            kind="promotion_event",            # already a VALID_KIND (audit_log.py:44)
            asof=_ensure_utc(asof),
            source="weekly_retro",
            payload={
                "weekly_retro_promotion_readiness": True,
                "active_belief_count": result.active_belief_count,
                "beliefs_distilled": result.beliefs_distilled,
            },
        )
    )


def run_weekly_retro(asof: datetime, *,
                     reflections_path: Path | None = None,
                     beliefs_path: Path | None = None,
                     emit_promotion: bool = True) -> WeeklyRetroResult:
    """Top-level: load -> Oracle-guard -> split -> group -> distill -> FINMEM
    promote/expire -> persist beliefs.jsonl -> (optionally) close O3. Pure +
    deterministic; the cron wraps this under the flag. `emit_promotion=False` lets
    tests exercise distillation without writing to the shared audit log."""
    asof = _ensure_utc(asof)
    bpath = beliefs_path or BELIEFS_PATH

    reflections = load_reflections(asof, path=reflections_path)

    # Distill per injection role (selective propagation; v1 ships PM only by default,
    # but the loop is role-keyed so W3/W7 can add roles without a schema change).
    new_beliefs: list[Belief] = distill_beliefs(
        reflections, asof=asof, role="portfolio_manager"
    )

    prior_rows = load_belief_rows(path=bpath)
    active = materialize_active(prior_rows, asof)

    kept, expired = decay_and_promote(active, new_beliefs, asof=asof)

    # Persist: only the genuinely-new distilled beliefs + expiry rows are appended
    # (append-only projection). Recurrence-promoted beliefs are re-appended so the
    # access/recency/tier bumps are durable.
    new_ids = {b.belief_id for b in new_beliefs}
    active_keys = {(b.role, b.lesson_category) for b in active}
    rows_to_write: list[Belief] = []
    for b in kept:
        # genuinely-new belief OR a promoted/decayed prior belief whose state changed
        if b.belief_id in new_ids and (b.role, b.lesson_category) not in active_keys:
            rows_to_write.append(b)
        elif (b.role, b.lesson_category) in active_keys:
            rows_to_write.append(b)
    rows_to_write.extend(expired)
    _append_beliefs(rows_to_write, bpath)

    final_active = materialize_active(load_belief_rows(path=bpath), asof)
    n_roles = max(1, len(INJECTION_ROLES))
    under_budget = len(final_active) <= BELIEF_BUDGET_PER_ROLE * n_roles

    transitions: list[str] = []
    if new_beliefs:
        transitions.append(f"distilled {len(new_beliefs)} belief(s)")
    if expired:
        transitions.append(f"expired {len(expired)} belief(s)")

    result = WeeklyRetroResult(
        asof=asof.isoformat(),
        n_reflections_read=len(reflections),
        beliefs_distilled=len(new_beliefs),
        beliefs_expired=len(expired),
        active_belief_count=len(final_active),
        under_budget=under_budget,
        promotion_readiness_emitted=False,
        transitions=transitions,
    )

    if emit_promotion:
        emit_promotion_readiness(result, asof)
        if result.under_budget:
            result.promotion_readiness_emitted = True
            transitions.append("weekly_retro_promotion_readiness=True")

    return result
