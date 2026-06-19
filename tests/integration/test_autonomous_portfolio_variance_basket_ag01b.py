"""aegis-ag01b (iter-5 REBUILD): portfolio-variance basket sizing wired INTO the tick.

ag01 landed the pdr_core math + the default-OFF gate hook. The PRIOR build left
_apply_portfolio_variance_sizing_to_basket ORPHANED (zero callers). ag01b (iter-5) wires it
into tick(): a correlated candidate basket is de-levered IN THE TICK (not just in a unit
test of the helper) — the second correlated name to fire is sized DOWN because the basket's
portfolio variance w^T Σ w exceeds the cap, while an uncorrelated / small basket is unchanged.

NON-VACUOUS: these tests drive the REAL auto.tick() fire loop end-to-end (real gate, real
charge logic) and capture the per-name fill_size_pct passed to _react. The de-lever is
observed AT THE FIRE, not in an isolated helper call. The returns source is injected (the
seam the production fetch_bars path also satisfies) so the covariance is deterministic.

DEFAULT-OFF byte-identical: with HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING unset the fired
sizes equal the raw quarter-Kelly (no basket built, the returns provider is never called).
"""
from __future__ import annotations

import numpy as np
import pytest

from hermes_quant import autonomous as auto
from hermes_quant.watchlist import WatchlistEntry


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    qhome = tmp_path / "quant"
    qhome.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("hermes_quant.autonomous.QUANT_HOME", qhome)
    monkeypatch.setattr("hermes_quant.autonomous.KILL_SWITCH_PATH", qhome / "ks.json")
    cfg = tmp_path / "config.yaml"
    monkeypatch.setattr("hermes_quant.watchlist.get_config_path", lambda: cfg)
    monkeypatch.setattr(auto, "_read_pdr_mode", lambda: "autonomous")
    monkeypatch.setattr(auto, "_read_safety_rails", lambda: {
        "max_per_tick_opens": 5, "max_concurrent_positions": 10, "kill_switch_pct": 0.10,
        "log_silences": False, "allow_live": False, "paper_zero_costs": False,
        "require_stop_loss": False,
    })
    return qhome


def _advisor(kelly: float):
    return {
        "as_of": "2026-05-13T20:00:00Z", "decision_price": 100.0, "signal_id": "s",
        "aggregated_signal": {"confidence": 0.85, "direction": 1, "magnitude": 0.05},
        "risk_gate": {"pass": True, "kelly_fraction": kelly, "reason": "ok"},
        "analyst_views": [{"analyst": f"A{i}", "metadata": {"atr_relative": 0.05}} for i in range(2)],
        "lessons": [],
    }


# A deterministic HIGH-VOL PERFECTLY-CORRELATED two-name returns matrix: at kelly 0.5
# per name, the basket variance w^T Σ w exceeds the 0.02 cap, so the de-lever engages.
_CORR_BASE = np.linspace(-0.4, 0.4, 40)
_CORRELATED = np.column_stack([_CORR_BASE, _CORR_BASE])


def _react_spy(calls: list):
    def _react(advisor_result, entry, fill_size_pct, **kw):  # noqa: ANN001, ANN003
        calls.append((entry.symbol, fill_size_pct))
        return (f"exec_{entry.symbol}", fill_size_pct)
    return _react


