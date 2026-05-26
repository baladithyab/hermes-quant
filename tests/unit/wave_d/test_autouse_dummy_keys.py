"""Tests for ADR-0038 §D.4 (P8) — autouse dummy third-party API keys.

Verifies that the autouse fixture in tests/conftest.py:
1. Is autouse (runs without explicit request).
2. Can be overridden by a test that needs a different value.
3. Does not leak across test boundaries.
"""

from __future__ import annotations

import os

import pytest


def test_autouse_fixture_injects_placeholder_keys() -> None:
    """All third-party API keys are populated with the placeholder value.

    The fixture is autouse, so this test does not request it explicitly.
    Every documented placeholder env var should be set to the
    ``"test-placeholder"`` sentinel.
    """
    expected = {
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "AWS_BEARER_TOKEN_BEDROCK",
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "ALPHAVANTAGE_API_KEY",
        "BINANCE_API_KEY",
        "BINANCE_SECRET",
        "COINBASE_API_KEY",
        "COINBASE_SECRET",
    }
    for key in expected:
        assert os.environ.get(key) == "test-placeholder", (
            f"{key} not set to placeholder by autouse fixture"
        )


def test_test_can_override_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test that needs a real value can override the placeholder.

    The local monkeypatch.setenv() takes precedence over the autouse
    fixture's setting for the duration of the test.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "my-real-test-key")
    assert os.environ["OPENROUTER_API_KEY"] == "my-real-test-key"
    # Other placeholders still in effect
    assert os.environ.get("ANTHROPIC_API_KEY") == "test-placeholder"


def test_no_leak_across_test_boundaries() -> None:
    """The override from the previous test does not leak.

    pytest's monkeypatch teardown runs at function scope; the autouse
    fixture re-applies its placeholder for every test. After the
    override-test ran, OPENROUTER_API_KEY should be back to the
    placeholder, not stuck at ``"my-real-test-key"``.
    """
    assert os.environ.get("OPENROUTER_API_KEY") == "test-placeholder"
