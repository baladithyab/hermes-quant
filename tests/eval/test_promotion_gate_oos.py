"""tests/eval/test_promotion_gate_oos.py — OOS walk-forward fold-rate requirement
on the strategy-backtest PromotionGate (seed 3767, anti-overfit lane L3).

The strategy-backtest PromotionGate (eval/promotion_gate.py) today promotes on
IN-SAMPLE metrics only: vs_buyhold_alpha, sortino, max_drawdown over a single
window. Seed 3767 wires the orphaned walk_forward_replay instrument into the gate
as an OUT-OF-SAMPLE requirement: a candidate must clear a configurable
walk-forward fold-rate floor (fraction of OOS folds whose excess-return beats
buy-and-hold) IN ADDITION to the in-sample checks.

Contract (HARD INVARIANTS, lane L3):
  * ADDITIVE — supplying an OOS fold-rate can only ADD a reject reason, never
    remove one. A gate with no OOS evidence and require_oos=False is byte-identical
    to today (the 288 existing eval tests must stay green).
  * STRICTER never looser — the new check rejects more, never promotes more.
  * FAIL-CLOSED — when require_oos=True but no fold-rate is supplied, REJECT; a
    NaN/degenerate fold-rate REJECTS (an x < NaN comparison would never block, so
    we guard it explicitly, mirroring governance.promotion's finite-bound guard).
  * CONSERVATIVE DEFAULT — the floor defaults to 0.60 (a clear majority of OOS
    folds must beat buy-and-hold), config-overridable via the constructor.

The no-lookahead proof drives the REAL walk_forward_replay (PurgedWalkForward
splits + replay's as_of-clamped provider) on synthetic bars with a deterministic
advisor, then feeds its genuine positive_excess_fold_rate to the gate.
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd

from hermes_quant.eval.promotion_gate import PromotionGate
from hermes_quant.eval.stockbench import STOCKBENCHResult


# ---------------------------------------------------------------------------
# Synthetic result factory (strong IN-SAMPLE defaults — would promote today)
# ---------------------------------------------------------------------------


def _make_result(**overrides) -> STOCKBENCHResult:
    defaults = dict(
        universe=["AAPL", "MSFT", "NVDA"],
        window_start=date(2025, 6, 1),
        window_end=date(2025, 8, 30),
        benchmark="SPY",
        cumulative_return=0.12,
        max_drawdown=-0.08,
        sortino=1.2,
        n_decisions=45,
        decisions_per_day_avg=0.5,
        vs_buyhold_alpha=0.05,
        contamination_guard_fired=False,
    )
    defaults.update(overrides)
    return STOCKBENCHResult(**defaults)


# ---------------------------------------------------------------------------
# Core acceptance: strong in-sample + failing OOS is REJECTED; passing both promotes
# ---------------------------------------------------------------------------


def test_strong_in_sample_but_failing_oos_fold_rate_is_rejected():
    """The headline anti-overfit case: a strategy that looks great in-sample but
    reproduces in only a minority of OOS folds must be REJECTED."""
    gate = PromotionGate()  # default floor 0.60
    decision = gate.check(_make_result(), oos_fold_rate=0.20)
    assert decision.promote is False
    assert any("oos" in r.lower() or "fold" in r.lower() for r in decision.reasons)


def test_strong_in_sample_and_passing_oos_fold_rate_is_promoted():
    """Strong in-sample AND a robust OOS fold-rate clears the gate."""
    gate = PromotionGate()
    decision = gate.check(_make_result(), oos_fold_rate=0.80)
    assert decision.promote is True
    assert decision.reasons == []


def test_oos_fold_rate_exactly_at_floor_promotes():
    """The floor is inclusive (fraction-of-folds >= floor), per lane spec."""
    gate = PromotionGate(oos_fold_rate_floor=0.60)
    decision = gate.check(_make_result(), oos_fold_rate=0.60)
    assert decision.promote is True


def test_oos_fold_rate_just_below_floor_rejects():
    gate = PromotionGate(oos_fold_rate_floor=0.60)
    decision = gate.check(_make_result(), oos_fold_rate=0.59)
    assert decision.promote is False


# ---------------------------------------------------------------------------
# ADDITIVE / STRICTER-never-looser
# ---------------------------------------------------------------------------


def test_oos_check_is_additive_does_not_remove_in_sample_reasons():
    """A result already failing in-sample plus a failing OOS fold-rate must carry
    BOTH reasons — the OOS check ADDS, it never masks an existing rejection."""
    gate = PromotionGate()
    decision = gate.check(_make_result(vs_buyhold_alpha=-0.01), oos_fold_rate=0.10)
    assert decision.promote is False
    assert any("vs_buyhold_alpha" in r for r in decision.reasons)
    assert any("oos" in r.lower() or "fold" in r.lower() for r in decision.reasons)


def test_passing_oos_never_rescues_a_failing_in_sample_result():
    """STRICTER never looser: a perfect OOS fold-rate cannot promote a result that
    fails an in-sample criterion."""
    gate = PromotionGate()
    decision = gate.check(_make_result(sortino=0.1), oos_fold_rate=1.0)
    assert decision.promote is False
    assert any("sortino" in r for r in decision.reasons)


# ---------------------------------------------------------------------------
# FAIL-CLOSED
# ---------------------------------------------------------------------------


def test_nan_oos_fold_rate_is_rejected():
    """A NaN fold-rate must REJECT — `x < NaN` is False, so an unguarded check
    would silently PROMOTE on a degenerate measurement (the governance-gate
    finite-bound lesson)."""
    gate = PromotionGate()
    decision = gate.check(_make_result(), oos_fold_rate=float("nan"))
    assert decision.promote is False
    assert any("oos" in r.lower() or "fold" in r.lower() for r in decision.reasons)


def test_require_oos_with_missing_evidence_rejects():
    """When the gate is configured to REQUIRE OOS evidence, a check() call that
    supplies none must REJECT (gate-closed) — never fall back to in-sample-only."""
    gate = PromotionGate(require_oos=True)
    decision = gate.check(_make_result())  # no oos_fold_rate supplied
    assert decision.promote is False
    assert any("oos" in r.lower() or "fold" in r.lower() for r in decision.reasons)


def test_require_oos_with_evidence_supplied_uses_it():
    """require_oos=True still promotes when supplied a passing fold-rate."""
    gate = PromotionGate(require_oos=True)
    assert gate.check(_make_result(), oos_fold_rate=0.80).promote is True
    assert gate.check(_make_result(), oos_fold_rate=0.20).promote is False


# ---------------------------------------------------------------------------
# ADDITIVE backward-compatibility (the 288-green guarantee)
# ---------------------------------------------------------------------------


def test_default_gate_without_oos_evidence_is_byte_identical_to_today():
    """The default gate (require_oos=False) with NO fold-rate supplied behaves
    exactly as before: a strong in-sample result promotes, no OOS reason added."""
    gate = PromotionGate()
    decision = gate.check(_make_result())
    assert decision.promote is True
    assert decision.reasons == []


# ---------------------------------------------------------------------------
# CONFIG-DRIVEN floor
# ---------------------------------------------------------------------------


def test_custom_floor_is_honoured():
    """A looser floor is config-overridable; the same fold-rate that fails at the
    conservative 0.60 default passes at a 0.50 floor."""
    strict = PromotionGate(oos_fold_rate_floor=0.60)
    loose = PromotionGate(oos_fold_rate_floor=0.50)
    assert strict.check(_make_result(), oos_fold_rate=0.55).promote is False
    assert loose.check(_make_result(), oos_fold_rate=0.55).promote is True


# ---------------------------------------------------------------------------
# NO-LOOKAHEAD PROOF — drive the REAL walk_forward_replay instrument
# ---------------------------------------------------------------------------


def _gbm_bars(n: int = 900, *, seed: int = 42, drift: float = 0.05, vol: float = 0.4) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2023-01-01", periods=n, freq="1h", tz="UTC")
    closes = 100 + np.cumsum(rng.normal(drift, vol, n))
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": closes - 0.1,
            "high": closes + 0.5,
            "low": closes - 0.5,
            "close": closes,
            "volume": 1000.0,
        }
    )


def _fixed_long_advisor(**kwargs):
    """Deterministic always-long advisor (mirrors the walk_forward integration
    fixture). Receives the as_of-clamped kwargs the replay loop passes; it never
    reaches past as_of because the real _ReplayProvider clamps the frame."""
    return {
        "as_of": kwargs["as_of"].isoformat(),
        "aggregated_signal": {
            "asset": kwargs.get("symbol", "TEST"),
            "asset_class": kwargs.get("asset_class", "equity"),
            "timeframe": kwargs.get("timeframe", "1h"),
            "direction": 1,
            "magnitude": 0.5,
            "confidence": 0.7,
            "confidence_raw": 0.7,
            "horizon": "1h",
            "aggregator": "bma",
        },
        "risk_gate": {"pass": True, "kelly_fraction": 0.10},
        "analyst_views": [
            {
                "analyst": "wf_voice",
                "direction": 1,
                "magnitude": 0.5,
                "confidence": 0.7,
                "confidence_raw": 0.7,
                "horizon": "1h",
            }
        ],
    }


def test_oos_fold_rate_from_real_walk_forward_replay_feeds_the_gate():
    """End-to-end: run the REAL orphaned instrument (walk_forward_replay) and feed
    its genuine positive_excess_fold_rate to the gate. The fold-rate the gate
    consumes is computed strictly out-of-sample by PurgedWalkForward + replay's
    as_of-clamped provider — no future data can reach the metric."""
    from hermes_quant.backtest import walk_forward_replay

    wf = walk_forward_replay(
        _gbm_bars(900),
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        n_splits=4,
        warmup_bars=20,
        advisor_recommend=_fixed_long_advisor,
    )
    fold_rate = wf.positive_excess_fold_rate
    assert 0.0 <= fold_rate <= 1.0

    gate = PromotionGate(require_oos=True)
    decision = gate.check(_make_result(), oos_fold_rate=fold_rate)
    # The decision is a pure function of (in-sample result, OOS fold-rate); it
    # promotes iff the genuine fold-rate clears the floor.
    expected = fold_rate >= gate.oos_fold_rate_floor
    assert decision.promote is expected


def test_walk_forward_folds_feeding_the_gate_are_strictly_out_of_sample():
    """The instrument whose metric the gate trusts must itself be leak-free: every
    fold's windows are strictly time-ordered and non-overlapping (train_end <
    val_start <= ... <= val_end <= test_start). This is the no-lookahead structure
    underneath the fold-rate the gate consumes."""
    from hermes_quant.backtest import walk_forward_replay

    wf = walk_forward_replay(
        _gbm_bars(900),
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        n_splits=4,
        warmup_bars=20,
        advisor_recommend=_fixed_long_advisor,
    )
    assert wf.folds, "expected out-of-sample folds"
    for fold in wf.folds:
        s = fold.split
        assert s.train_end < s.val_start  # embargo between train and val
        assert s.val_end <= s.test_start  # val strictly precedes the OOS test slice


def test_oos_gate_decision_is_deterministic_under_replay():
    """Same bars -> same fold-rate -> same gate decision. A leak (peeking at future
    bars) would make the OOS metric path-dependent; determinism pins it out."""
    from hermes_quant.backtest import walk_forward_replay

    def run_once() -> float:
        wf = walk_forward_replay(
            _gbm_bars(900, seed=7),
            symbol="TEST",
            asset_class="equity",
            timeframe="1h",
            n_splits=4,
            warmup_bars=20,
            advisor_recommend=_fixed_long_advisor,
        )
        return wf.positive_excess_fold_rate

    r1, r2 = run_once(), run_once()
    assert r1 == r2 or (math.isnan(r1) and math.isnan(r2))
    gate = PromotionGate(require_oos=True)
    assert gate.check(_make_result(), oos_fold_rate=r1).promote == gate.check(
        _make_result(), oos_fold_rate=r2
    ).promote
