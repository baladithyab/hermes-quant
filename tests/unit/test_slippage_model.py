"""Tests for hermes_quant.react.slippage_model (ADR-0070).

Locks in:
  * Determinism: same (proposal_id, asof_execution) → same slippage
  * Sign correctness: long pays positive (fill > decision), short pays positive
                      in the trader-adverse direction (fill < decision)
  * Hard cap: total_bps capped at config.max_total_bps
  * Auction premium: only fires when is_late_session=True
  * Per-asset-class config: equity vs crypto defaults
  * Late-session detector handles DST + outside-window cases
"""

from __future__ import annotations

import math

import pytest

from hermes_quant.react.slippage_model import (
    PaperSlippageConfig,
    apply_slippage,
    config_for_asset_class,
    is_late_session_equity,
    seed_for_fill,
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_equity_default_config() -> None:
    cfg = PaperSlippageConfig.equity_default()
    assert cfg.spread_cross_bps == 3.0
    assert cfg.impact_bps_per_pct_nav == 0.5
    assert cfg.queue_latency_seconds == 1.0
    assert cfg.auction_premium_bps == 10.0


def test_crypto_default_config_higher_costs() -> None:
    cfg = PaperSlippageConfig.crypto_default()
    eq = PaperSlippageConfig.equity_default()
    assert cfg.spread_cross_bps > eq.spread_cross_bps
    assert cfg.impact_bps_per_pct_nav > eq.impact_bps_per_pct_nav
    assert cfg.auction_premium_bps == 0.0  # no close auction in crypto


def test_config_for_asset_class_routing() -> None:
    assert config_for_asset_class("equity") == PaperSlippageConfig.equity_default()
    assert config_for_asset_class("crypto") == PaperSlippageConfig.crypto_default()
    # Unknown → equity default (conservative)
    assert config_for_asset_class("unknown") == PaperSlippageConfig.equity_default()


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_proposal_same_asof_produces_same_fill() -> None:
    fp1, b1 = apply_slippage(
        decision_price=100.0,
        target_pct=0.20,
        asof_execution="2026-05-28T17:09:00Z",
        proposal_id="prop_x",
        asset_class="equity",
    )
    fp2, b2 = apply_slippage(
        decision_price=100.0,
        target_pct=0.20,
        asof_execution="2026-05-28T17:09:00Z",
        proposal_id="prop_x",
        asset_class="equity",
    )
    assert fp1 == fp2
    assert b1 == b2


def test_different_proposal_produces_different_drift() -> None:
    """Same wall-clock + size, different proposal_id → different latency_drift."""
    _, b1 = apply_slippage(
        decision_price=100.0,
        target_pct=0.20,
        asof_execution="2026-05-28T17:09:00Z",
        proposal_id="prop_a",
        asset_class="equity",
    )
    _, b2 = apply_slippage(
        decision_price=100.0,
        target_pct=0.20,
        asof_execution="2026-05-28T17:09:00Z",
        proposal_id="prop_b",
        asset_class="equity",
    )
    # spread + impact are deterministic; only latency_drift varies
    assert b1["spread_bps"] == b2["spread_bps"]
    assert b1["impact_bps"] == b2["impact_bps"]
    assert b1["latency_drift_bps"] != b2["latency_drift_bps"]


def test_seed_function_64bit_uniform() -> None:
    s = seed_for_fill("prop_test", "2026-05-28T17:09:00Z")
    assert isinstance(s, int)
    assert 0 <= s < 2**64


# ---------------------------------------------------------------------------
# Sign correctness
# ---------------------------------------------------------------------------


def test_long_fill_paid_above_decision() -> None:
    fp, b = apply_slippage(
        decision_price=100.0,
        target_pct=0.20,
        asof_execution="2026-05-28T17:09:00Z",
        proposal_id="prop_long",
        asset_class="equity",
    )
    assert fp > 100.0
    assert b["total_bps"] > 0


def test_short_fill_filled_below_decision() -> None:
    fp, b = apply_slippage(
        decision_price=100.0,
        target_pct=-0.20,
        asof_execution="2026-05-28T17:09:00Z",
        proposal_id="prop_short",
        asset_class="equity",
    )
    assert fp < 100.0
    assert b["total_bps"] > 0  # cost is positive in bps


def test_zero_target_no_slippage() -> None:
    fp, b = apply_slippage(
        decision_price=100.0,
        target_pct=0.0,
        asof_execution="2026-05-28T17:09:00Z",
        proposal_id="prop_zero",
        asset_class="equity",
    )
    assert fp == 100.0
    assert b["total_bps"] == 0.0


# ---------------------------------------------------------------------------
# Trade-delta direction (ADR-0070 / position-reducing fills must pay a COST)
#
# Slippage direction must key off the SIGN OF THE TRADED DELTA
# (target_pct - current_position_pct), NOT the sign of the absolute target.
# A position-reducing fill is the opposite side from the position it reduces:
#   * trimming a LONG  (+0.20 -> +0.05)  is a SELL  -> fill_price < decision
#   * covering a SHORT (-0.20 -> -0.05)  is a BUY   -> fill_price > decision
# Keying off the target sign credits the exit/trim leg with a FAVORABLE price,
# which is the opposite of the cost slippage must model (module invariant:
# "Either way, the trader is the price-taker; slippage is a cost").
# ---------------------------------------------------------------------------


def test_trim_long_is_a_sell_filled_below_decision() -> None:
    """Trimming a +0.20 long to +0.05 is a SELL (~15% NAV sold); a higher sell
    price would be FAVORABLE, so slippage must push the fill price BELOW decision."""
    fp, b = apply_slippage(
        decision_price=100.0,
        target_pct=0.05,            # post-fill target (ADR-0091 Option E absolute)
        current_position_pct=0.20,  # currently +20% NAV long
        asof_execution="2026-05-28T17:09:00Z",
        proposal_id="prop_trim_long",
        asset_class="equity",
    )
    assert b["total_bps"] > 0
    assert fp < 100.0, f"trim of a long must fill BELOW decision (a sell cost), got {fp}"


def test_partial_cover_short_is_a_buy_filled_above_decision() -> None:
    """Partially covering a -0.20 short to -0.05 is a BUY (~15% NAV bought back);
    slippage must push the fill price ABOVE decision (paying up to buy)."""
    fp, b = apply_slippage(
        decision_price=100.0,
        target_pct=-0.05,            # still short, but smaller
        current_position_pct=-0.20,  # currently -20% NAV short
        asof_execution="2026-05-28T17:09:00Z",
        proposal_id="prop_cover_short",
        asset_class="equity",
    )
    assert b["total_bps"] > 0
    assert fp > 100.0, f"covering a short must fill ABOVE decision (a buy cost), got {fp}"


def test_full_close_long_is_a_sell_filled_below_decision() -> None:
    """Closing a +0.20 long all the way to flat (0.0) is a SELL."""
    fp, _ = apply_slippage(
        decision_price=100.0,
        target_pct=0.0 + 1e-12,     # ~flat post-fill, but a real sell of 20% NAV
        current_position_pct=0.20,
        asof_execution="2026-05-28T17:09:00Z",
        proposal_id="prop_close_long",
        asset_class="equity",
    )
    assert fp < 100.0, f"closing a long must fill BELOW decision, got {fp}"


def test_impact_keyed_off_trade_delta_not_absolute_target() -> None:
    """A small trim of a large position trades a small delta, so the impact term
    must reflect the |delta|, not the |target| (which would over-charge impact)."""
    # Trim +0.20 -> +0.18: a 2% NAV sell. Impact should match a 2% trade, not 18%.
    _, b = apply_slippage(
        decision_price=100.0,
        target_pct=0.18,
        current_position_pct=0.20,
        asof_execution="2026-05-28T17:09:00Z",
        proposal_id="prop_small_trim",
        asset_class="equity",
    )
    cfg = PaperSlippageConfig.equity_default()
    # impact = impact_bps_per_pct_nav * (|delta| * 100); |delta| = 0.02
    expected_impact = cfg.impact_bps_per_pct_nav * (0.02 * 100.0)
    assert math.isclose(b["impact_bps"], expected_impact, abs_tol=1e-9), (
        f"impact must key off |trade_delta|=0.02, got impact_bps={b['impact_bps']}"
    )


def test_opening_fill_unchanged_default_position_zero() -> None:
    """REGRESSION: a genuine opening fill (no prior position) is byte-identical to
    the pre-fix behavior. Omitting current_position_pct defaults to 0.0, so the
    trade delta equals the target and the direction/impact are exactly as before."""
    fp_default, b_default = apply_slippage(
        decision_price=100.0,
        target_pct=0.20,
        asof_execution="2026-05-28T17:09:00Z",
        proposal_id="prop_open",
        asset_class="equity",
    )
    fp_explicit, b_explicit = apply_slippage(
        decision_price=100.0,
        target_pct=0.20,
        current_position_pct=0.0,
        asof_execution="2026-05-28T17:09:00Z",
        proposal_id="prop_open",
        asset_class="equity",
    )
    assert fp_default == fp_explicit
    assert b_default == b_explicit
    # Opening a long still fills ABOVE decision (a buy pays up).
    assert fp_default > 100.0


def test_adding_to_long_is_a_buy_above_decision() -> None:
    """Increasing a +0.05 long to +0.20 is a BUY -> fill above decision."""
    fp, _ = apply_slippage(
        decision_price=100.0,
        target_pct=0.20,
        current_position_pct=0.05,
        asof_execution="2026-05-28T17:09:00Z",
        proposal_id="prop_add_long",
        asset_class="equity",
    )
    assert fp > 100.0


def test_adding_to_short_is_a_sell_below_decision() -> None:
    """Increasing a -0.05 short to -0.20 sells more short -> fill below decision."""
    fp, _ = apply_slippage(
        decision_price=100.0,
        target_pct=-0.20,
        current_position_pct=-0.05,
        asof_execution="2026-05-28T17:09:00Z",
        proposal_id="prop_add_short",
        asset_class="equity",
    )
    assert fp < 100.0


# ---------------------------------------------------------------------------
# Cap behavior
# ---------------------------------------------------------------------------


def test_max_total_bps_caps_extreme_drift() -> None:
    """A pathological RNG draw shouldn't move price by more than max_total_bps."""
    cfg = PaperSlippageConfig(
        spread_cross_bps=10.0,
        impact_bps_per_pct_nav=10.0,    # 200 bps for 20% NAV
        vol_drift_bps_per_sqrt_sec=50.0,
        queue_latency_seconds=10.0,
        max_total_bps=75.0,
    )
    fp, b = apply_slippage(
        decision_price=100.0,
        target_pct=0.20,
        asof_execution="2026-05-28T17:09:00Z",
        proposal_id="prop_extreme",
        asset_class="equity",
        config=cfg,
    )
    assert b["total_bps"] == 75.0  # capped
    assert b["total_bps_pre_cap"] > 75.0  # would have been higher uncapped
    # Fill price reflects only the capped slippage
    assert math.isclose((fp - 100.0) / 100.0 * 1e4, 75.0, abs_tol=1e-9)


def test_invalid_decision_price_raises() -> None:
    # Message wording widened (deep-review 2026-06-07) to "finite and > 0";
    # match the stable prefix so both the >0 and non-finite guards are covered.
    with pytest.raises(ValueError, match=r"decision_price must be"):
        apply_slippage(
            decision_price=0.0,
            target_pct=0.20,
            asof_execution="2026-05-28T17:09:00Z",
            proposal_id="prop_bad",
            asset_class="equity",
        )


def test_nonfinite_decision_price_raises() -> None:
    # NaN-fail-CLOSED: a NaN/inf decision_price must raise rather than produce a
    # NaN fill_price that corrupts the P&L ledger.
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match=r"decision_price must be"):
            apply_slippage(
                decision_price=bad,
                target_pct=0.20,
                asof_execution="2026-05-28T17:09:00Z",
                proposal_id="prop_bad",
                asset_class="equity",
            )


