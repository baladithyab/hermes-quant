"""Tests for quantcore.exec_guard — platform no-execution predicate (B-33)."""

from __future__ import annotations

import pytest

from quantcore.exec_guard import (
    evaluate,
    is_execution_tool,
    is_state_mutation_cli,
)


@pytest.mark.parametrize(
    "name",
    [
        "place_equity_order",
        "place_option_order",
        "cancel_equity_order",
        "cancel_option_order",
        "mcp__284687c2-baca__place_equity_order",
        "mcp__broker__transfer_funds",
        "withdraw_cash",
        "liquidate",
        "close_position",
    ],
)
def test_execution_tools_denied(name):
    blocked, reason = is_execution_tool(name)
    assert blocked and reason


@pytest.mark.parametrize(
    "name",
    [
        "get_equity_quotes",
        "get_option_chains",
        "get_option_quotes",
        "get_portfolio",
        "get_equity_historicals",
        "Read",
        "Bash",
        "search",
    ],
)
def test_read_tools_allowed(name):
    blocked, _ = is_execution_tool(name)
    assert not blocked


def test_state_mutation_cli():
    assert is_state_mutation_cli("python -m quantcore.cli fill --proposal-id x")[0]
    assert is_state_mutation_cli("python -m quantcore.cli decide --decision approval --id x")[0]
    assert is_state_mutation_cli("python -m quantcore.cli resume")[0]
    assert not is_state_mutation_cli("python -m quantcore.cli status")[0]
    assert not is_state_mutation_cli("python -m quantcore.cli scan AAPL")[0]
    assert not is_state_mutation_cli("ls -la")[0]


def test_evaluate_execution_always_denied():
    d, _ = evaluate("place_equity_order", {}, unattended=False)
    assert d == "deny"


def test_evaluate_unattended_gating():
    # AskUserQuestion: denied unattended, allowed interactively
    assert evaluate("AskUserQuestion", None, unattended=True)[0] == "deny"
    assert evaluate("AskUserQuestion", None, unattended=False)[0] == "allow"
    # fill via Bash: denied unattended, allowed interactively
    cmd = {"command": "python -m quantcore.cli fill --proposal-id x"}
    assert evaluate("Bash", cmd, unattended=True)[0] == "deny"
    assert evaluate("Bash", cmd, unattended=False)[0] == "allow"
    # read-only bash always fine
    assert evaluate("Bash", {"command": "python -m quantcore.cli status"}, unattended=True)[0] == "allow"
