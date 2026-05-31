"""W7 shadow EVAL-GATE — the deterministic criterion to FLIP the flag
(ADR-0080 W7 eval gate; capability-map §4 / §5).

The eval gate (verbatim from the plan):
    "in shadow, the red-team turn measurably changes the dissent-surfaced rate
     (vs the no-red-team baseline) without inflating the false-flat rate;
     aggregation stays deterministic (no vote-counting; the red-team turn is one
     more piece of evidence, never a ballot)."

Encoded deterministically over a fixed synthetic corpus of N debate states (no
LLM, no network). The three flip criteria:

  (1) dissent-surfaced rate ON is STRICTLY DIFFERENT from OFF (effect is real);
  (2) the FLAT set is IDENTICAL ON vs OFF — false_flat_rate == 0.0 (no harm);
  (3) the judge direction/confidence are bit-identical ON vs OFF (no vote).

Flip rule: the flag may be turned on in shadow IFF all three tests pass.
"""

from __future__ import annotations

from statistics import mean
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

from hermes_quant.agents.research_debate.stage import run_research_debate
from hermes_quant.aggregators import llm_committee as committee_mod
from hermes_quant.aggregators.deliberative import DeliberativeConfig
from hermes_quant.protocol import AggregatedSignal, AnalystView, MarketContext

CORPUS_N = 50
# A "high dissent" red-team confidence surfaces dissent (>= 0.5); "low" does not.
# The corpus alternates so the ON dissent-rate is a fixed, known fraction.
_DISSENT_THRESHOLD = 0.5


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_RESEARCH_DEBATE", raising=False)
    monkeypatch.delenv("HERMES_QUANT_RESEARCH_DEBATE_ROUNDS", raising=False)
    monkeypatch.delenv("HERMES_QUANT_REDTEAM_TURN", raising=False)
    yield


# ---------------------------------------------------------------------------
# Synthetic corpus builders (deterministic, seeded by index — no RNG)
# ---------------------------------------------------------------------------


def _ctx(i: int) -> MarketContext:
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
        asset=f"SYN{i:03d}",
        timeframe="1d",
        asset_class="equity",
        exchange=None,
        bars=bars,
        last_close=101.0,
        last_volume=1000.0,
        asof=ts[-1],
    )


def _view() -> AnalystView:
    return AnalystView(
        analyst="ta",
        direction=1,  # type: ignore[arg-type]
        magnitude=0.012,
        confidence=0.7,
        confidence_raw=0.7,
        horizon="1d",
        rationale="ta rationale",
    )


def _baseline(i: int) -> AggregatedSignal:
    return AggregatedSignal(
        asset=f"SYN{i:03d}",
        timeframe="1d",
        asset_class="equity",
        asof=pd.Timestamp("2024-01-01 01:00", tz="UTC"),
        direction=1,
        magnitude=0.012,
        confidence=0.6,
        confidence_raw=0.6,
        horizon="1d",
        components=(_view(),),
        aggregator="bma",
    )


def _config() -> DeliberativeConfig:
    return DeliberativeConfig(
        enable_llm_turns=True, max_debate_rounds=1, enable_risk_mgmt=False
    )


def _bull_json() -> str:
    return (
        '{"role": "bull_researcher", "stance": "long", "confidence": 0.7, '
        '"rationale": "trend.", "key_evidence": ["ta"], '
        '"counterarguments": "macro.", "metadata": {"tier": "quick", "round": 1}}'
    )


def _bear_json() -> str:
    return (
        '{"role": "bear_researcher", "stance": "cautious", "confidence": 0.4, '
        '"rationale": "macro.", "key_evidence": ["ta"], '
        '"counterarguments": "volume.", "metadata": {"tier": "quick", "round": 1}}'
    )


def _judge_json() -> str:
    return (
        '{"recommendation": "OVERWEIGHT", "confidence": 0.65, '
        '"rationale": "bull wins.", '
        '"strategic_actions": "add 1R.", "metadata": {}}'
    )


def _redteam_json(conf: float) -> str:
    return (
        '{"role": "bear_researcher", "stance": "regime assumption", '
        f'"confidence": {conf}, "rationale": "unstated regime assumption.", '
        '"key_evidence": ["regime"], '
        '"counterarguments": "do not act: base-rate ignored.", '
        '"metadata": {"tier": "quick", "red_team": true}}'
    )


def _redteam_conf_for_index(i: int) -> float:
    """Half the corpus gets a high-dissent critic (>=0.5), half low (<0.5).

    This guarantees a known, non-trivial ON dissent rate (~0.5) that is
    STRICTLY different from the OFF rate (0.0).
    """
    return 0.8 if (i % 2 == 0) else 0.2


def _staged_call_factory(redteam: str | None) -> Any:
    sequence = [_bull_json(), _bear_json(), _judge_json()]
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


