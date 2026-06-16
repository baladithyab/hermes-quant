"""quantcore.exec_guard — the no-execution predicate (B-33, arch §4.8, rail R12).

Rail #4 (no order execution, ever) was prompt-discipline in v0.1. Cowork PreToolUse
hooks let us make it a PLATFORM-ENFORCED invariant: a hook denies any order/transfer
tool call regardless of what the prompt says. Institutional AI (2601.11369) is the
evidence that enforcement, not declaration, is what binds under pressure.

This module is the testable decision logic; `hooks/deny_execution.py` is a thin
stdin/stdout shim that imports it (with an inline fallback). Two checks:

  * `is_execution_tool(name)`  — broker/order/transfer tools that must NEVER fire.
  * `is_state_mutation_cli(cmd)` — quantcore CLI verbs (approval/fill/resume) that an
    UNATTENDED scheduled turn must never run (a scheduled /watch could otherwise be
    injected into approving its own queued proposal — backlog B-11). Interactive
    sessions may run them; the unattended flag gates it.

Conservative by design: matches a curated set of execution patterns so read-only
data tools are never false-denied. stdlib only (no deps — the hook runs standalone).
"""

from __future__ import annotations

import re

# Tool-name patterns that are unambiguously order/transfer/execution surfaces.
# Matched case-insensitively as substrings of the (possibly mcp__server__) name.
_EXECUTION_PATTERNS = (
    r"place_[a-z]*_?order",
    r"submit_[a-z]*_?order",
    r"create_[a-z]*_?order",
    r"cancel_[a-z]*_?order",
    r"replace_[a-z]*_?order",
    r"\border\b.*\bplace\b",
    r"place_order",
    r"buy_[a-z]+",
    r"sell_[a-z]+",
    r"transfer",
    r"withdraw",
    r"deposit",
    r"wire_",
    r"move_money",
    r"liquidate",
    r"close_position",
    r"exercise",  # option exercise (relevant once options land, B-20)
    r"assign",
    r"execute_[a-z]*_?(order|trade)",
)
_EXEC_RE = re.compile("|".join(_EXECUTION_PATTERNS), re.IGNORECASE)

# quantcore CLI verbs that mutate the book / lift halts — forbidden unattended.
_STATE_MUTATION_RE = re.compile(
    r"\b(fill|resume)\b|--decision\s+approval|\bdecide\b.*approval",
    re.IGNORECASE,
)


def is_execution_tool(tool_name: str) -> tuple[bool, str]:
    """True if the tool places/cancels orders or moves money (rail #4 deny)."""
    if not tool_name:
        return False, ""
    if _EXEC_RE.search(tool_name):
        return True, (
            f"rail #4 (no execution, ever): tool '{tool_name}' is an order/transfer "
            "surface; cowork-quant never executes — the human places orders."
        )
    return False, ""


def is_state_mutation_cli(command: str) -> tuple[bool, str]:
    """True if a Bash command runs a quantcore CLI verb that approves a proposal,
    records a fill, or resumes a halt. Only enforced in unattended context."""
    if not command or "quantcore" not in command:
        return False, ""
    if _STATE_MUTATION_RE.search(command):
        return True, (
            "unattended turns may not approve/fill/resume (backlog B-11): a "
            "scheduled run must queue proposals only; the human reacts interactively."
        )
    return False, ""


def evaluate(tool_name: str, tool_input: dict | None, *, unattended: bool) -> tuple[str, str]:
    """Return (decision, reason). decision in {'allow','deny'}.

    'allow' here means 'no objection from this guard' — it does not pre-approve;
    the platform still applies normal permissioning.
    """
    blocked, reason = is_execution_tool(tool_name)
    if blocked:
        return "deny", reason
    if unattended and (tool_name or "").lower() in ("bash", "shell"):
        cmd = ""
        if tool_input:
            cmd = str(tool_input.get("command") or tool_input.get("cmd") or "")
        blocked, reason = is_state_mutation_cli(cmd)
        if blocked:
            return "deny", reason
    if unattended and (tool_name or "") == "AskUserQuestion":
        return "deny", "unattended turns cannot ask for human approval (fail-closed)."
    return "allow", ""
