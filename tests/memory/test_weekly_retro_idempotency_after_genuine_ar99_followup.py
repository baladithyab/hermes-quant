"""ar99 follow-up: weekly-retro idempotency must SURVIVE a prior genuine recurrence.

The ar99 fix (commit a6c6040) gates the FINMEM recurrence-promotion on genuinely-new
evidence: a recurrence promotes only when the freshly-distilled belief introduces a
backing ``decision_id`` not already in the ACTIVE belief's
``oracle_provenance["decision_ids"]``. That correctly suppresses a pure same-week
duplicate (no new evidence).

BUT the original fix never MERGED the genuinely-new decision_ids into the promoted
belief's provenance. ``decay_and_promote`` copies the active belief's provenance
verbatim (``Belief(**asdict(b))``) and updates access_counter / importance / tier /
recency / asof on a genuine promotion — but leaves ``decision_ids`` as the OLD
``prior_ids``. Consequence: once a genuine recurrence introduces a new decision_id
``d3``, the kept belief STILL records only ``{d1, d2}``. The very next same-week
DUPLICATE firing re-distills the same trailing reflections (``{d1, d2, d3}``),
recomputes ``fresh_ids - prior_ids = {d3}`` AGAIN (because prior_ids was never grown),
and DOUBLE-PROMOTES on zero new evidence — exactly the inflation ar99 set out to stop.

The cron re-distills a TRAILING window and passes ``asof=now()``, so a genuine new
decision landing this week is present in BOTH the genuine firing AND any same-week
duplicate firing's fresh set. This is the realistic compound scenario the original
ar99 tests did not cover (they stop after a single genuine promotion).

Fix: union ``prior_ids | fresh_ids`` into the promoted belief's
``oracle_provenance["decision_ids"]`` so the new-evidence guard is DURABLE. Self-
contained; does not import or edit the existing ar99 test module.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hermes_quant.memory.weekly_retro import (
    HALF_LIFE_DAYS,
    IMPORTANCE_BONUS_K,
    Belief,
    decay_and_promote,
)

ASOF = datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC)


def _belief(
    belief_id: str,
    decision_ids: set[str],
    *,
    tier: str = "weekly",
    access_counter: int = 0,
    importance: float = 1.0,
    asof_distilled: str = "2026-06-15T12:00:00+00:00",
) -> Belief:
    return Belief(
        schema_version=1,
        belief_id=belief_id,
        tier=tier,
        role="committee",
        lesson_category="thesis_invalidation_at_earnings",
        verbal_delta="x",
        alpha_evidence=0.6,
        support_n=6,
        half_life_days=HALF_LIFE_DAYS[tier],
        access_counter=access_counter,
        importance=importance,
        recency=1.0,
        oracle_provenance={
            "source": "agent_reflection",
            "tau_observable_max": "2026-06-14T00:00:00+00:00",
            "decision_ids": sorted(decision_ids),
        },
        asof_distilled=asof_distilled,
        status="active",
    )


def test_genuine_recurrence_merges_decision_ids_so_a_duplicate_is_idempotent() -> None:
    """After a GENUINE recurrence (introduces d3), the kept belief must record the
    UNION {d1,d2,d3}; a same-week DUPLICATE re-run of the SAME corpus must then NOT
    re-promote (the new-evidence guard is durable only if prior_ids absorbed d3)."""
    # Run 1: genuine recurrence — fresh introduces d3, not in the active belief.
    active = [_belief("b-active", {"d1", "d2"})]
    fresh = [_belief("b-fresh", {"d1", "d2", "d3"}, asof_distilled=ASOF.isoformat())]
    kept1, _ = decay_and_promote(active, fresh, asof=ASOF, budget=50)
    assert len(kept1) == 1
    b1 = kept1[0]
    # Genuine recurrence still promotes (ar99 byte-identical behavior).
    assert b1.access_counter == 1, "a genuine new-evidence recurrence must promote"
    assert b1.tier == "monthly"
    assert b1.importance == pytest.approx(1.0 + IMPORTANCE_BONUS_K)
    # THE FIX: the promoted belief must now carry the UNION of prior + fresh ids,
    # otherwise the new-evidence guard cannot recognise the next duplicate.
    assert set(b1.oracle_provenance.get("decision_ids") or []) == {"d1", "d2", "d3"}, (
        "a genuine promotion must merge the fresh decision_ids into the kept belief's "
        "provenance; otherwise prior_ids never grows and a same-week duplicate re-run "
        "re-detects d3 as 'new evidence' and double-promotes"
    )

    # Run 2: same-week DUPLICATE firing. The active set is run-1's output (b1); the
    # cron re-distills the SAME trailing reflections -> identical fresh {d1,d2,d3}.
    active2 = [b1]
    fresh2 = [_belief("b-fresh", {"d1", "d2", "d3"}, asof_distilled=ASOF.isoformat())]
    kept2, _ = decay_and_promote(active2, fresh2, asof=ASOF, budget=50)
    assert len(kept2) == 1
    b2 = kept2[0]
    # ZERO new evidence on the duplicate -> the promotion must NOT fire again.
    assert b2.access_counter == b1.access_counter, (
        f"same-week DUPLICATE re-run bumped access_counter {b1.access_counter}->"
        f"{b2.access_counter} after a prior genuine recurrence (double-fire: the fresh "
        "decision_ids were not merged into the kept belief on the genuine promotion)"
    )
    assert b2.importance == pytest.approx(b1.importance), (
        "duplicate re-run must not re-add IMPORTANCE_BONUS_K"
    )


def test_a_truly_new_decision_after_a_genuine_recurrence_still_promotes() -> None:
    """Non-vacuity / no-over-suppression: the merge must NOT freeze the guard. A LATER
    recurrence that introduces a genuinely-new d4 (beyond the merged {d1,d2,d3}) must
    still promote."""
    active = [_belief("b-active", {"d1", "d2"})]
    fresh = [_belief("b-fresh", {"d1", "d2", "d3"}, asof_distilled=ASOF.isoformat())]
    kept1, _ = decay_and_promote(active, fresh, asof=ASOF, budget=50)
    b1 = kept1[0]
    assert set(b1.oracle_provenance.get("decision_ids") or []) == {"d1", "d2", "d3"}

    # A genuinely-new later recurrence (d4 is new beyond the merged set).
    later = ASOF + timedelta(days=7)
    fresh2 = [_belief("b-fresh2", {"d1", "d2", "d3", "d4"}, asof_distilled=later.isoformat())]
    kept2, _ = decay_and_promote([b1], fresh2, asof=later, budget=50)
    b2 = kept2[0]
    assert b2.access_counter == b1.access_counter + 1, (
        "a genuinely-new d4 after the merge must still promote (no over-suppression)"
    )
    assert set(b2.oracle_provenance.get("decision_ids") or []) == {"d1", "d2", "d3", "d4"}
