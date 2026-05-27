"""hermes_quant.grounding.current_clear — Purge tool-call messages between analyst stages.

Wave 5 (ADR-0038 §W5). Mirrors TauricResearch's `create_msg_delete()` node.

TauricResearch v0.2.5 fix (gap #6): Anti-pattern is empty-memory hallucination.
Fabricated sentiment posts in v0.2.4 were traced to stale tool-call messages
polluting the context between analyst stages. Forcing tool calls BEFORE synthesis
(and clearing them afterward) eliminates this failure mode.

Ordering constraint
-------------------
For sentiment-adjacent analysts that call news/social tools:
  1. Analyst invokes tool → tool response appended as role=tool message.
  2. Analyst synthesizes view using tool response.
  3. current_clear() is called AFTER synthesis to drop stale tool messages.
  4. Next analyst stage starts with a clean message context.

See: docs/adr/ADR-0038-tradingagents-pattern-backfill.md §W5.current_clear
"""

from __future__ import annotations

from typing import Any


def current_clear(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Purge tool-call messages that precede the last analyst response.

    Specifically: drops any message with role='tool' that appears BEFORE
    the last message whose role is 'assistant' (analyst synthesis response).
    Messages after (or at) the last assistant message are kept intact so
    that the very latest tool call → synthesis pair survives if needed.

    Parameters
    ----------
    messages : list of message dicts, each with at least a 'role' key.

    Returns
    -------
    Filtered list with stale tool-call payloads removed.

    Examples
    --------
    >>> msgs = [
    ...     {"role": "user",      "content": "Analyze AAPL"},
    ...     {"role": "tool",      "content": "news_data ..."},   # DROPPED
    ...     {"role": "assistant", "content": "My view is ..."},  # last assistant
    ...     {"role": "tool",      "content": "extra_call ..."},  # KEPT (after last assistant)
    ... ]
    >>> current_clear(msgs)
    [
        {"role": "user",      "content": "Analyze AAPL"},
        {"role": "assistant", "content": "My view is ..."},
        {"role": "tool",      "content": "extra_call ..."},
    ]
    """
    # Find the index of the last assistant message
    last_assistant_idx: int = -1
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant":
            last_assistant_idx = i

    if last_assistant_idx < 0:
        # No assistant message found — nothing to anchor; return as-is.
        return list(messages)

    result: list[dict[str, Any]] = []
    for i, msg in enumerate(messages):
        role = msg.get("role")
        # Drop tool messages that are BEFORE the last assistant response
        if role == "tool" and i < last_assistant_idx:
            continue
        result.append(msg)

    return result
