"""Offline-deterministic tests for hermes_quant.data.mcp_bridge.

No live network / MCP subprocess: a fake dispatch backend is injected via
``set_dispatch_backend``. Verifies the money-software rails:
  - default OFF (flag unset → no calls, no side effects)
  - read-only server allowlist + money-write denylist
  - fail-closed safe wrapper
  - {"result": ...} / {"error": ...} envelope handling
  - byte-identical default path (flag off → backend never touched)
"""

from __future__ import annotations

import json

import pytest

from hermes_quant.data import mcp_bridge as mb


@pytest.fixture(autouse=True)
def _reset_backend():
    """Reset the injected backend before and after each test."""
    mb.set_dispatch_backend(None, None)
    yield
    mb.set_dispatch_backend(None, None)


@pytest.fixture
def _flag_on(monkeypatch):
    monkeypatch.setenv(mb.ENABLE_FLAG, "1")


@pytest.fixture
def _flag_off(monkeypatch):
    monkeypatch.delenv(mb.ENABLE_FLAG, raising=False)


class _FakeBackend:
    """Records discover() calls and serves canned dispatch results."""

    def __init__(self, results: dict[str, str]):
        self.results = results
        self.discover_calls = 0
        self.dispatch_calls: list[tuple[str, dict]] = []

    def discover(self) -> None:
        self.discover_calls += 1

    def dispatch(self, tool_name: str, args: dict) -> str:
        self.dispatch_calls.append((tool_name, dict(args)))
        if tool_name not in self.results:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})
        return self.results[tool_name]


# ---------------------------------------------------------------------------
# Allowlist / denylist purity (no flag, no backend needed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name",
    [
        "mcp_coingecko_execute",
        "mcp_tradingview_screen_stocks",
        "mcp_yahoo_finance_get_current_stock_price",
        "mcp_sec_edgar_get_recent_filings",
        "mcp_sec_edgar_analyze_8k",
    ],
)
def test_allowlisted_read_tools_recognized(tool_name):
    assert mb.is_read_only_mcp_tool(tool_name) is True


@pytest.mark.parametrize(
    "tool_name",
    [
        # non-mcp / non-prefixed
        "quant_recommend",
        "mcpfoo",
        "",
        # non-allowlisted server (brokerage — must NEVER be reachable here)
        "mcp_alpaca_get_account",
        "mcp_robinhood_get_positions",
        "mcp_longbridge_quote",
        "mcp_polygon_aggs",
        # money-write denylist hit even on an allowlisted-looking server
        "mcp_tradingview_execute_order",
        "mcp_coingecko_place_order",
        "mcp_sec_edgar_trade_something",
    ],
)
def test_denied_tools_rejected(tool_name):
    assert mb.is_read_only_mcp_tool(tool_name) is False


def test_split_tool_name_handles_multiunderscore_servers():
    assert mb._split_tool_name("mcp_yahoo_finance_get_dividends") == (
        "yahoo_finance",
        "get_dividends",
    )
    assert mb._split_tool_name("mcp_sec_edgar_get_cik_by_ticker") == (
        "sec_edgar",
        "get_cik_by_ticker",
    )


# ---------------------------------------------------------------------------
# Default OFF — byte-identical default path
# ---------------------------------------------------------------------------


def test_disabled_by_default(_flag_off):
    assert mb.is_enabled() is False


def test_strict_raises_when_disabled(_flag_off):
    backend = _FakeBackend({"mcp_coingecko_execute": json.dumps({"result": 1})})
    mb.set_dispatch_backend(backend.discover, backend.dispatch)
    with pytest.raises(mb.McpReadsDisabledError):
        mb.call_read_tool("mcp_coingecko_execute", {})
    # Rails: backend must NOT be touched when OFF.
    assert backend.discover_calls == 0
    assert backend.dispatch_calls == []


def test_safe_returns_none_when_disabled(_flag_off):
    backend = _FakeBackend({"mcp_coingecko_execute": json.dumps({"result": 1})})
    mb.set_dispatch_backend(backend.discover, backend.dispatch)
    assert mb.try_read_tool("mcp_coingecko_execute", {}) is None
    assert backend.discover_calls == 0
    assert backend.dispatch_calls == []


# ---------------------------------------------------------------------------
# Enabled — happy path + envelope handling
# ---------------------------------------------------------------------------


def test_enabled_unwraps_result_envelope(_flag_on):
    payload = [{"key": "quality_stocks"}]
    backend = _FakeBackend(
        {"mcp_tradingview_list_presets": json.dumps({"result": json.dumps(payload)})}
    )
    mb.set_dispatch_backend(backend.discover, backend.dispatch)
    out = mb.call_read_tool("mcp_tradingview_list_presets", {})
    # The inner result is itself a JSON string per the live server; we return it raw.
    assert out == json.dumps(payload)
    assert backend.discover_calls == 1
    assert backend.dispatch_calls == [("mcp_tradingview_list_presets", {})]


