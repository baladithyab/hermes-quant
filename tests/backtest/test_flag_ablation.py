"""tests/backtest/test_flag_ablation.py — FlagAblation harness tests (D1).

The flag-ablation harness runs the SAME walk-forward window with a feature flag
OFF vs ON and reports the Sharpe / Sortino / maxDD / DSR delta + a promote/hold
verdict. These unit tests are per-PR (no network, synthetic OHLCV, deterministic).

Load-bearing invariants proved here (money-software):
  * Both legs run and AblationResult carries off/on WalkForwardResult + deltas.
  * DETERMINISM: off-vs-off (on_value == off_value) is BIT-IDENTICAL.
  * NO ENV LEAKAGE: os.environ is restored after the call — for BOTH a
    pre-existing flag value AND an unset flag.
  * verdict() returns PROMOTE on a clear-improvement pair and HOLD on a
    null/marginal pair (conservative — gates real capital).
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd
import pytest

from hermes_quant.backtest.ablation import (
    AblationResult,
    run_flag_ablation,
    verdict,
)
from hermes_quant.backtest.engine import WalkForwardConfig, WalkForwardResult
from hermes_quant.backtest.strategy import Decision

_SELFTEST_FLAG = "HERMES_QUANT_ABLATION_SELFTEST"


# ---------------------------------------------------------------------------
# Synthetic OHLCV (no external data dependency)
# ---------------------------------------------------------------------------


def _gbm_ohlcv(n_days: int = 80, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start="2024-01-02", periods=n_days)
    rets = rng.normal(0.0004, 0.014, n_days)
    closes = 100.0 * np.cumprod(1 + rets)
    opens = np.roll(closes, 1)
    opens[0] = 100.0
    noise = rng.uniform(0.995, 1.005, n_days)
    highs = np.maximum(opens, closes) * noise
    lows = np.minimum(opens, closes) / noise
    volumes = rng.integers(100_000, 1_000_000, n_days).astype(float)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=dates,
    )


class _FlagSensitiveStrategy:
    """Test double: BUYs when the flag is "1" at construction time, else HOLDs.

    Reads the flag in __init__ (mirroring real strategies whose aggregator reads
    flags at construction, e.g. BMA posterior-persist) so the harness MUST build
    the strategy INSIDE the flag context for the flag to take effect.
    """

    def __init__(self, universe: list[str], flag: str) -> None:
        self._universe = universe
        self._on = os.environ.get(flag) == "1"

    def decide(self, asof: pd.Timestamp, lookback: pd.DataFrame) -> list[Decision]:
        if self._on:
            return [
                Decision(symbol=s, action="BUY", size_fraction=0.10, confidence=0.6, rationale="on")
                for s in self._universe
            ]
        return [
            Decision(symbol=s, action="HOLD", size_fraction=0.0, confidence=0.0, rationale="off")
            for s in self._universe
        ]


@pytest.fixture
def ohlcv():
    return _gbm_ohlcv()


@pytest.fixture
def config(ohlcv):
    dates = ohlcv.index
    return WalkForwardConfig(
        train_start=dates[0],
        train_end=dates[29],
        holdout_start=dates[30],
        holdout_end=dates[79],
        step_days=1,
        lookback_days=80,
        initial_nav=100_000.0,
    )


# ---------------------------------------------------------------------------
# Both legs + deltas
# ---------------------------------------------------------------------------


def test_returns_result_with_both_legs_and_deltas(ohlcv, config, monkeypatch):
    monkeypatch.delenv(_SELFTEST_FLAG, raising=False)
    universe = ["SYN"]
    result = run_flag_ablation(
        _SELFTEST_FLAG,
        strategy_factory=lambda: _FlagSensitiveStrategy(universe, _SELFTEST_FLAG),
        universe=universe,
        ohlcv=ohlcv,
        config=config,
    )
    assert isinstance(result, AblationResult)
    assert isinstance(result.off, WalkForwardResult)
    assert isinstance(result.on, WalkForwardResult)
    assert result.flag == _SELFTEST_FLAG

    # Deltas are exactly ON - OFF.
    assert result.d_sharpe == pytest.approx(result.on.sharpe - result.off.sharpe)
    assert result.d_sortino == pytest.approx(result.on.sortino - result.off.sortino)
    assert result.d_maxdd == pytest.approx(result.on.max_drawdown - result.off.max_drawdown)
    assert result.d_total_return == pytest.approx(
        result.on.total_return - result.off.total_return
    )
    assert result.d_alpha == pytest.approx(
        result.on.alpha_vs_benchmark - result.off.alpha_vs_benchmark
    )
    assert result.d_n_trades == result.on.n_trades - result.off.n_trades

    # The flag genuinely propagated: ON trades, OFF holds.
    assert result.off.n_trades == 0
    assert result.on.n_trades > 0
    assert result.verdict in {"PROMOTE", "HOLD"}


# ---------------------------------------------------------------------------
# Determinism: off-vs-off is bit-identical
# ---------------------------------------------------------------------------


def test_off_vs_off_is_bit_identical(ohlcv, config, monkeypatch):
    monkeypatch.delenv(_SELFTEST_FLAG, raising=False)
    universe = ["SYN"]
    result = run_flag_ablation(
        _SELFTEST_FLAG,
        on_value="0",
        off_value="0",
        strategy_factory=lambda: _FlagSensitiveStrategy(universe, _SELFTEST_FLAG),
        universe=universe,
        ohlcv=ohlcv,
        config=config,
    )
    # Same flag value both legs -> the ONLY difference vanishes -> identical.
    assert result.on.sharpe == result.off.sharpe
    assert result.on.sortino == result.off.sortino
    assert result.on.max_drawdown == result.off.max_drawdown
    assert result.on.total_return == result.off.total_return
    assert result.on.n_trades == result.off.n_trades
    assert result.on.nav_series == result.off.nav_series
    assert result.d_sharpe == 0.0
    assert result.d_maxdd == 0.0


# ---------------------------------------------------------------------------
# No env leakage — pre-existing value restored
# ---------------------------------------------------------------------------


def test_env_restored_when_flag_was_preset(ohlcv, config, monkeypatch):
    monkeypatch.setenv(_SELFTEST_FLAG, "preset-sentinel")
    universe = ["SYN"]
    run_flag_ablation(
        _SELFTEST_FLAG,
        strategy_factory=lambda: _FlagSensitiveStrategy(universe, _SELFTEST_FLAG),
        universe=universe,
        ohlcv=ohlcv,
        config=config,
    )
    assert os.environ.get(_SELFTEST_FLAG) == "preset-sentinel"


def test_env_restored_when_flag_was_unset(ohlcv, config, monkeypatch):
    monkeypatch.delenv(_SELFTEST_FLAG, raising=False)
    assert _SELFTEST_FLAG not in os.environ
    universe = ["SYN"]
    run_flag_ablation(
        _SELFTEST_FLAG,
        strategy_factory=lambda: _FlagSensitiveStrategy(universe, _SELFTEST_FLAG),
        universe=universe,
        ohlcv=ohlcv,
        config=config,
    )
    assert _SELFTEST_FLAG not in os.environ


# ---------------------------------------------------------------------------
# verdict() policy
# ---------------------------------------------------------------------------


def _wfr(sharpe: float, max_drawdown: float, nav_len: int = 41) -> WalkForwardResult:
    """Minimal WalkForwardResult for verdict() unit tests.

    nav_len controls the DSR observation count (n_obs = nav_len - 1); >= 31 so
    DSR is computable.
    """
    return WalkForwardResult(
        total_return=0.1,
        sharpe=sharpe,
        sortino=sharpe * 1.2,
        max_drawdown=max_drawdown,
        win_rate=0.55,
        n_trades=20,
        gross_pnl=1000.0,
        cost_pnl=-50.0,
        benchmark_return=0.05,
        alpha_vs_benchmark=0.05,
        nav_series=list(np.linspace(100_000.0, 110_000.0, nav_len)),
    )


def _build(off: WalkForwardResult, on: WalkForwardResult) -> AblationResult:
    from hermes_quant.backtest.ablation import _assemble_result

    return _assemble_result(
        flag="HERMES_QUANT_X", on_value="1", off_value="0", off=off, on=on
    )


def test_verdict_promote_on_clear_improvement():
    off = _wfr(sharpe=0.20, max_drawdown=-0.10)
    on = _wfr(sharpe=1.20, max_drawdown=-0.09)  # +1.0 Sharpe, maxDD not worse
    res = _build(off, on)
    assert verdict(res) == "PROMOTE"
    assert res.verdict == "PROMOTE"


def test_verdict_hold_on_marginal_sharpe():
    off = _wfr(sharpe=1.00, max_drawdown=-0.10)
    on = _wfr(sharpe=1.05, max_drawdown=-0.10)  # +0.05 < +0.10 threshold
    res = _build(off, on)
    assert verdict(res) == "HOLD"
    assert "sharpe" in res.verdict_reason.lower()


def test_verdict_hold_on_null():
    off = _wfr(sharpe=0.80, max_drawdown=-0.10)
    on = _wfr(sharpe=0.80, max_drawdown=-0.10)  # identical -> null
    res = _build(off, on)
    assert verdict(res) == "HOLD"


def test_verdict_hold_when_drawdown_materially_worse():
    off = _wfr(sharpe=0.20, max_drawdown=-0.05)
    on = _wfr(sharpe=1.50, max_drawdown=-0.30)  # big Sharpe gain but maxDD 25pp worse
    res = _build(off, on)
    assert verdict(res) == "HOLD"
    assert "drawdown" in res.verdict_reason.lower()


def test_verdict_hold_when_dsr_uncomputable():
    # Too few observations for DSR -> cannot confirm significance -> HOLD.
    off = _wfr(sharpe=0.20, max_drawdown=-0.10, nav_len=10)
    on = _wfr(sharpe=1.50, max_drawdown=-0.09, nav_len=10)
    res = _build(off, on)
    assert verdict(res) == "HOLD"
    assert res.dsr_on is None


def test_verdict_hold_on_nan_sharpe():
    """Regression: a NaN d_sharpe must force HOLD, never a spurious PROMOTE. A
    `<` comparison against NaN is always False, which would otherwise bypass the
    Sharpe gate; verdict() guards non-finite metrics explicitly (fail-closed)."""
    off = _wfr(sharpe=0.20, max_drawdown=-0.10)
    on = _wfr(sharpe=float("nan"), max_drawdown=-0.09)  # degenerate ON leg
    res = _build(off, on)
    assert verdict(res) == "HOLD"
    assert "non-finite" in res.verdict_reason.lower()


def test_verdict_hold_on_inf_drawdown():
    """A non-finite drawdown (e.g. -inf) also fails closed to HOLD."""
    off = _wfr(sharpe=0.20, max_drawdown=-0.10)
    on = _wfr(sharpe=1.50, max_drawdown=float("-inf"))
    res = _build(off, on)
    assert verdict(res) == "HOLD"


def test_verdict_hold_on_nonfinite_dsr():
    """Regression (codex review, claim 1): a NaN/inf ON deflated-Sharpe must force
    HOLD. The pre-fix finite-guard only checked d_sharpe + the two drawdowns, NOT
    dsr_on; since `NaN <= 0.50` is False AND `dsr_on is None` is False, a NaN DSR
    would fall through to PROMOTE despite a healthy Sharpe delta + drawdown. Build
    the result directly so we can pin dsr_on independent of the NAV series.
    """
    off = _wfr(sharpe=0.20, max_drawdown=-0.10)
    on = _wfr(sharpe=1.50, max_drawdown=-0.09)  # +1.30 Sharpe, drawdown fine
    for bad in (float("nan"), float("inf")):
        res = AblationResult(
            flag="HERMES_QUANT_X",
            off_value="0",
            on_value="1",
            off=off,
            on=on,
            d_sharpe=on.sharpe - off.sharpe,  # +1.30 — passes the Sharpe gate
            d_maxdd=on.max_drawdown - off.max_drawdown,
            dsr_off=0.60,
            dsr_on=bad,  # the degenerate axis
        )
        assert verdict(res) == "HOLD", bad
        assert "deflated-sharpe" in res.verdict_reason.lower()
        assert "finite" in res.verdict_reason.lower()
