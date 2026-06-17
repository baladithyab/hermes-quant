"""tests/shadow/test_rules.py — Unit tests for the 5 shadow rules.

Wave 8b / ADR-0049.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hermes_quant.shadow.rules import (
    AlwaysFollowAdvisorRule,
    InverseConsensusRule,
    SemanticOnlyRule,
    SentimentOnlyRule,
    ShadowDecision,
    TrendFollowingRule,
    default_rules,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2025, 6, 10, 14, 30, 0, tzinfo=timezone.utc)


def _gate_event(
    direction: str = "buy",
    ticker: str = "AAPL",
    vote_share: float = 0.7,
    analysts: list[str] | None = None,
    ta_direction: str | None = None,
    event_id: str = "evt-001",
    extra_payload: dict | None = None,
) -> dict:
    """Build a synthetic gate_approval audit event dict."""
    if analysts is None:
        analysts = ["semantic", "sentiment", "classical_ta"]
    payload: dict = {
        "ticker": ticker,
        "advisor_result": {"direction": direction, "confidence": 0.8},
        "signal_provenance": {
            "advisor_direction": direction,
            "vote_share": vote_share,
            "contributing_analysts": analysts,
        },
    }
    if ta_direction is not None:
        payload["signal_provenance"]["classical_ta_direction"] = ta_direction

    if extra_payload:
        payload.update(extra_payload)

    return {
        "event_id": event_id,
        "kind": "gate_approval",
        "asof": _NOW.isoformat(),
        "source": "test",
        "payload": payload,
    }


def _non_approval_event(kind: str = "fill") -> dict:
    return {
        "event_id": "evt-nongate",
        "kind": kind,
        "asof": _NOW.isoformat(),
        "source": "test",
        "payload": {"ticker": "AAPL", "direction": "buy"},
    }


# ===========================================================================
# AlwaysFollowAdvisorRule
# ===========================================================================


class TestAlwaysFollowAdvisorRule:
    rule = AlwaysFollowAdvisorRule()

    def test_fires_on_buy(self):
        decision = self.rule.evaluate(_gate_event(direction="buy"))
        assert decision is not None
        assert decision.action == "buy"
        assert decision.size_fraction == pytest.approx(0.10)
        assert decision.rule_name == "always_follow_advisor"

    def test_fires_on_sell(self):
        decision = self.rule.evaluate(_gate_event(direction="sell"))
        assert decision is not None
        assert decision.action == "sell"

    def test_returns_none_on_non_approval(self):
        assert self.rule.evaluate(_non_approval_event("fill")) is None
        assert self.rule.evaluate(_non_approval_event("gate_rejection")) is None

    def test_returns_none_when_no_direction(self):
        event = _gate_event()
        del event["payload"]["advisor_result"]
        del event["payload"]["signal_provenance"]["advisor_direction"]
        assert self.rule.evaluate(event) is None

    def test_ticker_extracted(self):
        decision = self.rule.evaluate(_gate_event(ticker="TSLA"))
        assert decision is not None
        assert decision.ticker == "TSLA"

    def test_decision_type(self):
        decision = self.rule.evaluate(_gate_event())
        assert isinstance(decision, ShadowDecision)

    def test_name_and_description(self):
        assert self.rule.name == "always_follow_advisor"
        assert "advisor" in self.rule.description.lower()


# ===========================================================================
# InverseConsensusRule
# ===========================================================================


class TestInverseConsensusRule:
    rule = InverseConsensusRule()

    def test_inverts_buy_to_sell(self):
        decision = self.rule.evaluate(_gate_event(direction="buy"))
        assert decision is not None
        assert decision.action == "sell"

    def test_inverts_sell_to_buy(self):
        decision = self.rule.evaluate(_gate_event(direction="sell"))
        assert decision is not None
        assert decision.action == "buy"

    def test_returns_none_on_non_approval(self):
        assert self.rule.evaluate(_non_approval_event()) is None

    def test_size_fraction(self):
        decision = self.rule.evaluate(_gate_event())
        assert decision is not None
        assert decision.size_fraction == pytest.approx(0.10)

    def test_rule_name(self):
        decision = self.rule.evaluate(_gate_event())
        assert decision.rule_name == "inverse_consensus"  # type: ignore[union-attr]


# ===========================================================================
# SemanticOnlyRule
# ===========================================================================


class TestSemanticOnlyRule:
    rule = SemanticOnlyRule()

    def test_fires_when_semantic_voted(self):
        event = _gate_event(analysts=["semantic", "classical_ta"])
        decision = self.rule.evaluate(event)
        assert decision is not None
        assert decision.action == "buy"

    def test_returns_none_when_semantic_absent(self):
        event = _gate_event(analysts=["sentiment", "classical_ta"])
        assert self.rule.evaluate(event) is None

    def test_returns_none_on_non_approval(self):
        assert self.rule.evaluate(_non_approval_event()) is None

    def test_size_fraction(self):
        event = _gate_event(analysts=["semantic"])
        decision = self.rule.evaluate(event)
        assert decision is not None
        assert decision.size_fraction == pytest.approx(0.10)

    def test_rule_name(self):
        event = _gate_event(analysts=["semantic"])
        decision = self.rule.evaluate(event)
        assert decision.rule_name == "semantic_only"  # type: ignore[union-attr]

    def test_semantic_analyst_alias(self):
        """semantic_analyst variant is also accepted."""
        event = _gate_event(analysts=["semantic_analyst"])
        assert self.rule.evaluate(event) is not None


# ===========================================================================
# SentimentOnlyRule
# ===========================================================================


class TestSentimentOnlyRule:
    rule = SentimentOnlyRule()

    def test_fires_when_sentiment_voted(self):
        event = _gate_event(analysts=["sentiment", "classical_ta"])
        decision = self.rule.evaluate(event)
        assert decision is not None
        assert decision.size_fraction == pytest.approx(0.10)

    def test_returns_none_when_sentiment_absent(self):
        event = _gate_event(analysts=["semantic", "classical_ta"])
        assert self.rule.evaluate(event) is None

    def test_returns_none_on_non_approval(self):
        assert self.rule.evaluate(_non_approval_event()) is None

    def test_uses_sentiment_specific_direction(self):
        """Sentiment analyst's own direction takes priority over advisor."""
        event = _gate_event(direction="buy", analysts=["sentiment"])
        event["payload"]["analyst_votes"] = {
            "sentiment": {"direction": "sell", "confidence": 0.9}
        }
        decision = self.rule.evaluate(event)
        assert decision is not None
        assert decision.action == "sell"  # follows sentiment's own vote

    def test_fallback_to_advisor_direction(self):
        """Falls back to advisor direction when no specific sentiment vote direction."""
        event = _gate_event(direction="sell", analysts=["sentiment"])
        decision = self.rule.evaluate(event)
        assert decision is not None
        assert decision.action == "sell"

    def test_rule_name(self):
        event = _gate_event(analysts=["sentiment"])
        decision = self.rule.evaluate(event)
        assert decision.rule_name == "sentiment_only"  # type: ignore[union-attr]


