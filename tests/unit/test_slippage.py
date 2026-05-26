"""Unit tests for hermes_quant.daemon.slippage — side-aware adverse-bps.

Anchor: synthesis-v2 §P1-ζ. Verifies:
- Buy adverse: (fill - decision) / decision (positive = paid more)
- Sell adverse: (decision - fill) / decision (positive = received less)
- Symmetry: same percentage worse on buy vs sell produces equal adverse
- Only positive adverse persisted in rolling estimator
- Bootstrap defaults until min_samples threshold
- Round-trip = 2× one-way
"""

from __future__ import annotations

import pytest

from hermes_quant.daemon.slippage import (
    DEFAULT_BOOTSTRAP_SLIPPAGE,
    RollingSlippageEstimator,
    compute_adverse_bps_signed,
)


class TestComputeAdverseBpsSigned:
    """The synthesis-v2 §P1-ζ canonical fix."""

    def test_buy_paying_more_is_positive_adverse(self):
        # Decided at $100, filled at $100.10 (paid 10 bps more) — bad
        adv = compute_adverse_bps_signed(decision_price=100.0, fill_price=100.10, side="buy")
        assert adv == pytest.approx(0.001)  # 10 bps

    def test_buy_paying_less_is_negative_adverse(self):
        # Decided at $100, filled at $99.90 (paid 10 bps less) — favorable
        adv = compute_adverse_bps_signed(decision_price=100.0, fill_price=99.90, side="buy")
        assert adv == pytest.approx(-0.001)

    def test_sell_receiving_less_is_positive_adverse(self):
        # Decided to sell at $100, filled at $99.90 (received 10 bps less) — bad
        adv = compute_adverse_bps_signed(decision_price=100.0, fill_price=99.90, side="sell")
        assert adv == pytest.approx(0.001)  # 10 bps adverse

    def test_sell_receiving_more_is_negative_adverse(self):
        # Decided to sell at $100, filled at $100.10 (received 10 bps more) — favorable
        adv = compute_adverse_bps_signed(decision_price=100.0, fill_price=100.10, side="sell")
        assert adv == pytest.approx(-0.001)

    def test_symmetry_buy_vs_sell(self):
        """A 10-bp adverse buy and a 10-bp adverse sell produce equal adverse fractions.

        This is THE bug the unsigned formula had: it would have shown buys
        adverse=+0.001 and sells adverse=-0.001 (or zero) for the same
        magnitude of cost — double-counting on buys, zero-counting on sells.
        """
        buy_adv = compute_adverse_bps_signed(decision_price=100.0, fill_price=100.10, side="buy")
        sell_adv = compute_adverse_bps_signed(decision_price=100.0, fill_price=99.90, side="sell")
        assert buy_adv == pytest.approx(sell_adv)
        assert buy_adv > 0  # Both are adverse (positive)

    def test_zero_decision_price_raises(self):
        with pytest.raises(ValueError):
            compute_adverse_bps_signed(decision_price=0.0, fill_price=1.0, side="buy")
        with pytest.raises(ValueError):
            compute_adverse_bps_signed(decision_price=-1.0, fill_price=1.0, side="buy")

    def test_invalid_side_raises(self):
        with pytest.raises(ValueError):
            compute_adverse_bps_signed(
                decision_price=100.0,
                fill_price=100.0,
                side="long",  # type: ignore
            )

    def test_no_change_zero_adverse(self):
        for side in ("buy", "sell"):
            adv = compute_adverse_bps_signed(
                decision_price=100.0,
                fill_price=100.0,
                side=side,  # type: ignore
            )
            assert adv == 0.0


class TestRollingSlippageEstimator:
    def test_bootstrap_default_below_min_samples(self):
        est = RollingSlippageEstimator(asset_class="crypto", min_samples_for_estimate=30)
        # No samples — bootstrap default
        assert est.estimate_round_trip() == pytest.approx(DEFAULT_BOOTSTRAP_SLIPPAGE["crypto"])
        # One-way is half of round-trip
        assert est.estimate_one_way() == pytest.approx(DEFAULT_BOOTSTRAP_SLIPPAGE["crypto"] / 2)

    def test_only_positive_adverse_persisted(self):
        """Synthesis-v2 §P1-ζ: favorable slippage is opportunity, not cost — drop it."""
        est = RollingSlippageEstimator(asset_class="crypto", min_samples_for_estimate=2)
        # 3 favorable observations
        est.observe(decision_price=100.0, fill_price=99.90, side="buy")  # adverse=-0.001
        est.observe(decision_price=100.0, fill_price=100.10, side="sell")  # adverse=-0.001
        est.observe(decision_price=100.0, fill_price=99.95, side="buy")  # adverse=-0.0005

        # All three were favorable — none persisted
        assert est.n_samples == 0

        # Now add 2 adverse observations
        est.observe(decision_price=100.0, fill_price=100.05, side="buy")  # +0.0005
        est.observe(decision_price=100.0, fill_price=99.95, side="sell")  # +0.0005

        assert est.n_samples == 2
        assert est.estimate_one_way() == pytest.approx(0.0005)
        assert est.estimate_round_trip() == pytest.approx(0.001)

    def test_samples_above_min_uses_rolling_mean(self):
        est = RollingSlippageEstimator(
            asset_class="crypto", min_samples_for_estimate=3, max_samples=10
        )
        # 4 adverse observations of 5 bps each
        for _ in range(4):
            est.observe(decision_price=100.0, fill_price=100.05, side="buy")
        # 0.0005 each, 4 samples, mean = 0.0005
        assert est.estimate_one_way() == pytest.approx(0.0005)

    def test_max_samples_ring_buffer(self):
        est = RollingSlippageEstimator(
            asset_class="crypto", min_samples_for_estimate=2, max_samples=3
        )
        # Push 5 adverse observations of varying magnitudes
        for fill in [100.10, 100.20, 100.30, 100.05, 100.05]:
            est.observe(decision_price=100.0, fill_price=fill, side="buy")
        # Only the last 3 should be retained: [0.003, 0.0005, 0.0005]
        # Mean = 0.0040 / 3 ≈ 0.001333
        assert est.n_samples == 3
        assert est.estimate_one_way() == pytest.approx((0.003 + 0.0005 + 0.0005) / 3)

    def test_bootstrap_default_per_asset_class(self):
        for cls, expected in DEFAULT_BOOTSTRAP_SLIPPAGE.items():
            est = RollingSlippageEstimator(asset_class=cls, min_samples_for_estimate=30)
            assert est.estimate_round_trip() == pytest.approx(expected)

    def test_unknown_asset_class_falls_back_to_crypto_default(self):
        est = RollingSlippageEstimator(asset_class="exotic-class", min_samples_for_estimate=30)
        # The fallback in the dataclass is 0.0012 (crypto default)
        assert est.estimate_round_trip() == pytest.approx(0.0012)

    def test_explicit_bootstrap_default_override(self):
        est = RollingSlippageEstimator(
            asset_class="crypto",
            bootstrap_default=0.005,  # 50 bps explicit override
            min_samples_for_estimate=30,
        )
        assert est.estimate_round_trip() == pytest.approx(0.005)

    def test_reset_clears_samples(self):
        est = RollingSlippageEstimator(asset_class="crypto", min_samples_for_estimate=2)
        est.observe(decision_price=100.0, fill_price=100.05, side="buy")
        assert est.n_samples == 1
        est.reset()
        assert est.n_samples == 0
        # Falls back to bootstrap
        assert est.estimate_round_trip() == pytest.approx(DEFAULT_BOOTSTRAP_SLIPPAGE["crypto"])
