"""Idempotency / double-fire regression for the W2 weekly-retro FINMEM promotion.

ADR-0081 §4. The cron (ops/scripts/quant-weekly-retro.py) calls
`run_weekly_retro(datetime.now(UTC))`, so `asof` is wall-clock NOW. Two firings in
the SAME week — a POSIX DOM/DOW OR-fire, a manual re-run, or a retry after a partial
failure — feed the SAME trailing reflections corpus to `distill_beliefs` a second
time. Because the recurrence key is `(role, lesson_category)`, the second run was
treated as a fresh FINMEM "recurrence": it bumped `access_counter`, reset `recency`,
added `IMPORTANCE_BONUS_K`, and upgraded the belief weekly->monthly (slower decay) —
all with ZERO new evidence.

That is a double-fire: the same evidence promoted the same belief twice. A belief's
tier/importance govern how long it survives and how heavily the LLM committee /
portfolio-manager prompts weight it, so a duplicate cron run silently corrupts the
decision-memory accounting.

The fix gates the recurrence-promotion on GENUINELY-NEW evidence: a recurrence
promotes only when the freshly-distilled belief introduces a backing decision_id not
already in the active belief's oracle_provenance. Same evidence re-distilled (decision
set is a subset) -> decay only, no promotion -> the second run is idempotent.

Uses the W1-liveness path-monkeypatch idiom: paths go to tmp_path so nothing touches
~/.hermes. Self-contained — does not import or edit the existing test module.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hermes_quant.memory import weekly_retro
from hermes_quant.memory.weekly_retro import (
    IMPORTANCE_BONUS_K,
    MIN_SUPPORT_N,
    decay_and_promote,
    distill_beliefs,
    materialize_active,
    run_weekly_retro,
)

ASOF = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _reflection(*, decision_id: str, alpha_return: float = 0.03) -> dict:
    res = ASOF - timedelta(days=2)
    tau = ASOF - timedelta(days=1)
    return {
        "schema_version": 1,
        "reflection_id": f"ref_{decision_id}",
        "decision_id": decision_id,
        "asof_resolution": res.isoformat(),
        "tau_observable": tau.isoformat(),
        "ticker": "AAPL",
        "raw_return": alpha_return,
        "alpha_return": alpha_return,
        "benchmark": "SPY",
        "holding_days": 5,
        "outcome_quality": 3,
        "reflection_text": "x",
        "lesson_category": "thesis_invalidation_at_earnings",
        "reflector_model": "stub-v0.1",
        "reflector_prompt_hash": "stub:abc",
    }


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True, default=str) + "\n")


def test_same_week_rerun_does_not_double_promote(tmp_path: Path) -> None:
    """Two cron firings in the same week with the SAME reflections -> no double-promote.

    RED before the fix: the 2nd run upgraded the belief weekly->monthly, bumped
    access_counter 0->1, and added IMPORTANCE_BONUS_K (1.0->2.0). GREEN after: the
    2nd run is a pure decay (no new evidence -> no recurrence promotion).
    """
    refl = tmp_path / "reflections.jsonl"
    bel = tmp_path / "beliefs.jsonl"
    rows = [
        _reflection(decision_id=f"dec_{i}") for i in range(MIN_SUPPORT_N + 1)
    ]
    _write(refl, rows)

    # Run 1: noon Monday.
    run_weekly_retro(ASOF, reflections_path=refl, beliefs_path=bel, emit_promotion=False)
    a1 = materialize_active(weekly_retro.load_belief_rows(path=bel), ASOF + timedelta(hours=1))
    assert len(a1) == 1, "first pass distills exactly one belief"
    b1 = a1[0]
    assert b1.tier == "weekly"
    assert b1.access_counter == 0
    assert b1.importance == 1.0

    # Run 2: cron double-fire 10 minutes later. SAME week, SAME reflections, beliefs.jsonl
    # INTACT. asof differs (wall-clock NOW) so the freshly-distilled belief_id differs,
    # which is exactly what made the stale code treat it as a recurrence.
    asof2 = ASOF + timedelta(minutes=10)
    run_weekly_retro(asof2, reflections_path=refl, beliefs_path=bel, emit_promotion=False)
    a2 = materialize_active(weekly_retro.load_belief_rows(path=bel), asof2 + timedelta(hours=1))
    assert len(a2) == 1, "still exactly one active belief after the duplicate run"
    b2 = a2[0]

    # The duplicate run carried NO new evidence -> the FINMEM promotion must NOT fire.
    assert b2.tier == "weekly", (
        f"duplicate same-week run upgraded tier weekly->{b2.tier} with no new evidence "
        "(double-fire promotion)"
    )
    assert b2.access_counter == 0, (
        f"duplicate same-week run bumped access_counter 0->{b2.access_counter} "
        "(double-fire promotion)"
    )
    assert b2.importance == 1.0, (
        f"duplicate same-week run inflated importance 1.0->{b2.importance} "
        "(double-fire promotion)"
    )


def test_genuine_new_evidence_still_promotes(tmp_path: Path) -> None:
    """Non-vacuity guard: a recurrence backed by a NEW decision_id STILL promotes.

    This pins the byte-identical genuine-recurrence path so the fix only suppresses the
    no-new-evidence duplicate, not a real second-week recurrence.
    """
    refl = tmp_path / "reflections.jsonl"
    bel = tmp_path / "beliefs.jsonl"

    # Week 1 corpus.
    _write(refl, [_reflection(decision_id=f"dec_{i}") for i in range(MIN_SUPPORT_N + 1)])
    run_weekly_retro(ASOF, reflections_path=refl, beliefs_path=bel, emit_promotion=False)

    # Week 2: a recurrence of the SAME pattern but backed by NEW decisions. tau within the
    # trailing window relative to the later asof so the rows resolve.
    asof2 = ASOF + timedelta(days=7)
    new_rows = []
    for i in range(MIN_SUPPORT_N + 1):
        r = _reflection(decision_id=f"dec_week2_{i}")
        r["asof_resolution"] = (asof2 - timedelta(days=1)).isoformat()
        r["tau_observable"] = (asof2 - timedelta(hours=1)).isoformat()
        new_rows.append(r)
    _write(refl, new_rows)

    run_weekly_retro(asof2, reflections_path=refl, beliefs_path=bel, emit_promotion=False)
    a2 = materialize_active(weekly_retro.load_belief_rows(path=bel), asof2 + timedelta(hours=1))
    assert len(a2) == 1
    b2 = a2[0]
    # Genuine recurrence with new evidence -> FINMEM promotion fires (unchanged behavior).
    assert b2.access_counter == 1, "a genuine new-evidence recurrence must still promote"
    assert b2.tier == "monthly", "a profitable new-evidence recurrence still upgrades tier"
    assert b2.importance == pytest.approx(1.0 + IMPORTANCE_BONUS_K)


def test_decay_and_promote_unit_no_promote_on_subset_evidence() -> None:
    """Unit-level: decay_and_promote does NOT promote when new decision_ids ⊆ active's."""
    active = distill_beliefs(
        [
            {
                "decision_id": f"dec_{i}",
                "ticker": "AAPL",
                "alpha_return": 0.03,
                "lesson_category": "thesis_invalidation_at_earnings",
                "tau_observable": (ASOF - timedelta(days=1)).isoformat(),
            }
            for i in range(MIN_SUPPORT_N + 1)
        ],
        asof=ASOF,
    )
    assert len(active) == 1
    # Re-distill the identical evidence at a later asof (new belief_id, same decision set).
    new = distill_beliefs(
        [
            {
                "decision_id": f"dec_{i}",
                "ticker": "AAPL",
                "alpha_return": 0.03,
                "lesson_category": "thesis_invalidation_at_earnings",
                "tau_observable": (ASOF - timedelta(days=1)).isoformat(),
            }
            for i in range(MIN_SUPPORT_N + 1)
        ],
        asof=ASOF + timedelta(minutes=10),
    )
    kept, _expired = decay_and_promote(active, new, asof=ASOF + timedelta(minutes=10))
    assert len(kept) == 1
    assert kept[0].access_counter == 0, "subset-evidence re-distill must not promote"
    assert kept[0].tier == "weekly"
    assert kept[0].importance == 1.0