# ---------------------------------------------------------------------------
# Auction premium
# ---------------------------------------------------------------------------


def test_auction_premium_added_when_late_session() -> None:
    fp_mid, b_mid = apply_slippage(
        decision_price=100.0,
        target_pct=0.20,
        asof_execution="2026-05-28T17:09:00Z",
        proposal_id="prop_x",
        asset_class="equity",
        is_late_session=False,
    )
    fp_late, b_late = apply_slippage(
        decision_price=100.0,
        target_pct=0.20,
        asof_execution="2026-05-28T17:09:00Z",
        proposal_id="prop_x",
        asset_class="equity",
        is_late_session=True,
    )
    assert b_late["auction_bps"] == 10.0
    assert b_mid["auction_bps"] == 0.0
    # Late fill is more expensive than mid-session fill
    assert (fp_late - 100.0) > (fp_mid - 100.0)


def test_crypto_no_auction_premium_even_late() -> None:
    """Crypto config sets auction_bps=0 — no close auction concept."""
    fp, b = apply_slippage(
        decision_price=100.0,
        target_pct=0.20,
        asof_execution="2026-05-28T17:09:00Z",
        proposal_id="prop_x",
        asset_class="crypto",
        is_late_session=True,
    )
    assert b["auction_bps"] == 0.0