def test_enabled_returns_dict_payload(_flag_on):
    backend = _FakeBackend(
        {"mcp_sec_edgar_get_cik_by_ticker": json.dumps({"cik": "0000320193", "ticker": "AAPL"})}
    )
    mb.set_dispatch_backend(backend.discover, backend.dispatch)
    out = mb.call_read_tool("mcp_sec_edgar_get_cik_by_ticker", {"ticker": "AAPL"})
    assert out == {"cik": "0000320193", "ticker": "AAPL"}


def test_enabled_args_default_to_empty(_flag_on):
    backend = _FakeBackend({"mcp_coingecko_search_docs": json.dumps({"result": "ok"})})
    mb.set_dispatch_backend(backend.discover, backend.dispatch)
    out = mb.call_read_tool("mcp_coingecko_search_docs")
    assert out == "ok"
    assert backend.dispatch_calls == [("mcp_coingecko_search_docs", {})]


def test_bare_text_payload_passthrough(_flag_on):
    # A non-JSON bare-text payload (no enclosing braces / quotes) is returned
    # verbatim rather than raising. (A JSON-number string like "189.42" would
    # instead parse to the float 189.42 — that is correct and exercised
    # implicitly elsewhere.)
    backend = _FakeBackend(
        {"mcp_yahoo_finance_get_current_stock_price": "AAPL 189.42 USD"}
    )
    mb.set_dispatch_backend(backend.discover, backend.dispatch)
    out = mb.call_read_tool("mcp_yahoo_finance_get_current_stock_price", {"ticker": "AAPL"})
    assert out == "AAPL 189.42 USD"


# ---------------------------------------------------------------------------
# Enabled — denial + fail-closed
# ---------------------------------------------------------------------------


def test_strict_denies_non_allowlisted_server(_flag_on):
    backend = _FakeBackend({"mcp_alpaca_place_stock_order": json.dumps({"result": "filled"})})
    mb.set_dispatch_backend(backend.discover, backend.dispatch)
    with pytest.raises(mb.McpReadDeniedError):
        mb.call_read_tool("mcp_alpaca_place_stock_order", {})
    # Money-write tool on a brokerage server must NEVER reach dispatch.
    assert backend.dispatch_calls == []


def test_strict_denies_money_write_on_allowlisted_server(_flag_on):
    backend = _FakeBackend({})
    mb.set_dispatch_backend(backend.discover, backend.dispatch)
    with pytest.raises(mb.McpReadDeniedError):
        mb.call_read_tool("mcp_tradingview_execute_order", {})
    assert backend.dispatch_calls == []


def test_safe_returns_none_on_denied(_flag_on):
    backend = _FakeBackend({})
    mb.set_dispatch_backend(backend.discover, backend.dispatch)
    assert mb.try_read_tool("mcp_robinhood_get_positions", {}) is None
    assert backend.dispatch_calls == []


def test_error_envelope_raises(_flag_on):
    backend = _FakeBackend(
        {"mcp_sec_edgar_get_recent_filings": json.dumps({"error": "server not connected"})}
    )
    mb.set_dispatch_backend(backend.discover, backend.dispatch)
    with pytest.raises(mb.McpReadUnavailableError):
        mb.call_read_tool("mcp_sec_edgar_get_recent_filings", {"ticker": "AAPL"})


def test_safe_returns_none_on_error_envelope(_flag_on):
    backend = _FakeBackend(
        {"mcp_sec_edgar_get_recent_filings": json.dumps({"error": "boom"})}
    )
    mb.set_dispatch_backend(backend.discover, backend.dispatch)
    assert mb.try_read_tool("mcp_sec_edgar_get_recent_filings", {}) is None


def test_dispatch_exception_is_failclosed(_flag_on):
    def _boom(tool_name, args):
        raise RuntimeError("transport wedged")

    def _noop():
        return None

    mb.set_dispatch_backend(_noop, _boom)
    with pytest.raises(mb.McpReadUnavailableError):
        mb.call_read_tool("mcp_coingecko_execute", {})
    assert mb.try_read_tool("mcp_coingecko_execute", {}) is None


def test_discovery_exception_is_failclosed(_flag_on):
    def _boom_discover():
        raise RuntimeError("cannot reach host")

    def _dispatch(tool_name, args):  # pragma: no cover — never reached
        return json.dumps({"result": 1})

    mb.set_dispatch_backend(_boom_discover, _dispatch)
    with pytest.raises(mb.McpReadUnavailableError):
        mb.call_read_tool("mcp_coingecko_execute", {})


def test_set_dispatch_backend_validates_pairing():
    with pytest.raises(ValueError):
        mb.set_dispatch_backend(lambda: None, None)
