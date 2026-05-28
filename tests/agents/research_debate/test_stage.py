"""Unit + integration tests for the Bull/Bear adversarial debate stage (ADR-0065).

Coverage tracks ``docs/design/v0.6.1-bull-bear-debate.md`` §6 Test Plan.

Pattern notes
-------------
* All LLM calls are stubbed via the ``run_one_turn`` and ``run_judge``
  injection points exposed by ``run_research_debate``. No live network and no
  ``unittest.mock.patch`` of private symbols on ``llm_committee.py``.
* The audit-log path is auto-isolated by ``tests/conftest.py`` (autouse
  fixture redirects ``audit_log.AUDIT_LOG_PATH`` per-test), so any test that
  asserts on emitted audit rows can simply read the per-test journal file
  and parse JSONL.
* The committed ``PortfolioRating`` enum uses UPPERCASE labels
  (``BUY``/``OVERWEIGHT``/``HOLD``/``UNDERWEIGHT``/``SELL``). The design doc
  draft in §6 referenced the legacy mixed-case Literal — we test against the
  shipped enum and call out the divergence in the parent-agent summary.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from hermes_quant.agents.research_debate.schemas import (
    BullBearTurn,
    InvestDebateState,
    PortfolioRating,
    ResearchPlan,
)
from hermes_quant.agents.research_debate import stage as stage_mod
from hermes_quant.agents.research_debate.stage import (
    DEFAULT_MAX_ROUNDS,
    MAX_ALLOWED_ROUNDS,
    RESEARCH_DEBATE_AUDIT_KIND,
    RESEARCH_ROUNDS_ENV_VAR,
    _resolve_max_rounds,
    run_research_debate,
)
from hermes_quant.agents.trader import TraderAction, TraderProposal
from hermes_quant.aggregators.deliberative import CommitteeTurn, DeliberativeConfig
from hermes_quant.protocol import AggregatedSignal, AnalystView, MarketContext


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ctx(asset: str = "AAPL") -> MarketContext:
    ts = pd.date_range("2024-01-01", periods=2, freq="1h", tz="UTC")
    bars = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [100, 101],
            "high": [101, 102],
            "low": [99, 100],
            "close": [100, 101],
            "volume": [1000, 1000],
        }
    )
    return MarketContext(
        asset=asset,
        timeframe="1d",
        asset_class="equity",
        exchange=None,
        bars=bars,
        last_close=101.0,
        last_volume=1000.0,
        asof=ts[-1],
    )


def _view(name: str = "ta", direction: int = 1, conf: float = 0.7) -> AnalystView:
    return AnalystView(
        analyst=name,
        direction=direction,  # type: ignore[arg-type]
        magnitude=0.012,
        confidence=conf,
        confidence_raw=conf,
        horizon="1d",
        rationale=f"{name} rationale",
    )


def _baseline() -> AggregatedSignal:
    v = _view()
    return AggregatedSignal(
        asset="AAPL",
        timeframe="1d",
        asset_class="equity",
        asof=pd.Timestamp("2024-01-01 01:00", tz="UTC"),
        direction=1,
        magnitude=0.012,
        confidence=0.6,
        confidence_raw=0.6,
        horizon="1d",
        components=(v,),
        aggregator="bma",
    )


def _config() -> DeliberativeConfig:
    return DeliberativeConfig(enable_llm_turns=True, max_debate_rounds=1)


def _bull_turn_obj(rationale: str = "Strong trend; ta confirms.", conf: float = 0.7) -> BullBearTurn:
    return BullBearTurn(
        role="bull_researcher",
        stance="long the breakout",
        confidence=conf,
        rationale=rationale,
        key_evidence=["ta"],
        counterarguments="Bear will note macro overhang.",
        metadata={"tier": "quick"},
    )


def _bear_turn_obj(rationale: str = "Macro overhang; weak follow-through.", conf: float = 0.4) -> BullBearTurn:
    return BullBearTurn(
        role="bear_researcher",
        stance="cautious",
        confidence=conf,
        rationale=rationale,
        key_evidence=["ta"],
        counterarguments="Bull will cite breakout volume.",
        metadata={"tier": "quick"},
    )


def _committee_turn_from(structured: BullBearTurn, role: str) -> CommitteeTurn:
    """Wrap a BullBearTurn into a CommitteeTurn shaped exactly like the one
    ``llm_committee._run_one_turn`` emits — that's what the stage expects from
    ``run_one_turn`` (it reads ``turn.metadata['structured']``)."""
    return CommitteeTurn(
        role=role,  # type: ignore[arg-type]
        stance=structured.stance,
        direction=1 if role == "bull_researcher" else -1,  # type: ignore[arg-type]
        confidence=structured.confidence,
        rationale=structured.rationale,
        model="llm:test-stub",
        input_hash=None,
        metadata={
            "tier": "quick",
            "model_id": "test-stub",
            "prompt_hash": "0" * 64,
            "key_evidence": list(structured.key_evidence),
            "counterarguments": structured.counterarguments,
            "structured": structured.model_dump(),
        },
        tier="quick",
    )


def _make_alternating_run_one_turn(
    *,
    bull_seq: list[BullBearTurn | None] | None = None,
    bear_seq: list[BullBearTurn | None] | None = None,
):
    """Build a ``run_one_turn`` stub that returns prepared turns by role.

    Each call pops the next item off the role's queue. Returning ``None``
    simulates a dropped turn (per the stage's failure-closed contract).
    Default sequences (one bull then one bear with predictable rationale) are
    used when a queue is exhausted to keep tests resilient under retries.
    """
    bull = list(bull_seq or [_bull_turn_obj()])
    bear = list(bear_seq or [_bear_turn_obj()])

    def _stub(*, role: str, **kwargs: Any) -> CommitteeTurn | None:
        if role == "bull_researcher":
            payload = bull.pop(0) if bull else _bull_turn_obj()
        else:
            payload = bear.pop(0) if bear else _bear_turn_obj()
        if payload is None:
            return None
        return _committee_turn_from(payload, role)

    return _stub


def _make_judge(
    rec: PortfolioRating | None = PortfolioRating.OVERWEIGHT,
    *,
    raise_exc: Exception | None = None,
):
    """Build a ``run_judge`` stub returning a fixed ResearchPlan (or None)."""

    def _stub(**kwargs: Any) -> ResearchPlan | None:
        if raise_exc is not None:
            raise raise_exc
        if rec is None:
            return None
        return ResearchPlan(
            recommendation=rec,
            confidence=0.65,
            rationale="Bull case stronger than bear case net of evidence.",
            strategic_actions="Enter on close above prior high; stop below day low.",
            horizon_emphasis="1d",
            metadata={"tier": "deep"},
        )

    return _stub


def _audit_rows(audit_path: Path, kind: str | None = None) -> list[dict[str, Any]]:
    if not audit_path.exists():
        return []
    rows = []
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        evt = json.loads(line)
        if kind is None or evt.get("kind") == kind:
            rows.append(evt)
    return rows


# ---------------------------------------------------------------------------
# Unit tests T1-T14
# ---------------------------------------------------------------------------


# T1 ------------------------------------------------------------------
def test_one_round_happy_path(_isolate_governance_audit_log: Path) -> None:
    state = run_research_debate(
        ctx=_ctx(),
        baseline_signal=_baseline(),
        analyst_views=[_view()],
        config=_config(),
        client=MagicMock(),
        max_rounds=1,
        run_one_turn=_make_alternating_run_one_turn(),
        run_judge=_make_judge(PortfolioRating.OVERWEIGHT),
    )
    assert state.count == 2
    assert len(state.bull_turns) == 1
    assert len(state.bear_turns) == 1
    assert state.terminated_reason == "max_rounds_reached"
    assert state.judge_decision is not None
    assert state.judge_decision.recommendation == PortfolioRating.OVERWEIGHT


# T2 ------------------------------------------------------------------
def test_three_round_full_debate(
    _isolate_governance_audit_log: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(RESEARCH_ROUNDS_ENV_VAR, "3")
    state = run_research_debate(
        ctx=_ctx(),
        baseline_signal=_baseline(),
        analyst_views=[_view()],
        config=_config(),
        client=MagicMock(),
        run_one_turn=_make_alternating_run_one_turn(
            bull_seq=[
                _bull_turn_obj("Round 1 bull"),
                _bull_turn_obj("Round 2 bull"),
                _bull_turn_obj("Round 3 bull"),
            ],
            bear_seq=[
                _bear_turn_obj("Round 1 bear"),
                _bear_turn_obj("Round 2 bear"),
                _bear_turn_obj("Round 3 bear"),
            ],
        ),
        run_judge=_make_judge(),
    )
    assert state.count == 6
    assert len(state.bull_turns) == 3
    assert len(state.bear_turns) == 3
    # round markers in bull/bear histories
    for r in (1, 2, 3):
        assert f"[Bull r{r}]" in state.bull_history
        assert f"[Bear r{r}]" in state.bear_history
    # last role to speak in a 3-round debate is the bear (count=5 odd → bear)
    assert state.current_response.startswith("Bear:")


# T3 ------------------------------------------------------------------
def test_bail_on_two_consecutive_failures(
    _isolate_governance_audit_log: Path,
) -> None:
    """First two turns return None → bail; judge still runs on empty state."""

    calls = {"n": 0}

    def _failing_turn(**kwargs: Any) -> CommitteeTurn | None:
        calls["n"] += 1
        return None

    state = run_research_debate(
        ctx=_ctx(),
        baseline_signal=_baseline(),
        analyst_views=[_view()],
        config=_config(),
        client=MagicMock(),
        max_rounds=1,
        run_one_turn=_failing_turn,
        run_judge=_make_judge(PortfolioRating.HOLD),
    )
    assert state.terminated_reason == "two_consecutive_failures"
    assert state.bull_turns == []
    assert state.bear_turns == []
    # Judge still ran on the partial (empty) state.
    assert state.judge_decision is not None
    assert state.judge_decision.recommendation == PortfolioRating.HOLD


# T4 ------------------------------------------------------------------
def test_research_plan_validation_failure_yields_none_judge(
    _isolate_governance_audit_log: Path,
) -> None:
    """Judge returns an invalid ResearchPlan-like dict → state.judge_decision is None,
    audit row still emitted with final_recommendation=None."""

    def _bad_judge(**kwargs: Any) -> Any:
        # Missing required fields; the stage's coerce path will try
        # ResearchPlan.model_validate(...) and reject this as invalid.
        return {"not": "a research plan"}

    state = run_research_debate(
        ctx=_ctx(),
        baseline_signal=_baseline(),
        analyst_views=[_view()],
        config=_config(),
        client=MagicMock(),
        max_rounds=1,
        run_one_turn=_make_alternating_run_one_turn(),
        run_judge=_bad_judge,
    )
    assert state.judge_decision is None
    # The terminated_reason is still the success path's tag because the loop
    # itself completed; the judge failure does NOT mutate it (see stage.py
    # comment in §Failure-closed contract).
    assert state.terminated_reason == "max_rounds_reached"
    rows = _audit_rows(_isolate_governance_audit_log, RESEARCH_DEBATE_AUDIT_KIND)
    assert len(rows) == 1
    assert rows[0]["payload"]["final_recommendation"] is None


# T5 ------------------------------------------------------------------
def test_portfolio_rating_enum_round_trip() -> None:
    # value identity
    assert PortfolioRating.BUY.value == "BUY"
    # construction from string
    assert PortfolioRating("BUY") is PortfolioRating.BUY
    assert PortfolioRating("SELL") is PortfolioRating.SELL
    # JSON serialization preserves the label (StrEnum invariant)
    assert json.dumps({"r": PortfolioRating.BUY}) == '{"r": "BUY"}'
    # Signed intensity ladder
    assert PortfolioRating.SELL.signed_intensity == -2
    assert PortfolioRating.UNDERWEIGHT.signed_intensity == -1
    assert PortfolioRating.HOLD.signed_intensity == 0
    assert PortfolioRating.OVERWEIGHT.signed_intensity == 1
    assert PortfolioRating.BUY.signed_intensity == 2


# T6 ------------------------------------------------------------------
def test_opponent_argument_injected_into_bull_prompt() -> None:
    """The bear's prior rationale must appear verbatim in the bull's round-2
    prompt, and the prompt hash must differ from round 1's."""
    from hermes_quant.aggregators.llm_committee import _prompt_hash, _render_prompt

    ctx = _ctx()
    bsig = _baseline()
    views = [_view()]
    bear_speech = "Macro headwinds will crush the breakout — UNIQUE-PHRASE-BEAR-7531"

    sys1, usr1 = _render_prompt(
        role="bull_researcher",
        market_context=ctx,
        analyst_views=views,
        baseline_signal=bsig,
        prior_turns=[],
        current_response=None,  # round 1: no opponent yet
        own_history=None,
        round_index=1,
        conversational_preamble="cv-preamble",
    )
    h1 = _prompt_hash(sys1, usr1)

    sys2, usr2 = _render_prompt(
        role="bull_researcher",
        market_context=ctx,
        analyst_views=views,
        baseline_signal=bsig,
        prior_turns=[],
        current_response=f"Bear: {bear_speech}",
        own_history="[Bull r1] my prior thread",
        round_index=2,
        conversational_preamble="cv-preamble",
    )
    h2 = _prompt_hash(sys2, usr2)

    assert bear_speech in usr2
    assert h1 != h2


# T7 ------------------------------------------------------------------
def test_opponent_argument_injected_into_bear_prompt() -> None:
    """Mirror of T6 for bear referencing bull rationale."""
    from hermes_quant.aggregators.llm_committee import _prompt_hash, _render_prompt

    ctx = _ctx()
    bsig = _baseline()
    views = [_view()]
    bull_speech = "Volume confirms the breakout — UNIQUE-PHRASE-BULL-9248"

    sys1, usr1 = _render_prompt(
        role="bear_researcher",
        market_context=ctx,
        analyst_views=views,
        baseline_signal=bsig,
        prior_turns=[],
        current_response=None,
        own_history=None,
        round_index=1,
        conversational_preamble="cv-preamble",
    )
    h1 = _prompt_hash(sys1, usr1)

    sys2, usr2 = _render_prompt(
        role="bear_researcher",
        market_context=ctx,
        analyst_views=views,
        baseline_signal=bsig,
        prior_turns=[],
        current_response=f"Bull: {bull_speech}",
        own_history="[Bear r1] earlier",
        round_index=2,
        conversational_preamble="cv-preamble",
    )
    h2 = _prompt_hash(sys2, usr2)

    assert bull_speech in usr2
    assert h1 != h2


# T8 ------------------------------------------------------------------
def test_first_turn_graceful_degradation_uses_open_the_debate_sentinel() -> None:
    """At round 1 with no opponent yet, the prompt must show the safe
    sentinel string '(no prior turn — open the debate)'."""
    from hermes_quant.aggregators.llm_committee import _render_prompt

    sys1, usr1 = _render_prompt(
        role="bull_researcher",
        market_context=_ctx(),
        analyst_views=[_view()],
        baseline_signal=_baseline(),
        prior_turns=[],
        current_response=None,
        own_history=None,
        round_index=1,
        conversational_preamble="cv-preamble",
    )
    assert "(no prior turn — open the debate)" in usr1


# T9 ------------------------------------------------------------------
def test_audit_log_emission_one_row_per_stage(
    _isolate_governance_audit_log: Path,
) -> None:
    state = run_research_debate(
        ctx=_ctx(asset="MSFT"),
        baseline_signal=_baseline(),
        analyst_views=[_view()],
        config=_config(),
        client=MagicMock(),
        max_rounds=1,
        run_one_turn=_make_alternating_run_one_turn(),
        run_judge=_make_judge(PortfolioRating.BUY),
    )
    rows = _audit_rows(_isolate_governance_audit_log, RESEARCH_DEBATE_AUDIT_KIND)
    assert len(rows) == 1
    payload = rows[0]["payload"]
    expected_keys = {
        "proposal_id",
        "asset",
        "asof",
        "rounds_configured",
        "bull_count",
        "bear_count",
        "terminated_reason",
        "final_recommendation",
        "research_plan",
    }
    assert expected_keys.issubset(payload.keys())
    assert payload["asset"] == "MSFT"
    assert payload["bull_count"] == 1
    assert payload["bear_count"] == 1
    assert payload["final_recommendation"] == "BUY"
    # H1 fix (v0.6.2): stage row carries summaries, not per-turn payloads.
    # Per-turn forensics still live on the dispatch-side committee_turn rows.
    assert "bull_turns_summary" in payload
    assert "bear_turns_summary" in payload
    assert "bull_turns" not in payload
    assert "bear_turns" not in payload
    assert isinstance(payload["bull_turns_summary"], list)
    assert isinstance(payload["bear_turns_summary"], list)
    assert len(payload["bull_turns_summary"]) == 1
    assert len(payload["bear_turns_summary"]) == 1
    bs = payload["bull_turns_summary"][0]
    assert {"stance", "confidence", "rationale_chars"} == set(bs.keys())
    # research_plan dict must round-trip via ResearchPlan.model_validate
    rp = ResearchPlan.model_validate(payload["research_plan"])
    assert rp.recommendation == PortfolioRating.BUY
    # Confirm in-memory state matches what we asserted on the row.
    assert state.judge_decision is not None
    assert state.judge_decision.recommendation == PortfolioRating.BUY


# T10 -----------------------------------------------------------------
SNAPSHOT_PATH = Path(__file__).parent / "_legacy_prompt_hashes.json"


def test_backward_compat_legacy_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With HERMES_QUANT_RESEARCH_DEBATE unset, run_llm_committee must take
    the legacy parallel-emit for-loop path. We snapshot the prompt_hash of
    every emitted committee_turn and pin it against a fixture file. On the
    first run (no fixture present) we write it; on subsequent runs we compare
    byte-for-byte to detect silent drift.

    Note: the hashes pinned here are the post-bull_bear-rewrite hashes, NOT
    the pre-v0.6.1 ones — the prompt template was rewritten in commit 14dacd0
    by deliberate design (see ADR-0065 §Test Plan T10 commentary in
    llm_committee.py:266). What we are pinning is "no further drift
    introduced by Workstream B beyond the documented prompt rewrite".
    """
    from unittest.mock import MagicMock as _MM

    monkeypatch.delenv("HERMES_QUANT_RESEARCH_DEBATE", raising=False)
    # We patch run_research_debate so that even if the dispatch wiring lands
    # later, this test still verifies the legacy fall-through behaviour by
    # asserting run_research_debate was NOT invoked.
    with patch.object(
        stage_mod, "run_research_debate", side_effect=AssertionError("must not run when flag off")
    ) as patched_stage:
        # Build deterministic stubbed LLM responses for the legacy path.
        from hermes_quant.aggregators.llm_committee import (
            BullBearTurn as LegacyBBT,
            ResearchPlan as LegacyRP,
            run_llm_committee,
        )

        def _bull_json() -> str:
            return LegacyBBT(
                role="bull_researcher",
                stance="long",
                confidence=0.7,
                rationale="strong",
                key_evidence=["ta"],
                counterarguments="bear may say macro",
                metadata={"tier": "quick"},
            ).model_dump_json()

        def _bear_json() -> str:
            return LegacyBBT(
                role="bear_researcher",
                stance="cautious",
                confidence=0.4,
                rationale="weak",
                key_evidence=["ta"],
                counterarguments="bull cites breakout",
                metadata={"tier": "quick"},
            ).model_dump_json()

        def _judge_json() -> str:
            return LegacyRP(
                recommendation="Overweight",  # type: ignore[arg-type]
                confidence=0.65,
                rationale="net long bias",
                overrules_baseline=False,
                strategic_actions="enter on close above day high",
                horizon_emphasis="1d",
                metadata={"tier": "deep"},
            ).model_dump_json()

        client = _MM()
        responses = iter([_bull_json(), _bear_json(), _judge_json()])

        def _create(**_kwargs: Any) -> Any:
            r = _MM()
            r.choices = [_MM()]
            r.choices[0].message.content = next(responses)
            return r

        client.chat.completions.create.side_effect = _create

        out = run_llm_committee(
            market_context=_ctx(),
            analyst_views=[_view()],
            baseline_signal=_baseline(),
            config=_config(),
            client=client,
        )

        assert patched_stage.call_count == 0, (
            "run_research_debate must NOT be invoked when "
            "HERMES_QUANT_RESEARCH_DEBATE is unset"
        )

    assert len(out) == 3, f"expected 3 committee turns from legacy path, got {len(out)}"
    role_hash_pairs = [
        (t.role, t.metadata["prompt_hash"]) for t in out if t.metadata
    ]

    if not SNAPSHOT_PATH.exists():
        # First run: write the snapshot. Subsequent runs compare against it.
        SNAPSHOT_PATH.write_text(
            json.dumps(role_hash_pairs, indent=2, sort_keys=False),
            encoding="utf-8",
        )
        pytest.skip(
            f"snapshot fixture seeded at {SNAPSHOT_PATH.name}; re-run test to verify"
        )
    expected = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    expected_norm = [tuple(p) for p in expected]
    assert role_hash_pairs == expected_norm, (
        "Legacy-path prompt hashes drifted from snapshot. Either the prompt "
        "template was edited (intentional → regenerate snapshot) or a "
        "non-bit-identical change leaked into the legacy committee path."
    )


# T11 -----------------------------------------------------------------
def test_label_stability_under_portfolio_rating_enum() -> None:
    """Each of the 5 valid label strings round-trips through ResearchPlan.

    ADR-0065 v0.6.1-fix-C3: case-insensitive on the wire (mixed-case is
    accepted via field_validator and normalised to UPPERCASE before enum
    coercion). Truly non-canonical labels (e.g. ``STRONG_BUY``) still raise.
    """
    base = {
        "confidence": 0.6,
        "rationale": "x",
        "strategic_actions": "y",
    }
    for label in ("BUY", "OVERWEIGHT", "HOLD", "UNDERWEIGHT", "SELL"):
        rp = ResearchPlan.model_validate({"recommendation": label, **base})
        assert rp.recommendation.value == label
        # Re-serialise must yield the same label byte-for-byte
        assert json.loads(rp.model_dump_json())["recommendation"] == label
    # Mixed-case is now accepted (C3) — normalised to UPPERCASE.
    rp_lower = ResearchPlan.model_validate({"recommendation": "buy", **base})
    assert rp_lower.recommendation == PortfolioRating.BUY
    # non-canonical still rejected
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ResearchPlan.model_validate({"recommendation": "STRONG_BUY", **base})


# T12 -----------------------------------------------------------------
def test_resolve_research_max_rounds_clamping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No env, no arg → DEFAULT_MAX_ROUNDS
    monkeypatch.delenv(RESEARCH_ROUNDS_ENV_VAR, raising=False)
    assert _resolve_max_rounds(None) == DEFAULT_MAX_ROUNDS

    # Env=99 → cap to MAX_ALLOWED_ROUNDS
    monkeypatch.setenv(RESEARCH_ROUNDS_ENV_VAR, "99")
    assert _resolve_max_rounds(None) == MAX_ALLOWED_ROUNDS

    # Env=0 → floor to 1
    monkeypatch.setenv(RESEARCH_ROUNDS_ENV_VAR, "0")
    assert _resolve_max_rounds(None) == 1

    # Env="abc" → warning + default
    monkeypatch.setenv(RESEARCH_ROUNDS_ENV_VAR, "abc")
    assert _resolve_max_rounds(None) == DEFAULT_MAX_ROUNDS

    # Explicit arg always overrides env (and is clamped too)
    monkeypatch.setenv(RESEARCH_ROUNDS_ENV_VAR, "1")
    assert _resolve_max_rounds(5) == MAX_ALLOWED_ROUNDS
    assert _resolve_max_rounds(2) == 2


# T13 -----------------------------------------------------------------
def test_research_plan_dropped_overrules_baseline() -> None:
    """The schemas.ResearchPlan must reject the legacy ``overrules_baseline``
    field (extra='forbid' pinning the migration)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ResearchPlan.model_validate(
            {
                "recommendation": "BUY",
                "confidence": 0.7,
                "rationale": "x",
                "strategic_actions": "y",
                "overrules_baseline": True,
            }
        )


# T14 -----------------------------------------------------------------
def test_trader_proposal_accepts_research_plan_recommendation() -> None:
    """TraderProposal accepts research_plan_recommendation as PortfolioRating
    or None; rejects junk strings."""
    from pydantic import ValidationError

    # Accepts an enum value
    p = TraderProposal(
        action=TraderAction.BUY,
        size_fraction=0.1,
        confidence=0.7,
        rationale="x",
        research_plan_recommendation=PortfolioRating.OVERWEIGHT,
        research_plan_id="rdp-abc123",
    )
    assert p.research_plan_recommendation == PortfolioRating.OVERWEIGHT
    assert p.research_plan_id == "rdp-abc123"

    # Accepts None (back-compat default)
    p2 = TraderProposal(
        action=TraderAction.HOLD,
        size_fraction=0.0,
        confidence=0.5,
        rationale="legacy callsite",
    )
    assert p2.research_plan_recommendation is None
    assert p2.research_plan_id is None

    # Rejects non-canonical string
    with pytest.raises(ValidationError):
        TraderProposal(
            action=TraderAction.HOLD,
            size_fraction=0.0,
            confidence=0.5,
            rationale="x",
            research_plan_recommendation="not-a-rating",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Integration tests I1-I2
# ---------------------------------------------------------------------------


# I1 ------------------------------------------------------------------
def test_i1_stage_run_emits_audit_row_and_research_plan_with_valid_rating(
    _isolate_governance_audit_log: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end stage call (with stubbed LLM) must:
      * emit exactly ONE 'research_debate' audit row,
      * the in-memory ResearchPlan.recommendation must be one of the 5 valid
        PortfolioRating enum strings,
      * a TraderProposal can be constructed downstream linking back to the
        debate via research_plan_recommendation + research_plan_id.
    """
    monkeypatch.setenv("HERMES_QUANT_RESEARCH_DEBATE", "1")
    monkeypatch.setenv(RESEARCH_ROUNDS_ENV_VAR, "2")

    state = run_research_debate(
        ctx=_ctx(asset="GOOG"),
        baseline_signal=_baseline(),
        analyst_views=[_view()],
        config=_config(),
        client=MagicMock(),
        run_one_turn=_make_alternating_run_one_turn(
            bull_seq=[_bull_turn_obj("r1 bull"), _bull_turn_obj("r2 bull")],
            bear_seq=[_bear_turn_obj("r1 bear"), _bear_turn_obj("r2 bear")],
        ),
        run_judge=_make_judge(PortfolioRating.OVERWEIGHT),
        proposal_id="rdp-i1-fixed",
    )
    # 2 rounds = 4 turns
    assert state.count == 4
    assert state.judge_decision is not None
    assert state.judge_decision.recommendation in tuple(PortfolioRating)

    rows = _audit_rows(_isolate_governance_audit_log, RESEARCH_DEBATE_AUDIT_KIND)
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["proposal_id"] == "rdp-i1-fixed"
    assert payload["final_recommendation"] in {r.value for r in PortfolioRating}

    # Downstream TraderProposal carrying the join keys
    proposal = TraderProposal(
        action=TraderAction.BUY,
        size_fraction=0.1,
        confidence=state.judge_decision.confidence,
        rationale="aligned with research plan",
        research_plan_recommendation=state.judge_decision.recommendation,
        research_plan_id=payload["proposal_id"],
    )
    assert proposal.research_plan_recommendation == PortfolioRating.OVERWEIGHT
    assert proposal.research_plan_id == "rdp-i1-fixed"


# I2 ------------------------------------------------------------------
def test_i2_journal_replay_round_trip(
    _isolate_governance_audit_log: Path,
) -> None:
    """The persisted research_debate row's research_plan, after re-validation
    through ResearchPlan, matches the in-memory state.judge_decision
    byte-for-byte once both are canonicalised via model_dump_json()."""
    state = run_research_debate(
        ctx=_ctx(),
        baseline_signal=_baseline(),
        analyst_views=[_view()],
        config=_config(),
        client=MagicMock(),
        max_rounds=1,
        run_one_turn=_make_alternating_run_one_turn(),
        run_judge=_make_judge(PortfolioRating.BUY),
    )
    rows = _audit_rows(_isolate_governance_audit_log, RESEARCH_DEBATE_AUDIT_KIND)
    assert len(rows) == 1
    persisted = rows[0]["payload"]["research_plan"]
    assert persisted is not None

    replayed = ResearchPlan.model_validate(persisted)
    assert state.judge_decision is not None
    assert replayed.model_dump_json() == state.judge_decision.model_dump_json()


# C3 -----------------------------------------------------------------
def test_research_plan_recommendation_case_insensitive() -> None:
    """ADR-0065 v0.6.1-fix-C3: PortfolioRating accepts mixed-case strings.

    Tauric-style LLM judges sometimes emit ``"Buy"`` instead of ``"BUY"``;
    the field validator must normalise before enum coercion.
    """
    plan = ResearchPlan.model_validate(
        {
            "recommendation": "Buy",
            "confidence": 0.5,
            "rationale": "x",
            "strategic_actions": "size up small",
        }
    )
    assert plan.recommendation == PortfolioRating.BUY

    # Lower-case must also round-trip.
    plan_lower = ResearchPlan.model_validate(
        {
            "recommendation": "sell",
            "confidence": 0.5,
            "rationale": "x",
            "strategic_actions": "size down",
        }
    )
    assert plan_lower.recommendation == PortfolioRating.SELL