# ===========================================================================
# TrendFollowingRule
# ===========================================================================


class TestTrendFollowingRule:
    rule = TrendFollowingRule()

    def test_fires_on_full_confluence(self):
        event = _gate_event(direction="buy", vote_share=0.75, ta_direction="buy")
        decision = self.rule.evaluate(event)
        assert decision is not None
        assert decision.action == "buy"
        assert decision.size_fraction == pytest.approx(0.15)

    def test_returns_none_when_ta_disagrees(self):
        event = _gate_event(direction="buy", vote_share=0.75, ta_direction="sell")
        assert self.rule.evaluate(event) is None

    def test_returns_none_when_vote_share_too_low(self):
        event = _gate_event(direction="buy", vote_share=0.55, ta_direction="buy")
        assert self.rule.evaluate(event) is None

    def test_returns_none_when_no_ta_direction(self):
        event = _gate_event(direction="buy", vote_share=0.80)
        assert self.rule.evaluate(event) is None

    def test_returns_none_on_non_approval(self):
        assert self.rule.evaluate(_non_approval_event()) is None

    def test_vote_share_exactly_at_threshold_fails(self):
        """vote_share == 0.6 is NOT enough (must be strictly > 0.6)."""
        event = _gate_event(direction="buy", vote_share=0.60, ta_direction="buy")
        assert self.rule.evaluate(event) is None

    def test_rule_name(self):
        event = _gate_event(direction="buy", vote_share=0.8, ta_direction="buy")
        decision = self.rule.evaluate(event)
        assert decision.rule_name == "trend_following"  # type: ignore[union-attr]


