"""W3 monthly meta-retro engine unit tests + eval-gate acceptance criteria (plan §6).

Covers the four-condition eval gate (plan §4) as pytest-verifiable criteria:
  1. Reproduces byte-identical given config_hash + immutable corpus.
  2. Candidates pass novelty/dedup.
  3. Persona deltas are telemetry-only, |delta| <= 0.10.
  4. Oracle provenance preserved + debate-row asof<asof guard + advisory-plane only.

Plus the RD-Agent failure-tag rubric, FINCON repeat-threshold, and propose-only invariants.
All fixtures are frozen + deterministic (no network, no LLM, asof injected).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hermes_quant.memory import meta_retro
from hermes_quant.memory.meta_retro import (
    FAILURE_TAG_APPROACH,
    FAILURE_TAG_IMPLEMENTATION,
    apply_weekly_to_monthly,
    compute_lesson_trends,
    compute_persona_calibration,
    run_meta_retro,
    synthesize_candidate_hypotheses,
)
from hermes_quant.research.hypothesis import HypothesisRegistry

ASOF = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# fixtures (frozen corpora)
# ---------------------------------------------------------------------------


def _weekly_belief(
    *,
    belief_id: str,
    lesson_category: str,
    alpha_evidence: float,
    asof_distilled: datetime,
    support_n: int = 4,
    role: str = "portfolio_manager",
    tau_observable: datetime | None = None,
    recency: float = 1.0,
    importance: float = 1.0,
    status: str = "active",
    tier: str = "weekly",
) -> dict:
    tau = tau_observable or (asof_distilled - timedelta(hours=12))
    return {
        "schema_version": 1,
        "belief_id": belief_id,
        "tier": tier,
        "role": role,
        "lesson_category": lesson_category,
        "verbal_delta": f"belief about {lesson_category}",
        "alpha_evidence": alpha_evidence,
        "support_n": support_n,
        "half_life_days": 14.0,
        "access_counter": 0,
        "importance": importance,
        "recency": recency,
        "oracle_provenance": {
            "source": "agent_reflection",
            "tau_observable_max": tau.isoformat(),
            "decision_ids": [f"dec_{belief_id}"],
        },
        "asof_distilled": asof_distilled.isoformat(),
        "status": status,
    }


def _write_beliefs(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True, default=str) + "\n")


def _repeating_corpus() -> list[dict]:
    """(regime_shift_invalidation) recurs in 3 distinct weeks as a winner; (noise) once."""
    rows: list[dict] = []
    for w in range(3):
        wk = ASOF - timedelta(days=7 * (w + 1))
        rows.append(
            _weekly_belief(
                belief_id=f"bel_weekly_pm_AAPL_w{w}",
                lesson_category="regime_shift_invalidation",
                alpha_evidence=0.04,
                asof_distilled=wk,
            )
        )
    rows.append(
        _weekly_belief(
            belief_id="bel_weekly_pm_TSLA_solo",
            lesson_category="noise_trade_no_lesson",
            alpha_evidence=0.01,
            asof_distilled=ASOF - timedelta(days=5),
        )
    )
    return rows


def _debate_row(
    *, proposal_id: str, asof: datetime, final_recommendation: str, with_bull=True, with_bear=True
) -> dict:
    payload: dict = {
        "proposal_id": proposal_id,
        "asset": "AAPL",
        "asof": asof.isoformat(),
        "final_recommendation": final_recommendation,
    }
    if with_bull:
        payload["bull_turns_summary"] = [{"stance": "bull", "confidence": 0.9, "rationale_chars": 100}]
    if with_bear:
        payload["bear_turns_summary"] = [{"stance": "bear", "confidence": 0.4, "rationale_chars": 80}]
    return payload


# ---------------------------------------------------------------------------
# GATE CONDITION 1 — reproduces byte-identical (config_hash)
# ---------------------------------------------------------------------------


def test_reproduces_byte_identical_config_hash(tmp_path) -> None:
    bpath = tmp_path / "beliefs.jsonl"
    mpath = tmp_path / "meta_retros.jsonl"
    _write_beliefs(bpath, _repeating_corpus())
    reg = HypothesisRegistry(path=tmp_path / "hyp1.jsonl")

    cfg = dict(window_days=28, repeat_threshold=2, novelty_threshold=0.85, max_candidates=5)

    r1 = run_meta_retro(
        ASOF, **cfg, realized_alpha_by_proposal=lambda _p: None,
        beliefs_path=bpath, meta_retros_path=mpath, registry=reg,
    )
    # Second run over the SAME immutable input corpus (fresh beliefs file so the prior
    # run's monthly/expired appends do not perturb the input; the contract is "same
    # immutable corpus -> same report").
    bpath2 = tmp_path / "beliefs2.jsonl"
    _write_beliefs(bpath2, _repeating_corpus())
    reg2 = HypothesisRegistry(path=tmp_path / "hyp2.jsonl")
    r2 = run_meta_retro(
        ASOF, **cfg, realized_alpha_by_proposal=lambda _p: None,
        beliefs_path=bpath2, meta_retros_path=tmp_path / "meta2.jsonl", registry=reg2,
    )

    assert r1.meta_retro_id == r2.meta_retro_id
    assert r1.config_hash == r2.config_hash
    assert sorted(c["claim"] for c in r1.candidate_hypotheses) == sorted(
        c["claim"] for c in r2.candidate_hypotheses
    )
    assert r1.beliefs_promoted == r2.beliefs_promoted
    assert r1.beliefs_expired == r2.beliefs_expired


# ---------------------------------------------------------------------------
# SAFETY — external truth wins over the debate's own confidence
# ---------------------------------------------------------------------------


def test_persona_calibration_uses_realized_alpha_not_confidence() -> None:
    """Bull confidence=0.9 but realized alpha is NEGATIVE -> bull scored INCORRECT."""
    row = _debate_row(proposal_id="p1", asof=ASOF - timedelta(days=3), final_recommendation="BUY")
    # External truth: the trade lost alpha despite the bull's high confidence.
    cal = compute_persona_calibration([row], realized_alpha_by_proposal=lambda _p: -0.05)
    by_role = {c.role: c for c in cal}
    assert by_role["bull_researcher"].n_correct == 0, "bull must be wrong on negative alpha"
    assert by_role["bear_researcher"].n_correct == 1, "bear is right on negative alpha"
    # The judge said BUY (+1) but alpha was negative -> judge incorrect.
    assert by_role["judge"].n_correct == 0


# ---------------------------------------------------------------------------
# GATE CONDITION 3 — persona deltas telemetry-only + clamped
# ---------------------------------------------------------------------------


def test_persona_deltas_are_telemetry_only_and_clamped() -> None:
    rows = [
        _debate_row(proposal_id=f"p{i}", asof=ASOF - timedelta(days=i + 1), final_recommendation="BUY")
        for i in range(6)
    ]
    # Mix of outcomes so hit_rate is not trivially 0/1.
    alphas = {"p0": 0.05, "p1": 0.03, "p2": -0.02, "p3": 0.04, "p4": -0.01, "p5": 0.06}
    cal = compute_persona_calibration(rows, realized_alpha_by_proposal=lambda p: alphas[p])
    assert cal
    for c in cal:
        assert c.telemetry_only is True
        assert abs(c.proposed_weight_delta) <= 0.10


def test_clamp_caps_extreme_hit_rate() -> None:
    """A perfect (or zero) hit-rate would push the raw delta beyond +/-0.10 only if the
    scale were large; assert the clamp holds at the boundary regardless."""
    rows = [
        _debate_row(proposal_id=f"p{i}", asof=ASOF - timedelta(days=i + 1), final_recommendation="BUY")
        for i in range(10)
    ]
    cal = compute_persona_calibration(rows, realized_alpha_by_proposal=lambda _p: 0.05)
    for c in cal:
        assert -0.10 <= c.proposed_weight_delta <= 0.10


# ---------------------------------------------------------------------------
# FINCON repeat-threshold
# ---------------------------------------------------------------------------


def test_lesson_trend_repeat_threshold() -> None:
    # category present in 3/3 distinct weeks -> repeats with threshold 2.
    trends = compute_lesson_trends(_repeating_corpus(), repeat_threshold=2)
    by_cat = {t.lesson_category: t for t in trends}
    assert by_cat["regime_shift_invalidation"].repeats is True
    assert by_cat["regime_shift_invalidation"].weeks_present == 3
    # present in 1 week -> does not repeat.
    assert by_cat["noise_trade_no_lesson"].repeats is False
    assert by_cat["noise_trade_no_lesson"].weeks_present == 1


# ---------------------------------------------------------------------------
# RD-Agent failure-tag rubric (approach yields NO candidate; implementation MAY)
# ---------------------------------------------------------------------------


def test_failure_tag_implementation_vs_approach(tmp_path) -> None:
    # deep-negative recurring trend -> approach (abandon); mild-negative -> implementation.
    rows: list[dict] = []
    for w in range(2):
        wk = ASOF - timedelta(days=7 * (w + 1))
        rows.append(_weekly_belief(belief_id=f"bel_weekly_pm_DEEP_w{w}",
                                   lesson_category="thesis_invalidation_at_earnings",
                                   alpha_evidence=-0.05, asof_distilled=wk))
        rows.append(_weekly_belief(belief_id=f"bel_weekly_pm_MILD_w{w}",
                                   lesson_category="correct_call_too_late",
                                   alpha_evidence=-0.005, asof_distilled=wk))
    trends = compute_lesson_trends(rows, repeat_threshold=2)
    by_cat = {t.lesson_category: t for t in trends}
    assert by_cat["thesis_invalidation_at_earnings"].failure_tag == FAILURE_TAG_APPROACH
    assert by_cat["correct_call_too_late"].failure_tag == FAILURE_TAG_IMPLEMENTATION

    cands = synthesize_candidate_hypotheses(
        trends, [], novelty_threshold=0.85, max_candidates=5,
    )
    src_cats = {c.source_lesson_category for c in cands}
    # approach-tagged trend yields NO candidate; implementation-tagged MAY.
    assert "thesis_invalidation_at_earnings" not in src_cats
    assert "correct_call_too_late" in src_cats


# ---------------------------------------------------------------------------
# GATE CONDITION 4a — promotion copies Oracle provenance unchanged
# ---------------------------------------------------------------------------


def test_weekly_to_monthly_promotion_copies_oracle_provenance() -> None:
    corpus = _repeating_corpus()
    trends = compute_lesson_trends(corpus, repeat_threshold=2)
    new_rows, promoted, expired = apply_weekly_to_monthly(
        corpus, ASOF, trends, weekly_to_monthly_half_life_days=90.0,
    )
    assert promoted, "the repeating category should be promoted to monthly"
    monthly = [r for r in new_rows if r["tier"] == "monthly"]
    assert monthly
    # Every monthly row's oracle_provenance is byte-identical to a source weekly belief's.
    source_provs = {
        json.dumps(r["oracle_provenance"], sort_keys=True)
        for r in corpus
        if r["lesson_category"] == "regime_shift_invalidation"
    }
    for m in monthly:
        assert m["tier"] == "monthly"
        assert m["half_life_days"] == 90.0
        assert json.dumps(m["oracle_provenance"], sort_keys=True) in source_provs


def test_belief_expiry_is_append_only() -> None:
    """Expiry adds a status='expired' NEW row; the original active row is untouched."""
    corpus = _repeating_corpus()
    # Make the solo (non-recurring) belief decayed below epsilon so it expires.
    for r in corpus:
        if r["belief_id"] == "bel_weekly_pm_TSLA_solo":
            r["recency"] = 0.01
    trends = compute_lesson_trends(corpus, repeat_threshold=2)
    new_rows, _promoted, expired = apply_weekly_to_monthly(
        corpus, ASOF, trends, weekly_to_monthly_half_life_days=90.0,
    )
    assert "bel_weekly_pm_TSLA_solo" in expired
    expired_rows = [r for r in new_rows if r["status"] == "expired"]
    assert any(r["belief_id"] == "bel_weekly_pm_TSLA_solo" for r in expired_rows)
    # The original input row object is not mutated to 'expired'.
    orig = next(r for r in corpus if r["belief_id"] == "bel_weekly_pm_TSLA_solo")
    assert orig["status"] == "active"


# ---------------------------------------------------------------------------
# GATE CONDITION 4b — debate-row Oracle guard excludes future rows
# ---------------------------------------------------------------------------


def test_debate_oracle_guard_excludes_future_rows(tmp_path, monkeypatch) -> None:
    """A research_debate row with asof >= the distillation tick is excluded from calibration."""
    from hermes_quant.governance import audit_log

    log_path = tmp_path / "audit_log.jsonl"
    monkeypatch.setattr(audit_log, "AUDIT_LOG_PATH", log_path)

    # past row (eligible) + future row (must be excluded by evt.asof < asof).
    audit_log.append(audit_log.GovernanceEvent(
        kind="research_debate", asof=ASOF - timedelta(days=2), source="t",
        payload=_debate_row(proposal_id="past", asof=ASOF - timedelta(days=2),
                            final_recommendation="BUY"),
    ))
    audit_log.append(audit_log.GovernanceEvent(
        kind="research_debate", asof=ASOF + timedelta(days=2), source="t",
        payload=_debate_row(proposal_id="future", asof=ASOF + timedelta(days=2),
                            final_recommendation="BUY"),
    ))

    rows = meta_retro._load_debate_rows(ASOF, ASOF - timedelta(days=28))
    pids = {r["proposal_id"] for r in rows}
    assert "past" in pids
    assert "future" not in pids, "future debate row must be Oracle-guarded out"


# ---------------------------------------------------------------------------
# PROPOSE-ONLY — candidates registered status='open' only
# ---------------------------------------------------------------------------


def test_candidates_registered_status_open_only(tmp_path) -> None:
    bpath = tmp_path / "beliefs.jsonl"
    _write_beliefs(bpath, _repeating_corpus())
    reg = HypothesisRegistry(path=tmp_path / "hyp.jsonl")

    report = run_meta_retro(
        ASOF, register_candidates=True, realized_alpha_by_proposal=lambda _p: None,
        beliefs_path=bpath, meta_retros_path=tmp_path / "m.jsonl", registry=reg,
    )
    assert report.candidate_hypotheses, "the positive recurring trend should yield a candidate"
    open_hyps = list(reg.read_all_open())
    assert open_hyps
    for h in open_hyps:
        assert h.status == "open"
        assert h.author == "quant-monthly-meta-retro"
    # None registered as running/validated.
    assert not list(reg.read_all_running())
    assert not list(reg.read_all_resolved())


def test_candidates_pass_novelty_against_existing_registry(tmp_path) -> None:
    """GATE CONDITION 2: every emitted candidate has novelty_max_sim < threshold."""
    bpath = tmp_path / "beliefs.jsonl"
    _write_beliefs(bpath, _repeating_corpus())
    reg = HypothesisRegistry(path=tmp_path / "hyp.jsonl")
    report = run_meta_retro(
        ASOF, novelty_threshold=0.85, register_candidates=False,
        realized_alpha_by_proposal=lambda _p: None,
        beliefs_path=bpath, meta_retros_path=tmp_path / "m.jsonl", registry=reg,
    )
    for c in report.candidate_hypotheses:
        assert c["novelty_max_sim"] < 0.85


def test_report_is_telemetry_only(tmp_path) -> None:
    bpath = tmp_path / "beliefs.jsonl"
    _write_beliefs(bpath, _repeating_corpus())
    report = run_meta_retro(
        ASOF, realized_alpha_by_proposal=lambda _p: None,
        beliefs_path=bpath, meta_retros_path=tmp_path / "m.jsonl",
        registry=HypothesisRegistry(path=tmp_path / "h.jsonl"),
    )
    assert report.telemetry_only is True
    for p in report.persona_calibration:
        assert p["telemetry_only"] is True


# ---------------------------------------------------------------------------
# DETERMINISM — _default_realized_alpha_lookup is file-order independent
# ---------------------------------------------------------------------------


def _write_reflections(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r, default=str) + "\n")


def test_default_alpha_lookup_is_deterministic(tmp_path, monkeypatch) -> None:
    """Two independent builds of the default lookup over the SAME corpus return
    identical results, AND the substring-join tie-break is independent of the
    reflections-file line order (sorted-by-decision_id, not insertion order).

    Corpus: two decision_ids both substring-matching the same proposal_id but
    carrying DIFFERENT realized alpha. The lookup must resolve the same one in
    sorted key order regardless of which line came first in the file.
    """
    from hermes_quant.memory import reflector as reflector_mod
    from hermes_quant.memory.meta_retro import _default_realized_alpha_lookup

    # Both "dec_aaa" and "dec_bbb" are substrings of this proposal_id.
    proposal_id = "rdp-dec_aaa-dec_bbb-tail"
    rows_order_1 = [
        {"decision_id": "dec_bbb", "alpha_return": 0.07},
        {"decision_id": "dec_aaa", "alpha_return": -0.03},
    ]
    rows_order_2 = list(reversed(rows_order_1))

    p1 = tmp_path / "reflections1.jsonl"
    p2 = tmp_path / "reflections2.jsonl"
    _write_reflections(p1, rows_order_1)
    _write_reflections(p2, rows_order_2)

    # Build A over file-order 1; build B over the REVERSED file-order.
    monkeypatch.setattr(reflector_mod, "REFLECTIONS_PATH", p1)
    lookup_a = _default_realized_alpha_lookup()
    a1 = lookup_a(proposal_id)
    a2 = lookup_a(proposal_id)  # two calls on the same build are identical

    monkeypatch.setattr(reflector_mod, "REFLECTIONS_PATH", p2)
    lookup_b = _default_realized_alpha_lookup()
    b1 = lookup_b(proposal_id)

    # Same build → identical across calls.
    assert a1 == a2
    # Reversed file order → still identical (sorted tie-break: "dec_aaa" wins).
    assert a1 == b1
    assert a1 == -0.03  # dec_aaa (sorts before dec_bbb), NOT 0.07

    # Direct exact-id match still wins over the substring path, deterministically.
    monkeypatch.setattr(reflector_mod, "REFLECTIONS_PATH", p1)
    lookup_c = _default_realized_alpha_lookup()
    assert lookup_c("dec_bbb") == 0.07
    # Unresolvable proposal_id → None (that debate row is simply not scored).
    assert lookup_c("totally-unrelated-id") is None
    assert lookup_c("") is None
