"""Unit tests for the deterministic structure-selection table (ADR-0082 Part B).

Pure, deterministic, offline (no network, no model, no I/O). Asserts the rails:

  * the table is a DETERMINISTIC function (same inputs => same StrategyKind);
  * a bullish + premium-capture + (rich-IV) maps sensibly to cash_secured_put,
    and low-IV premium-capture abstains (thin premium);
  * EVERY table output is a valid _MULTI_LEG_STRATEGIES member or None (abstain) —
    never naked, never a non-producible kind;
  * flag-OFF is byte-identical: select_structure_for_plan returns None for every
    plan while HERMES_QUANT_STRUCTURE_SELECT is unset/!=1.
"""

from __future__ import annotations

import itertools

import pytest

from hermes_quant.agents.research_debate.schemas import (
    PortfolioRating,
    ResearchPlan,
    StructureIntent,
)
from hermes_quant.options.recipes import StrategyKind  # noqa: F401 — type doc
from hermes_quant.options.structure_select import (
    _STRUCTURE_TABLE,
    IV_RANK_HIGH_MIN,
    IV_RANK_LOW_MAX,
    STRUCTURE_SELECT_FLAG,
    Direction,
    IVRegime,
    classify_iv_regime,
    direction_from_rating,
    select_structure,
    select_structure_for_plan,
    structure_select_enabled,
)
from hermes_quant.tools import _MULTI_LEG_STRATEGIES

# The producer's buildable set is the ONLY admissible non-None output.
_PRODUCIBLE = set(_MULTI_LEG_STRATEGIES)


def _plan(rating: PortfolioRating, intent: StructureIntent | None) -> ResearchPlan:
    return ResearchPlan(
        recommendation=rating,
        confidence=0.7,
        rationale="x",
        strategic_actions="y",
        structure_intent=intent,
    )


# --------------------------------------------------------------------------- #
# IV-regime classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "iv_rank,expected",
    [
        (0.0, IVRegime.LOW),
        (29.999, IVRegime.LOW),
        (IV_RANK_LOW_MAX, IVRegime.MID),  # 30 -> MID (half-open)
        (40.0, IVRegime.MID),
        (49.999, IVRegime.MID),
        (IV_RANK_HIGH_MIN, IVRegime.HIGH),  # 50 -> HIGH
        (75.0, IVRegime.HIGH),
        (100.0, IVRegime.HIGH),
    ],
)
def test_classify_iv_regime_buckets(iv_rank, expected):
    assert classify_iv_regime(iv_rank) is expected


@pytest.mark.parametrize("bad", [-0.01, 100.01, float("nan"), float("inf"), -float("inf")])
def test_classify_iv_regime_out_of_range_abstains(bad):
    # An unknown / out-of-range vol regime must abstain (fail-closed), never default.
    assert classify_iv_regime(bad) is None


def test_classify_iv_regime_is_deterministic():
    for r in (10.0, 35.0, 60.0):
        assert classify_iv_regime(r) is classify_iv_regime(r)


# --------------------------------------------------------------------------- #
# Direction distillation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "rating,expected",
    [
        (PortfolioRating.BUY, Direction.BULLISH),
        (PortfolioRating.OVERWEIGHT, Direction.BULLISH),
        (PortfolioRating.HOLD, Direction.NEUTRAL),
        (PortfolioRating.UNDERWEIGHT, Direction.BEARISH),
        (PortfolioRating.SELL, Direction.BEARISH),
    ],
)
def test_direction_from_rating(rating, expected):
    assert direction_from_rating(rating) is expected


# --------------------------------------------------------------------------- #
# Determinism: same inputs => same StrategyKind
# --------------------------------------------------------------------------- #
def test_select_structure_is_deterministic():
    for direction, intent, regime in itertools.product(
        Direction, StructureIntent, IVRegime
    ):
        a = select_structure(
            direction=direction, structure_intent=intent, iv_regime=regime
        )
        b = select_structure(
            direction=direction, structure_intent=intent, iv_regime=regime
        )
        assert a == b


# --------------------------------------------------------------------------- #
# The headline sensible mapping
# --------------------------------------------------------------------------- #
def test_bullish_premium_capture_rich_iv_is_cash_secured_put():
    # HIGH and MID IV: premium worth selling -> sell puts to get paid to go long.
    assert (
        select_structure(
            direction=Direction.BULLISH,
            structure_intent=StructureIntent.PREMIUM_CAPTURE,
            iv_regime=IVRegime.HIGH,
        )
        == "cash_secured_put"
    )
    assert (
        select_structure(
            direction=Direction.BULLISH,
            structure_intent=StructureIntent.PREMIUM_CAPTURE,
            iv_rank=42.0,  # MID
        )
        == "cash_secured_put"
    )


def test_bullish_premium_capture_low_iv_abstains():
    # LOW IV: thin premium -> abstain (silence -> equity path).
    assert (
        select_structure(
            direction=Direction.BULLISH,
            structure_intent=StructureIntent.PREMIUM_CAPTURE,
            iv_rank=10.0,  # LOW
        )
        is None
    )


def test_bearish_premium_capture_is_covered_call_and_neutral_is_wheel():
    assert (
        select_structure(
            direction=Direction.BEARISH,
            structure_intent=StructureIntent.PREMIUM_CAPTURE,
            iv_regime=IVRegime.HIGH,
        )
        == "covered_call"
    )
    assert (
        select_structure(
            direction=Direction.NEUTRAL,
            structure_intent=StructureIntent.PREMIUM_CAPTURE,
            iv_regime=IVRegime.MID,
        )
        == "wheel"
    )


