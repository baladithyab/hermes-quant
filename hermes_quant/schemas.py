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
                "description": "Asset class. Defaults to recipe asset_class when recipe_id is provided, else equity.",
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
            "recipe_id": {
                "type": "string",
                "description": "Optional PDR recipe id (default: btc-usdt-mvp). Selects analyst/aggregator/risk-gate composition.",
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
            "semantic_packets": {
                "type": "array",
                "description": "Optional precomputed Hermes semantic packets for HermesSemanticAnalyst. Read-only input artifacts; the tool does not call models.",
                "items": {"type": "object"},
            },
            "committee_turns": {
                "type": "array",
                "description": "Optional model-backed deliberation turns for DeliberativeCommitteeAggregator. Must be replayable artifacts, not live calls.",
                "items": {"type": "object"},
            },
        },
        "required": ["symbol"],
    },
}


QUANT_RECIPES = {
    "name": "quant_recipes",
    "description": "List available PDR recipes — named Perceive-Decide-React trading-system compositions. Read-only.",
    "parameters": {
        "type": "object",
        "properties": {},
    },
}


# ---------------------------------------------------------------------------
# HITL React surface (ADR-0015)
# ---------------------------------------------------------------------------

QUANT_PROPOSE = {
    "name": "quant_propose",
    "description": "Propose a trade for human-in-the-loop approval (ADR-0015). "
                   "Runs the same advisor pipeline as quant_recommend, then "
                   "registers a PENDING proposal with a TTL (default 15 min) "
                   "that the operator must approve via quant_approve or reject "
                   "via quant_reject. Returns proposal_id + the full advisor "
                   "result. Requires config quant.pdr.mode=hitl (returns a "
                   "mode_mismatch error otherwise). The proposal is stored "
                   "durably; approval triggers a paper-mode React (executions "
                   "bus write) that the calibrator learns from.",
    "parameters": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string",
                       "description": "Ticker or pair (e.g., 'AAPL', 'BTC/USDT')"},
            "asset_class": {"type": "string",
                            "enum": ["equity", "etf", "crypto", "fx"],
                            "default": "equity"},
            "timeframe": {"type": "string",
                          "enum": ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]},
            "lookback_bars": {"type": "integer", "minimum": 50, "maximum": 2000},
            "ttl_minutes": {"type": "integer", "minimum": 1, "maximum": 1440,
                            "default": 15,
                            "description": "Proposal expires this many minutes from creation. "
                                           "Expiration = automatic rejection with "
                                           "reason='ttl_elapsed'."},
            "as_of": {"type": "string",
                      "description": "Optional ISO timestamp anchor (replay mode)"},
        },
        "required": ["symbol"],
    },
}


QUANT_APPROVE = {
    "name": "quant_approve",
    "description": "Approve a pending proposal — fires the React adapter to "
                   "execute the trade in paper mode (writes to executions.jsonl "
                   "for the daemon's calibrator to consume). Per ADR-0015, "
                   "v0.1.2 only supports paper mode; live brokers gated to "
                   "v0.2 with explicit --live opt-in. Approval advances "
                   "pending → approved one-way; expired or already-approved "
                   "proposals return an error.",
    "parameters": {
        "type": "object",
        "properties": {
            "proposal_id": {"type": "string",
                            "description": "The proposal_id returned by quant_propose"},
            "size_override_pct": {
                "type": "number",
                "description": "Optional signed override of the advisor's "
                               "Kelly fraction (e.g. -0.03 for 3% short, "
                               "0.025 for 2.5% long). Omit to use the "
                               "advisor's recommendation.",
            },
        },
        "required": ["proposal_id"],
    },
}


QUANT_REJECT = {
    "name": "quant_reject",
    "description": "Reject a pending proposal with a reason (required). The "
                   "rejection persists to the settlement journal as a 'human "
                   "override' lesson; if quant.calibration.learn_from_rejections "
                   "is true (default), the calibrator updates as if the trade's "
                   "outcome were the opposite of its predicted direction "
                   "(per ADR-0015 §D8). Rejecting an expired proposal returns "
                   "an error.",
    "parameters": {
        "type": "object",
        "properties": {
            "proposal_id": {"type": "string"},
            "reason": {"type": "string",
                       "description": "Why are you rejecting? Becomes a journal entry. "
                                      "Required, non-empty."},
        },
        "required": ["proposal_id", "reason"],
    },
}


