"""hermes_quant.data.mcp_bridge — thin, default-OFF, READ-ONLY seam for
consuming Hermes-host MCP data tools from inside the plugin.

PHASE 5 (Seed hermes-quant-b9eb). This module is the *one tested place* an
analyst, a recipe, or a ``no_agent`` cron script can reach an enabled
read-only MCP data tool (coingecko / tradingview / yahoo-finance / sec-edgar)
without hand-rolling registry calls. It is **purely additive** and does
nothing unless the operator opts in.

WHY THIS EXISTS (and what it is NOT)
------------------------------------
The Hermes host already auto-exposes every ``config.yaml`` ``mcp_servers``
entry to the agent: ``tools.mcp_tool.discover_mcp_tools()`` connects to each
server, lists its tools, and registers them into the global
``tools.registry`` as ``mcp_<server>_<tool>`` (toolset ``mcp-<server>``).
The chat LLM and ``no_agent=False`` advisor briefs therefore see those tools
*for free* — no plugin wiring is needed (see docs/operations/MCP-INTEGRATION.md
§8). This module exists only for the **programmatic** path: deterministic
package code (an analyst, a recipe builder, a no_agent cron) that wants to
call a read-only MCP tool by name and get JSON back, with the money-software
rails enforced *here* rather than trusted to the caller.

RAILS (non-negotiable, enforced in-module)
-------------------------------------------
1. **Default OFF.** Returns ``None`` / raises ``McpReadsDisabledError`` unless
   ``HERMES_QUANT_MCP_READS_ENABLED=1`` is set in the (tool-guarded)
   environment. With the flag unset, NOTHING is imported, connected, or
   dispatched — the default/existing path is byte-identical.
2. **READ-ONLY allowlist.** Only the four keyless read-only servers
   (coingecko, tradingview, yahoo-finance, sec-edgar) are reachable, AND only
   tools whose names do not match the money-write denylist. There is no code
   path here that can place an order, cancel, close a position, or mutate
   account config. The deterministic risk gate (ADR-0004) + HITL (ADR-0015)
   remain the sole order authority; this seam produces *evidence that can only
   silence, never authorize* (HERMES-INTEGRATION.md §3).
3. **Fail-closed.** Any error (MCP SDK absent, host not importable, server not
   connected, tool missing, dispatch error, denylist hit, non-allowlisted
   server) returns ``None`` from the safe wrapper or raises a typed error from
   the strict wrapper — never a partial/ambiguous result, never an exception
   that escapes into a trading path.
4. **No-lookahead / asof-honest.** This module does not touch time. Callers
   that fold MCP reads into a decision MUST honor the existing as-of gates
   (e.g. ``evidence/lookahead_gate.py``); an MCP read is live data and must be
   stamped with the wall-clock fetch time by the caller, never back-dated.

OFFLINE TESTS: this module never requires a live network. Tests inject a fake
registry/discovery via :func:`set_dispatch_backend` so the consumption seam is
exercised deterministically against recorded fixtures.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Flag + allowlist constants
# ---------------------------------------------------------------------------

#: Master default-OFF flag. The operator sets this in the tool-guarded
#: ~/.hermes/.env (FEATURE-ENABLEMENT.md §0); the agent cannot flip it.
ENABLE_FLAG = "HERMES_QUANT_MCP_READS_ENABLED"

#: The ONLY servers this bridge will dispatch to. Mirrors the four keyless
#: read-only servers shipped LIVE in config.yaml (MCP-INTEGRATION.md "Live
#: state"). Brokerage / underlying-can-write servers (alpaca, robinhood,
#: longbridge) are deliberately ABSENT and can never be added here without a
#: separate, explicit code change + review — this bridge is read-data-only.
#:
#: Names are the *sanitized* server-name components (hyphens → underscores),
#: matching the ``mcp_<server>_<tool>`` registry prefix exactly.
READ_ONLY_SERVERS = frozenset({
    "coingecko",
    "tradingview",
    "yahoo_finance",
    "sec_edgar",
})

#: Defense-in-depth: even within an allowlisted server, refuse any tool whose
#: (lowercased) name contains a money-write verb. The four shipped servers
#: have NO such tools (verified), so this is belt-and-braces against a server
#: silently adding a write surface in a future version. Substring match on the
#: bare tool name (the part after ``mcp_<server>_``).
_MONEY_WRITE_DENY_SUBSTRINGS = (
    "place_order",
    "submit_order",
    "execute_order",
    "execute_portfolio",
    "cancel_order",
    "replace_order",
    "close_position",
    "close_all",
    "exercise_option",
    "update_account",
    "create_order",
    "buy",
    "sell",
    "withdraw",
    "transfer",
    "trade",
)

# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class McpReadsDisabledError(RuntimeError):
    """Raised by the strict API when the master flag is not set to ``1``."""


class McpReadDeniedError(RuntimeError):
    """Raised by the strict API when a tool is not an allowlisted read-only MCP tool."""


class McpReadUnavailableError(RuntimeError):
    """Raised by the strict API when the host MCP machinery is unavailable or the call fails."""


# ---------------------------------------------------------------------------
# Pluggable dispatch backend (for offline-deterministic tests)
# ---------------------------------------------------------------------------
#
# Production wires the real Hermes host registry lazily (see
# _default_backend). Tests inject a fake via set_dispatch_backend() so no live
# network / MCP subprocess is ever spawned.

#: A backend is ``(discover_fn, dispatch_fn)`` where
#:   discover_fn() -> None            (idempotent host MCP discovery)
#:   dispatch_fn(tool_name, args) -> str   (JSON string, Hermes tool convention)
_Backend = tuple[Callable[[], None], Callable[[str, dict], str]]

_backend: _Backend | None = None


def set_dispatch_backend(
    discover_fn: Callable[[], None] | None,
    dispatch_fn: Callable[[str, dict], str] | None,
) -> None:
    """Override the dispatch backend (TESTS ONLY).

    Pass ``(None, None)`` to reset to the lazy real-host backend. Never call
    this in production code — the default backend wires the live host
    ``tools.registry`` on first use.
    """
    global _backend
    if discover_fn is None and dispatch_fn is None:
        _backend = None
        return
    if discover_fn is None or dispatch_fn is None:
        raise ValueError("set_dispatch_backend requires both fns or both None")
    _backend = (discover_fn, dispatch_fn)


def _default_backend() -> _Backend:
    """Lazily wire the live Hermes-host MCP registry.

    Imports are deferred to call time so this module imports cleanly in any
    environment (including the plugin's own offline test runner where the
    Hermes host package is absent). Raises ``McpReadUnavailableError`` if the host
    MCP machinery cannot be imported.
    """
    try:
        from tools.mcp_tool import discover_mcp_tools  # type: ignore
        from tools.registry import registry  # type: ignore
    except Exception as exc:  # pragma: no cover — host-absent path
        raise McpReadUnavailableError(
            f"Hermes host MCP machinery unavailable: {exc}. This bridge only "
            "works inside the Hermes host process where tools.registry lives."
        ) from exc

    def _dispatch(tool_name: str, args: dict) -> str:
        return registry.dispatch(tool_name, args)

    return discover_mcp_tools, _dispatch


def _resolve_backend() -> _Backend:
    return _backend if _backend is not None else _default_backend()


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def is_enabled() -> bool:
    """Return True iff the operator opted into MCP reads (default OFF)."""
    return os.environ.get(ENABLE_FLAG, "0") == "1"


def _split_tool_name(tool_name: str) -> tuple[str, str]:
    """Split ``mcp_<server>_<tool>`` into (server, tool).

    The server is matched greedily against :data:`READ_ONLY_SERVERS` so that
    multi-underscore server names (``yahoo_finance``, ``sec_edgar``) resolve
    correctly without ambiguity. Returns ``("", "")`` if the name is not an
    ``mcp_``-prefixed allowlisted-server tool.
    """
    if not isinstance(tool_name, str) or not tool_name.startswith("mcp_"):
        return ("", "")
    remainder = tool_name[len("mcp_"):]
    # Longest-prefix match against the allowlist (handles underscore-containing
    # server names deterministically).
    for server in sorted(READ_ONLY_SERVERS, key=len, reverse=True):
        prefix = server + "_"
        if remainder.startswith(prefix):
            return (server, remainder[len(prefix):])
    return ("", "")


def is_read_only_mcp_tool(tool_name: str) -> bool:
    """Return True iff ``tool_name`` is an allowlisted read-only MCP data tool.

    Enforces BOTH the server allowlist (:data:`READ_ONLY_SERVERS`) and the
    money-write denylist (:data:`_MONEY_WRITE_DENY_SUBSTRINGS`). Pure / no
    side effects — safe to call regardless of the flag.
    """
    server, tool = _split_tool_name(tool_name)
    if not server:
        return False
    lowered = tool.lower()
    return not any(bad in lowered for bad in _MONEY_WRITE_DENY_SUBSTRINGS)


# ---------------------------------------------------------------------------
# Public API — strict (raises) and safe (returns None) variants
# ---------------------------------------------------------------------------


def call_read_tool(tool_name: str, args: dict | None = None) -> Any:
    """Call an allowlisted read-only MCP tool and return its parsed result.

    STRICT variant: raises on any rail violation or failure. Use this when the
    caller wants to handle/log the specific failure mode.

    Args:
        tool_name: the registry name, e.g. ``"mcp_sec_edgar_get_recent_filings"``.
        args: tool arguments (JSON-serializable dict). ``None`` → ``{}``.

    Returns:
        The parsed JSON payload. The Hermes tool convention is a JSON string;
        we parse it. If the payload is ``{"result": <x>}`` we return ``<x>``;
        if it is ``{"error": ...}`` we raise :class:`McpReadUnavailableError`.

    Raises:
        McpReadsDisabledError: the master flag is not ``1``.
        McpReadDeniedError: ``tool_name`` is not an allowlisted read-only MCP tool.
        McpReadUnavailableError: host machinery absent, server not connected, or the
            dispatch returned an error / unparseable payload.
    """
    if not is_enabled():
        raise McpReadsDisabledError(
            f"{ENABLE_FLAG} is not set to '1'; MCP reads are OFF by default. "
            "The operator opts in via the tool-guarded ~/.hermes/.env."
        )
    if not is_read_only_mcp_tool(tool_name):
        raise McpReadDeniedError(
            f"'{tool_name}' is not an allowlisted read-only MCP data tool. "
            f"Allowed servers: {sorted(READ_ONLY_SERVERS)}; money-write tool "
            "names are denied."
        )

    args = dict(args or {})
    discover_fn, dispatch_fn = _resolve_backend()

    # Idempotent: ensures the server is connected + its tools registered.
    try:
        discover_fn()
    except McpReadUnavailableError:
        raise
    except Exception as exc:
        raise McpReadUnavailableError(f"MCP discovery failed: {exc}") from exc

    try:
        raw = dispatch_fn(tool_name, args)
    except Exception as exc:
        raise McpReadUnavailableError(f"MCP dispatch of '{tool_name}' failed: {exc}") from exc

    return _parse_dispatch_result(tool_name, raw)


def try_read_tool(tool_name: str, args: dict | None = None) -> Any | None:
    """Fail-closed convenience wrapper around :func:`call_read_tool`.

    Returns ``None`` on ANY failure (flag off, denied, unavailable, parse
    error) and logs at debug. Use this in deterministic decision code where an
    MCP read is *optional enrichment* and its absence must degrade silently to
    the existing behavior (the money-software default: silence beats noise).
    """
    try:
        return call_read_tool(tool_name, args)
    except McpReadsDisabledError:
        # Expected when OFF — debug only, no noise.
        logger.debug("MCP reads disabled (%s != 1); '%s' skipped", ENABLE_FLAG, tool_name)
        return None
    except (McpReadDeniedError, McpReadUnavailableError) as exc:
        logger.debug("MCP read '%s' unavailable, degrading to None: %s", tool_name, exc)
        return None


def _parse_dispatch_result(tool_name: str, raw: Any) -> Any:
    """Parse a Hermes tool-dispatch result (a JSON string) into a payload.

    Raises :class:`McpReadUnavailableError` on an ``{"error": ...}`` envelope or
    unparseable output. Unwraps the conventional ``{"result": <x>}`` envelope.
    """
    if raw is None:
        raise McpReadUnavailableError(f"MCP tool '{tool_name}' returned no result")
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            # Some MCP tools return bare text; treat as the payload itself.
            return raw
    else:
        payload = raw
    if isinstance(payload, dict):
        if "error" in payload:
            raise McpReadUnavailableError(
                f"MCP tool '{tool_name}' returned error: {payload.get('error')}"
            )
        if "result" in payload and len(payload) == 1:
            return payload["result"]
    return payload
