"""tests/unit/test_research_risk_tier.py — defensive RiskTier guard (B30).

The research plane must NEVER claim live-trading execution authority. These tests
pin the keyword guard at the ``HypothesisRegistry.register()`` seam:

  - A live-authority-implying hypothesis is FLAGGED + downgraded (default) or
    refused (opt-in hard-block flag).
  - A normal research hypothesis passes unchanged and is annotated research_only.
  - The classifier is fail-closed (empty/ambiguous -> research_only) and never
    returns a tier that grants authority.
"""

from __future__ import annotations

import pytest

from hermes_quant.research.hypothesis import (
    Hypothesis,
    HypothesisRegistry,
    ResearchAuthorityViolation,
)
from hermes_quant.research.risk_tier import (
    RiskTier,
    block_on_flag_enabled,
    classify_risk_tier,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minimal_hypothesis(**overrides) -> Hypothesis:
    defaults = dict(
        author="test-agent",
        claim="Sentiment analyst increases Sharpe by >=0.10",
        null_hypothesis="Sentiment makes no difference (alpha <= 0)",
        success_criteria=["sharpe >= 0.10"],
        falsification_criteria=["sharpe < 0.0"],
        experiment_design="Walk-forward backtest over 90 days",
        duration_target_days=90,
        scope={"universe": ["AAPL"], "env": "paper"},
    )
    defaults.update(overrides)
    return Hypothesis(**defaults)


@pytest.fixture
def registry(tmp_path):
    return HypothesisRegistry(path=tmp_path / "hypotheses.jsonl")


@pytest.fixture(autouse=True)
def _clear_block_flag(monkeypatch):
    """Default-OFF: ensure the opt-in hard-block flag is unset unless a test sets it."""
    monkeypatch.delenv("HERMES_QUANT_RESEARCH_RISK_TIER_BLOCK", raising=False)


# ---------------------------------------------------------------------------
# Pure classifier
# ---------------------------------------------------------------------------


def test_classify_normal_research_is_research_only():
    r = classify_risk_tier(
        "Adding a sentiment analyst increases Sharpe over a 6mo backtest",
        "Sentiment makes no difference (alpha <= 0)",
        "Walk-forward backtest; paper simulation only",
    )
    assert r.tier is RiskTier.RESEARCH_ONLY
    assert r.is_research_only
    assert not r.is_flagged
    assert r.matched == ()


def test_classify_live_authority_is_flagged():
    r = classify_risk_tier(
        "This strategy should live trade with real money via the broker",
    )
    assert r.tier is RiskTier.FLAGGED
    assert r.is_flagged
    assert "live trade" in r.matched
    assert "real money" in r.matched


def test_classify_bypass_risk_gate_is_flagged():
    r = classify_risk_tier("We can bypass the risk gate to place an order faster")
    assert r.is_flagged
    assert "bypass the risk gate" in r.matched
    assert "place an order" in r.matched


def test_classify_empty_is_research_only_fail_closed():
    assert classify_risk_tier().tier is RiskTier.RESEARCH_ONLY
    assert classify_risk_tier("", None, "   ").tier is RiskTier.RESEARCH_ONLY


def test_classify_word_boundary_no_false_positive():
    # "alive" / "delivery" must NOT trip the "live" phrases; "traded" must NOT
    # trip "trade live". Benign research prose stays research_only.
    r = classify_risk_tier(
        "The strategy stayed alive through delivery of traded volume",
        "No effect",
        "Backtest only",
    )
    assert r.tier is RiskTier.RESEARCH_ONLY


def test_classify_is_deterministic_sorted():
    r = classify_risk_tier("go live and real money and execute live now")
    assert list(r.matched) == sorted(r.matched)


def test_risk_tier_has_no_live_member():
    # FAIL-CLOSED by construction: the guard can never assign live authority.
    assert {t.value for t in RiskTier} == {"research_only", "flagged"}


def test_block_flag_default_off(monkeypatch):
    monkeypatch.delenv("HERMES_QUANT_RESEARCH_RISK_TIER_BLOCK", raising=False)
    assert block_on_flag_enabled() is False
    monkeypatch.setenv("HERMES_QUANT_RESEARCH_RISK_TIER_BLOCK", "1")
    assert block_on_flag_enabled() is True


# ---------------------------------------------------------------------------
# register() guard — default (downgrade + annotate) path
# ---------------------------------------------------------------------------


def test_normal_hypothesis_registers_unchanged(registry):
    """Behaviour-preserving: a normal hypothesis still registers + round-trips."""
    h = _minimal_hypothesis()
    hyp_id = registry.register(h)
    recovered = registry.read(hyp_id)
    assert recovered is not None
    assert recovered.claim == h.claim
    assert recovered.status == "open"


def test_normal_hypothesis_annotated_research_only(registry):
    hyp_id = registry.register(_minimal_hypothesis())
    rows = [r for r in registry._iter_rows() if r.get("hypothesis_id") == hyp_id]
    reg_row = next(r for r in rows if r.get("kind") == "hypothesis")
    assert reg_row["risk_tier"] == "research_only"
    assert reg_row["risk_tier_flagged"] == []


def test_live_authority_hypothesis_is_flagged_and_downgraded(registry):
    """A live-authority-implying hypothesis is flagged (matched phrases recorded)
    but downgraded to research_only — it still registers, never grants authority."""
    h = _minimal_hypothesis(
        claim="Once validated, route orders to live trading with real capital",
    )
    hyp_id = registry.register(h)  # default flag OFF -> does not raise
    reg_row = next(
        r
        for r in registry._iter_rows()
        if r.get("kind") == "hypothesis" and r.get("hypothesis_id") == hyp_id
    )
    # Downgraded to research_only (never live) despite the flagged text...
    assert reg_row["risk_tier"] == "research_only"
    # ...with the offending phrases preserved for audit.
    assert "live trading" in reg_row["risk_tier_flagged"]
    assert "real capital" in reg_row["risk_tier_flagged"]
    # And it round-trips as a normal research_only hypothesis.
    assert registry.read(hyp_id).status == "open"


# ---------------------------------------------------------------------------
# register() guard — opt-in hard-block path
# ---------------------------------------------------------------------------


def test_block_flag_refuses_flagged_hypothesis(registry, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_RESEARCH_RISK_TIER_BLOCK", "1")
    h = _minimal_hypothesis(
        claim="Deploy to production and trade live without the risk gate",
    )
    with pytest.raises(ResearchAuthorityViolation):
        registry.register(h)


def test_block_flag_allows_normal_hypothesis(registry, monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_RESEARCH_RISK_TIER_BLOCK", "1")
    # Even with the hard-block flag set, a clean research hypothesis registers.
    hyp_id = registry.register(_minimal_hypothesis())
    assert registry.read(hyp_id) is not None
