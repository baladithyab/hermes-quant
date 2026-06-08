"""Tests for hermes_quant.agents.trader + hermes_quant.agents.structured_output.

ADR-0044: Wave 2 coverage.

Test categories:
  1. TraderProposal validation (field constraints, cross-field stop/entry)
  2. TraderNode rating → size_fraction mapping (all 5 tiers)
  3. TraderNode price-level derivation
  4. TraderNode graceful fallback (missing fields, bad recommendation)
  5. structured_output.bind_structured provider routing
  6. invoke_structured_or_freetext mock-client integration
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from hermes_quant.agents.trader import (
    TraderAction,
    TraderNode,
    TraderProposal,
    _FALLBACK_CONF,
    _FALLBACK_SIZE,
    _RATING_SIZE_FRACTION,
    _rating_to_action,
)
from hermes_quant.agents.structured_output import (
    bind_structured,
    invoke_structured_or_freetext,
    _detect_provider,
)


# ==========================================================================
# 1. TraderProposal — field validation
# ==========================================================================


class TestTraderProposalFieldValidation:
    """Pydantic constraint coverage for every Field annotation."""

    def _valid_kwargs(self, **overrides) -> dict:
        """Minimal valid TraderProposal kwargs."""
        base = dict(
            action=TraderAction.BUY,
            size_fraction=0.20,
            entry_price=100.0,
            stop_loss=96.0,
            target_price=108.0,
            time_horizon_days=30,
            confidence=0.75,
            rationale="The research manager issued a Buy. Entry near current close. Stop below 2×ATR.",
            warning_message=None,
        )
        base.update(overrides)
        return base

    def test_valid_buy_proposal(self):
        p = TraderProposal(**self._valid_kwargs())
        assert p.action == TraderAction.BUY
        assert p.size_fraction == 0.20
        assert p.stop_loss is not None and p.entry_price is not None
        assert p.stop_loss < p.entry_price  # BUY constraint

    def test_valid_sell_proposal(self):
        p = TraderProposal(**self._valid_kwargs(
            action=TraderAction.SELL,
            stop_loss=104.0,  # > entry for SELL
        ))
        assert p.action == TraderAction.SELL
        assert p.stop_loss is not None and p.entry_price is not None
        assert p.stop_loss > p.entry_price

    def test_valid_hold_proposal(self):
        p = TraderProposal(**self._valid_kwargs(
            action=TraderAction.HOLD,
            size_fraction=0.0,
            stop_loss=95.0,  # HOLD: stop < entry is allowed
        ))
        assert p.action == TraderAction.HOLD

    def test_size_fraction_ge_zero(self):
        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            TraderProposal(**self._valid_kwargs(size_fraction=-0.01))

    def test_size_fraction_le_one(self):
        with pytest.raises(ValidationError, match="less than or equal to 1"):
            TraderProposal(**self._valid_kwargs(size_fraction=1.001))

    def test_size_fraction_zero_is_valid(self):
        p = TraderProposal(**self._valid_kwargs(
            action=TraderAction.HOLD,
            size_fraction=0.0,
            stop_loss=None,
        ))
        assert p.size_fraction == 0.0

    def test_size_fraction_one_is_valid(self):
        p = TraderProposal(**self._valid_kwargs(size_fraction=1.0))
        assert p.size_fraction == 1.0

    def test_entry_price_must_be_positive(self):
        with pytest.raises(ValidationError, match="greater than 0"):
            TraderProposal(**self._valid_kwargs(entry_price=0.0))

    def test_entry_price_negative_rejected(self):
        with pytest.raises(ValidationError, match="greater than 0"):
            TraderProposal(**self._valid_kwargs(entry_price=-1.0))

    def test_entry_price_none_allowed(self):
        p = TraderProposal(**self._valid_kwargs(entry_price=None, stop_loss=None))
        assert p.entry_price is None

    def test_stop_loss_must_be_positive(self):
        with pytest.raises(ValidationError, match="greater than 0"):
            TraderProposal(**self._valid_kwargs(stop_loss=0.0))

    def test_stop_loss_none_allowed(self):
        p = TraderProposal(**self._valid_kwargs(stop_loss=None))
        assert p.stop_loss is None

    def test_time_horizon_ge_1(self):
        with pytest.raises(ValidationError, match="greater than or equal to 1"):
            TraderProposal(**self._valid_kwargs(time_horizon_days=0))

    def test_time_horizon_le_365(self):
        with pytest.raises(ValidationError, match="less than or equal to 365"):
            TraderProposal(**self._valid_kwargs(time_horizon_days=366))

    def test_time_horizon_boundary_1(self):
        p = TraderProposal(**self._valid_kwargs(time_horizon_days=1))
        assert p.time_horizon_days == 1

    def test_time_horizon_boundary_365(self):
        p = TraderProposal(**self._valid_kwargs(time_horizon_days=365))
        assert p.time_horizon_days == 365

    def test_time_horizon_none_allowed(self):
        p = TraderProposal(**self._valid_kwargs(time_horizon_days=None))
        assert p.time_horizon_days is None

    def test_confidence_ge_zero(self):
        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            TraderProposal(**self._valid_kwargs(confidence=-0.01))

    def test_confidence_le_one(self):
        with pytest.raises(ValidationError, match="less than or equal to 1"):
            TraderProposal(**self._valid_kwargs(confidence=1.001))

    def test_rationale_max_length(self):
        with pytest.raises(ValidationError, match="at most 2048"):
            TraderProposal(**self._valid_kwargs(rationale="x" * 2049))

    def test_rationale_at_max_length(self):
        p = TraderProposal(**self._valid_kwargs(rationale="x" * 2048))
        assert len(p.rationale) == 2048

    def test_rationale_empty_rejected(self):
        with pytest.raises(ValidationError, match="at least 1"):
            TraderProposal(**self._valid_kwargs(rationale=""))

    def test_action_enum_coercion_from_string(self):
        p = TraderProposal(**self._valid_kwargs(action="BUY"))
        assert p.action == TraderAction.BUY

    def test_action_enum_coercion_sell(self):
        p = TraderProposal(**self._valid_kwargs(action="SELL", stop_loss=104.0))
        assert p.action == TraderAction.SELL

    def test_action_enum_coercion_hold(self):
        p = TraderProposal(**self._valid_kwargs(action="HOLD", stop_loss=None))
        assert p.action == TraderAction.HOLD

    def test_extra_field_forbidden(self):
        with pytest.raises(ValidationError, match="extra_field"):
            TraderProposal(**self._valid_kwargs(extra_field="boom"))


class TestTraderProposalCrossFieldValidation:
    """Stop vs entry cross-field constraints."""

    def test_buy_stop_must_be_below_entry(self):
        with pytest.raises(ValidationError, match="stop_loss.*must be.*entry_price|BUY action stop"):
            TraderProposal(
                action=TraderAction.BUY,
                size_fraction=0.20,
                entry_price=100.0,
                stop_loss=101.0,  # above entry → invalid for BUY
                confidence=0.7,
                rationale="test",
            )

    def test_buy_stop_equal_to_entry_rejected(self):
        with pytest.raises(ValidationError):
            TraderProposal(
                action=TraderAction.BUY,
                size_fraction=0.20,
                entry_price=100.0,
                stop_loss=100.0,  # equal → invalid
                confidence=0.7,
                rationale="test",
            )

    def test_sell_stop_must_be_above_entry(self):
        with pytest.raises(ValidationError, match="stop_loss.*must be.*entry_price|SELL action stop"):
            TraderProposal(
                action=TraderAction.SELL,
                size_fraction=0.20,
                entry_price=100.0,
                stop_loss=99.0,  # below entry → invalid for SELL
                confidence=0.7,
                rationale="test",
            )

    def test_sell_stop_equal_to_entry_rejected(self):
        with pytest.raises(ValidationError):
            TraderProposal(
                action=TraderAction.SELL,
                size_fraction=0.20,
                entry_price=100.0,
                stop_loss=100.0,  # equal → invalid
                confidence=0.7,
                rationale="test",
            )

    def test_hold_stop_any_valid(self):
        """HOLD action: no directional stop constraint."""
        p = TraderProposal(
            action=TraderAction.HOLD,
            size_fraction=0.0,
            entry_price=100.0,
            stop_loss=95.0,  # below entry, fine for HOLD
            confidence=0.5,
            rationale="hold with stop",
        )
        assert p.stop_loss == 95.0

    def test_no_entry_no_stop_validation_skipped(self):
        """Without both prices, cross-field validation is skipped."""
        p = TraderProposal(
            action=TraderAction.BUY,
            size_fraction=0.10,
            entry_price=None,
            stop_loss=None,
            confidence=0.6,
            rationale="prices unavailable",
        )
        assert p.entry_price is None
        assert p.stop_loss is None


# ==========================================================================
# 2. TraderNode — rating → size_fraction mapping
# ==========================================================================


class TestTraderNodeSizingLadder:
    """All 5 tiers must map to the expected size_fraction."""

    def _plan(self, recommendation: str) -> dict:
        return {
            "recommendation": recommendation,
            "confidence": 0.70,
            "rationale": "Test rationale from research manager judge.",
            "strategic_actions": "Enter near current market price.",
            "horizon_emphasis": None,
        }

    @pytest.mark.parametrize("rating,expected_size", [
        ("Buy", 0.20),
        ("Overweight", 0.10),
        ("Hold", 0.00),
        ("Underweight", 0.10),  # abs() of -0.10
        ("Sell", 0.20),         # abs() of -0.20
    ])
    def test_rating_to_size_fraction(self, rating: str, expected_size: float):
        node = TraderNode()
        proposal = node(self._plan(rating))
        assert proposal.size_fraction == expected_size
        assert proposal.warning_message is None

    @pytest.mark.parametrize("rating,expected_action", [
        ("Buy", TraderAction.BUY),
        ("Overweight", TraderAction.BUY),
        ("Hold", TraderAction.HOLD),
        ("Underweight", TraderAction.SELL),
        ("Sell", TraderAction.SELL),
    ])
    def test_rating_to_action(self, rating: str, expected_action: TraderAction):
        assert _rating_to_action(rating) == expected_action

    def test_all_five_ratings_covered_in_map(self):
        assert set(_RATING_SIZE_FRACTION.keys()) == {
            "Buy", "Overweight", "Hold", "Underweight", "Sell"
        }

    def test_buy_confidence_passed_through(self):
        node = TraderNode()
        plan = self._plan("Buy")
        plan["confidence"] = 0.88
        proposal = node(plan)
        assert proposal.confidence == pytest.approx(0.88)

    def test_hold_size_is_zero(self):
        node = TraderNode()
        proposal = node(self._plan("Hold"))
        assert proposal.size_fraction == 0.0
        assert proposal.action == TraderAction.HOLD


# ==========================================================================
# 3. TraderNode — price level derivation
# ==========================================================================


def _signal_with_price(close: float, atr_relative: float) -> dict:
    """Helper: mock advisor signal dict with price data."""
    return {
        "direction": 1,
        "confidence": 0.75,
        "magnitude": 0.02,
        "metadata": {
            "last_close": close,
            "atr_relative": atr_relative,
        },
        "data_quality": {"bars_received": 252},
    }


class TestTraderNodePriceLevels:
    def _plan(self, rating: str = "Buy") -> dict:
        return {
            "recommendation": rating,
            "confidence": 0.70,
            "rationale": "Rationale from research manager.",
            "strategic_actions": "Enter at market.",
        }

    def test_buy_stop_below_entry(self):
        node = TraderNode(atr_multiplier=2.0)
        signal = _signal_with_price(close=100.0, atr_relative=0.02)  # ATR=$2
        proposal = node(self._plan("Buy"), signal)
        # stop = 100 - 2*2 = 96
        assert proposal.entry_price == pytest.approx(100.0)
        assert proposal.stop_loss is not None
        assert proposal.stop_loss == pytest.approx(96.0)
        assert proposal.stop_loss < proposal.entry_price  # type: ignore[operator]

    def test_buy_target_above_entry(self):
        node = TraderNode(atr_multiplier=2.0)
        signal = _signal_with_price(close=100.0, atr_relative=0.02)
        proposal = node(self._plan("Buy"), signal)
        # target = 100 + 2*2 = 104
        assert proposal.target_price is not None
        assert proposal.target_price == pytest.approx(104.0)
        assert proposal.target_price > proposal.entry_price  # type: ignore[operator]

    def test_sell_stop_above_entry(self):
        node = TraderNode(atr_multiplier=2.0)
        signal = _signal_with_price(close=100.0, atr_relative=0.02)
        proposal = node(self._plan("Sell"), signal)
        # stop = 100 + 2*2 = 104
        assert proposal.stop_loss is not None
        assert proposal.stop_loss == pytest.approx(104.0)
        assert proposal.stop_loss > proposal.entry_price  # type: ignore[operator]

    def test_sell_target_below_entry(self):
        node = TraderNode(atr_multiplier=2.0)
        signal = _signal_with_price(close=100.0, atr_relative=0.02)
        proposal = node(self._plan("Sell"), signal)
        # target = 100 - 4 = 96
        assert proposal.target_price == pytest.approx(96.0)

    def test_hold_no_stop(self):
        node = TraderNode()
        signal = _signal_with_price(close=100.0, atr_relative=0.02)
        proposal = node(self._plan("Hold"), signal)
        # HOLD: no directional stop/target from ATR
        assert proposal.stop_loss is None
        assert proposal.target_price is None

    def test_missing_price_produces_none(self):
        node = TraderNode()
        proposal = node(self._plan("Buy"), {})
        assert proposal.entry_price is None
        assert proposal.stop_loss is None

    def test_zero_price_produces_none(self):
        node = TraderNode()
        signal = {"metadata": {"last_close": 0.0, "atr_relative": 0.02}}
        proposal = node(self._plan("Buy"), signal)
        assert proposal.entry_price is None  # 0 is not positive

    def test_nan_atr_falls_back_to_pct_stop(self):
        # Deep-review 2026-06-07 root-cause fix: ATR unusable (NaN) but we HAVE a
        # price -> the trader now places a DEFAULT PERCENTAGE stop rather than
        # leaving stop_loss=None (the June-4 ASTS stopless-loss bug). Previously
        # this asserted stop_loss is None — that encoded the buggy behavior.
        node = TraderNode(default_stop_pct=0.08)
        signal = {"metadata": {"last_close": 100.0, "atr_relative": float("nan")}}
        proposal = node(self._plan("Buy"), signal)
        assert proposal.entry_price == pytest.approx(100.0)
        assert proposal.stop_loss is not None
        # 8% below 100 for a BUY = 92
        assert proposal.stop_loss == pytest.approx(92.0)
        assert proposal.stop_loss < proposal.entry_price  # type: ignore[operator]

    def test_missing_atr_falls_back_to_pct_stop_buy(self):
        node = TraderNode(default_stop_pct=0.08)
        signal = {"metadata": {"last_close": 50.0}}  # no atr_relative at all
        proposal = node(self._plan("Buy"), signal)
        assert proposal.stop_loss == pytest.approx(46.0)  # 50 * (1 - 0.08)
        assert proposal.target_price == pytest.approx(54.0)

    def test_missing_atr_falls_back_to_pct_stop_sell(self):
        node = TraderNode(default_stop_pct=0.10)
        signal = {"metadata": {"last_close": 50.0}}
        proposal = node(self._plan("Sell"), signal)
        # SELL: stop ABOVE entry = 55; target below = 45
        assert proposal.stop_loss == pytest.approx(55.0)
        assert proposal.stop_loss > proposal.entry_price  # type: ignore[operator]
        assert proposal.target_price == pytest.approx(45.0)

    def test_no_price_still_produces_none_stop(self):
        # The fallback needs a PRICE; with no price at all, stop stays None
        # (can't anchor a percentage to nothing). HOLD also stays stopless.
        node = TraderNode()
        assert node(self._plan("Buy"), {}).stop_loss is None
        signal = {"metadata": {"last_close": 100.0, "atr_relative": float("nan")}}
        assert node(self._plan("Hold"), signal).stop_loss is None


# ==========================================================================
# 4. TraderNode — graceful fallback
# ==========================================================================


class TestTraderNodeGracefulFallback:
    """TraderNode must never raise; returns conservative defaults on bad input."""

    def test_missing_recommendation_triggers_fallback(self):
        node = TraderNode()
        proposal = node({})
        assert proposal.warning_message is not None
        assert proposal.action == TraderAction.HOLD
        assert proposal.size_fraction == _FALLBACK_SIZE
        assert proposal.confidence == _FALLBACK_CONF

    def test_invalid_recommendation_triggers_fallback(self):
        node = TraderNode()
        proposal = node({"recommendation": "Strong Buy", "confidence": 0.9,
                         "rationale": "test", "strategic_actions": "buy"})
        assert proposal.warning_message is not None
        assert proposal.size_fraction == _FALLBACK_SIZE

    def test_missing_confidence_triggers_fallback(self):
        node = TraderNode()
        proposal = node({"recommendation": "Buy", "rationale": "test",
                         "strategic_actions": "buy"})
        assert proposal.warning_message is not None

    def test_empty_rationale_triggers_fallback(self):
        node = TraderNode()
        proposal = node({"recommendation": "Buy", "confidence": 0.7,
                         "rationale": "", "strategic_actions": ""})
        assert proposal.warning_message is not None

    def test_fallback_proposal_is_valid_pydantic(self):
        """Even the fallback must be a valid TraderProposal."""
        node = TraderNode()
        proposal = node(None)  # type: ignore[arg-type]
        # Should not raise
        assert isinstance(proposal, TraderProposal)

    def test_fallback_rationale_mentions_fallback(self):
        node = TraderNode()
        proposal = node({})
        assert "fallback" in proposal.rationale.lower() or "fallback" in (proposal.warning_message or "").lower()

    def test_fallback_time_horizon_is_none(self):
        node = TraderNode()
        proposal = node({})
        assert proposal.time_horizon_days is None


# ==========================================================================
# 5. bind_structured — provider routing
# ==========================================================================


class TestBindStructured:
    """bind_structured must return expected keys for each provider."""

    def test_openai_returns_response_format(self):
        result = bind_structured("openai/gpt-4o", TraderProposal)
        assert "response_format" in result
        rf = result["response_format"]
        assert rf["type"] == "json_schema"
        assert "json_schema" in rf
        assert rf["json_schema"]["name"] == "TraderProposal"
        assert rf["json_schema"]["strict"] is True
        assert "schema" in rf["json_schema"]

    def test_xai_same_as_openai(self):
        openai_result = bind_structured("openai/gpt-4o", TraderProposal)
        xai_result = bind_structured("xai/grok-3-mini", TraderProposal)
        # Both use the json_schema path
        assert openai_result["response_format"]["type"] == xai_result["response_format"]["type"]

    def test_anthropic_returns_tools(self):
        result = bind_structured("anthropic/claude-3-5-haiku-20241022", TraderProposal)
        assert "tools" in result
        assert "tool_choice" in result
        assert len(result["tools"]) == 1
        tool = result["tools"][0]
        assert tool["name"] == "TraderProposal"
        assert "input_schema" in tool
        assert result["tool_choice"] == {"type": "any"}

    def test_google_returns_response_schema(self):
        result = bind_structured("google/gemini-2.0-flash", TraderProposal)
        assert "response_schema" in result
        assert "response_mime_type" in result
        assert result["response_mime_type"] == "application/json"

    def test_unknown_provider_returns_empty_dict(self):
        result = bind_structured("ollama/llama3", TraderProposal)
        assert result == {}

    def test_empty_model_id_returns_empty_dict(self):
        result = bind_structured("", TraderProposal)
        assert result == {}

    def test_detect_provider_openai(self):
        assert _detect_provider("openai/gpt-4o") == "openai"

    def test_detect_provider_xai(self):
        assert _detect_provider("xai/grok-3-mini") == "openai"

    def test_detect_provider_anthropic(self):
        assert _detect_provider("anthropic/claude-3-opus-20240229") == "anthropic"

    def test_detect_provider_google(self):
        assert _detect_provider("google/gemini-1.5-pro") == "google"

    def test_detect_provider_unknown(self):
        assert _detect_provider("ollama/mistral") == "unknown"

    def test_schema_contains_required_fields(self):
        """JSON schema emitted by bind_structured must include TraderProposal fields."""
        result = bind_structured("openai/gpt-4o", TraderProposal)
        schema = result["response_format"]["json_schema"]["schema"]
        properties = schema.get("properties", {})
        assert "action" in properties
        assert "size_fraction" in properties
        assert "confidence" in properties
        assert "rationale" in properties


# ==========================================================================
# 6. invoke_structured_or_freetext — mock client integration
# ==========================================================================


def _make_oai_mock(content: str):
    """Build a minimal mock resembling an OpenAI chat completion."""
    mock = MagicMock()
    mock.choices = [MagicMock()]
    mock.choices[0].message.content = content
    mock.choices[0].message.tool_calls = None
    mock.model_dump.return_value = {"text": content}
    return mock


def _valid_proposal_json(**overrides) -> str:
    base = {
        "action": "BUY",
        "size_fraction": 0.15,
        "entry_price": 100.0,
        "stop_loss": 96.0,
        "target_price": 108.0,
        "time_horizon_days": 30,
        "confidence": 0.72,
        "rationale": "Research manager issued Buy. Entry near close. Stop 2×ATR below.",
        "warning_message": None,
    }
    base.update(overrides)
    return json.dumps(base)


class TestInvokeStructuredOrFreetext:
    """invoke_structured_or_freetext with mocked clients."""

    def _make_client_with_response(self, content: str):
        """Client with .chat.completions.create() interface."""
        response_obj = _make_oai_mock(content)
        client = MagicMock()
        client.chat.completions.create.return_value = response_obj
        return client

    def test_valid_json_returns_parsed_obj(self):
        client = self._make_client_with_response(_valid_proposal_json())
        obj, raw = invoke_structured_or_freetext(
            client=client,
            prompt=[{"role": "user", "content": "Make a proposal"}],
            schema=TraderProposal,
            model_id="openai/gpt-4o",
        )
        assert isinstance(obj, TraderProposal)
        assert obj.action == TraderAction.BUY
        assert obj.confidence == pytest.approx(0.72)

    def test_pydantic_validation_error_returns_none(self):
        """Invalid JSON (bad field values) → (None, raw)."""
        bad_json = json.dumps({
            "action": "BUY",
            "size_fraction": 2.5,      # > 1.0 → invalid
            "confidence": 0.7,
            "rationale": "test",
        })
        client = self._make_client_with_response(bad_json)
        obj, raw = invoke_structured_or_freetext(
            client=client,
            prompt=[{"role": "user", "content": "Make a proposal"}],
            schema=TraderProposal,
            model_id="openai/gpt-4o",
        )
        assert obj is None
        assert raw is not None  # raw response still returned

    def test_bad_json_returns_none(self):
        """Malformed JSON → (None, raw)."""
        client = self._make_client_with_response("not json at all")
        obj, raw = invoke_structured_or_freetext(
            client=client,
            prompt=[{"role": "user", "content": "Make a proposal"}],
            schema=TraderProposal,
            model_id="openai/gpt-4o",
        )
        assert obj is None

    def test_fenced_json_fallback(self):
        """JSON in a ```json ... ``` fence should be parsed as fallback."""
        fenced = f"Here is my analysis:\n\n```json\n{_valid_proposal_json()}\n```\n\nDone."
        client = self._make_client_with_response(fenced)
        obj, raw = invoke_structured_or_freetext(
            client=client,
            prompt=[{"role": "user", "content": "Make a proposal"}],
            schema=TraderProposal,
            model_id="openai/gpt-4o",
        )
        assert isinstance(obj, TraderProposal)

    def test_callable_client_interface(self):
        """Plain callable client (not OAI-style SDK)."""
        valid = _valid_proposal_json()

        def mock_client(messages, model, **kwargs):
            return {"text": valid}

        obj, raw = invoke_structured_or_freetext(
            client=mock_client,
            prompt=[{"role": "user", "content": "Make a proposal"}],
            schema=TraderProposal,
            model_id="openai/gpt-4o",
        )
        assert isinstance(obj, TraderProposal)

    def test_client_exception_returns_none(self):
        """LLM call raises → (None, raw with error key)."""
        client = MagicMock()
        client.chat.completions.create.side_effect = RuntimeError("timeout")
        obj, raw = invoke_structured_or_freetext(
            client=client,
            prompt=[{"role": "user", "content": "Make a proposal"}],
            schema=TraderProposal,
            model_id="openai/gpt-4o",
        )
        assert obj is None
        assert "error" in raw

    def test_anthropic_provider_kwargs_passed(self):
        """bind_structured kwargs are forwarded for anthropic provider."""
        client = self._make_client_with_response(_valid_proposal_json())
        obj, raw = invoke_structured_or_freetext(
            client=client,
            prompt=[{"role": "user", "content": "trade?"}],
            schema=TraderProposal,
            model_id="anthropic/claude-3-5-haiku-20241022",
        )
        call_kwargs = client.chat.completions.create.call_args
        # tools kwarg should have been passed
        assert "tools" in call_kwargs.kwargs or (
            call_kwargs.args and len(call_kwargs.args) > 0
        )

    def test_system_message_prepended(self):
        """system= kwarg is prepended to the messages list."""
        client = self._make_client_with_response(_valid_proposal_json())
        invoke_structured_or_freetext(
            client=client,
            prompt=[{"role": "user", "content": "trade?"}],
            schema=TraderProposal,
            model_id="openai/gpt-4o",
            system="You are a trader.",
        )
        call_kwargs = client.chat.completions.create.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs.args[1]
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "You are a trader."
