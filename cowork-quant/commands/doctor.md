---
description: Environment + data-path health check for cowork-quant
allowed-tools: ["Read", "Bash", "Glob", "ToolSearch"]
---

Diagnose the plugin environment and report a pass/fail table:

1. Python >= 3.10 available in the sandbox; pydantic importable.
2. quantcore installs (`pip install -e <plugin>/scripts/quantcore --quiet`)
   and `python -m quantcore.cli verify --state-dir <workspace>/quant-state`
   passes (an empty/absent ledger verifies as ok).
3. quant-state/ exists in the workspace folder, config.json parses (create a
   default conservative one if absent, telling the user).
4. Data paths: try ONE cheap call on each — yahoo-finance MCP (a quote),
   coingecko MCP (BTC price), sec-edgar MCP (a CIK lookup), and sandbox
   yfinance (1 day of SPY). Report which paths work; the plugin needs at
   least one equity path and (if crypto is on the watchlist) one crypto path.
5. Run the quantcore test suite if the user asks for a deep check:
   `python -m pytest <plugin>/scripts/quantcore/tests -q`.
6. Confirm the rails: report the active risk profile and its limits from
   config.json.

If a data path fails, do NOT fall back to curl/requests scraping — report it
and suggest enabling the corresponding MCP connector instead.
