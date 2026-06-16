"""W2 weekly-retro distillation + FINMEM + Oracle-provenance + O3-emission unit tests.

ADR-0081 §1/§2/§4. Uses the W1-liveness test idiom: monkeypatch the belief/reflection
paths to tmp_path so nothing touches ~/.hermes, and synthesize reflection rows directly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hermes_quant.memory import weekly_retro
from hermes_quant.memory.weekly_retro import (
    BELIEF_BUDGET_PER_ROLE,
    CURRENT_BELIEF_SCHEMA_VERSION,
    HALF_LIFE_DAYS,
    MIN_SUPPORT_N,
    RECENCY_EXPIRE_EPSILON,
    Belief,
    access_touch,
    decay_and_promote,
    distill_beliefs,
    materialize_active,
    run_weekly_retro,
    split_winners_losers,
)

ASOF = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _reflection(
    *,
    decision_id: str,
    ticker: str,
    alpha_return: float,
    raw_return: float | None = None,
    lesson_category: str = "thesis_invalidation_at_earnings",
    asof_resolution: datetime | None = None,
    tau_observable: datetime | None = None,
    holding_days: int = 5,
) -> dict:
    res = asof_resolution or (ASOF - timedelta(days=2))
    tau = tau_observable or (ASOF - timedelta(days=1))
    return {
        "schema_version": 1,
        "reflection_id": f"ref_{decision_id}",
        "decision_id": decision_id,
        "asof_resolution": res.isoformat(),
        "tau_observable": tau.isoformat(),
        "ticker": ticker.upper(),
        "raw_return": raw_return if raw_return is not None else alpha_return,
        "alpha_return": alpha_return,
        "benchmark": "SPY",
        "holding_days": holding_days,
        "outcome_quality": 3,
        "reflection_text": f"reflection for {ticker} {lesson_category}",
        "lesson_category": lesson_category,
        "reflector_model": "stub-v0.1",
        "reflector_prompt_hash": "stub:abc",
    }


def _write_reflections(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True, default=str) + "\n")


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "reflections.jsonl", tmp_path / "beliefs.jsonl"


# ---------------------------------------------------------------------------
# Split-by-alpha (closes tauric gap #8)
# ---------------------------------------------------------------------------


def test_split_is_by_alpha_not_raw_pnl() -> None:
    """A row with raw_return>0 but alpha_return<0 lands in the LOSER split."""
    good_raw_bad_alpha = _reflection(
        decision_id="dec_a", ticker="AAPL", alpha_return=-0.03, raw_return=+0.05
    )
    bad_raw_good_alpha = _reflection(
        decision_id="dec_b", ticker="AAPL", alpha_return=+0.04, raw_return=-0.02
    )
    winners, losers = split_winners_losers([good_raw_bad_alpha, bad_raw_good_alpha])
    assert good_raw_bad_alpha in losers, "positive raw P&L but negative alpha is a LOSER"
    assert bad_raw_good_alpha in winners, "negative raw P&L but positive alpha is a WINNER"


# ---------------------------------------------------------------------------
# Distillation: budget cap + min support
# ---------------------------------------------------------------------------


def test_distill_respects_budget_cap() -> None:
    rows = []
    # 6 distinct (category, ticker) groups, each with enough support.
    for g in range(6):
        for i in range(MIN_SUPPORT_N + 1):
            rows.append(
                _reflection(
                    decision_id=f"dec_{g}_{i}",
                    ticker=f"TKR{g}",
                    alpha_return=0.02 * (g + 1),
                    lesson_category="regime_shift_invalidation",
                )
            )
    beliefs = distill_beliefs(rows, asof=ASOF, role="portfolio_manager")
    pm = [b for b in beliefs if b.role == "portfolio_manager"]
    assert len(pm) <= BELIEF_BUDGET_PER_ROLE


def test_distill_requires_min_support() -> None:
    """A group with support_n < MIN_SUPPORT_N produces NO belief."""
    rows = [
        _reflection(decision_id=f"dec_{i}", ticker="AAPL", alpha_return=0.03)
        for i in range(MIN_SUPPORT_N - 1)
    ]
    beliefs = distill_beliefs(rows, asof=ASOF)
    assert beliefs == [], "no single-trade / thin-support beliefs"


# ---------------------------------------------------------------------------
# Oracle provenance
# ---------------------------------------------------------------------------


def test_belief_carries_oracle_provenance() -> None:
    tau_a = ASOF - timedelta(days=3)
    tau_b = ASOF - timedelta(days=1)
    rows = [
        _reflection(decision_id="dec_a", ticker="AAPL", alpha_return=0.03, tau_observable=tau_a),
        _reflection(decision_id="dec_b", ticker="AAPL", alpha_return=0.04, tau_observable=tau_b),
        _reflection(decision_id="dec_c", ticker="AAPL", alpha_return=0.05, tau_observable=tau_a),
    ]
    beliefs = distill_beliefs(rows, asof=ASOF)
    assert beliefs
    b = beliefs[0]
    prov = b.oracle_provenance
    assert prov["source"] == "agent_reflection"
    # tau_observable_max == max over backers
    assert weekly_retro._parse_dt(prov["tau_observable_max"]) == tau_b
    assert set(prov["decision_ids"]) == {"dec_a", "dec_b", "dec_c"}
    assert prov["decision_ids"], "non-empty decision_ids audit trail"


def test_belief_level_oracle_guard_excludes_future(paths) -> None:
    """A belief whose tau_observable_max >= asof is NOT returned by materialize_active.

    Mirrors test_w1_oracle_guard_excludes_future_reflection.
    """
    _refl_path, bpath = paths
    # tau_observable in the future relative to the read asof.
    future_tau = ASOF + timedelta(days=10)
    rows = [
        _reflection(decision_id=f"dec_{i}", ticker="AAPL", alpha_return=0.03,
                    tau_observable=future_tau)
        for i in range(MIN_SUPPORT_N)
    ]
    # Distill at a LATER asof so the belief is created, then read at an EARLIER asof.
    beliefs = distill_beliefs(rows, asof=future_tau + timedelta(days=1))
    assert beliefs
    weekly_retro._append_beliefs(beliefs, bpath)

    belief_rows = weekly_retro.load_belief_rows(path=bpath)
    active_now = materialize_active(belief_rows, ASOF)
    assert active_now == [], "belief surfacing a future outcome must be excluded at this asof"


# ---------------------------------------------------------------------------
# FINMEM decay / access / expire
# ---------------------------------------------------------------------------


def test_finmem_decay_expires_stale_belief(paths) -> None:
    """Advance asof >> half_life_days; recency drops below epsilon -> expired row."""
    _refl_path, bpath = paths
    rows = [
        _reflection(decision_id=f"dec_{i}", ticker="AAPL", alpha_return=0.03)
        for i in range(MIN_SUPPORT_N)
    ]
    first = distill_beliefs(rows, asof=ASOF)
    assert first
    weekly_retro._append_beliefs(first, bpath)

    # A much-later pass with NO recurrence (different category) -> the old belief decays.
    later = ASOF + timedelta(days=120)  # >> weekly half-life (14d)
    active = materialize_active(weekly_retro.load_belief_rows(path=bpath), later)
    assert active, "still active before decay pass"
    kept, expired = weekly_retro.decay_and_promote(active, [], asof=later)
    assert expired, "a belief aged 120d past a 14d half-life must expire"
    assert all(b.recency < RECENCY_EXPIRE_EPSILON for b in expired)

    weekly_retro._append_beliefs(expired, bpath)
    active_after = materialize_active(weekly_retro.load_belief_rows(path=bpath), later)
    assert active_after == [], "expired belief no longer materializes as active"


def test_finmem_access_bump_resets_recency(paths) -> None:
    _refl_path, bpath = paths
    rows = [
        _reflection(decision_id=f"dec_{i}", ticker="AAPL", alpha_return=0.03)
        for i in range(MIN_SUPPORT_N)
    ]
    beliefs = distill_beliefs(rows, asof=ASOF)
    weekly_retro._append_beliefs(beliefs, bpath)
    bid = beliefs[0].belief_id

    access_touch(bid, path=bpath)

    latest = materialize_active(weekly_retro.load_belief_rows(path=bpath), ASOF + timedelta(hours=1))
    touched = [b for b in latest if b.belief_id == bid]
    assert touched
    assert touched[0].recency == 1.0
    assert touched[0].access_counter == 1


# ---------------------------------------------------------------------------
# FINMEM promotion recurrence must be order-invariant (no last-writer-wins)
# ---------------------------------------------------------------------------


def _belief(
    *,
    belief_id: str,
    ticker: str,
    alpha_evidence: float,
    lesson_category: str = "momentum",
    role: str = "portfolio_manager",
    tier: str = "weekly",
    importance: float = 1.0,
    support_n: int = 5,
    asof_distilled: datetime = ASOF,
) -> Belief:
    return Belief(
        schema_version=CURRENT_BELIEF_SCHEMA_VERSION,
        belief_id=belief_id,
        tier=tier,
        role=role,
        lesson_category=lesson_category,
        verbal_delta=f"belief for {ticker} {lesson_category}",
        alpha_evidence=alpha_evidence,
        support_n=support_n,
        half_life_days=HALF_LIFE_DAYS[tier],
        access_counter=0,
        importance=importance,
        recency=1.0,
        oracle_provenance={"tau_observable_max": (ASOF - timedelta(days=1)).isoformat()},
        asof_distilled=asof_distilled.isoformat(),
        status="active",
    )


def test_decay_and_promote_recurrence_is_order_invariant() -> None:
    """A same-(role,category) belief's promotion must NOT depend on `new` ordering.

    distill_beliefs can emit two same-(role, lesson_category) beliefs for DIFFERENT
    tickers in the same week (e.g. AAPL momentum alpha=+0.05 and TSLA momentum
    alpha<=0). The promotion gate at decay_and_promote reads ONE survivor's alpha
    sign to decide whether an ACTIVE same-category belief is upgraded weekly->monthly
    (importance += K, half_life 14d -> 60d, i.e. 3x stickier in the live PM/RM prompt).
    Permuting `new` must not flip the kept belief's tier / importance / half_life.
    """
    active = [_belief(belief_id="active-MSFT-momentum", ticker="MSFT", alpha_evidence=0.0)]
    winner = _belief(belief_id="bel_weekly_pm_AAPL", ticker="AAPL", alpha_evidence=+0.05)
    loser = _belief(belief_id="bel_weekly_pm_TSLA", ticker="TSLA", alpha_evidence=-0.03)

    kept_a, _ = decay_and_promote(list(active), [winner, loser], asof=ASOF)
    kept_b, _ = decay_and_promote(list(active), [loser, winner], asof=ASOF)

    a = next(b for b in kept_a if b.belief_id == "active-MSFT-momentum")
    b = next(x for x in kept_b if x.belief_id == "active-MSFT-momentum")

    assert (a.tier, a.importance, a.half_life_days) == (
        b.tier, b.importance, b.half_life_days
    ), (
        "decay_and_promote promotion must be order-invariant under permutation of "
        f"`new`; got {(a.tier, a.importance, a.half_life_days)} vs "
        f"{(b.tier, b.importance, b.half_life_days)} (last-writer-wins recurrence key)"
    )


def test_decay_and_promote_promotes_on_net_positive_category_evidence() -> None:
    """With a genuine winner present, the active category belief IS promoted
    (net support-weighted alpha is positive), regardless of which new belief is last."""
    active = [_belief(belief_id="active-MSFT-momentum", ticker="MSFT", alpha_evidence=0.0)]
    # AAPL winner clearly dominates (large magnitude + support); TSLA is flat.
    winner = _belief(belief_id="bel_weekly_pm_AAPL", ticker="AAPL",
                     alpha_evidence=+0.05, support_n=8)
    flat = _belief(belief_id="bel_weekly_pm_TSLA", ticker="TSLA",
                   alpha_evidence=0.0, support_n=4)

    for ordering in ([winner, flat], [flat, winner]):
        kept, _ = decay_and_promote(list(active), list(ordering), asof=ASOF)
        promoted = next(b for b in kept if b.belief_id == "active-MSFT-momentum")
        assert promoted.tier == "monthly", "net-positive category evidence must promote"
        assert promoted.importance == 2.0
        assert promoted.half_life_days == HALF_LIFE_DAYS["monthly"]


def test_decay_and_promote_denies_on_net_nonpositive_category_evidence() -> None:
    """When the dominant same-category evidence is a loss/flat, an active belief is
    refreshed (access bump / recency reset) but NOT upgraded to monthly stickiness."""
    active = [_belief(belief_id="active-MSFT-momentum", ticker="MSFT", alpha_evidence=0.0)]
    # The loss dominates by support weight; the small winner cannot flip the net.
    loser = _belief(belief_id="bel_weekly_pm_TSLA", ticker="TSLA",
                    alpha_evidence=-0.05, support_n=9)
    tiny_win = _belief(belief_id="bel_weekly_pm_AAPL", ticker="AAPL",
                       alpha_evidence=+0.01, support_n=2)

    for ordering in ([loser, tiny_win], [tiny_win, loser]):
        kept, _ = decay_and_promote(list(active), list(ordering), asof=ASOF)
        refreshed = next(b for b in kept if b.belief_id == "active-MSFT-momentum")
        assert refreshed.tier == "weekly", "net-nonpositive evidence must not promote"
        assert refreshed.importance == 1.0
        assert refreshed.half_life_days == HALF_LIFE_DAYS["weekly"]
        assert refreshed.access_counter == 1, "recurrence still bumps the access counter"


# ---------------------------------------------------------------------------
# Rebuildable / deterministic projection
# ---------------------------------------------------------------------------


def test_beliefs_jsonl_is_append_only_projection(paths) -> None:
    """Delete beliefs.jsonl, re-run on the SAME reflections+asof -> identical active set."""
    refl_path, bpath = paths
    rows = [
        _reflection(decision_id=f"dec_{i}", ticker="AAPL", alpha_return=0.03)
        for i in range(MIN_SUPPORT_N + 1)
    ]
    _write_reflections(refl_path, rows)

    r1 = run_weekly_retro(ASOF, reflections_path=refl_path, beliefs_path=bpath,
                          emit_promotion=False)
    active1 = materialize_active(weekly_retro.load_belief_rows(path=bpath), ASOF + timedelta(hours=1))

    bpath.unlink()  # delete the derived projection
    r2 = run_weekly_retro(ASOF, reflections_path=refl_path, beliefs_path=bpath,
                          emit_promotion=False)
    active2 = materialize_active(weekly_retro.load_belief_rows(path=bpath), ASOF + timedelta(hours=1))

    assert r1.beliefs_distilled == r2.beliefs_distilled
    assert {b.belief_id for b in active1} == {b.belief_id for b in active2}
    assert [b.verbal_delta for b in active1] == [b.verbal_delta for b in active2]


def test_distill_is_deterministic() -> None:
    rows = [
        _reflection(decision_id=f"dec_{i}", ticker="AAPL", alpha_return=0.03)
        for i in range(MIN_SUPPORT_N + 2)
    ]
    b1 = distill_beliefs(rows, asof=ASOF)
    b2 = distill_beliefs(rows, asof=ASOF)
    assert [b.belief_id for b in b1] == [b.belief_id for b in b2]
    assert [b.verbal_delta for b in b1] == [b.verbal_delta for b in b2]


# ---------------------------------------------------------------------------
# O3 closure — the end-to-end gate-field producer
# ---------------------------------------------------------------------------


def test_emit_promotion_readiness_closes_O3(tmp_path, monkeypatch) -> None:  # noqa: N802
    """run_weekly_retro emits the producer the gate at promotion.py:158 consumes."""
    from hermes_quant.governance import audit_log, promotion

    audit_p = tmp_path / "governance" / "audit_log.jsonl"
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", audit_p)

    refl_path = tmp_path / "reflections.jsonl"
    bpath = tmp_path / "beliefs.jsonl"
    rows = [
        _reflection(decision_id=f"dec_{i}", ticker="AAPL", alpha_return=0.03)
        for i in range(MIN_SUPPORT_N + 1)
    ]
    _write_reflections(refl_path, rows)

    result = run_weekly_retro(ASOF, reflections_path=refl_path, beliefs_path=bpath,
                              emit_promotion=True)
    assert result.promotion_readiness_emitted is True

    # The emitted row matches the test_promotion.py:39-50 seed shape.
    emitted = [
        e for e in audit_log.read(kinds=["promotion_event"])
        if e.source == "weekly_retro"
        and e.payload.get("weekly_retro_promotion_readiness") is True
    ]
    assert emitted, "a weekly_retro promotion_event with readiness=True must exist"

    # End-to-end: the gate no longer blocks on weekly_retro_promotion_readiness.
    decision = promotion.evaluate(ASOF)
    assert decision.weekly_retro_promotion_readiness is True
    assert "weekly_retro_promotion_readiness=False" not in decision.blocked_by


def test_emit_promotion_readiness_skipped_when_over_budget(tmp_path, monkeypatch) -> None:
    """If the pass is NOT under budget, no readiness row is emitted (necessary-not-sufficient)."""
    from hermes_quant.governance import audit_log
    from hermes_quant.memory.weekly_retro import WeeklyRetroResult, emit_promotion_readiness

    audit_p = tmp_path / "governance" / "audit_log.jsonl"
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", audit_p)

    over = WeeklyRetroResult(
        asof=ASOF.isoformat(), n_reflections_read=0, beliefs_distilled=0,
        beliefs_expired=0, active_belief_count=999, under_budget=False,
        promotion_readiness_emitted=False, transitions=[],
    )
    emit_promotion_readiness(over, ASOF)
    rows = list(audit_log.read(kinds=["promotion_event"]))
    assert rows == [], "over-budget pass must NOT emit a readiness row"
