"""RED->GREEN regression: replay() must NOT double-apply the direction sign.

Defect (money-software, ADR-0020 backtest empirical gate):
    The advisor's `risk_gate["kelly_fraction"]` is ALREADY SIGNED — it is a
    verbatim copy of `Action.target_position_pct`, whose protocol contract is
    "signed; e.g. 0.10 = 10% NAV long, -0.05 = 5% NAV short"
    (hermes_quant/protocol.py). The gate's quarter_kelly_size() returns a
    NEGATIVE target for a short signal.

    replay() computed `signed_target = direction * target_pct`, multiplying the
    already-signed kelly_fraction by `direction` a SECOND time. For a short
    signal (direction = -1, kelly_fraction = -0.20) this yields
    `-1 * -0.20 = +0.20` — a LONG position. Every short trade was inverted to
    long (and back) in the backtest P&L, systematically mis-scoring any
    strategy that takes short trades against the empirical charter gate.

    Every OTHER consumer of kelly_fraction (autonomous.py, tools.py,
    journal/writer.py) uses it directly as the signed target — replay() was the
    lone inconsistency.

These tests inject a known short / long signal through the advisor seam and
assert the signed target handed to the portfolio matches the signal direction.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from hermes_quant.backtest.portfolio import PaperPortfolio
from hermes_quant.backtest.replay import replay


def _bars(n: int = 200, *, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    closes = 100 + np.cumsum(rng.normal(0.0, 0.5, n))
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


def _const_advisor(direction: int, kelly_fraction: float):
    """Build an advisor stub returning a constant passing gate.

    kelly_fraction is the ALREADY-SIGNED gate output (negative for a short),
    mirroring the real advisor's `_action_to_gate_dict` which copies
    Action.target_position_pct verbatim.
    """

    def _advisor(**kwargs):
        asof = kwargs["as_of"]
        return {
            "as_of": asof.isoformat() if hasattr(asof, "isoformat") else str(asof),
            "aggregated_signal": {
                "direction": direction,
                "magnitude": 0.05,
                "confidence": 0.9,
                "metadata": {},
            },
            "risk_gate": {
                "pass": True,
                "kelly_fraction": float(kelly_fraction),
                "recommended_action": "short_with_stop" if direction < 0 else "long_with_stop",
            },
            "analyst_views": [],
        }

    return _advisor


def _capture_signed_targets(monkeypatch) -> list[float]:
    """Patch PaperPortfolio.apply_target to record the signed target it receives,
    then delegate to the real implementation."""
    captured: list[float] = []
    real_apply = PaperPortfolio.apply_target

    def _spy(self, target_position_pct, bar_close, **kw):
        captured.append(float(target_position_pct))
        return real_apply(self, target_position_pct, bar_close, **kw)

    monkeypatch.setattr(PaperPortfolio, "apply_target", _spy)
    return captured


def test_replay_short_signal_targets_a_short_position(monkeypatch):
    """A short signal (direction=-1, signed kelly_fraction=-0.20) must hand the
    portfolio a NEGATIVE (short) target. The pre-fix double-sign produced +0.20."""
    captured = _capture_signed_targets(monkeypatch)
    bars = _bars(200)

    replay(
        bars,
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        warmup_bars=60,
        learn_from_fills=False,
        advisor_recommend=_const_advisor(direction=-1, kelly_fraction=-0.20),
    )

    acting = [t for t in captured if abs(t) > 1e-9]
    assert acting, "expected at least one non-flat target from the short signal"
    # Every acting target must be SHORT (negative) — same sign as the signal.
    assert all(t < 0 for t in acting), (
        f"short signal produced non-short targets {acting[:5]} — "
        "direction sign was double-applied (kelly_fraction is already signed)"
    )


def test_replay_long_signal_targets_a_long_position(monkeypatch):
    """Control: a long signal (direction=+1, signed kelly_fraction=+0.20) targets
    a LONG (positive) position. Proves the assertion is non-vacuous and that the
    fix does not break the long path."""
    captured = _capture_signed_targets(monkeypatch)
    bars = _bars(200)

    replay(
        bars,
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        warmup_bars=60,
        learn_from_fills=False,
        advisor_recommend=_const_advisor(direction=1, kelly_fraction=0.20),
    )

    acting = [t for t in captured if abs(t) > 1e-9]
    assert acting, "expected at least one non-flat target from the long signal"
    assert all(t > 0 for t in acting), (
        f"long signal produced non-long targets {acting[:5]}"
    )
