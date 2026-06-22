#!/usr/bin/env python3
"""PreToolUse deny-hook for cowork-quant (B-33, arch §4.8, rail R12).

Reads the PreToolUse payload on stdin and DENIES any order/transfer/execution tool
(rail #4, platform-enforced), and — in an unattended/scheduled context — any
quantcore CLI verb that approves/fills/resumes (B-11) and AskUserQuestion.

Wire in hooks.json. Self-contained: imports quantcore.exec_guard if importable,
else uses an inline copy (the hook must run even before the package is installed).

Output: on deny, a PreToolUse hookSpecificOutput JSON with permissionDecision=deny.
On allow, no output (defers to normal permissioning). Never raises — a hook crash
must not block the user; but a recognized execution tool is always denied.
"""
from __future__ import annotations

import json
import os
import re
import sys


def _inline_eval(tool_name: str, tool_input: dict | None, unattended: bool):
    exec_re = re.compile(
        r"place_[a-z]*_?order|submit_[a-z]*_?order|create_[a-z]*_?order|"
        r"cancel_[a-z]*_?order|replace_[a-z]*_?order|place_order|buy_[a-z]+|"
        r"sell_[a-z]+|transfer|withdraw|deposit|wire_|move_money|liquidate|"
        r"close_position|exercise|assign|execute_[a-z]*_?(order|trade)",
        re.IGNORECASE,
    )
    if tool_name and exec_re.search(tool_name):
        return "deny", f"rail #4 (no execution): tool '{tool_name}' is an order/transfer surface."
    if unattended and (tool_name or "").lower() in ("bash", "shell"):
        cmd = str((tool_input or {}).get("command") or (tool_input or {}).get("cmd") or "")
        if ("quantcore" in cmd or "cli" in cmd) and re.search(
            r"\b(fill|resume)\b|--decision\s+approval", cmd, re.IGNORECASE
        ):
            return "deny", "unattended turns may not approve/fill/resume (B-11)."
    if unattended and tool_name == "AskUserQuestion":
        return "deny", "unattended turns cannot ask for human approval (fail-closed)."
    return "allow", ""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # cannot parse -> do not block normal operation
    tool_name = payload.get("tool_name") or payload.get("toolName") or ""
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    # unattended if a scheduled-turn sentinel env/flag is present
    unattended = bool(
        os.environ.get("COWORK_QUANT_UNATTENDED")
        or os.environ.get("CLAUDE_NON_INTERACTIVE")
    )
    try:
        from quantcore.exec_guard import evaluate  # type: ignore

        decision, reason = evaluate(tool_name, tool_input, unattended=unattended)
    except Exception:
        decision, reason = _inline_eval(tool_name, tool_input, unattended)

    if decision == "deny":
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