# ---------------------------------------------------------------------------
# Order-of-magnitude sanity
# ---------------------------------------------------------------------------


def test_20pct_nav_equity_yields_realistic_slippage() -> None:
    """A 20% NAV equity fill mid-session should land in the 10-25 bps range."""
    fp, b = apply_slippage(
        decision_price=100.0,
        target_pct=0.20,
        asof_execution="2026-05-28T17:09:00Z",
        proposal_id="prop_20pct",
        asset_class="equity",
        is_late_session=False,
    )
    bps = (fp - 100.0) / 100.0 * 1e4
    assert 10.0 < bps < 25.0, f"unrealistic slippage: {bps} bps"


def test_1pct_nav_yields_smaller_slippage() -> None:
    """A 1% NAV fill should slip much less than a 20% NAV fill."""
    fp_small, _ = apply_slippage(
        decision_price=100.0, target_pct=0.01,
        asof_execution="2026-05-28T17:09:00Z",
        proposal_id="prop_small", asset_class="equity",
    )
    fp_big, _ = apply_slippage(
        decision_price=100.0, target_pct=0.20,
        asof_execution="2026-05-28T17:09:00Z",
        proposal_id="prop_big", asset_class="equity",
    )
    assert (fp_small - 100.0) < (fp_big - 100.0)


