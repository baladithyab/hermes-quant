"""tests/grounding/test_current_clear.py — Unit tests for current_clear node.

Wave 5 acceptance tests:
  - Tool-call messages are dropped between analyst rounds
  - Analyst rationales (assistant messages) are kept
  - Messages after the last assistant message are preserved
"""
from __future__ import annotations

from hermes_quant.grounding.current_clear import current_clear


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


# ---------------------------------------------------------------------------
# Test: basic filtering
# ---------------------------------------------------------------------------


def test_tool_messages_before_last_assistant_are_dropped():
    """Tool-call payloads appearing before the last assistant message must be dropped."""
    messages = [
        _msg("user", "Analyze AAPL"),
        _msg("tool", "news_data: AAPL announced earnings"),   # BEFORE last assistant → DROP
        _msg("assistant", "My view: bullish based on earnings"),  # last assistant
    ]
    result = current_clear(messages)

    roles = [m["role"] for m in result]
    assert "tool" not in roles, (
        "Tool message before last assistant should have been dropped"
    )
    assert "assistant" in roles
    assert "user" in roles


def test_analyst_rationale_kept():
    """Assistant messages (analyst synthesis) must always be preserved."""
    messages = [
        _msg("user", "Analyze AAPL"),
        _msg("tool", "sentiment_data: positive"),
        _msg("assistant", "Bullish: strong momentum [gt_AAPL_20260527_close]"),
    ]
    result = current_clear(messages)

    contents = [m["content"] for m in result]
    assert any("Bullish" in c for c in contents), "Analyst rationale must be kept"


def test_tool_messages_after_last_assistant_kept():
    """Tool messages AFTER the last assistant message must be preserved."""
    messages = [
        _msg("user", "Analyze AAPL"),
        _msg("tool", "old_tool_call"),              # BEFORE last assistant → DROP
        _msg("assistant", "My view is bullish"),     # last assistant
        _msg("tool", "follow_up_tool_call"),         # AFTER last assistant → KEEP
    ]
    result = current_clear(messages)

    contents = [m["content"] for m in result]
    assert "follow_up_tool_call" in contents, (
        "Tool message AFTER last assistant must be kept"
    )
    assert "old_tool_call" not in contents, (
        "Tool message BEFORE last assistant must be dropped"
    )


def test_multiple_tool_messages_before_assistant_all_dropped():
    """Multiple tool-call messages before the last assistant are all dropped."""
    messages = [
        _msg("user", "Analyze"),
        _msg("tool", "news_1"),
        _msg("tool", "news_2"),
        _msg("tool", "news_3"),
        _msg("assistant", "Synthesis complete"),
    ]
    result = current_clear(messages)
    roles = [m["role"] for m in result]
    assert roles.count("tool") == 0
    assert roles.count("assistant") == 1
    assert roles.count("user") == 1


def test_no_assistant_message_returns_as_is():
    """If no assistant message exists, return the messages unchanged."""
    messages = [
        _msg("user", "Analyze AAPL"),
        _msg("tool", "fetched_data"),
    ]
    result = current_clear(messages)
    assert result == messages


def test_empty_list_returns_empty():
    """Empty input must return empty list."""
    assert current_clear([]) == []


def test_user_messages_always_kept():
    """User messages must never be dropped regardless of position."""
    messages = [
        _msg("user", "First request"),
        _msg("tool", "tool_response"),
        _msg("user", "Second request"),
        _msg("assistant", "My synthesis"),
    ]
    result = current_clear(messages)
    user_contents = [m["content"] for m in result if m["role"] == "user"]
    assert "First request" in user_contents
    assert "Second request" in user_contents


def test_multiple_assistant_messages_anchors_to_last():
    """When multiple assistant messages exist, anchor to the LAST one."""
    messages = [
        _msg("user", "Analyze"),
        _msg("tool", "round1_tool"),          # before first assistant → DROP
        _msg("assistant", "Round 1 view"),    # NOT last assistant
        _msg("tool", "round2_tool"),          # before last assistant → DROP
        _msg("assistant", "Round 2 view"),    # last assistant
        _msg("tool", "after_round2_tool"),    # after last assistant → KEEP
    ]
    result = current_clear(messages)
    contents = [m["content"] for m in result]

    assert "round1_tool" not in contents, "round1_tool should be dropped"
    assert "round2_tool" not in contents, "round2_tool should be dropped"
    assert "after_round2_tool" in contents, "after_round2_tool should be kept"
    assert "Round 1 view" in contents, "Earlier assistant messages must be kept"
    assert "Round 2 view" in contents, "Last assistant message must be kept"


def test_system_messages_always_kept():
    """System messages (e.g., analyst persona) must never be dropped."""
    messages = [
        _msg("system", "You are a financial analyst"),
        _msg("user", "Analyze AAPL"),
        _msg("tool", "news_fetch"),
        _msg("assistant", "Bullish view"),
    ]
    result = current_clear(messages)
    roles = [m["role"] for m in result]
    assert "system" in roles


def test_current_clear_returns_new_list():
    """current_clear must return a new list, not mutate the input."""
    messages = [
        _msg("user", "Analyze"),
        _msg("assistant", "Done"),
    ]
    original = list(messages)
    result = current_clear(messages)
    assert messages == original, "Input must not be mutated"
    assert result is not messages, "Must return a new list object"


# ---------------------------------------------------------------------------
# Test: realistic multi-analyst pipeline scenario
# ---------------------------------------------------------------------------


def test_realistic_pipeline_two_analysts():
    """Simulate two analyst rounds; only the inter-round tool calls are dropped."""
    messages = [
        _msg("system", "Pipeline orchestrator"),
        _msg("user", "Analyze TSLA for 1d horizon"),
        # Sentiment analyst tool calls + synthesis (round 1)
        _msg("tool", "sentiment: fetched 20 news items"),      # DROP (before last assistant)
        _msg("tool", "social: reddit score=+12"),               # DROP
        _msg("assistant", "Sentiment analyst: bullish +2.50%"),
        # Technical analyst tool calls + synthesis (round 2 — last assistant)
        _msg("tool", "ta_indicators: RSI=65.3, MACD=+0.12"),   # DROP (before last assistant)
        _msg("assistant", "Technical analyst: bullish, RSI confirms"),  # last assistant
    ]
    result = current_clear(messages)
    roles = [m["role"] for m in result]
    contents = [m["content"] for m in result]

    # Tool calls must be gone
    assert roles.count("tool") == 0

    # Both analyst rationales must survive
    assert any("Sentiment analyst" in c for c in contents)
    assert any("Technical analyst" in c for c in contents)

    # System and user must survive
    assert "system" in roles
    assert "user" in roles