# --------------------------------------------------------------------------- #
# Output domain: always a producible StrategyKind or None — NEVER naked / other
# --------------------------------------------------------------------------- #
def test_every_output_is_producible_kind_or_none():
    for direction, intent, regime in itertools.product(
        Direction, StructureIntent, IVRegime
    ):
        out = select_structure(
            direction=direction, structure_intent=intent, iv_regime=regime
        )
        assert out is None or out in _PRODUCIBLE, (direction, intent, regime, out)


def test_table_values_are_producible_kinds():
    # The literal table itself can only ever name producible (gate-admissible) kinds.
    for value in _STRUCTURE_TABLE.values():
        assert value in _PRODUCIBLE


def test_non_producible_intents_always_abstain(monkeypatch):
    # DEFINED_RISK_DEBIT and LONG_PREMIUM have no producer -> MUST abstain for every
    # (direction, regime) always.
    # DEFINED_RISK_CREDIT is producible (bull_put_spread / bear_call_spread) BUT only
    # when HERMES_QUANT_VERTICAL_SPREADS=1; with the flag OFF it abstains for every
    # combination (byte-identical to today).
    monkeypatch.delenv("HERMES_QUANT_VERTICAL_SPREADS", raising=False)
    for intent in (
        StructureIntent.DEFINED_RISK_CREDIT,
        StructureIntent.DEFINED_RISK_DEBIT,
        StructureIntent.LONG_PREMIUM,
    ):
        for direction, regime in itertools.product(Direction, IVRegime):
            assert (
                select_structure(
                    direction=direction, structure_intent=intent, iv_regime=regime
                )
                is None
            )


# --------------------------------------------------------------------------- #
# Silence-by-default on intent / IV input
# --------------------------------------------------------------------------- #
def test_none_intent_abstains():
    assert (
        select_structure(
            direction=Direction.BULLISH,
            structure_intent=StructureIntent.NONE,
            iv_regime=IVRegime.HIGH,
        )
        is None
    )
    assert (
        select_structure(
            direction=Direction.BULLISH, structure_intent=None, iv_regime=IVRegime.HIGH
        )
        is None
    )


def test_missing_iv_input_abstains():
    # No regime and no rank -> unknown vol regime -> abstain.
    assert (
        select_structure(
            direction=Direction.BULLISH,
            structure_intent=StructureIntent.PREMIUM_CAPTURE,
        )
        is None
    )
    # Out-of-range rank -> abstain.
    assert (
        select_structure(
            direction=Direction.BULLISH,
            structure_intent=StructureIntent.PREMIUM_CAPTURE,
            iv_rank=999.0,
        )
        is None
    )


def test_iv_regime_wins_over_iv_rank_when_both_supplied():
    # Explicit regime takes precedence over a (contradictory) rank.
    assert (
        select_structure(
            direction=Direction.BULLISH,
            structure_intent=StructureIntent.PREMIUM_CAPTURE,
            iv_rank=5.0,  # would be LOW -> abstain
            iv_regime=IVRegime.HIGH,  # but explicit HIGH wins -> CSP
        )
        == "cash_secured_put"
    )


# --------------------------------------------------------------------------- #
# Flag gating — default-OFF is byte-identical (abstain for EVERY plan)
# --------------------------------------------------------------------------- #
def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv(STRUCTURE_SELECT_FLAG, raising=False)
    assert structure_select_enabled() is False


@pytest.mark.parametrize("flagval", ["0", "", "true", "yes", "2", "on"])
def test_flag_fail_closed_only_literal_1_enables(monkeypatch, flagval):
    monkeypatch.setenv(STRUCTURE_SELECT_FLAG, flagval)
    assert structure_select_enabled() is False


def test_flag_one_enables(monkeypatch):
    monkeypatch.setenv(STRUCTURE_SELECT_FLAG, "1")
    assert structure_select_enabled() is True


def test_select_for_plan_flag_off_is_byte_identical_none(monkeypatch):
    # With the flag OFF (default), the consumer seam abstains for EVERY plan/regime
    # combination — i.e. the equity path is byte-identical (no structure ever).
    monkeypatch.delenv(STRUCTURE_SELECT_FLAG, raising=False)
    for rating, intent, rank in itertools.product(
        PortfolioRating,
        StructureIntent,
        (5.0, 35.0, 70.0, None),
    ):
        plan = _plan(rating, intent)
        assert select_structure_for_plan(plan, iv_rank=rank) is None


def test_select_for_plan_flag_on_selects(monkeypatch):
    monkeypatch.setenv(STRUCTURE_SELECT_FLAG, "1")
    plan = _plan(PortfolioRating.BUY, StructureIntent.PREMIUM_CAPTURE)
    assert select_structure_for_plan(plan, iv_rank=70.0) == "cash_secured_put"
    # absent intent on the plan -> abstain even with the flag on.
    plan_none = _plan(PortfolioRating.BUY, None)
    assert select_structure_for_plan(plan_none, iv_rank=70.0) is None


def test_select_for_plan_output_is_producible_or_none_flag_on(monkeypatch):
    monkeypatch.setenv(STRUCTURE_SELECT_FLAG, "1")
    for rating, intent, rank in itertools.product(
        PortfolioRating,
        StructureIntent,
        (5.0, 35.0, 70.0, None),
    ):
        plan = _plan(rating, intent)
        out = select_structure_for_plan(plan, iv_rank=rank)
        assert out is None or out in _PRODUCIBLE
