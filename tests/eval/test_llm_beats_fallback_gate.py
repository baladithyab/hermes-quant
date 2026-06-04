"""tests/eval/test_llm_beats_fallback_gate.py — Gate-3 keystone (lane W2B, B41-b).

The OOS *LLM-beats-deterministic-fallback* eval gate proves, offline and
deterministically, that an LLM decision stage beats its deterministic fallback
on REALIZED decision quality. It is advisory-plane / eval-only: it produces a
per-axis verdict a human reads (PromotionDecision-shaped: passed + reasons[] +
suggested_action + metrics). It flips no flag and touches no decision path.

These tests pin the gate's CONTRACT before the module exists (TDD):

The unifying model
------------------
Every episode's realized decision quality = action * realized_forward_return.
  r_llm[i] = llm_action[i] * ret[i]
  r_fb[i]  = fallback_action[i] * ret[i]
  d[i]     = r_llm[i] - r_fb[i]          (the delta the gate scores)
RiskCommittee approval ∈ {0,1} (approve earns the trade's fwd return, reject
earns 0 = silence = flat); Trader position ∈ [-1,1]. ONE engine, two thin axis
wrappers differing only in action-domain validation and (committee only) an
approval-precision read-out.

The five criteria (ALL must pass)
---------------------------------
1. EFFECT_REAL        — mean(d) > 0 AND dsr.deflated_sharpe(d) >= dsr_floor.
2. REGIME_BREADTH     — LLM beats fallback (per-regime mean d > 0) in >= 2
                        regimes INCLUDING the drawdown regime.
3. OOS_REPRODUCIBLE   — cv.PurgedWalkForward beats-fallback fold-rate >= floor.
4. CONTAMINATION_CLEAN— structural observable_asof STRICTLY > asof for every
                        episode + (when a knowledge_cutoff is set) the realized
                        outcome lies after it + a lookahead shuffle test on the
                        lag-1 autocovariance of d (clean ⟺ p_value <= alpha).
5. HARMLESS           — not achieved via excess risk: LLM downside-deviation
                        ratio within tol AND max-drawdown not materially worse
                        than fallback (strictest: either trips → fail).

The four fail-modes proven below
--------------------------------
(a) beats in 2 regimes incl. drawdown        → PASS
(b) beats in only 1 regime                   → REGIME_BREADTH fails
(c) "beats" via a scattered (memorized) edge → CONTAMINATION_CLEAN fails
(d) beats but via excess risk                → HARMLESS fails
(e) determinism: same corpus twice           → byte-identical verdict
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hermes_quant.eval.llm_beats_fallback_gate import (
    CONTAMINATION_CLEAN,
    EFFECT_REAL,
    HARMLESS,
    OOS_REPRODUCIBLE,
    REGIME_BREADTH,
    CriterionResult,
    Episode,
    GateConfig,
    GateVerdict,
    RiskCommitteeAxis,
    TraderAxis,
)

BASE = pd.Timestamp("2026-01-01T00:00:00Z")
# Decisions well after any plausible LLM training cutoff -> never contaminated
# by the knowledge-cutoff sub-check (clean corpora).
PRE_CUTOFF = BASE - pd.Timedelta(days=365)


# ---------------------------------------------------------------------------
# Episode factory + corpus builders
# ---------------------------------------------------------------------------


def _ep(
    i: int,
    regime: str,
    llm: float,
    fb: float,
    ret: float,
    *,
    horizon_h: int = 24,
    knowledge_cutoff: pd.Timestamp | None = PRE_CUTOFF,
) -> Episode:
    """One episode at hour `i`, forward-observable `horizon_h` later."""
    asof = BASE + pd.Timedelta(hours=i)
    return Episode(
        asof=asof,
        observable_asof=asof + pd.Timedelta(hours=horizon_h),
        regime=regime,
        llm_action=float(llm),
        fallback_action=float(fb),
        realized_forward_return=float(ret),
        knowledge_cutoff=knowledge_cutoff,
    )


# A SMOOTH, SYMMETRIC ramp 0.004..0.020 over a 25-step block. Two properties the
# fixtures lean on:
#   * symmetric (arithmetic grid -> skew ≈ 0) so the DSR variance-term stays
#     positive even at the high per-observation Sharpe a synthetic edge produces;
#   * smooth (adjacent values nearly equal) so the delta series is temporally
#     CLUSTERED -> high lag-1 autocovariance -> a clear shuffle outlier -> clean.
_RAMP_LO, _RAMP_HI, _BLOCK = 0.004, 0.020, 25


def _ramp(k: int) -> float:
    return _RAMP_LO + (_RAMP_HI - _RAMP_LO) * (k / (_BLOCK - 1))


def _committee_pass() -> list[Episode]:
    """Genuine, harmless, clustered committee edge across 2 regimes incl. drawdown.

    Six contiguous blocks alternate trend / drawdown so the realized edge is
    temporally CLUSTERED (a smooth sawtooth in d -> high lag-1 autocov ->
    contamination-clean) and every walk-forward test window sits inside a beaten
    regime (OOS reproducible). Both block types yield d = +ramp (a clean,
    symmetric, all-positive sawtooth):
      * trend    (market up):   LLM approves a winner the fallback rejected
                                (llm=1, fb=0, ret=+ramp) -> d = +ramp. LLM only
                                ever has upside.
      * drawdown (market down): fallback approves a loser, LLM correctly sits out
                                (llm=0, fb=1, ret=-ramp) -> d = -ret = +ramp > 0.
                                The fallback eats the loss; the LLM is flat (less
                                risk) -> HARMLESS.
    """
    eps: list[Episode] = []
    i = 0
    for block in range(6):
        if block % 2 == 0:  # trend block (market up): LLM approves the winner
            for k in range(_BLOCK):
                eps.append(_ep(i, "trend", llm=1, fb=0, ret=_ramp(k)))
                i += 1
        else:  # drawdown block (market down): LLM sits out, fallback eats the loss
            for k in range(_BLOCK):
                eps.append(_ep(i, "drawdown", llm=0, fb=1, ret=-_ramp(k)))
                i += 1
    return eps


def _committee_breadth_fail() -> list[Episode]:
    """Identical to the pass corpus EXCEPT the LLM no longer beats in drawdown.

    In every drawdown block the LLM now also approves the loser (llm=1, fb=1)
    so d == 0 there -> the drawdown regime is NOT beaten. The only differential
    vs `_committee_pass` is the drawdown edge, so a flipped verdict isolates the
    REGIME_BREADTH gate. The trend edge (and overall positive mean) is intact.
    """
    eps: list[Episode] = []
    i = 0
    for block in range(6):
        if block % 2 == 0:  # trend block: beaten (unchanged)
            for k in range(_BLOCK):
                eps.append(_ep(i, "trend", llm=1, fb=0, ret=_ramp(k)))
                i += 1
        else:  # drawdown block: LLM now ALSO holds the loser -> d == 0, not beaten
            for k in range(_BLOCK):
                eps.append(_ep(i, "drawdown", llm=1, fb=1, ret=-_ramp(k)))
                i += 1
    return eps


def _committee_contaminated(seed: int = 7) -> list[Episode]:
    """A SCATTERED (memorized / oracle) edge — positive but temporally i.i.d.

    Market direction is drawn i.i.d. (seeded). The fallback always approves
    (fb=1); the "oracle" LLM approves iff the realized return is positive
    (llm = 1 iff ret>0). So the LLM dodges every loser:
        ret>0  -> llm=1 -> d = (1-1)*ret = 0
        ret<=0 -> llm=0 -> d = (0-1)*ret = -ret >= 0
    The positive deltas land on SCATTERED (i.i.d.) episodes -> lag-1
    autocovariance ~ 0 -> shuffle-invariant -> p_value large -> CONTAMINATION
    fires. mean(d) > 0, the edge spans both regimes, and the LLM has zero
    downside (it only ever sits out losers), so EFFECT/BREADTH/HARMLESS all pass
    — contamination is the SOLE failure (the discriminator vs `_committee_pass`).
    """
    rng = np.random.default_rng(seed)
    eps: list[Episode] = []
    for i in range(160):
        regime = "trend" if (i // 20) % 2 == 0 else "drawdown"
        # i.i.d. signed magnitude — scattered in time, no temporal structure.
        ret = float(rng.choice([-1.0, 1.0]) * rng.uniform(0.005, 0.025))
        llm = 1 if ret > 0 else 0  # perfect (memorized) foresight
        eps.append(_ep(i, regime, llm=llm, fb=1, ret=ret))
    return eps


def _trader_pass() -> list[Episode]:
    """Genuine, harmless, clustered TRADER edge ([-1,1]) across 2 regimes.

    trend (up):     LLM long +0.8, fallback long +0.3 -> d = +0.5*ret > 0.
    drawdown (down):LLM short -0.8, fallback short -0.3 -> d = -0.5*ret > 0.
    Both sides only ever profit (no negative realized return for either book),
    so downside-deviation and drawdown are 0 for both -> HARMLESS passes.
    """
    eps: list[Episode] = []
    i = 0
    for block in range(6):
        if block % 2 == 0:
            for k in range(_BLOCK):
                eps.append(_ep(i, "trend", llm=0.8, fb=0.3, ret=_ramp(k)))
                i += 1
        else:
            for k in range(_BLOCK):
                eps.append(_ep(i, "drawdown", llm=-0.8, fb=-0.3, ret=-_ramp(k)))
                i += 1
    return eps


def _trader_excess_risk() -> list[Episode]:
    """LLM beats fallback but BUYS the edge with excess risk (HARMLESS must fail).

    Trend + drawdown blocks are the harmless `_trader_pass` shape (both books all
    upside). A trailing "volatile" regime adds a CONTIGUOUS win-run then loss-run
    where the LLM is fully levered (+1.0) and the fallback barely participates
    (+0.05):
        win-run  (+0.06): r_llm=+0.06, r_fb=+0.003
        loss-run (-0.04): r_llm=-0.04 (deep LLM drawdown), r_fb=-0.002 (tiny)
    d = (1.0-0.05)*ret = 0.95*ret stays clustered (contiguous runs -> positive
    autocov -> contamination clean) and its block mean is positive (LLM beats),
    so EFFECT/BREADTH/CONTAMINATION pass. But the LLM's downside-deviation and
    max-drawdown dwarf the fallback's -> HARMLESS is the SOLE failure.
    """
    eps: list[Episode] = []
    i = 0
    for block in range(4):  # trend/drawdown x2 -> harmless, beaten, clustered
        if block % 2 == 0:
            for k in range(_BLOCK):
                eps.append(_ep(i, "trend", llm=0.8, fb=0.3, ret=_ramp(k)))
                i += 1
        else:
            for k in range(_BLOCK):
                eps.append(_ep(i, "drawdown", llm=-0.8, fb=-0.3, ret=-_ramp(k)))
                i += 1
    # Volatile regime: LLM over-levers; fallback stays near-flat. Contiguous
    # win-run then loss-run keeps d = 0.95*ret CLUSTERED (positive autocov ->
    # contamination clean) and block-positive (LLM beats), while heaping all the
    # downside on the LLM book (deep LLM drawdown, negligible fallback drawdown).
    for _ in range(20):  # contiguous WIN run
        eps.append(_ep(i, "volatile", llm=1.0, fb=0.05, ret=0.06))
        i += 1
    for _ in range(20):  # contiguous LOSS run -> deep LLM drawdown only
        eps.append(_ep(i, "volatile", llm=1.0, fb=0.05, ret=-0.04))
        i += 1
    return eps


# ---------------------------------------------------------------------------
# Verdict shape
# ---------------------------------------------------------------------------


def test_verdict_is_promotiondecision_shaped():
    """The verdict mirrors PromotionDecision: a bool, a reasons list, a
    suggested_action string, a metrics dict, and a per-criterion breakdown."""
    verdict = RiskCommitteeAxis().evaluate(_committee_pass())
    assert isinstance(verdict, GateVerdict)
    assert isinstance(verdict.passed, bool)
    assert isinstance(verdict.reasons, list)
    assert isinstance(verdict.suggested_action, str) and verdict.suggested_action
    assert isinstance(verdict.metrics, dict)
    assert verdict.axis == "risk_committee"
    assert all(isinstance(c, CriterionResult) for c in verdict.criteria)
    # The five named criteria are always evaluated and reported.
    assert {c.name for c in verdict.criteria} == {
        EFFECT_REAL,
        REGIME_BREADTH,
        OOS_REPRODUCIBLE,
        CONTAMINATION_CLEAN,
        HARMLESS,
    }
    # passed ⟺ no failed criterion ⟺ reasons empty.
    assert verdict.passed == (verdict.reasons == [])
    assert (not verdict.passed) == bool(verdict.failed_criteria)


# ---------------------------------------------------------------------------
# (a) PASS — beats in >= 2 regimes including drawdown, real + harmless + clean
# ---------------------------------------------------------------------------


def test_committee_axis_passes_when_llm_beats_fallback_two_regimes():
    verdict = RiskCommitteeAxis().evaluate(_committee_pass())
    assert verdict.passed is True, f"unexpected failures: {verdict.reasons}"
    assert verdict.reasons == []
    assert verdict.failed_criteria == []


def test_trader_axis_passes_when_llm_beats_fallback_two_regimes():
    verdict = TraderAxis().evaluate(_trader_pass())
    assert verdict.passed is True, f"unexpected failures: {verdict.reasons}"
    assert verdict.failed_criteria == []


# ---------------------------------------------------------------------------
# (b) REGIME_BREADTH — beats in only one regime → fail (needs >=2 incl drawdown)
# ---------------------------------------------------------------------------


def test_breadth_fail_when_only_one_regime_beaten():
    """Differential isolation: the breadth-fail corpus is byte-identical to the
    pass corpus except the LLM no longer beats in the drawdown regime. The pass
    corpus passes; this one fails AND raises the breadth criterion."""
    pass_verdict = RiskCommitteeAxis().evaluate(_committee_pass())
    fail_verdict = RiskCommitteeAxis().evaluate(_committee_breadth_fail())
    assert pass_verdict.passed is True
    assert fail_verdict.passed is False
    assert REGIME_BREADTH in fail_verdict.failed_criteria
    # The breadth criterion's metrics name the beaten regimes — drawdown is absent.
    breadth = next(c for c in fail_verdict.criteria if c.name == REGIME_BREADTH)
    assert "drawdown" not in breadth.metrics.get("beaten_regimes", [])


def test_breadth_requires_the_drawdown_regime_specifically():
    """Even two beaten regimes fail breadth when NEITHER is the drawdown regime —
    drawdown survival is the load-bearing case for money-software."""
    eps: list[Episode] = []
    i = 0
    # Two non-drawdown regimes, both beaten; NO drawdown regime present at all.
    for regime in ["trend", "range", "trend", "range"]:
        for k in range(_BLOCK):
            eps.append(_ep(i, regime, llm=1, fb=0, ret=_ramp(k)))
            i += 1
    verdict = RiskCommitteeAxis().evaluate(eps)
    assert verdict.passed is False
    assert REGIME_BREADTH in verdict.failed_criteria


# ---------------------------------------------------------------------------
# (c) CONTAMINATION_CLEAN — a scattered (memorized) edge is caught
# ---------------------------------------------------------------------------


def test_contamination_fires_on_scattered_oracle_edge():
    """A positive edge that is temporally SCATTERED (i.i.d., memorized) trips the
    lag-1-autocovariance shuffle test, while the clustered genuine edge stays
    clean. Asserting BOTH directions proves the guard discriminates (it is not a
    one-sided trip)."""
    clean = RiskCommitteeAxis().evaluate(_committee_pass())
    dirty = RiskCommitteeAxis().evaluate(_committee_contaminated())

    clean_c = next(c for c in clean.criteria if c.name == CONTAMINATION_CLEAN)
    dirty_c = next(c for c in dirty.criteria if c.name == CONTAMINATION_CLEAN)
    assert clean_c.passed is True, "genuine clustered edge must read as clean"
    assert dirty_c.passed is False, "scattered memorized edge must read as contaminated"

    # Polarity convention (documented): clean ⟺ shuffle p_value <= alpha.
    assert clean_c.metrics["shuffle_p_value"] <= clean_c.metrics["shuffle_alpha"]
    assert dirty_c.metrics["shuffle_p_value"] > dirty_c.metrics["shuffle_alpha"]

    assert dirty.passed is False
    assert CONTAMINATION_CLEAN in dirty.failed_criteria


def test_contamination_fails_on_structural_lookahead():
    """observable_asof must be STRICTLY after asof for every episode. One episode
    whose outcome is observable at-or-before the decision time is a structural
    look-ahead and trips the contamination guard before any statistic runs."""
    eps = _committee_pass()
    bad = eps[10]
    eps[10] = Episode(
        asof=bad.asof,
        observable_asof=bad.asof,  # NOT strictly after -> structural leak
        regime=bad.regime,
        llm_action=bad.llm_action,
        fallback_action=bad.fallback_action,
        realized_forward_return=bad.realized_forward_return,
        knowledge_cutoff=bad.knowledge_cutoff,
    )
    verdict = RiskCommitteeAxis().evaluate(eps)
    assert verdict.passed is False
    assert CONTAMINATION_CLEAN in verdict.failed_criteria
    contam = next(c for c in verdict.criteria if c.name == CONTAMINATION_CLEAN)
    assert contam.metrics.get("structural_ok") is False


def test_contamination_fails_when_outcome_within_knowledge_cutoff():
    """If the realized outcome (observable_asof) lies at-or-before the LLM's
    knowledge cutoff, the model may have trained on it — contamination. The clean
    corpus sets knowledge_cutoff a year before the corpus; here one episode's
    cutoff is moved past its outcome."""
    eps = _committee_pass()
    bad = eps[20]
    eps[20] = Episode(
        asof=bad.asof,
        observable_asof=bad.observable_asof,
        regime=bad.regime,
        llm_action=bad.llm_action,
        fallback_action=bad.fallback_action,
        realized_forward_return=bad.realized_forward_return,
        knowledge_cutoff=bad.observable_asof + pd.Timedelta(hours=1),  # outcome <= cutoff
    )
    verdict = RiskCommitteeAxis().evaluate(eps)
    assert verdict.passed is False
    assert CONTAMINATION_CLEAN in verdict.failed_criteria
    contam = next(c for c in verdict.criteria if c.name == CONTAMINATION_CLEAN)
    assert contam.metrics.get("knowledge_cutoff_ok") is False


# ---------------------------------------------------------------------------
# (d) HARMLESS — beats but via excess risk → fail
# ---------------------------------------------------------------------------


def test_harmless_fails_when_edge_is_bought_with_excess_risk():
    """The trader corpus beats the fallback (EFFECT/BREADTH/CONTAMINATION pass)
    but the LLM's downside-deviation and drawdown dwarf the fallback's — the edge
    is NOT harmless. HARMLESS is the sole failure (real-and-harmless splits the
    'statistically real' axis from the 'not via excess risk' axis)."""
    verdict = TraderAxis().evaluate(_trader_excess_risk())
    assert verdict.passed is False
    assert HARMLESS in verdict.failed_criteria
    # The OTHER four still pass — the edge is real, broad, and clean; only risky.
    assert verdict.failed_criteria == [HARMLESS], (
        f"expected HARMLESS to be the sole failure, got {verdict.failed_criteria}"
    )
    harm = next(c for c in verdict.criteria if c.name == HARMLESS)
    # Either the downside-dev ratio or the max-drawdown comparison trips.
    assert (
        harm.metrics["downside_dev_ratio"] > 1.0 + harm.metrics["downside_dev_tol"]
        or harm.metrics["llm_max_drawdown"] < harm.metrics["fallback_max_drawdown"]
    )


# ---------------------------------------------------------------------------
# (e) Determinism — same corpus → byte-identical verdict (no wall-clock/RNG)
# ---------------------------------------------------------------------------


def test_same_corpus_yields_identical_verdict():
    axis = RiskCommitteeAxis()
    v1 = axis.evaluate(_committee_pass())
    v2 = axis.evaluate(_committee_pass())
    assert v1.passed == v2.passed
    assert v1.reasons == v2.reasons
    # Total, NaN-safe equality over the whole verdict (incl. all float metrics).
    assert repr(v1) == repr(v2)


def test_contaminated_verdict_is_deterministic_too():
    """Determinism must hold on the path that actually exercises the seeded
    shuffle (the contaminated corpus drives shuffle_timestamps_test)."""
    v1 = RiskCommitteeAxis().evaluate(_committee_contaminated())
    v2 = RiskCommitteeAxis().evaluate(_committee_contaminated())
    assert repr(v1) == repr(v2)


# ---------------------------------------------------------------------------
# Axis action-domain validation (the two axes differ here)
# ---------------------------------------------------------------------------


def test_risk_committee_axis_rejects_non_binary_actions():
    """Committee approval is ∈ {0,1}; a fractional action is a corpus-construction
    error and must fail fast (not silently score)."""
    eps = _committee_pass()
    eps.append(_ep(999, "trend", llm=0.5, fb=0, ret=0.01))  # 0.5 ∉ {0,1}
    with pytest.raises(ValueError, match="(?i)binary|committee|approval|0.*1"):
        RiskCommitteeAxis().evaluate(eps)


def test_trader_axis_rejects_out_of_range_actions():
    """Trader position is ∈ [-1,1]; |action| > 1 escapes the domain."""
    eps = _trader_pass()
    eps.append(_ep(999, "trend", llm=1.5, fb=0.3, ret=0.01))  # 1.5 ∉ [-1,1]
    with pytest.raises(ValueError, match="(?i)\\[-1|range|trader|position"):
        TraderAxis().evaluate(eps)


def test_trader_axis_accepts_binary_actions_committee_rejects_fractional():
    """The committee domain is strictly inside the trader domain: a {0,1} corpus
    is valid for BOTH axes; a fractional corpus is valid only for the trader."""
    binary = _committee_pass()
    # {0,1} ⊂ [-1,1] -> trader accepts it without raising.
    assert TraderAxis().evaluate(binary).axis == "trader"
    # Fractional trader corpus -> committee rejects.
    with pytest.raises(ValueError):
        RiskCommitteeAxis().evaluate(_trader_pass())


# ---------------------------------------------------------------------------
# RiskCommittee-only approval-precision read-out
# ---------------------------------------------------------------------------


def test_committee_verdict_reports_approval_precision():
    """The committee axis adds an approval-precision read-out: of the trades each
    side APPROVED, what fraction had a positive realized forward return. It is a
    read-out only — it does not change pass/fail — but it must be present and
    correctly favour the LLM on the pass corpus (the LLM approves only winners)."""
    verdict = RiskCommitteeAxis().evaluate(_committee_pass())
    assert "llm_approval_precision" in verdict.metrics
    assert "fallback_approval_precision" in verdict.metrics
    llm_p = verdict.metrics["llm_approval_precision"]
    fb_p = verdict.metrics["fallback_approval_precision"]
    assert 0.0 <= llm_p <= 1.0 and 0.0 <= fb_p <= 1.0
    # On the pass corpus the LLM only ever approves winners (trend, ret>0); the
    # fallback only ever approves losers (drawdown, ret<0).
    assert llm_p == pytest.approx(1.0)
    assert fb_p == pytest.approx(0.0)
    # The trader axis does NOT emit an approval-precision read-out.
    assert "llm_approval_precision" not in TraderAxis().evaluate(_trader_pass()).metrics


# ---------------------------------------------------------------------------
# Config knobs are honoured (fail-closed defaults overridable)
# ---------------------------------------------------------------------------


def test_dsr_floor_is_configurable_and_fail_closed():
    """An impossibly strict DSR floor (> 1) makes EFFECT_REAL unreachable -> the
    gate fails closed even on a genuine edge."""
    cfg = GateConfig(dsr_floor=1.0000001)
    verdict = RiskCommitteeAxis(cfg).evaluate(_committee_pass())
    assert verdict.passed is False
    assert EFFECT_REAL in verdict.failed_criteria


def test_insufficient_episodes_fails_closed_not_crash():
    """Too few episodes for DSR / walk-forward must produce a FAIL verdict (a
    human-readable refusal), never an exception — this is an offline harness."""
    tiny = _committee_pass()[:8]  # < 30 (DSR) and < n_splits*10 (walk-forward)
    verdict = RiskCommitteeAxis().evaluate(tiny)
    assert verdict.passed is False
    assert EFFECT_REAL in verdict.failed_criteria
    assert OOS_REPRODUCIBLE in verdict.failed_criteria
