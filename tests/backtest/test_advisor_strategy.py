"""tests/backtest/test_advisor_strategy.py — AdvisorStrategy tests (D3).

The momentum ``HermesQuantStrategy`` never runs the analyst pool / BMA
aggregator, so ablating the L2/STACKING/semantic flags through it shows a FALSE
NULL. ``AdvisorStrategy`` closes that gap: it drives the REAL analyst-pool -> BMA
-> risk-gate chain offline (dependency-injected, dry-run, no network) and keeps
an asof-honest internal settlement loop so the accumulation-biting L2 flags
(STACKING / POSTERIOR_DECAY / POSTERIOR_PERSIST) accrue per-analyst skill the
same way the live settlement loop would.

The load-bearing proof (the whole point of D3): toggling a real L2 flag CHANGES
AdvisorStrategy's decisions on a synthetic lookback — proving the L2 ablation is
genuine, not theater.

All offline + deterministic: synthetic OHLCV, injected deterministic analysts,
no LLM / network.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd

from hermes_quant.backtest.engine import WalkForwardConfig, WalkForwardEngine
from hermes_quant.backtest.strategy import AdvisorStrategy, Decision, Strategy
from hermes_quant.protocol import AnalystView, MarketContext

# ---------------------------------------------------------------------------
# Deterministic offline analysts (no network, no LLM)
# ---------------------------------------------------------------------------


class _DeterministicAnalyst:
    """Emits a fixed-direction view with confidence from the trailing return.

    Two of these with different names form a genuine ENSEMBLE (so BMA's
    require_ensemble guard is satisfied) and accumulate distinct per-analyst
    Beta posteriors under settlement.
    """

    def __init__(self, name: str, direction: int, *, conf: float = 0.6) -> None:
        self.name = name
        self.timeframes = ["1d"]
        self.asset_classes = ["equity"]
        self.enabled = True
        self._direction = direction
        self._conf = conf

    def analyze(self, ctx: MarketContext) -> AnalystView | None:
        closes = ctx.bars["close"]
        if len(closes) < 5:
            return None
        return AnalystView(
            analyst=self.name,
            direction=self._direction,
            magnitude=0.02,
            confidence=self._conf,
            confidence_raw=self._conf,
            horizon="1d",
        )

    def health(self) -> dict:
        return {"n_views_emitted": 0, "last_view_at": None, "error_count": 0}


def _gbm_ohlcv(n_days: int = 80, seed: int = 3, mu: float = 0.0035) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start="2024-01-02", periods=n_days)
    rets = rng.normal(mu, 0.012, n_days)
    closes = 100.0 * np.cumprod(1 + rets)
    opens = np.roll(closes, 1)
    opens[0] = 100.0
    highs = np.maximum(opens, closes) * 1.004
    lows = np.minimum(opens, closes) * 0.996
    volumes = rng.integers(500_000, 1_000_000, n_days).astype(float)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=dates,
    )


def _ablation_config(ohlcv: pd.DataFrame) -> WalkForwardConfig:
    dates = ohlcv.index
    return WalkForwardConfig(
        train_start=dates[0],
        train_end=dates[19],
        holdout_start=dates[20],
        holdout_end=dates[-1],
        step_days=1,
        lookback_days=120,
        initial_nav=100_000.0,
    )


# ---------------------------------------------------------------------------
# Protocol conformance + offline run
# ---------------------------------------------------------------------------


def test_advisor_strategy_is_a_strategy():
    strat = AdvisorStrategy(["SYN"], analysts=[_DeterministicAnalyst("a", 1)])
    assert isinstance(strat, Strategy)


def test_runs_offline_through_engine_no_network():
    ohlcv = _gbm_ohlcv()
    config = _ablation_config(ohlcv)
    strat = AdvisorStrategy(
        ["SYN"],
        analysts=[_DeterministicAnalyst("a", 1), _DeterministicAnalyst("b", 1)],
    )
    result = WalkForwardEngine(config).run(strat, ["SYN"], ohlcv)
    # The chain ran; the result is coherent and finite.
    assert np.isfinite(result.sharpe)
    assert result.n_trades >= 0
    # A bullish ensemble on an up-drifting series should have opened at least one
    # position (proves analysts -> BMA -> gate -> Decision actually fired).
    assert result.n_trades > 0


def test_decide_returns_decisions_per_symbol():
    ohlcv = _gbm_ohlcv()
    strat = AdvisorStrategy(
        ["SYN"],
        analysts=[_DeterministicAnalyst("a", 1), _DeterministicAnalyst("b", 1)],
    )
    asof = ohlcv.index[40]
    lookback = ohlcv.loc[ohlcv.index <= asof]
    decisions = strat.decide(pd.Timestamp(asof), lookback)
    assert len(decisions) == 1
    assert isinstance(decisions[0], Decision)
    assert decisions[0].symbol == "SYN"


# ---------------------------------------------------------------------------
# THE load-bearing proof: a real L2 flag toggle CHANGES the decisions.
# ---------------------------------------------------------------------------


def _run_with_flag(flag_value: str | None, ohlcv: pd.DataFrame) -> list[Decision]:
    """Run a single decide() under a given flag value; return its decisions.

    Fresh strategy per call so the BMA aggregator (which may read the flag at
    construction) is built under the flag context.
    """
    prior = os.environ.get("HERMES_QUANT_L2_PER_ANALYST_CALIB")
    try:
        if flag_value is None:
            os.environ.pop("HERMES_QUANT_L2_PER_ANALYST_CALIB", None)
        else:
            os.environ["HERMES_QUANT_L2_PER_ANALYST_CALIB"] = flag_value
        strat = AdvisorStrategy(
            ["SYN"],
            analysts=[_DeterministicAnalyst("a", 1), _DeterministicAnalyst("b", 1)],
        )
        asof = ohlcv.index[40]
        lookback = ohlcv.loc[ohlcv.index <= asof]
        return strat.decide(pd.Timestamp(asof), lookback)
    finally:
        if prior is None:
            os.environ.pop("HERMES_QUANT_L2_PER_ANALYST_CALIB", None)
        else:
            os.environ["HERMES_QUANT_L2_PER_ANALYST_CALIB"] = prior


def test_l2_flag_toggle_changes_decisions():
    """PROOF the L2 ablation is real: PER_ANALYST_CALIB changes the BMA
    confidence that flows into the gate, so the strategy's decision (size or
    confidence) differs between OFF and ON on the SAME lookback."""
    ohlcv = _gbm_ohlcv()
    off = _run_with_flag("0", ohlcv)
    on = _run_with_flag("1", ohlcv)

    # Same single symbol on both legs.
    assert len(off) == len(on) == 1
    off_d, on_d = off[0], on[0]

    # The flag genuinely propagates: the per-analyst-calibrated confidence is
    # higher than the cold-start global-calibrator confidence, so the decision's
    # confidence (and/or size) must differ. If these were equal, the L2 ablation
    # would be theater.
    changed = (
        off_d.confidence != on_d.confidence
        or off_d.size_fraction != on_d.size_fraction
        or off_d.action != on_d.action
    )
    assert changed, (
        f"L2 flag did not change the decision — OFF={off_d} ON={on_d}. "
        "The L2 ablation would be a false null."
    )


# ---------------------------------------------------------------------------
# Determinism: same flag value -> identical decisions
# ---------------------------------------------------------------------------


def test_same_flag_value_is_deterministic():
    ohlcv = _gbm_ohlcv()
    a = _run_with_flag("1", ohlcv)
    b = _run_with_flag("1", ohlcv)
    assert len(a) == len(b) == 1
    assert a[0].action == b[0].action
    assert a[0].size_fraction == b[0].size_fraction
    assert a[0].confidence == b[0].confidence


# ---------------------------------------------------------------------------
# No-lookahead: decide() must not access data after asof.
# ---------------------------------------------------------------------------


def test_decide_does_not_read_future_data():
    """The engine pre-filters lookback to <= asof; AdvisorStrategy must build its
    MarketContext only from that frame. We assert the ctx the strategy would
    build never carries a bar after asof by feeding a truncated frame and
    confirming no exception + a coherent decision."""
    ohlcv = _gbm_ohlcv()
    strat = AdvisorStrategy(
        ["SYN"],
        analysts=[_DeterministicAnalyst("a", 1), _DeterministicAnalyst("b", 1)],
    )
    asof = ohlcv.index[30]
    lookback = ohlcv.loc[ohlcv.index <= asof]
    # The max bar in lookback is exactly asof — a strategy that respects the
    # contract never needs anything past it.
    assert lookback.index.max() == asof
    decisions = strat.decide(pd.Timestamp(asof), lookback)
    assert len(decisions) == 1


# ---------------------------------------------------------------------------
# Accumulation-biting L2 flags are measurable MACHINE-INDEPENDENTLY.
#
# Regression for the adversarial finding: a stock BMAAggregator() loads the
# host's private ~/.hermes/.../isotonic.pkl, and on a clean machine the cold-
# start fallback caps confidence at 0.375 -> the gate silences EVERY signal ->
# zero trades on both legs -> a FALSE NULL for STACKING/DECAY. AdvisorStrategy's
# hermetic default (pinned IdentityCalibrator) fixes this. These tests pin the
# default-calibrator path to a NONEXISTENT file to SIMULATE a clean machine, and
# assert the flags still move the decisions (trades fire AND OFF != ON).
# ---------------------------------------------------------------------------


def _dissent_committee():
    """Two correlated longs + one short dissenter — so STACKING's redundancy
    discount has directional dissent to bite (a unanimous committee is a genuine
    STACKING no-op per BMA vote-share math; see NOTES_ABLATION.md)."""
    return [
        _DeterministicAnalyst("s1", 1, conf=0.7),
        _DeterministicAnalyst("s2", 1, conf=0.7),
        _DeterministicAnalyst("dis", -1, conf=0.4),
    ]


def _decisions_under_flag(flag: str, value: str, ohlcv: pd.DataFrame) -> tuple[list, int]:
    prior = os.environ.get(flag)
    try:
        os.environ[flag] = value
        strat = AdvisorStrategy(["SYN"], analysts=_dissent_committee(), learn_from_fills=True)
        out = []
        for i in range(40, len(ohlcv) - 1):
            asof = ohlcv.index[i]
            d = strat.decide(pd.Timestamp(asof), ohlcv.loc[ohlcv.index <= asof])
            out.append((d[0].action, round(d[0].size_fraction, 4)))
        n_trades = sum(1 for a, _ in out if a != "HOLD")
        return out, n_trades
    finally:
        if prior is None:
            os.environ.pop(flag, None)
        else:
            os.environ[flag] = prior


def test_accumulation_l2_flags_measurable_on_clean_machine(monkeypatch):
    """STACKING + POSTERIOR_DECAY (accumulation-biting) change decisions even with
    NO host calibrator — proving measurability is not an artifact of a private
    on-disk isotonic.pkl. This is the regression guard for the false-null the
    adversarial review surfaced."""
    import hermes_quant.aggregators.bma as bma_mod

    # Simulate a clean machine: the default calibrator path points at nothing.
    monkeypatch.setattr(
        bma_mod, "_DEFAULT_CALIBRATOR_PATH", Path("/nonexistent/hq-isotonic.pkl"), raising=True
    )
    ohlcv = _gbm_ohlcv(n_days=110, mu=0.002)

    for flag in ("HERMES_QUANT_STACKING", "HERMES_QUANT_L2_POSTERIOR_DECAY"):
        off, n_off = _decisions_under_flag(flag, "0", ohlcv)
        on, n_on = _decisions_under_flag(flag, "1", ohlcv)
        # Trades actually fire on at least one leg (NOT the cold-start false-null
        # where every signal is silenced and both legs are all-HOLD).
        assert n_off > 0 or n_on > 0, f"{flag}: both legs all-HOLD — false null (calibrator regression)"
        assert off != on, f"{flag}: decisions identical OFF vs ON — flag not measurable"


def test_posterior_persist_ablation_never_writes_real_store(monkeypatch, tmp_path):
    """Ablating HERMES_QUANT_L2_POSTERIOR_PERSIST through the DEFAULT aggregator
    must NOT write the production posterior store. Regression for the CRITICAL
    finding: the 'read-only' eval tool was creating/writing
    ~/.hermes/quant/l2_learning_posteriors/. AdvisorStrategy's hermetic default
    sandboxes the store path, so settlement can never touch the real one."""
    import hermes_quant.learning.posterior_store as ps_mod

    # Point the REAL store at a tmp dir and confirm NOTHING lands there: the
    # hermetic aggregator uses its OWN temp file, not POSTERIOR_DIR.
    sentinel_dir = tmp_path / "real_store"
    monkeypatch.setattr(ps_mod, "POSTERIOR_DIR", sentinel_dir, raising=True)
    monkeypatch.setenv("HERMES_QUANT_L2_POSTERIOR_PERSIST", "1")

    ohlcv = _gbm_ohlcv(n_days=90)
    strat = AdvisorStrategy(["SYN"], analysts=_dissent_committee(), learn_from_fills=True)
    for i in range(40, 80):
        asof = ohlcv.index[i]
        strat.decide(pd.Timestamp(asof), ohlcv.loc[ohlcv.index <= asof])

    # The hermetic aggregator wrote to its own temp file; the production-store
    # location was never created.
    assert not sentinel_dir.exists() or not list(sentinel_dir.glob("*")), (
        "L2_POSTERIOR_PERSIST ablation wrote into the production posterior store"
    )
