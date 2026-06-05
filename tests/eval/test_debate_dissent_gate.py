"""B41-e ResearchDebate dissent-quality OOS gate tests."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from hermes_quant.eval.debate_dissent_gate import (
    CONTAMINATION_CLEAN,
    EFFECT_REAL,
    HARMLESS,
    OOS_REPRODUCIBLE,
    REGIME_BREADTH,
    DebateDissentEpisode,
    DebateDissentGate,
    DebateDissentGateConfig,
)

BASE = pd.Timestamp("2026-01-01T00:00:00Z")
PRE_CUTOFF = BASE - pd.Timedelta(days=365)
_BLOCK = 25
_RAMP_LO = 0.004
_RAMP_HI = 0.020


def _ramp(k: int) -> float:
    return _RAMP_LO + (_RAMP_HI - _RAMP_LO) * (k / (_BLOCK - 1))


def _ep(
    i: int,
    regime: str,
    debate: float,
    legacy: float,
    ret: float,
    *,
    debate_conf: float = 0.7,
    legacy_conf: float = 0.6,
) -> DebateDissentEpisode:
    asof = BASE + pd.Timedelta(hours=i)
    return DebateDissentEpisode(
        asof=asof,
        observable_asof=asof + pd.Timedelta(hours=24),
        regime=regime,
        debate_action=float(debate),
        legacy_committee_action=float(legacy),
        realized_forward_return=float(ret),
        debate_confidence=float(debate_conf),
        legacy_committee_confidence=float(legacy_conf),
        knowledge_cutoff=PRE_CUTOFF,
    )


def _pass_corpus() -> list[DebateDissentEpisode]:
    eps: list[DebateDissentEpisode] = []
    i = 0
    for block in range(6):
        if block % 2 == 0:
            for k in range(_BLOCK):
                eps.append(_ep(i, "trend", debate=0.20, legacy=0.0, ret=_ramp(k)))
                i += 1
        else:
            for k in range(_BLOCK):
                eps.append(_ep(i, "drawdown", debate=0.0, legacy=0.20, ret=-_ramp(k)))
                i += 1
    return eps


def _one_regime_only() -> list[DebateDissentEpisode]:
    eps: list[DebateDissentEpisode] = []
    i = 0
    for _ in range(6):
        for k in range(_BLOCK):
            eps.append(_ep(i, "trend", debate=0.20, legacy=0.0, ret=_ramp(k)))
            i += 1
    return eps


def _contaminated(seed: int = 7) -> list[DebateDissentEpisode]:
    rng = np.random.default_rng(seed)
    eps: list[DebateDissentEpisode] = []
    for i in range(160):
        regime = "trend" if (i // 20) % 2 == 0 else "drawdown"
        ret = float(rng.choice([-1.0, 1.0]) * rng.uniform(0.005, 0.025))
        debate = 0.20 if ret > 0 else 0.0
        eps.append(_ep(i, regime, debate=debate, legacy=0.20, ret=ret))
    return eps


def _excess_risk() -> list[DebateDissentEpisode]:
    eps: list[DebateDissentEpisode] = []
    i = 0
    for block in range(4):
        if block % 2 == 0:
            for k in range(_BLOCK):
                eps.append(_ep(i, "trend", debate=0.20, legacy=0.10, ret=_ramp(k)))
                i += 1
        else:
            for k in range(_BLOCK):
                eps.append(_ep(i, "drawdown", debate=-0.20, legacy=-0.10, ret=-_ramp(k)))
                i += 1
    for _ in range(20):
        eps.append(_ep(i, "volatile", debate=0.20, legacy=0.05, ret=0.060))
        i += 1
    for _ in range(20):
        eps.append(_ep(i, "volatile", debate=0.20, legacy=0.05, ret=-0.040))
        i += 1
    return eps


def _names(verdict) -> list[str]:
    return verdict.failed_criteria


def test_gate_passes_when_debate_beats_legacy_including_drawdown() -> None:
    verdict = DebateDissentGate().check(_pass_corpus())

    assert verdict.passed is True, verdict.reasons
    assert verdict.reasons == []
    breadth = next(c for c in verdict.criteria if c.name == REGIME_BREADTH)
    assert breadth.metrics["drawdown_beaten"] is True
    assert set(breadth.metrics["beaten_regimes"]) >= {"trend", "drawdown"}


def test_breadth_fails_as_sole_criterion_with_one_regime_only() -> None:
    verdict = DebateDissentGate().check(_one_regime_only())

    assert verdict.passed is False
    assert _names(verdict) == [REGIME_BREADTH]


def test_contamination_fails_as_sole_criterion_on_scattered_oracle_edge() -> None:
    verdict = DebateDissentGate().check(_contaminated())

    assert verdict.passed is False
    assert _names(verdict) == [CONTAMINATION_CLEAN]
    contam = next(c for c in verdict.criteria if c.name == CONTAMINATION_CLEAN)
    assert contam.metrics["shuffle_p_value"] > contam.metrics["shuffle_alpha"]


def test_harmless_fails_as_sole_criterion_when_edge_buys_excess_risk() -> None:
    verdict = DebateDissentGate().check(_excess_risk())

    assert verdict.passed is False
    assert _names(verdict) == [HARMLESS]
    harm = next(c for c in verdict.criteria if c.name == HARMLESS)
    assert (
        harm.metrics["downside_dev_ratio"] > 1.0 + harm.metrics["downside_dev_tol"]
        or harm.metrics["debate_max_drawdown"] < harm.metrics["legacy_max_drawdown"]
    )


def test_effect_fails_as_sole_criterion_when_dsr_below_floor() -> None:
    cfg = DebateDissentGateConfig(dsr_floor=1.0000001)
    verdict = DebateDissentGate(cfg).check(_pass_corpus())

    assert verdict.passed is False
    assert _names(verdict) == [EFFECT_REAL]


def test_oos_fails_as_sole_criterion_when_fold_rate_below_floor() -> None:
    cfg = DebateDissentGateConfig(fold_rate_floor=1.0000001)
    verdict = DebateDissentGate(cfg).check(_pass_corpus())

    assert verdict.passed is False
    assert _names(verdict) == [OOS_REPRODUCIBLE]


def test_same_corpus_yields_byte_identical_verdict() -> None:
    v1 = DebateDissentGate().check(_pass_corpus())
    v2 = DebateDissentGate().check(_pass_corpus())

    assert repr(v1) == repr(v2)


def test_contaminated_corpus_yields_byte_identical_verdict() -> None:
    v1 = DebateDissentGate().check(_contaminated())
    v2 = DebateDissentGate().check(_contaminated())

    assert repr(v1) == repr(v2)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("realized_forward_return", math.nan),
        ("debate_confidence", math.inf),
        ("legacy_committee_confidence", math.nan),
    ],
)
def test_non_finite_returns_or_confidences_fail_closed(field: str, value: float) -> None:
    eps = _pass_corpus()
    bad = eps[0]
    payload = {
        "asof": bad.asof,
        "observable_asof": bad.observable_asof,
        "regime": bad.regime,
        "debate_action": bad.debate_action,
        "legacy_committee_action": bad.legacy_committee_action,
        "realized_forward_return": bad.realized_forward_return,
        "debate_confidence": bad.debate_confidence,
        "legacy_committee_confidence": bad.legacy_committee_confidence,
        "knowledge_cutoff": bad.knowledge_cutoff,
    }
    payload[field] = value
    eps[0] = DebateDissentEpisode(**payload)

    verdict = DebateDissentGate().check(eps)

    assert verdict.passed is False
    assert verdict.metrics["finite_ok"] is False
    assert verdict.failed_criteria