# ===========================================================================
# default_rules
# ===========================================================================


def _real_gate_emitter_event(direction_int: int, ticker: str = "AAPL") -> dict:
    """Build the EXACT payload the canonical gate emitter writes (risk/gate.py +
    pdr_core/gate.py _audit_approval): flat ``direction`` as a SIGNED INT, ``asset``
    (not ``ticker``), ``signal_provenance`` WITHOUT an ``advisor_direction`` string and
    WITHOUT ``advisor_result``. This is what scripts/shadow-replay-daily.py actually
    feeds the rules in production — the prior string-only helper masked the ar122 bug.
    """
    return {
        "event_id": "evt-real",
        "kind": "gate_approval",
        "asof": _NOW.isoformat(),
        "source": "risk.gate",
        "payload": {
            "asset": ticker,
            "direction": direction_int,  # int(signal.direction): 1 / -1 / 0
            "magnitude": 0.05,
            "confidence": 0.8,
            "target_position_pct": 0.10,
            "reason": "ok",
            "asof": _NOW.isoformat(),
            "signal_provenance": {
                "vote_share": 0.7,
                "contributing_analysts": ["semantic", "sentiment", "classical_ta"],
            },
        },
    }


class TestAr122RealGateEmitterIntDirection:
    """ar122: the shadow rules must act on the REAL gate_approval shape (flat int
    direction), not only the string fixtures the other tests use. RED before the fix:
    every rule returned None on the int payload → the ADR-0049 shadow eval ledger was
    vacuous in production.
    """

    def test_always_follow_acts_on_int_long(self):
        d = AlwaysFollowAdvisorRule().evaluate(_real_gate_emitter_event(1))
        assert d is not None, (
            "ar122: AlwaysFollowAdvisorRule returned None on the REAL gate payload "
            "(flat direction=1 int) — the shadow eval is vacuous in prod"
        )
        assert d.action == "buy"

    def test_always_follow_acts_on_int_short(self):
        d = AlwaysFollowAdvisorRule().evaluate(_real_gate_emitter_event(-1))
        assert d is not None and d.action == "sell"

    def test_inverse_consensus_acts_on_int(self):
        d = InverseConsensusRule().evaluate(_real_gate_emitter_event(1))
        assert d is not None, "ar122: InverseConsensusRule vacuous on the real int payload"
        assert d.action == "sell"  # inverse of a long

    def test_flat_int_direction_is_none(self):
        # direction=0 (flat) is genuinely undeterminable → None (not a spurious fire).
        assert AlwaysFollowAdvisorRule().evaluate(_real_gate_emitter_event(0)) is None

    def test_at_least_one_default_rule_fires_on_real_payload(self):
        """The whole point of ADR-0049: at least one shadow rule records a decision
        from a real gate_approval. Pre-ar122 ALL five returned None."""
        ev = _real_gate_emitter_event(1)
        decisions = [r.evaluate(ev) for r in default_rules()]
        assert any(d is not None for d in decisions), (
            "ar122: NO default shadow rule acted on the real int-direction gate event "
            "— the ADR-0049 eval ledger records zero decisions in production"
        )


class TestDefaultRules:
    def test_returns_five_rules(self):
        rules = default_rules()
        assert len(rules) == 5

    def test_all_names_unique(self):
        names = [r.name for r in default_rules()]
        assert len(names) == len(set(names))

    def test_all_have_description(self):
        for rule in default_rules():
            assert isinstance(rule.description, str)
            assert len(rule.description) > 10