QUANT_PENDING = {
    "name": "quant_pending",
    "description": "List currently-pending proposals (sweeps expired ones first). "
                   "Read-only.",
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
            "symbol": {"type": "string",
                       "description": "Optional filter by symbol"},
        },
        "required": [],
    },
}


QUANT_PROPOSAL = {
    "name": "quant_proposal",
    "description": "Look up a single proposal's full record by proposal_id, "
                   "including state, advisor result, and approval/rejection "
                   "metadata. Read-only.",
    "parameters": {
        "type": "object",
        "properties": {
            "proposal_id": {"type": "string"},
        },
        "required": ["proposal_id"],
    },
}


# ---------------------------------------------------------------------------
# Autonomous-mode surface (ADR-0016)
# ---------------------------------------------------------------------------

QUANT_AUTONOMOUS_TICK = {
    "name": "quant_autonomous_tick",
    "description": "Run a single autonomous-mode tick (ADR-0016) over the "
                   "configured watchlist. The orchestrator: Perceive (advisor) "
                   "-> Decide (BMA + risk gate inside advisor) -> Gate "
                   "(4-dim silence-bias) -> React (PaperReactor on FIRE). "
                   "Per ADR-0016 §D11, this tool DEFAULTS TO DRY RUN — the "
                   "tool surface is agent-callable and dry-run is the safe "
                   "default. Real (paper) trades fire when invoked from the "
                   "Hermes cron-script path with dry_run=False. Requires "
                   "config quant.pdr.mode=autonomous (returns mode_mismatch "
                   "otherwise). Returns structured per-symbol decisions + "
                   "fire/silence/error counts + kill-switch state.",
    "parameters": {
        "type": "object",
        "properties": {
            "dry_run": {
                "type": "boolean",
                "default": True,
                "description": "When True (default for the tool surface), "
                               "report what would happen without firing the "
                               "React adapter. The cron-script path sets "
                               "dry_run=False to fire real paper trades.",
            },
        },
        "required": [],
    },
}


QUANT_AUTONOMOUS_STATUS = {
    "name": "quant_autonomous_status",
    "description": "Show current autonomous-mode state: PDR mode, watchlist, "
                   "silence-bias config, kill-switch state, recent tick "
                   "summary. Read-only.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


QUANT_WATCHLIST_ADD = {
    "name": "quant_watchlist_add",
    "description": "Add a symbol to the autonomous-mode watchlist (ADR-0016). "
                   "Idempotent on (symbol, asset_class) — duplicates are "
                   "replaced, not appended. Persists to "
                   "~/.hermes/config.yaml::quant.autonomous.watchlist.",
    "parameters": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string",
                       "description": "Ticker or pair (e.g. 'AAPL', 'BTC/USDT')"},
            "asset_class": {"type": "string",
                            "enum": ["equity", "etf", "crypto", "fx"],
                            "default": "equity"},
            "timeframe": {"type": "string",
                          "enum": ["1m", "5m", "15m", "30m", "1h", "4h", "1d"],
                          "description": "Bar timeframe; defaults per asset class"},
        },
        "required": ["symbol", "asset_class"],
    },
}


QUANT_WATCHLIST_REMOVE = {
    "name": "quant_watchlist_remove",
    "description": "Remove a symbol from the autonomous-mode watchlist. "
                   "Returns whether anything was removed.",
    "parameters": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "asset_class": {"type": "string",
                            "enum": ["equity", "etf", "crypto", "fx"],
                            "description": "Optional; if set, only remove "
                                           "entries matching this asset_class"},
        },
        "required": ["symbol"],
    },
}


QUANT_WATCHLIST_LIST = {
    "name": "quant_watchlist_list",
    "description": "List current autonomous-mode watchlist entries. Read-only.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
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
