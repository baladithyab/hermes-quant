"""hermes_quant.schemas — JSON Schema definitions for the four read-only tools.

Per ADR-0007: tools are read-only views over daemon state. They do not
spawn the daemon, change config, or place trades.
"""
from __future__ import annotations

QUANT_STATUS = {
    "name": "quant_status",
    "description": "Read the current status of the hermes-quant daemon: running/stopped, "
                   "uptime, last tick, signal rate, current positions per partition, "
                   "active halts. Read-only; does NOT control the daemon.",
    "parameters": {
        "type": "object",
        "properties": {
            "account": {
                "type": "string",
                "description": "Optional account_id filter (e.g. 'alpaca-paper'). "
                               "Omit for all accounts.",
            },
        },
        "required": [],
    },
}

QUANT_SHOW_SIGNALS = {
    "name": "quant_show_signals",
    "description": "Show recent signals from the daemon's signal bus. Read-only.",
    "parameters": {
        "type": "object",
        "properties": {
            "n": {"type": "integer", "default": 20,
                  "description": "Max signals to return (most recent first)"},
            "asset": {"type": "string",
                      "description": "Optional asset filter (e.g. 'BTC/USDT')"},
            "direction": {"type": "string", "enum": ["long", "short", "flat", "any"],
                          "default": "any"},
        },
        "required": [],
    },
}

QUANT_SHOW_VIEWS = {
    "name": "quant_show_views",
    "description": "Show recent per-analyst views for a given asset. Read-only. "
                   "Useful for understanding why the aggregator made a particular call.",
    "parameters": {
        "type": "object",
        "properties": {
            "asset": {"type": "string",
                      "description": "Asset to inspect (e.g. 'BTC/USDT')"},
            "analyst": {"type": "string",
                        "description": "Optional analyst name filter"},
            "n": {"type": "integer", "default": 10,
                  "description": "Max views per analyst"},
        },
        "required": ["asset"],
    },
}

QUANT_RECOMMEND = {
    "name": "quant_recommend",
    "description": "Get a snapshot-in-time trading recommendation for a single "
                   "symbol — runs the analyst pool, BMA aggregator, and risk "
                   "gate, and returns a structured advisor view with optional "
                   "journal lessons. Read-only; does NOT trade, does NOT update "
                   "calibrators, does NOT require a running daemon. Bootstrap "
                   "path: yfinance with no API key (per ADR-0014). Use this "
                   "when the user asks 'what does the system say about X' or "
                   "wants a sanity check on the daemon's current view. The "
                   "result includes 'caveats' and 'doctor' fields the operator "
                   "should surface back — these are NOT optional disclaimers.",
    "parameters": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "Ticker or pair (e.g., 'AAPL', 'SPY', 'BTC/USDT')",
            },
            "asset_class": {
                "type": "string",
                "enum": ["equity", "etf", "crypto", "fx"],
                "default": "equity",
            },
            "timeframe": {
                "type": "string",
                "enum": ["1m", "5m", "15m", "30m", "1h", "4h", "1d"],
                "description": "Bar timeframe; defaults: equity/etf=1d, crypto/fx=1h",
            },
            "lookback_bars": {
                "type": "integer",
                "minimum": 50,
                "maximum": 2000,
                "description": "How many bars of history to fetch; default per timeframe",
            },
            "include_lessons": {
                "type": "boolean",
                "default": True,
                "description": "Pull recent journal entries for context. Set false to save tokens.",
            },
            "as_of": {
                "type": "string",
                "description": "Optional ISO timestamp to anchor the view (replay mode); "
                               "bars filtered to <= as_of (lookahead enforcement). "
                               "Omit for live snapshot.",
            },
        },
        "required": ["symbol"],
    },
}


QUANT_DOCTOR = {
    "name": "quant_doctor",
    "description": "Run a comprehensive health check on the hermes-quant daemon, "
                   "data providers, analyst pool, broker connectivity, calibrators, "
                   "and Kronos availability. Read-only diagnostic.",
    "parameters": {
        "type": "object",
        "properties": {
            "calibration": {"type": "boolean", "default": False,
                            "description": "If true, include per-analyst calibration table (slower)"},
        },
        "required": [],
    },
}
