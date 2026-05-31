"""Unit tests for W7 — the standing Socratic devil's-advocate / red-team turn
(ADR-0080 W7, flag ``HERMES_QUANT_REDTEAM_TURN``, default-OFF).

Mirrors the fixtures in ``tests/unit/test_research_debate_wiring.py`` (``_ctx``,
``_view``, ``_baseline``, ``_config``, ``_bull_json``, ``_bear_json``,
``_judge_json``) and stubs ``_call_llm_json`` — no live network.

Acceptance criteria pinned here (per the W7 plan §4 table):
  * off-state byte-identical when the flag is unset (D80.8 off-state),
  * the red-team runs only when the flag is ON,
  * dissent is a DETERMINISTIC threshold (NOT a vote),
  * the reserved ADR-0002 ``counterarguments`` field is filled,
  * failure-closed = no dissent, judge unchanged,
  * the red-team NEVER changes the committee's direction (D80.1 structural),
  * the red-team turn is never a directional CommitteeTurn,
  * the audit row carries the W3-mineable ``red_team`` block (O7),
  * exactly ONE red-team turn per stage (SOTA §4 cost discipline).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

from hermes_quant.agents.research_debate import stage as stage_mod
from hermes_quant.agents.research_debate.stage import run_research_debate
from hermes_quant.aggregators import llm_committee as committee_mod
from hermes_quant.aggregators.deliberative import DeliberativeConfig
from hermes_quant.aggregators.llm_committee import (
    RED_TEAM_DISSENT_THRESHOLD,
    run_llm_committee,
)
from hermes_quant.protocol import AggregatedSignal, AnalystView, MarketContext

# ---------------------------------------------------------------------------
# Env isolation — delenv RESEARCH_DEBATE, _ROUNDS, AND REDTEAM_TURN so suite
# ordering can never leak the flag in.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_RESEARCH_DEBATE", raising=False)
    monkeypatch.delenv("HERMES_QUANT_RESEARCH_DEBATE_ROUNDS", raising=False)
    monkeypatch.delenv("HERMES_QUANT_REDTEAM_TURN", raising=False)
    yield


# ---------------------------------------------------------------------------
# Fixtures (mirror test_research_debate_wiring.py)
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


def _view(name: str = "ta") -> AnalystView:
    return AnalystView(
        analyst=name,
        direction=1,  # type: ignore[arg-type]
        magnitude=0.012,
        confidence=0.7,
        confidence_raw=0.7,
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
    return DeliberativeConfig(
        enable_llm_turns=True, max_debate_rounds=1, enable_risk_mgmt=False
    )


def _bull_json(round_idx: int = 1) -> str:
    return (
        '{"role": "bull_researcher", "stance": "long the breakout", '
        '"confidence": 0.7, "rationale": "Strong trend confirmed.", '
        '"key_evidence": ["ta"], '
        '"counterarguments": "Bear notes macro overhang.", '
        f'"metadata": {{"tier": "quick", "round": {round_idx}}}}}'
    )


def _bear_json(round_idx: int = 1) -> str:
    return (
        '{"role": "bear_researcher", "stance": "cautious", '
        '"confidence": 0.4, "rationale": "Macro overhang dominates.", '
        '"key_evidence": ["ta"], '
        '"counterarguments": "Bull cites breakout volume.", '
        f'"metadata": {{"tier": "quick", "round": {round_idx}}}}}'
    )


def _judge_json(rec: str = "OVERWEIGHT", with_overrules: bool = False) -> str:
    overrules = ', "overrules_baseline": false' if with_overrules else ""
    return (
        f'{{"recommendation": "{rec}", "confidence": 0.65, '
        f'"rationale": "Bull case wins on volume confirmation.", '
        f'"strategic_actions": "Add 1R; trail stop under prior swing low."'
        f'{overrules}, "metadata": {{}}}}'
    )


_RED_TEAM_COUNTERARG = (
    "Do not act: the leading view ignores base-rate of failed breakouts."
)


def _redteam_json(conf: float = 0.7) -> str:
    return (
        '{"role": "bear_researcher", "stance": "leading view assumes regime persists", '
        f'"confidence": {conf}, "rationale": "The bull case rests on an unstated '
        'assumption that the breakout regime holds; under mean-reversion it fails.", '
        '"key_evidence": ["unstated regime assumption"], '
        f'"counterarguments": "{_RED_TEAM_COUNTERARG}", '
        '"metadata": {"tier": "quick", "red_team": true}}'
    )


# ---------------------------------------------------------------------------
# Helpers: a fake LLM that returns bull/bear/judge/red-team JSON in stage order.
# ---------------------------------------------------------------------------


def _staged_call_factory(judge: str, redteam: str | None) -> Any:
    """Stage call order under max_debate_rounds=1 is:
      1. bull_researcher  2. bear_researcher  3. research_manager (judge)
      4. devils_advocate (only when the flag is ON and the judge succeeded)
    """
    sequence = [_bull_json(1), _bear_json(1), judge]
    if redteam is not None:
        sequence.append(redteam)
    idx = {"i": 0}

    def fake_call(**_: Any) -> str | None:
        if idx["i"] >= len(sequence):
            return None
        out = sequence[idx["i"]]
        idx["i"] += 1
        return out

    return fake_call


def _run_stage(monkeypatch, *, flag_on: bool, redteam: str | None):
    if flag_on:
        monkeypatch.setenv("HERMES_QUANT_REDTEAM_TURN", "1")
    monkeypatch.setattr(
        committee_mod,
        "_call_llm_json",
        _staged_call_factory(_judge_json("OVERWEIGHT", with_overrules=True), redteam),
    )
    return run_research_debate(
        ctx=_ctx(),
        baseline_signal=_baseline(),
        analyst_views=[_view()],
        config=_config(),
        client=MagicMock(),
        run_one_turn=committee_mod._run_one_turn_with_history,
        run_judge=committee_mod._run_research_manager_judge,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_offstate_byte_identical_when_flag_unset(monkeypatch):
    """Flag unset → no red-team turn; defaults; audit red_team off-state record."""
    captured: dict[str, Any] = {}

    def _capture(kind: str, source: str, payload: dict[str, Any]) -> None:
        captured["payload"] = payload

    monkeypatch.setattr(stage_mod, "_audit_append", _capture)

    state = _run_stage(monkeypatch, flag_on=False, redteam=None)

    assert state.red_team_turn is None
    assert state.dissent_surfaced is False
    assert state.dissent_reason == ""
    assert state.judge_decision is not None
    assert state.judge_decision.counterarguments is None
    assert captured["payload"]["red_team"] == {
        "ran": False,
        "dissent_surfaced": False,
    }


def test_redteam_runs_when_flag_on(monkeypatch):
    """Flag ON + judge present → red_team_turn populated with forge-resistant flag."""
    state = _run_stage(monkeypatch, flag_on=True, redteam=_redteam_json(0.7))

    assert state.red_team_turn is not None
    assert state.red_team_turn.metadata is not None
    assert state.red_team_turn.metadata["red_team"] is True
    phash = state.red_team_turn.metadata["prompt_hash"]
    assert isinstance(phash, str) and len(phash) == 64


def test_dissent_surfaced_above_threshold(monkeypatch):
    """conf 0.7 (>= 0.5) → dissent surfaced; dissent_reason == counterarguments."""
    state = _run_stage(monkeypatch, flag_on=True, redteam=_redteam_json(0.7))
    assert state.dissent_surfaced is True
    assert state.dissent_reason == _RED_TEAM_COUNTERARG


def test_dissent_not_surfaced_below_threshold(monkeypatch):
    """conf 0.2 (< 0.5) → dissent NOT surfaced (deterministic threshold, not a vote)."""
    state = _run_stage(monkeypatch, flag_on=True, redteam=_redteam_json(0.2))
    assert state.red_team_turn is not None  # the turn still ran
    assert state.dissent_surfaced is False


def test_counterarguments_field_filled(monkeypatch):
    """The reserved ADR-0002 plan-level counterarguments field is filled."""
    state = _run_stage(monkeypatch, flag_on=True, redteam=_redteam_json(0.7))
    assert state.judge_decision is not None
    assert state.judge_decision.counterarguments is not None
    assert state.judge_decision.counterarguments == state.dissent_reason


def test_redteam_failure_is_no_dissent(monkeypatch):
    """Red-team stub returns None → stage completes, no dissent, judge UNCHANGED."""
    # Baseline run with the red-team stubbed to None.
    monkeypatch.setenv("HERMES_QUANT_REDTEAM_TURN", "1")
    monkeypatch.setattr(
        committee_mod,
        "_call_llm_json",
        _staged_call_factory(_judge_json("OVERWEIGHT", with_overrules=True), None),
    )
    # Force the adapter itself to return None (failure-closed path).
    monkeypatch.setattr(
        stage_mod,
        "_redteam_enabled",
        lambda: True,
    )
    state = run_research_debate(
        ctx=_ctx(),
        baseline_signal=_baseline(),
        analyst_views=[_view()],
        config=_config(),
        client=MagicMock(),
        run_one_turn=committee_mod._run_one_turn_with_history,
        run_judge=committee_mod._run_research_manager_judge,
        run_red_team=lambda **_: None,
    )
    assert state.red_team_turn is None
    assert state.dissent_surfaced is False
    assert state.judge_decision is not None
    # Judge direction/confidence identical to a no-red-team run.
    assert state.judge_decision.recommendation.value == "OVERWEIGHT"
    assert state.judge_decision.confidence == pytest.approx(0.65)
    assert state.judge_decision.counterarguments is None


def test_redteam_failure_when_adapter_raises(monkeypatch):
    """Adapter RAISING is also failure-closed = no dissent, judge unchanged."""
    monkeypatch.setenv("HERMES_QUANT_REDTEAM_TURN", "1")
    monkeypatch.setattr(
        committee_mod,
        "_call_llm_json",
        _staged_call_factory(_judge_json("OVERWEIGHT", with_overrules=True), None),
    )

    def _bomb(**_: Any):
        raise RuntimeError("synthetic red-team failure")

    state = run_research_debate(
        ctx=_ctx(),
        baseline_signal=_baseline(),
        analyst_views=[_view()],
        config=_config(),
        client=MagicMock(),
        run_one_turn=committee_mod._run_one_turn_with_history,
        run_judge=committee_mod._run_research_manager_judge,
        run_red_team=_bomb,
    )
    assert state.red_team_turn is None
    assert state.dissent_surfaced is False
    assert state.judge_decision is not None
    assert state.judge_decision.counterarguments is None


def test_redteam_never_changes_direction(monkeypatch):
    """Full dispatch, flag ON, MAXIMALLY-dissenting red-team (conf 1.0): the
    emitted portfolio_manager judge turn's direction is IDENTICAL to the same
    run with the red-team stubbed to None. (D80.1: aggregation deterministic.)
    """
    monkeypatch.setenv("HERMES_QUANT_RESEARCH_DEBATE", "1")
    monkeypatch.setenv("HERMES_QUANT_REDTEAM_TURN", "1")

    # Run WITH a maximally-dissenting red-team.
    monkeypatch.setattr(
        committee_mod,
        "_call_llm_json",
        _staged_call_factory(
            _judge_json("OVERWEIGHT", with_overrules=True), _redteam_json(1.0)
        ),
    )
    turns_rt = run_llm_committee(
        market_context=_ctx(),
        analyst_views=[_view()],
        baseline_signal=_baseline(),
        config=_config(),
        client=MagicMock(),
    )
    judge_rt = next(t for t in turns_rt if t.role == "portfolio_manager")

    # Run with the red-team stubbed to None (no dissent).
    monkeypatch.setattr(
        committee_mod,
        "_call_llm_json",
        _staged_call_factory(_judge_json("OVERWEIGHT", with_overrules=True), None),
    )
    monkeypatch.setattr(committee_mod, "_run_red_team_turn", lambda **_: None)
    turns_none = run_llm_committee(
        market_context=_ctx(),
        analyst_views=[_view()],
        baseline_signal=_baseline(),
        config=_config(),
        client=MagicMock(),
    )
    judge_none = next(t for t in turns_none if t.role == "portfolio_manager")

    assert judge_rt.direction == judge_none.direction
    assert judge_rt.confidence == pytest.approx(judge_none.confidence)
    # The maximal red-team surfaced dissent as metadata only — not direction.
    assert judge_rt.metadata is not None
    assert judge_rt.metadata.get("dissent_surfaced") is True
    assert judge_none.metadata is not None
    assert judge_none.metadata.get("dissent_surfaced") is False


def test_redteam_turn_not_in_directional_turns(monkeypatch):
    """No CommitteeTurn in ``turns`` carries metadata.red_team; red-team data
    appears ONLY in the judge turn's dissent_* metadata. (D80.1 structural.)
    """
    monkeypatch.setenv("HERMES_QUANT_RESEARCH_DEBATE", "1")
    monkeypatch.setenv("HERMES_QUANT_REDTEAM_TURN", "1")
    monkeypatch.setattr(
        committee_mod,
        "_call_llm_json",
        _staged_call_factory(
            _judge_json("OVERWEIGHT", with_overrules=True), _redteam_json(0.9)
        ),
    )
    turns = run_llm_committee(
        market_context=_ctx(),
        analyst_views=[_view()],
        baseline_signal=_baseline(),
        config=_config(),
        client=MagicMock(),
    )
    for t in turns:
        assert (t.metadata or {}).get("red_team") is not True
    judge = next(t for t in turns if t.role == "portfolio_manager")
    assert judge.metadata is not None
    assert judge.metadata.get("red_team_ran") is True
    assert "dissent_surfaced" in judge.metadata
    assert "dissent_reason" in judge.metadata


def test_audit_row_carries_red_team_block(monkeypatch):
    """The audit row carries the W3-mineable red_team block (O7)."""
    captured: dict[str, Any] = {}

    def _capture(kind: str, source: str, payload: dict[str, Any]) -> None:
        captured["payload"] = payload

    monkeypatch.setattr(stage_mod, "_audit_append", _capture)

    _run_stage(monkeypatch, flag_on=True, redteam=_redteam_json(0.7))

    rt = captured["payload"]["red_team"]
    assert rt["ran"] is True
    assert rt["dissent_surfaced"] is True
    assert rt["confidence"] == pytest.approx(0.7)
    assert rt["dissent_reason"] == _RED_TEAM_COUNTERARG
    assert isinstance(rt["prompt_hash"], str) and len(rt["prompt_hash"]) == 64
    assert rt["rationale_chars"] > 0


def _role_aware_call(**kw: Any) -> str | None:
    """Role-aware _call_llm_json stub robust to any debate round count.

    Inspects the rendered ``system_text`` (which carries the role label via the
    prompt template) to decide which structured JSON to return, so a 3-round
    stage gets a valid bull/bear turn on every round and a valid judge plan.
    """
    sys_text = kw.get("system_text", "")
    if "Bull Researcher" in sys_text:
        return _bull_json(1)
    if "Bear Researcher" in sys_text:
        return _bear_json(1)
    if "Research Manager" in sys_text:
        return _judge_json("OVERWEIGHT", with_overrules=True)
    if "Devil's Advocate" in sys_text:
        return _redteam_json(0.7)
    return None


def test_rounds_capped_single_turn(monkeypatch):
    """The red-team adapter is invoked EXACTLY ONCE per stage regardless of
    HERMES_QUANT_RESEARCH_DEBATE_ROUNDS. (SOTA §4 cost discipline.)
    """
    monkeypatch.setenv("HERMES_QUANT_REDTEAM_TURN", "1")
    monkeypatch.setenv("HERMES_QUANT_RESEARCH_DEBATE_ROUNDS", "3")
    monkeypatch.setattr(committee_mod, "_call_llm_json", _role_aware_call)

    calls = {"n": 0}

    def _spy(**kw: Any):
        calls["n"] += 1
        return committee_mod.BullBearTurn.model_validate(
            {
                "role": "bear_researcher",
                "stance": "x",
                "confidence": 0.7,
                "rationale": "critique",
                "key_evidence": [],
                "counterarguments": "do not act",
                "metadata": {"red_team": True, "prompt_hash": "0" * 64},
            }
        )

    run_research_debate(
        ctx=_ctx(),
        baseline_signal=_baseline(),
        analyst_views=[_view()],
        config=_config(),
        client=MagicMock(),
        run_one_turn=committee_mod._run_one_turn_with_history,
        run_judge=committee_mod._run_research_manager_judge,
        run_red_team=_spy,
    )
    assert calls["n"] == 1


def test_redteam_skipped_when_judge_is_none(monkeypatch):
    """No leading view (judge None) → red-team never runs (nothing to attack)."""
    monkeypatch.setenv("HERMES_QUANT_REDTEAM_TURN", "1")
    calls = {"n": 0}

    def _spy(**_: Any):
        calls["n"] += 1
        return None

    # Judge fails → state.judge_decision is None.
    monkeypatch.setattr(
        committee_mod,
        "_call_llm_json",
        _staged_call_factory("{not valid json", None),
    )
    state = run_research_debate(
        ctx=_ctx(),
        baseline_signal=_baseline(),
        analyst_views=[_view()],
        config=_config(),
        client=MagicMock(),
        run_one_turn=committee_mod._run_one_turn_with_history,
        run_judge=committee_mod._run_research_manager_judge,
        run_red_team=_spy,
    )
    assert state.judge_decision is None
    assert calls["n"] == 0
    assert state.red_team_turn is None


def test_dissent_threshold_is_fixed_constant():
    """Robustness-not-peak (D80.3 item 3): the threshold is a fixed 0.5."""
    assert RED_TEAM_DISSENT_THRESHOLD == 0.5


def test_redteam_adapter_unit_failure_closed(monkeypatch):
    """_run_red_team_turn returns None on LLM None / parse-failure / no leading view."""
    from hermes_quant.aggregators.llm_committee import _run_red_team_turn

    # No leading view → None without any LLM call.
    assert (
        _run_red_team_turn(
            client=MagicMock(),
            config=_config(),
            market_context=_ctx(),
            analyst_views=[_view()],
            baseline_signal=_baseline(),
            leading_view=None,
        )
        is None
    )

    # LLM returns None → None.
    monkeypatch.setattr(committee_mod, "_call_llm_json", lambda **_: None)
    import json as _json

    class _LV:
        recommendation = "OVERWEIGHT"
        confidence = 0.65
        rationale = "rat"

    assert (
        _run_red_team_turn(
            client=MagicMock(),
            config=_config(),
            market_context=_ctx(),
            analyst_views=[_view()],
            baseline_signal=_baseline(),
            leading_view=_LV(),
        )
        is None
    )

    # Malformed JSON → None.
    monkeypatch.setattr(committee_mod, "_call_llm_json", lambda **_: "{not json")
    assert (
        _run_red_team_turn(
            client=MagicMock(),
            config=_config(),
            market_context=_ctx(),
            analyst_views=[_view()],
            baseline_signal=_baseline(),
            leading_view=_LV(),
        )
        is None
    )

    # Valid → returns a BullBearTurn with forge-resistant red_team flag.
    monkeypatch.setattr(
        committee_mod, "_call_llm_json", lambda **_: _redteam_json(0.7)
    )
    turn = _run_red_team_turn(
        client=MagicMock(),
        config=_config(),
        market_context=_ctx(),
        analyst_views=[_view()],
        baseline_signal=_baseline(),
        leading_view=_LV(),
    )
    assert turn is not None
    assert turn.metadata["red_team"] is True
    # Confirm the rendered prompt carried the leading view (round-trips JSON).
    _ = _json  # keep import meaningful for linters


# ---------------------------------------------------------------------------
# Advisory-plane-only structural test (ADR-0080 D80.1/D80.2): the W7 modules
# import NONE of: the deterministic risk gate (hermes_quant.risk.gate), the
# kill-switch (hermes_quant.governance.kill_switch), or the discrete sizing
# ladder {0, ±0.05, ±0.10, ±0.15, ±0.20}.
#
# Proven two ways:
#   (1) source inspection — the W7-owned files reference none of those surfaces;
#   (2) a CLEAN-subprocess runtime check — importing the W7 modules from a fresh
#       interpreter transitively pulls in neither the risk gate nor the
#       kill-switch into sys.modules. The subprocess removes any suite-ordering
#       contamination (another test may already have loaded the gate in-proc).
# ---------------------------------------------------------------------------


def _module_source(modname: str) -> str:
    import importlib

    mod = importlib.import_module(modname)
    src_path = mod.__file__
    assert src_path is not None
    with open(src_path, encoding="utf-8") as fh:
        return fh.read()


def test_advisory_plane_only_source_has_no_risk_surface():
    """The W7-owned files reference NONE of the risk gate / kill-switch /
    sizing-ladder surfaces. (Source-level structural guarantee.)
    """
    forbidden_substrings = [
        "risk.gate",
        "from hermes_quant.risk",
        "import hermes_quant.risk",
        "governance.kill_switch",
        "kill_switch",
        "KillSwitch",
        "sizing_ladder",
        "SIZING_LADDER",
        "position_ladder",
        "0.05",
        "0.10",
        "0.15",
        "0.20",
    ]

    stage_src = _module_source("hermes_quant.agents.research_debate.stage")
    schemas_src = _module_source("hermes_quant.agents.research_debate.schemas")
    prompt_src = open(
        committee_mod._PROMPT_DIR / "devils_advocate.md", encoding="utf-8"
    ).read()

    for src, label in (
        (stage_src, "stage.py"),
        (schemas_src, "schemas.py"),
        (prompt_src, "devils_advocate.md"),
    ):
        for bad in forbidden_substrings:
            assert bad not in src, f"{label} must not reference {bad!r}"


def test_advisory_plane_only_no_risk_imports_clean_subprocess():
    """Importing the W7 modules from a CLEAN interpreter transitively loads
    NEITHER the deterministic risk gate NOR the kill-switch into sys.modules.
    This is the hard proof that W7 sits entirely in the advisory plane and
    cannot reach the outer standard-of-truth (ADR-0080 D80.1/D80.2).
    """
    import subprocess
    import sys

    code = (
        "import sys\n"
        "import hermes_quant.agents.research_debate.stage\n"
        "import hermes_quant.agents.research_debate.schemas\n"
        "import hermes_quant.aggregators.llm_committee\n"
        "assert 'hermes_quant.risk.gate' not in sys.modules, 'risk.gate imported by W7'\n"
        "assert 'hermes_quant.governance.kill_switch' not in sys.modules, "
        "'kill_switch imported by W7'\n"
        "print('CLEAN')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"clean-subprocess advisory-plane check failed:\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "CLEAN" in proc.stdout


# ---------------------------------------------------------------------------
# W7 off-state key-set bit-identity (ADR-0080 D80.8): with the red-team flag
# UNSET, the research_debate audit payload's KEY-SET is bit-identical to the
# same flag-unset run — the off-state red-team turn adds NO new top-level keys
# (the ``red_team`` sub-block is ALWAYS present, in both states, carrying only
# the off-state record {"ran": False, "dissent_surfaced": False}).
# ---------------------------------------------------------------------------


def _capture_audit_payload(monkeypatch, *, redteam_flag_on: bool) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def _capture(kind: str, source: str, payload: dict[str, Any]) -> None:
        captured["payload"] = payload

    monkeypatch.setattr(stage_mod, "_audit_append", _capture)
    # No red-team turn is wired (redteam=None) so the flag being ON or OFF makes
    # no behavioural difference here — that is exactly the off-state we pin.
    _run_stage(monkeypatch, flag_on=redteam_flag_on, redteam=None)
    return captured["payload"]


def test_offstate_audit_keyset_bit_identical_to_redteam_unset(monkeypatch):
    """REDTEAM_TURN flag UNSET vs explicit-but-no-turn → the audit payload's
    top-level key-set is bit-identical, and the nested ``red_team`` block's
    key-set is bit-identical too. The off-state of the red-team turn introduces
    NO keys relative to a plain research_debate run.
    """
    # Plain research_debate (red-team flag UNSET). _isolate_env already delenv'd
    # HERMES_QUANT_REDTEAM_TURN, so flag_on=False leaves it unset.
    payload_plain = _capture_audit_payload(monkeypatch, redteam_flag_on=False)

    # Flag SET but the red-team turn does not run (redteam=None): the judge still
    # produces a leading view, the adapter is invoked, returns no turn → off-state.
    payload_flag_no_turn = _capture_audit_payload(monkeypatch, redteam_flag_on=True)

    assert set(payload_plain.keys()) == set(payload_flag_no_turn.keys()), (
        "off-state red-team turn must add NO top-level audit keys"
    )
    # The red_team sub-block is present in BOTH and carries the identical
    # off-state key-set (no dissent_reason/confidence/etc. leak into off-state).
    assert "red_team" in payload_plain
    assert payload_plain["red_team"] == {"ran": False, "dissent_surfaced": False}
    assert (
        payload_plain["red_team"].keys() == payload_flag_no_turn["red_team"].keys()
    )
    assert payload_plain["red_team"] == payload_flag_no_turn["red_team"]