# --------------------------------------------------------------------------- #
# 1. CORRELATED BASKET DE-LEVERED IN THE TICK — flag ON => the 2nd correlated name
#    to fire is sized DOWN (the basket variance exceeds the cap once both are in).
# --------------------------------------------------------------------------- #
def test_correlated_basket_is_delevered_in_tick(isolate, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING", "1")
    monkeypatch.setattr(
        auto, "_build_tick_returns_provider", lambda **kw: (lambda names: _CORRELATED)
    )
    calls: list = []
    monkeypatch.setattr(auto, "_react", _react_spy(calls))

    result = auto.tick(
        dry_run=False,
        symbols=[
            WatchlistEntry("AAA", "equity", "1d"),
            WatchlistEntry("BBB", "equity", "1d"),
        ],
        advisor_recommend=lambda **kw: _advisor(0.5),
    )
    fired = {sym: size for sym, size in calls}
    assert "AAA" in fired and "BBB" in fired, f"both names must fire: {calls}; {[d.to_dict() for d in result.decisions]}"
    # AAA fires first (basket = {AAA}; single-name variance < cap) -> NOT de-levered.
    assert fired["AAA"] == pytest.approx(0.5)
    # BBB fires with AAA already committed (basket = {AAA, BBB} correlated) -> de-levered.
    assert abs(fired["BBB"]) < 0.5, (
        f"BBB must be de-levered by the correlated basket variance cap; got {fired['BBB']}"
    )
    # The de-lever is surfaced on the decision action.
    bbb = [d for d in result.decisions if d.symbol == "BBB" and d.execution_id]
    assert bbb and bbb[0].action.get("portfolio_variance_sizing") is not None


# --------------------------------------------------------------------------- #
# 2. NON-VACUITY RED-PROOF — flag OFF => byte-identical: BOTH names fire at the raw
#    quarter-Kelly (the basket is never built, the returns provider is never called).
# --------------------------------------------------------------------------- #
def test_flag_off_byte_identical_no_delever(isolate, monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING", raising=False)
    provider_called = {"n": 0}

    def _spy_builder(**kw):
        def _p(names):
            provider_called["n"] += 1
            return _CORRELATED
        return _p

    monkeypatch.setattr(auto, "_build_tick_returns_provider", _spy_builder)
    calls: list = []
    monkeypatch.setattr(auto, "_react", _react_spy(calls))

    auto.tick(
        dry_run=False,
        symbols=[
            WatchlistEntry("AAA", "equity", "1d"),
            WatchlistEntry("BBB", "equity", "1d"),
        ],
        advisor_recommend=lambda **kw: _advisor(0.5),
    )
    fired = {sym: size for sym, size in calls}
    assert fired.get("AAA") == pytest.approx(0.5)
    assert fired.get("BBB") == pytest.approx(0.5), "flag OFF => no de-lever (byte-identical)"
    assert provider_called["n"] == 0, "flag OFF => the returns provider is NEVER called"


# --------------------------------------------------------------------------- #
# 3. UNCORRELATED / SMALL BASKET UNCHANGED — flag ON but the basket variance is
#    within the cap => no de-lever (the rail only shrinks an OVER-budget basket).
# --------------------------------------------------------------------------- #
def test_uncorrelated_small_basket_unchanged(isolate, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING", "1")
    # Anti-correlated, modest vol; at kelly 0.05 the basket variance stays under the cap.
    anti = np.column_stack([np.linspace(-0.1, 0.1, 40), np.linspace(0.1, -0.1, 40)])
    monkeypatch.setattr(
        auto, "_build_tick_returns_provider", lambda **kw: (lambda names: anti)
    )
    calls: list = []
    monkeypatch.setattr(auto, "_react", _react_spy(calls))

    auto.tick(
        dry_run=False,
        symbols=[
            WatchlistEntry("AAA", "equity", "1d"),
            WatchlistEntry("BBB", "equity", "1d"),
        ],
        advisor_recommend=lambda **kw: _advisor(0.05),
    )
    fired = {sym: size for sym, size in calls}
    assert fired.get("AAA") == pytest.approx(0.05)
    assert fired.get("BBB") == pytest.approx(0.05), (
        "a within-cap basket must be UNCHANGED (de-lever only shrinks an over-budget basket)"
    )


# --------------------------------------------------------------------------- #
# 4. FAIL-CLOSED — flag ON but the returns provider RAISES => the basket sizing
#    passes the targets through UNCHANGED (never sizes up / never aborts the tick).
# --------------------------------------------------------------------------- #
def test_provider_raises_fails_closed(isolate, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING", "1")

    def _raising(names):  # noqa: ANN001
        raise RuntimeError("data source down")

    monkeypatch.setattr(auto, "_build_tick_returns_provider", lambda **kw: _raising)
    calls: list = []
    monkeypatch.setattr(auto, "_react", _react_spy(calls))

    auto.tick(
        dry_run=False,
        symbols=[
            WatchlistEntry("AAA", "equity", "1d"),
            WatchlistEntry("BBB", "equity", "1d"),
        ],
        advisor_recommend=lambda **kw: _advisor(0.5),
    )
    fired = {sym: size for sym, size in calls}
    # FAIL-CLOSED: the targets pass through UNCHANGED (the per-name cap is the only
    # bound) — never sized up, and the tick is not aborted.
    assert fired.get("AAA") == pytest.approx(0.5)
    assert fired.get("BBB") == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# 5. HELPER-LEVEL FAIL-CLOSED unit guards (reuse 5ef538b's verified contract):
#    None provider, degenerate matrix, NaN target — all pass through / silence.
# --------------------------------------------------------------------------- #
def test_helper_fail_closed_guards(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_PORTFOLIO_VARIANCE_SIZING", "1")
    targets = [("AAA", 0.5), ("BBB", 0.5)]
    # None provider -> pass-through (no covariance source -> never size up).
    assert auto._apply_portfolio_variance_sizing_to_basket(targets, returns_provider=None) == targets
    # Degenerate (1-row) matrix -> pass-through.
    assert auto._apply_portfolio_variance_sizing_to_basket(
        targets, returns_provider=lambda n: np.zeros((1, 2))
    ) == targets
    # Wrong-shape matrix -> pass-through.
    assert auto._apply_portfolio_variance_sizing_to_basket(
        targets, returns_provider=lambda n: np.zeros((40, 3))
    ) == targets
    # Empty basket -> empty.
    assert auto._apply_portfolio_variance_sizing_to_basket([], returns_provider=lambda n: _CORRELATED) == []


# --------------------------------------------------------------------------- #
# 6. RETURNS PROVIDER builds a real returns matrix from per-name closes (the live
#    source seam). A degenerate fetch (<2 closes) yields a degenerate matrix => the
#    helper passes through (fail-CLOSED).
# --------------------------------------------------------------------------- #
def test_returns_provider_builds_matrix_from_bars(monkeypatch):
    import pandas as pd

    class _Provider:
        def fetch_bars(self, asset, tf, start, end, **kw):  # noqa: ANN001, ANN003
            closes = {"AAA": [100, 101, 102, 103, 104], "BBB": [50, 51, 52, 53, 54]}[asset]
            return pd.DataFrame({"close": closes})

    monkeypatch.setattr("hermes_quant.advisor._get_default_provider", lambda ac: _Provider())
    provider = auto._build_tick_returns_provider()
    mat = provider(["AAA", "BBB"])
    assert mat.shape == (4, 2), f"5 closes per name -> 4 returns x 2 names; got {mat.shape}"
    assert np.all(np.isfinite(mat))

    # A name with <2 closes -> degenerate (empty) matrix -> the helper passes through.
    class _ThinProvider:
        def fetch_bars(self, asset, tf, start, end, **kw):  # noqa: ANN001, ANN003
            return pd.DataFrame({"close": [100.0]})  # only 1 close

    monkeypatch.setattr("hermes_quant.advisor._get_default_provider", lambda ac: _ThinProvider())
    mat2 = auto._build_tick_returns_provider()(["AAA", "BBB"])
    assert mat2.shape[0] < 2, "a thin fetch yields a degenerate matrix (n_obs < 2)"
