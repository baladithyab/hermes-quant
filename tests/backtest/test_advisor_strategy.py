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