# ---------------------------------------------------------------------------
# Late-session detector
# ---------------------------------------------------------------------------


def test_late_session_detector_edt() -> None:
    """5/28 15:45 ET (= 19:45 UTC EDT) is within 30 min of close."""
    assert is_late_session_equity("2026-05-28T19:45:00Z") is True


def test_late_session_detector_mid_session_false() -> None:
    """13:09 ET = 17:09 UTC (EDT) is NOT late-session."""
    assert is_late_session_equity("2026-05-28T17:09:00Z") is False


def test_late_session_detector_post_close_false() -> None:
    """17:00 ET = 21:00 UTC is post-close, not late-session-window."""
    assert is_late_session_equity("2026-05-28T21:00:00Z") is False


def test_late_session_detector_at_close() -> None:
    """16:00 ET = 20:00 UTC is the boundary; treat as inside late window."""
    assert is_late_session_equity("2026-05-28T20:00:00Z") is True


def test_late_session_detector_winter_est() -> None:
    """In January (EST = UTC-5), 15:45 ET = 20:45 UTC."""
    assert is_late_session_equity("2026-01-15T20:45:00Z") is True
    # 15:45 UTC in January = 10:45 ET (mid-session)
    assert is_late_session_equity("2026-01-15T15:45:00Z") is False


def test_late_session_detector_handles_invalid() -> None:
    assert is_late_session_equity("") is False
    assert is_late_session_equity("not-an-iso-string") is False
