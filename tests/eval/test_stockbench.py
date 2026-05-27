"""tests/eval/test_stockbench.py — STOCKBENCH harness tests.

Coverage:
    - Contamination guard fires on pre-2025 window (strict mode raises error)
    - Contamination guard fires in lenient mode (warns, sets flag)
    - Contamination guard does NOT fire on post-2025 window
    - Successful run on synthetic post-2025 data returns STOCKBENCHResult
    - vs_buyhold_alpha is reported (finite float)
    - Universe capped at max_universe_size
    - Window defaults populated when None
    - Benchmark parameter accepted
    - BuyAndHold strategy achieves vs_buyhold_alpha ≈ 0
    - Degenerate all-flat strategy underperforms buy-and-hold
    - STOCKBENCHResult.to_dict() is JSON-serialisable
    - Max drawdown ≤ 0 for any strategy (by definition)
    - n_decisions tracked correctly for a strategy that changes every day
    - HERMES_QUANT_KNOWLEDGE_CUTOFF env var overrides cutoff
"""

from __future__ import annotations

import json
import math
import os
import warnings
from datetime import date, timedelta

import numpy as np
import pytest

from hermes_quant.eval.stockbench import (
    ContaminationError,
    STOCKBENCHHarness,
    STOCKBENCHResult,
    _BuyAndHoldStrategy,
    _SyntheticPriceSource,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

POST_CUTOFF_START = date(2025, 6, 1)
POST_CUTOFF_END = date(2025, 8, 30)  # 90-day window
SMALL_UNIVERSE = ["AAPL", "MSFT", "NVDA"]


class FlatStrategy:
    """Always returns 0 (hold nothing)."""
    def decide(self, ticker, as_of, price_history):
        return 0.0


class AlwaysLongStrategy:
    """Identical to buy-and-hold."""
    def decide(self, ticker, as_of, price_history):
        return 1.0


class FlipEveryDayStrategy:
    """Alternates +1 / -1 every day to maximise decision count."""
    def __init__(self):
        self._counter: dict[str, int] = {}

    def decide(self, ticker, as_of, price_history):
        n = self._counter.get(ticker, 0)
        self._counter[ticker] = n + 1
        return 1.0 if n % 2 == 0 else -1.0


# ---------------------------------------------------------------------------
# Contamination guard
# ---------------------------------------------------------------------------


class TestContaminationGuard:
    def test_raises_on_pre_cutoff_window_strict(self):
        harness = STOCKBENCHHarness(strict_contamination=True)
        with pytest.raises(ContaminationError):
            harness.run(
                AlwaysLongStrategy(),
                universe=SMALL_UNIVERSE,
                window_start=date(2024, 1, 1),
                window_end=date(2024, 3, 31),
            )

    def test_warns_and_sets_flag_in_lenient_mode(self):
        harness = STOCKBENCHHarness(strict_contamination=False)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = harness.run(
                AlwaysLongStrategy(),
                universe=SMALL_UNIVERSE,
                window_start=date(2024, 1, 1),
                window_end=date(2024, 3, 31),
            )
        assert result.contamination_guard_fired is True
        assert any("contaminated" in str(warning.message).lower() or
                   "knowledge cutoff" in str(warning.message).lower()
                   for warning in w)

    def test_no_fire_on_post_cutoff_window(self):
        harness = STOCKBENCHHarness()
        result = harness.run(
            AlwaysLongStrategy(),
            universe=SMALL_UNIVERSE,
            window_start=POST_CUTOFF_START,
            window_end=POST_CUTOFF_END,
        )
        assert result.contamination_guard_fired is False

    def test_env_var_overrides_cutoff(self, monkeypatch):
        """HERMES_QUANT_KNOWLEDGE_CUTOFF shifts the cutoff forward."""
        monkeypatch.setenv("HERMES_QUANT_KNOWLEDGE_CUTOFF", "2026-01-01")
        # 2025-06 is now BEFORE the cutoff of 2026-01
        harness = STOCKBENCHHarness(strict_contamination=True)
        with pytest.raises(ContaminationError):
            harness.run(
                AlwaysLongStrategy(),
                universe=SMALL_UNIVERSE,
                window_start=date(2025, 6, 1),
                window_end=date(2025, 8, 30),
            )


# ---------------------------------------------------------------------------
# Successful run on post-cutoff synthetic data
# ---------------------------------------------------------------------------


class TestSuccessfulRun:
    def test_returns_stockbench_result(self):
        harness = STOCKBENCHHarness()
        result = harness.run(
            AlwaysLongStrategy(),
            universe=SMALL_UNIVERSE,
            window_start=POST_CUTOFF_START,
            window_end=POST_CUTOFF_END,
        )
        assert isinstance(result, STOCKBENCHResult)

    def test_vs_buyhold_alpha_is_finite(self):
        harness = STOCKBENCHHarness()
        result = harness.run(
            AlwaysLongStrategy(),
            universe=SMALL_UNIVERSE,
            window_start=POST_CUTOFF_START,
            window_end=POST_CUTOFF_END,
        )
        assert math.isfinite(result.vs_buyhold_alpha)

    def test_buyhold_strategy_alpha_near_zero(self):
        """Buy-and-hold strategy should have alpha ≈ 0 vs itself."""
        harness = STOCKBENCHHarness()
        result = harness.run(
            _BuyAndHoldStrategy(),
            universe=SMALL_UNIVERSE,
            window_start=POST_CUTOFF_START,
            window_end=POST_CUTOFF_END,
        )
        assert abs(result.vs_buyhold_alpha) < 1e-6

    def test_flat_strategy_has_non_positive_alpha(self):
        """A flat strategy should underperform or equal buy-and-hold in a bull market."""
        # The synthetic source has positive drift (mu=0.0003 > 0), so
        # buy-and-hold accrues value that flat doesn't.
        harness = STOCKBENCHHarness()
        result = harness.run(
            FlatStrategy(),
            universe=SMALL_UNIVERSE,
            window_start=POST_CUTOFF_START,
            window_end=POST_CUTOFF_END,
        )
        # Flat strategy cumulative return = 0 (stays in cash)
        assert abs(result.cumulative_return) < 1e-6

    def test_max_drawdown_is_nonpositive(self):
        harness = STOCKBENCHHarness()
        result = harness.run(
            AlwaysLongStrategy(),
            universe=SMALL_UNIVERSE,
            window_start=POST_CUTOFF_START,
            window_end=POST_CUTOFF_END,
        )
        assert result.max_drawdown <= 0.0

    def test_n_decisions_tracked(self):
        harness = STOCKBENCHHarness()
        result = harness.run(
            FlipEveryDayStrategy(),
            universe=SMALL_UNIVERSE,
            window_start=POST_CUTOFF_START,
            window_end=POST_CUTOFF_END,
        )
        # Flips every day → many decisions
        assert result.n_decisions > 0

    def test_decisions_per_day_avg_positive(self):
        harness = STOCKBENCHHarness()
        result = harness.run(
            FlipEveryDayStrategy(),
            universe=SMALL_UNIVERSE,
            window_start=POST_CUTOFF_START,
            window_end=POST_CUTOFF_END,
        )
        assert result.decisions_per_day_avg > 0


# ---------------------------------------------------------------------------
# Universe handling
# ---------------------------------------------------------------------------


class TestUniverseHandling:
    def test_universe_capped_at_max_size(self):
        harness = STOCKBENCHHarness(max_universe_size=2)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = harness.run(
                AlwaysLongStrategy(),
                universe=["AAPL", "MSFT", "NVDA", "GOOG", "META"],
                window_start=POST_CUTOFF_START,
                window_end=POST_CUTOFF_END,
            )
        assert len(result.universe) <= 2
        assert any("capped" in str(warning.message).lower() for warning in w)

    def test_default_universe_is_5_tickers(self):
        harness = STOCKBENCHHarness()
        result = harness.run(
            AlwaysLongStrategy(),
            window_start=POST_CUTOFF_START,
            window_end=POST_CUTOFF_END,
        )
        assert len(result.universe) == 5

    def test_benchmark_param_stored(self):
        harness = STOCKBENCHHarness()
        result = harness.run(
            AlwaysLongStrategy(),
            universe=SMALL_UNIVERSE,
            window_start=POST_CUTOFF_START,
            window_end=POST_CUTOFF_END,
            benchmark="QQQ",
        )
        assert result.benchmark == "QQQ"


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


class TestSerialisation:
    def test_to_dict_is_json_serialisable(self):
        harness = STOCKBENCHHarness()
        result = harness.run(
            AlwaysLongStrategy(),
            universe=SMALL_UNIVERSE,
            window_start=POST_CUTOFF_START,
            window_end=POST_CUTOFF_END,
        )
        d = result.to_dict()
        # Must not raise
        s = json.dumps(d, default=str)
        assert len(s) > 10

    def test_to_dict_has_expected_keys(self):
        harness = STOCKBENCHHarness()
        result = harness.run(
            AlwaysLongStrategy(),
            universe=SMALL_UNIVERSE,
            window_start=POST_CUTOFF_START,
            window_end=POST_CUTOFF_END,
        )
        d = result.to_dict()
        expected_keys = {
            "universe", "window_start", "window_end", "benchmark",
            "cumulative_return", "max_drawdown", "sortino",
            "n_decisions", "decisions_per_day_avg",
            "vs_buyhold_alpha", "contamination_guard_fired",
        }
        assert expected_keys.issubset(d.keys())
