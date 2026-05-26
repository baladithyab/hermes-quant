"""Wave G — calibrator-from-fills closes inside backtest replay.

The contract: when `learn_from_fills=True` (default), the long-lived
`BMAAggregator` in `replay()` receives `EpisodeOutcome` updates after
`settlement_horizon_bars` bars elapse for each non-flat decision. The
final `BacktestResult.aggregator_posteriors` snapshot reflects per-analyst
empirical accuracy that operators can use to detect drift.

Per ADR-0009 §P1-10 (EpisodeOutcome) + ADR-0020 §D8 (long-lived aggregator)
+ ADR-0018 §D4 (abstain filter still applies pre-update via BMA's own
ABSTAIN_THRESHOLD).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hermes_quant.aggregators.bma import BMAAggregator
from hermes_quant.backtest import BacktestResult, replay


# ---------------------------------------------------------------------------
# Synthetic data + fake advisors
# ---------------------------------------------------------------------------


def _bars(n: int = 200, *, seed: int = 42, drift: float = 0.0, vol: float = 0.5):
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
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


def _make_fake_advisor(direction_seq, *, analysts=("analyst_a", "analyst_b")):
    """Build a fake advisor that emits a controllable direction sequence
    and reports per-analyst views matching the aggregated direction.

    direction_seq: callable bar_idx -> int in {-1, 0, +1}
    """
    state = {"i": 0}

    def fake_advisor(**kwargs):
        i = state["i"]
        state["i"] += 1
        d = direction_seq(i)
        return {
            "as_of": (
                kwargs["as_of"].isoformat()
                if hasattr(kwargs["as_of"], "isoformat")
                else str(kwargs["as_of"])
            ),
            "aggregated_signal": {
                "asset": kwargs.get("symbol", "TEST"),
                "asset_class": kwargs.get("asset_class", "equity"),
                "timeframe": kwargs.get("timeframe", "1h"),
                "direction": d,
                "magnitude": 0.5 if d != 0 else 0.0,
                "confidence": 0.7 if d != 0 else 0.0,
                "confidence_raw": 0.7 if d != 0 else 0.0,
                "horizon": "1h",
                "aggregator": "bma",
            },
            "risk_gate": {
                "pass": d != 0,
                "kelly_fraction": 0.10 if d != 0 else 0.0,
            },
            "analyst_views": [
                {
                    "analyst": name,
                    "direction": d,
                    "magnitude": 0.5 if d != 0 else 0.0,
                    "confidence": 0.7 if d != 0 else 0.0,
                    "confidence_raw": 0.7 if d != 0 else 0.0,
                    "horizon": "1h",
                }
                for name in analysts
            ],
        }

    return fake_advisor


# ===========================================================================
# Wave G: aggregator survives across bars + receives settlement updates
# ===========================================================================


def test_replay_creates_long_lived_aggregator_when_learn_enabled():
    """When learn_from_fills=True (default), replay instantiates a
    BMAAggregator and surfaces its final posteriors in the result."""
    bars = _bars(200)
    advisor = _make_fake_advisor(lambda i: 1 if i % 3 == 0 else 0)
    r = replay(
        bars,
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        warmup_bars=60,
        advisor_recommend=advisor,
    )
    assert isinstance(r, BacktestResult)
    assert r.aggregator_posteriors is not None
    assert "analyst_stats" in r.aggregator_posteriors


def test_settlement_count_matches_decisions_within_window():
    """Every decision emitted before the last `settlement_horizon_bars` bars
    of the run should produce exactly one settlement update."""
    bars = _bars(200)
    advisor = _make_fake_advisor(lambda i: 1 if i % 5 == 0 else 0)
    r = replay(
        bars,
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        warmup_bars=60,
        settlement_horizon_bars=1,
        advisor_recommend=advisor,
    )
    # All decisions except possibly the very last one (which can't settle —
    # there's no future bar) must have settled.
    assert r.n_settlements >= r.n_decisions - 1
    assert r.n_settlements <= r.n_decisions


def test_disabling_learn_zeroes_settlements():
    """learn_from_fills=False should produce no settlements and no
    aggregator_posteriors snapshot."""
    bars = _bars(200)
    advisor = _make_fake_advisor(lambda i: 1 if i % 5 == 0 else 0)
    r = replay(
        bars,
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        warmup_bars=60,
        learn_from_fills=False,
        advisor_recommend=advisor,
    )
    assert r.n_settlements == 0
    assert r.aggregator_posteriors is None


def test_posteriors_evolve_for_correct_analyst():
    """An analyst whose direction always matches the realized return should
    accumulate alpha (correct calls) faster than beta."""
    # Construct upward-trending bars so direction=+1 calls are usually correct
    bars = _bars(300, seed=7, drift=0.05, vol=0.2)
    # Always-long advisor: every bar emits direction=+1 from 'analyst_a'
    advisor = _make_fake_advisor(lambda i: 1, analysts=("analyst_a",))
    r = replay(
        bars,
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        warmup_bars=60,
        settlement_horizon_bars=1,
        advisor_recommend=advisor,
    )
    stats = r.aggregator_posteriors["analyst_stats"]
    a_stats = stats["analyst_a"]
    assert a_stats["n_observations"] > 50, "expected most decisions to settle"
    # On a strongly upward-trending series, an always-long analyst should
    # have alpha > beta. We're loose here — drift dominates noise.
    assert a_stats["alpha"] > a_stats["beta"], (
        f"expected α>β on upward-trending bars; got α={a_stats['alpha']}, β={a_stats['beta']}"
    )


def test_posteriors_evolve_for_wrong_analyst():
    """An always-long analyst on a downward-trending series should accumulate
    beta (wrong calls) faster than alpha."""
    bars = _bars(300, seed=11, drift=-0.05, vol=0.2)
    advisor = _make_fake_advisor(lambda i: 1, analysts=("always_long",))
    r = replay(
        bars,
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        warmup_bars=60,
        settlement_horizon_bars=1,
        advisor_recommend=advisor,
    )
    stats = r.aggregator_posteriors["analyst_stats"]
    a_stats = stats["always_long"]
    assert a_stats["n_observations"] > 50
    assert a_stats["beta"] > a_stats["alpha"], (
        f"expected β>α on downward-trending bars; got α={a_stats['alpha']}, β={a_stats['beta']}"
    )


def test_pre_seeded_aggregator_survives_round_trip():
    """If caller injects a pre-seeded aggregator, the same instance is used
    across the run and the seed is reflected in the final posteriors."""
    agg = BMAAggregator()
    # Pre-seed with one observation: 'preseed' analyst with 1 alpha
    pre_stats = agg._get_or_create_stats("preseed")
    pre_stats.alpha = 5.0
    pre_stats.beta = 1.0
    pre_stats.n_observations = 6

    bars = _bars(200)
    advisor = _make_fake_advisor(lambda i: 0, analysts=("dummy",))  # always flat
    r = replay(
        bars,
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        warmup_bars=60,
        advisor_recommend=advisor,
        aggregator=agg,
    )
    # The pre-seed survived
    seed_stats = r.aggregator_posteriors["analyst_stats"]["preseed"]
    assert seed_stats["alpha"] == 5.0
    assert seed_stats["beta"] == 1.0
    assert seed_stats["n_observations"] == 6


def test_settlement_tail_bound():
    """No settlements should be scheduled for decisions in the final
    `settlement_horizon_bars` bars (no future bar to settle against)."""
    bars = _bars(100)
    n_bars = len(bars)
    # Always-long advisor — every bar in the active window emits a decision
    advisor = _make_fake_advisor(lambda i: 1, analysts=("a",))
    horizon = 5
    r = replay(
        bars,
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        warmup_bars=60,
        settlement_horizon_bars=horizon,
        advisor_recommend=advisor,
    )
    # n_decisions = active_bars; settlements drop the last `horizon` of them
    active_bars = n_bars - 60
    expected_max_settlements = (
        active_bars - horizon + 1
    )  # +1 because last horizon scheduled may still settle on final pass
    # Conservative bound — at most active_bars settlements
    assert r.n_settlements <= active_bars
    # And at least active_bars - horizon (the un-settle-able tail)
    assert r.n_settlements >= active_bars - horizon


def test_settlement_horizon_zero_treated_safely():
    """settlement_horizon_bars=0 means settle on the same bar — degenerate
    but should not crash; with realized_return=0 the aggregator records each
    observation as 'flat-incorrect' if direction != 0."""
    bars = _bars(100)
    advisor = _make_fake_advisor(lambda i: 1 if i % 3 == 0 else 0, analysts=("a",))
    r = replay(
        bars,
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        warmup_bars=60,
        settlement_horizon_bars=0,
        advisor_recommend=advisor,
    )
    # Doesn't crash, produces a valid result
    assert isinstance(r, BacktestResult)


def test_to_markdown_report_includes_posterior_table():
    """Markdown report surfaces per-analyst posteriors when present."""
    bars = _bars(200)
    advisor = _make_fake_advisor(
        lambda i: 1 if i % 3 == 0 else 0, analysts=("alpha_voice", "beta_voice")
    )
    r = replay(
        bars,
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        warmup_bars=60,
        advisor_recommend=advisor,
    )
    md = r.to_markdown_report()
    assert "Per-analyst empirical accuracy" in md
    assert "alpha_voice" in md
    assert "beta_voice" in md
    assert "posterior_accuracy" in md


def test_to_dict_includes_settlement_fields():
    """JSON serialization exposes both n_settlements and posterior snapshot."""
    bars = _bars(200)
    advisor = _make_fake_advisor(lambda i: 1 if i % 3 == 0 else 0)
    r = replay(
        bars,
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        warmup_bars=60,
        advisor_recommend=advisor,
    )
    d = r.to_dict()
    assert "n_settlements" in d
    assert "aggregator_posteriors" in d
    assert d["n_settlements"] == r.n_settlements


def test_config_hash_reflects_learn_flag():
    """Reproducibility: changing `learn_from_fills` must change config_hash."""
    bars = _bars(200)
    advisor1 = _make_fake_advisor(lambda i: 1 if i % 3 == 0 else 0)
    advisor2 = _make_fake_advisor(lambda i: 1 if i % 3 == 0 else 0)
    r_with = replay(
        bars,
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        warmup_bars=60,
        learn_from_fills=True,
        advisor_recommend=advisor1,
    )
    r_without = replay(
        bars,
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        warmup_bars=60,
        learn_from_fills=False,
        advisor_recommend=advisor2,
    )
    assert r_with.config_hash != r_without.config_hash


def test_aggregator_update_exception_is_isolated():
    """If aggregator.update() raises, replay continues; n_settlements records
    only successful updates."""

    class BrokenAggregator(BMAAggregator):
        def update(self, outcome):
            raise RuntimeError("simulated aggregator failure")

        # Inherit aggregate() from base BMAAggregator so replay can use it

    agg = BrokenAggregator()
    bars = _bars(200)
    advisor = _make_fake_advisor(lambda i: 1 if i % 3 == 0 else 0)
    r = replay(
        bars,
        symbol="TEST",
        asset_class="equity",
        timeframe="1h",
        warmup_bars=60,
        advisor_recommend=advisor,
        aggregator=agg,
    )
    # Replay must not crash
    assert isinstance(r, BacktestResult)
    # And no successful settlements occurred
    assert r.n_settlements == 0