def _run_one(monkeypatch, i: int, *, flag_on: bool):
    if flag_on:
        monkeypatch.setenv("HERMES_QUANT_REDTEAM_TURN", "1")
    else:
        monkeypatch.delenv("HERMES_QUANT_REDTEAM_TURN", raising=False)
    redteam = _redteam_json(_redteam_conf_for_index(i)) if flag_on else None
    monkeypatch.setattr(
        committee_mod, "_call_llm_json", _staged_call_factory(redteam)
    )
    return run_research_debate(
        ctx=_ctx(i),
        baseline_signal=_baseline(i),
        analyst_views=[_view()],
        config=_config(),
        client=MagicMock(),
        run_one_turn=committee_mod._run_one_turn_with_history,
        run_judge=committee_mod._run_research_manager_judge,
    )


def _build_corpus(monkeypatch, *, flag_on: bool) -> list:
    return [_run_one(monkeypatch, i, flag_on=flag_on) for i in range(CORPUS_N)]


def _dissent_surfaced_rate(states) -> float:
    return mean(1.0 if s.dissent_surfaced else 0.0 for s in states)


def _is_flat(state) -> bool:
    """A debate-stage 'flat' proxy: judge is None or HOLD (direction 0).

    The red-team CANNOT change this (it never touches the judge's
    recommendation), so the FLAT set must be identical ON vs OFF.
    """
    if state.judge_decision is None:
        return True
    return state.judge_decision.recommendation.value == "HOLD"


def _false_flat_rate(states_off, states_on) -> float:
    """A 'false flat' is a state that is FLAT under red-team-ON but NOT FLAT
    under red-team-OFF — i.e. a flat the red-team CAUSED. W7 can never cause a
    flat (it never touches direction), so this MUST be 0.0.
    """
    caused = 0
    for off, on in zip(states_off, states_on, strict=True):
        if _is_flat(on) and not _is_flat(off):
            caused += 1
    return caused / len(states_on)


# ---------------------------------------------------------------------------
# The three flip-criterion tests
# ---------------------------------------------------------------------------


def test_eval_gate_dissent_rate_changes(monkeypatch):
    """(1) Effect is real: ON dissent rate STRICTLY differs from OFF (== 0.0)."""
    off = _build_corpus(monkeypatch, flag_on=False)
    on = _build_corpus(monkeypatch, flag_on=True)

    rate_off = _dissent_surfaced_rate(off)
    rate_on = _dissent_surfaced_rate(on)

    assert rate_off == 0.0, "off-state must surface zero dissent"
    assert rate_on > 0.0, "red-team ON must surface some dissent"
    assert rate_on != rate_off, "dissent-surfaced rate must measurably change"
    # By construction half the corpus has a high-dissent critic.
    assert rate_on == pytest.approx(0.5)


def test_eval_gate_false_flat_rate_not_inflated(monkeypatch):
    """(2) The HARD gate: the FLAT set is IDENTICAL ON vs OFF; false_flat == 0."""
    off = _build_corpus(monkeypatch, flag_on=False)
    on = _build_corpus(monkeypatch, flag_on=True)

    flats_off = [_is_flat(s) for s in off]
    flats_on = [_is_flat(s) for s in on]

    assert flats_off == flats_on, "red-team must NOT change which states are FLAT"
    assert _false_flat_rate(off, on) == 0.0


def test_eval_gate_aggregation_deterministic(monkeypatch):
    """(3) No vote: judge recommendation + confidence bit-identical ON vs OFF."""
    off = _build_corpus(monkeypatch, flag_on=False)
    on = _build_corpus(monkeypatch, flag_on=True)

    for s_off, s_on in zip(off, on, strict=True):
        assert (s_off.judge_decision is None) == (s_on.judge_decision is None)
        if s_off.judge_decision is None:
            continue
        assert (
            s_off.judge_decision.recommendation
            == s_on.judge_decision.recommendation
        )
        assert s_off.judge_decision.confidence == pytest.approx(
            s_on.judge_decision.confidence
        )


def test_flip_rule_all_three_criteria(monkeypatch):
    """The composite flip rule: the flag may flip IFF all three pass together."""
    off = _build_corpus(monkeypatch, flag_on=False)
    on = _build_corpus(monkeypatch, flag_on=True)

    # (1) effect real
    effect_real = _dissent_surfaced_rate(on) != _dissent_surfaced_rate(off)
    # (2) no harm
    no_harm = _false_flat_rate(off, on) == 0.0
    # (3) no vote
    no_vote = all(
        (s_off.judge_decision is None and s_on.judge_decision is None)
        or (
            s_off.judge_decision is not None
            and s_on.judge_decision is not None
            and s_off.judge_decision.recommendation
            == s_on.judge_decision.recommendation
            and s_off.judge_decision.confidence
            == pytest.approx(s_on.judge_decision.confidence)
        )
        for s_off, s_on in zip(off, on, strict=True)
    )

    may_flip = effect_real and no_harm and no_vote
    assert may_flip, (
        f"flip criteria: effect_real={effect_real}, no_harm={no_harm}, "
        f"no_vote={no_vote}"
    )
