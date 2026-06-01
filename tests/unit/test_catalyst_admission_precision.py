"""ADR-0075 admission-precision eval axis (B05 CODE half) — the eval GATE for
HERMES_QUANT_CATALYST_ONBOARDING.

`run_admission_precision` measures precision CONDITIONAL ON ADMISSION: of the
out-of-universe names ADR-0075 onboarding would actually admit (fresh, conf>=TAU_CONF,
mag>=TAU_MAG, non-neutral, tradeable), what fraction moved in the packet's stance
direction at >= a stated bar? Unlike `run_precision` (packet-stance vs return), this is
the gate-relevant question because only ADMITTED names get traded.

Fully OFFLINE/deterministic off a versioned fixture (tests/fixtures/catalyst_onboarding,
NEVER /tmp): the gate is replayed from the episode fields, the realized forward returns
were captured ONCE offline and committed. External truth, never self-graded. No network.

The flag-flip is an OPERATOR action; this axis only MEASURES whether the bar is cleared.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_quant.catalyst.eval import (
    AdmissionEpisode,
    run_admission_precision,
)
from hermes_quant.catalyst.onboarding import TAU_CONF, TAU_MAG

FIXT = Path(__file__).resolve().parents[1] / "fixtures" / "catalyst_onboarding"
FIXTURE_FILE = FIXT / "admission_episodes.v1.json"
MIN_HIT_RATE = 0.6  # the stated admission hit-rate bar (D74.7 floor; matches the fixture)


def _load_fixture() -> dict:
    return json.loads(FIXTURE_FILE.read_text())  # versioned, NOT /tmp, NOT live network


def _load_episodes() -> list[AdmissionEpisode]:
    data = _load_fixture()
    eps: list[AdmissionEpisode] = []
    for e in data["episodes"]:
        eps.append(
            AdmissionEpisode(
                symbol=e["symbol"],
                stance=e["stance"],
                confidence=float(e["confidence"]),
                magnitude=float(e["magnitude"]),
                realized_forward_return=float(e["realized_forward_return"]),
                in_universe=bool(e.get("in_universe", False)),
                tradeable=bool(e.get("tradeable", True)),
                horizon=e.get("horizon", "1d"),
                label=e.get("label", ""),
            )
        )
    return eps


# ---------------------------------------------------------------------------
# fixture hygiene
# ---------------------------------------------------------------------------

def test_fixture_is_versioned_and_documented():
    assert FIXTURE_FILE.exists(), "fixture must be committed under tests/fixtures, not /tmp"
    assert "/tmp" not in str(FIXTURE_FILE)
    data = _load_fixture()
    assert data["episodes"], "fixture has episodes"
    # the gate-linkage statement is part of the fixture's defensibility record.
    assert "HERMES_QUANT_CATALYST_ONBOARDING" in data["gate_linkage"]
    # thresholds in the fixture match the live onboarding thresholds (no drift).
    assert data["tau_conf"] == pytest.approx(TAU_CONF)
    assert data["tau_mag"] == pytest.approx(TAU_MAG)
    assert data["min_hit_rate"] == pytest.approx(MIN_HIT_RATE)
    # the seed episode (the real LUNR Blue-Origin move) is present.
    assert any(e["symbol"] == "LUNR" for e in data["episodes"])


# ---------------------------------------------------------------------------
# the GATE: admission hit-rate clears the bar
# ---------------------------------------------------------------------------

def test_admission_precision_clears_bar():
    """The axis runs on the fixture, computes admission hit-rate, and PASSES at the
    bar. 5 admitted (LUNR/RKLB/ASTS/LCID/SPR), 5 scored, 4 hits => 0.80 >= 0.60."""
    res = run_admission_precision(_load_episodes(), min_hit_rate=MIN_HIT_RATE)
    assert res.passed, f"admission gate FAIL: hit_rate={res.hit_rate} misses={res.misses}"
    assert res.n_admitted == 5
    assert res.n_scored == 5
    assert res.hits == 4
    assert res.hit_rate == 0.80


def test_admission_precision_fails_above_achieved_bar():
    """Bar is a real threshold: at 0.85 the 0.80 set FAILS (not vacuously passing)."""
    res = run_admission_precision(_load_episodes(), min_hit_rate=0.85)
    assert not res.passed
    assert res.hit_rate == 0.80


def test_documented_miss_is_in_the_denominator():
    """The SPR miss is counted (the bar is cleared WITH a miss, not by cherry-picking).
    Honest precision: the miss appears in res.misses and drags the rate from 1.0 to 0.8."""
    res = run_admission_precision(_load_episodes(), min_hit_rate=MIN_HIT_RATE)
    missed = {m.split(":")[0] for m in res.misses}
    assert missed == {"SPR"}
    # without the miss it would be a perfect 4/4; the miss makes it an honest 4/5.
    assert res.hits == res.n_scored - 1


# ---------------------------------------------------------------------------
# NEGATIVE CONTROL: benign admissions do not inflate (or tank) precision
# ---------------------------------------------------------------------------

def test_negative_control_episodes_are_not_admitted():
    """The 5 negative-control names are EXCLUDED from scoring by the admission gate —
    they are neither hits nor misses. PLUG (sub-conf), JOBY (sub-mag), AMD (in-universe),
    SOFI (not tradeable), RIVN (neutral)."""
    res = run_admission_precision(_load_episodes(), min_hit_rate=MIN_HIT_RATE)
    rejected = {r.split(":")[0] for r in res.rejected}
    assert rejected == {"PLUG", "JOBY", "AMD", "SOFI", "RIVN"}
    # excluded means: n_admitted < n_episodes, and the scored set is exactly the admitted.
    assert res.n_episodes == 10
    assert res.n_admitted == 5
    assert res.n_scored <= res.n_admitted


def test_correct_benign_admission_does_not_inflate_precision():
    """The CRUX negative control: JOBY (sub-magnitude) and SOFI (not tradeable) are both
    directionally CORRECT. If the gate leaked them through they would PAD the hit-rate
    with free hits (0.80 -> ~0.857). Because they are NOT admitted, precision stays 0.80.
    Counter-check: force them admissible and confirm the rate WOULD inflate."""
    eps = _load_episodes()
    baseline = run_admission_precision(eps, min_hit_rate=MIN_HIT_RATE)
    assert baseline.hit_rate == 0.80

    # Construct a leaky variant where the two correct benign names ARE admissible.
    leaky = []
    for e in eps:
        if e.symbol == "JOBY":  # bump magnitude over the floor
            leaky.append(AdmissionEpisode(
                symbol=e.symbol, stance=e.stance, confidence=e.confidence,
                magnitude=TAU_MAG, realized_forward_return=e.realized_forward_return,
                in_universe=False, tradeable=True, horizon=e.horizon, label=e.label))
        elif e.symbol == "SOFI":  # flip tradeable
            leaky.append(AdmissionEpisode(
                symbol=e.symbol, stance=e.stance, confidence=e.confidence,
                magnitude=e.magnitude, realized_forward_return=e.realized_forward_return,
                in_universe=False, tradeable=True, horizon=e.horizon, label=e.label))
        else:
            leaky.append(e)
    leaked = run_admission_precision(leaky, min_hit_rate=MIN_HIT_RATE)
    # both are correct -> they would be free hits, inflating the rate above the honest 0.80.
    assert leaked.n_scored == baseline.n_scored + 2
    assert leaked.hits == baseline.hits + 2
    assert leaked.hit_rate > baseline.hit_rate  # proves they WOULD have inflated it
    # the real gate kept them OUT, so the honest measurement is unaffected.
    assert baseline.hit_rate == 0.80


def test_wrong_benign_admission_does_not_tank_precision():
    """The mirror: PLUG (sub-confidence) and AMD (in-universe) are directionally WRONG.
    If leaked they would TANK the rate (0.80 -> 0.571, failing the bar). Because they are
    rejected, the honest rate stays 0.80 and the gate still PASSES."""
    eps = _load_episodes()
    leaky = []
    for e in eps:
        if e.symbol == "PLUG":  # bump confidence over the floor
            leaky.append(AdmissionEpisode(
                symbol=e.symbol, stance=e.stance, confidence=TAU_CONF,
                magnitude=e.magnitude, realized_forward_return=e.realized_forward_return,
                in_universe=False, tradeable=True, horizon=e.horizon, label=e.label))
        elif e.symbol == "AMD":  # treat as out-of-universe
            leaky.append(AdmissionEpisode(
                symbol=e.symbol, stance=e.stance, confidence=e.confidence,
                magnitude=e.magnitude, realized_forward_return=e.realized_forward_return,
                in_universe=False, tradeable=True, horizon=e.horizon, label=e.label))
        else:
            leaky.append(e)
    leaked = run_admission_precision(leaky, min_hit_rate=MIN_HIT_RATE)
    assert leaked.hits == 4  # no new hits (both wrong)
    assert leaked.n_scored == 7  # 5 honest + 2 leaked wrong ones
    assert leaked.hit_rate < MIN_HIT_RATE  # would FAIL the bar if leaked
    assert not leaked.passed
    # the real gate keeps them out -> honest measurement passes.
    assert run_admission_precision(eps, min_hit_rate=MIN_HIT_RATE).passed


# ---------------------------------------------------------------------------
# gate replay edge cases (deterministic, no network)
# ---------------------------------------------------------------------------

def test_no_admitted_episodes_does_not_vacuously_pass():
    """If nothing is admissible, the gate must NOT pass (scored==0 is a fail, not a
    free pass) — a flag should never be flipped on an empty measurement."""
    eps = [AdmissionEpisode("XXX", "bullish", 0.10, 0.001, 5.0, in_universe=False)]
    res = run_admission_precision(eps, min_hit_rate=MIN_HIT_RATE)
    assert res.n_admitted == 0
    assert res.n_scored == 0
    assert not res.passed


def test_flat_return_admitted_but_not_scored():
    """A name that is admitted but has a flat (0.0) realized return is not directionally
    scorable — admitted, not counted as hit or miss."""
    eps = [
        AdmissionEpisode("ZZZ", "bullish", 0.80, 0.06, 0.0, in_universe=False),
        AdmissionEpisode("LUNR", "bearish", 0.82, 0.06, -4.09, in_universe=False),
    ]
    res = run_admission_precision(eps, min_hit_rate=MIN_HIT_RATE)
    assert res.n_admitted == 2
    assert res.n_scored == 1  # ZZZ admitted but flat -> not scored
    assert res.hits == 1
    assert res.passed
