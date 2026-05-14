"""hermes-quant — ARIA-powered multi-analyst algorithmic trading framework.

Distributed as a Hermes Agent plugin. See README.md and docs/adr/ for the
architecture. AGENTS.md has the development guide.

Plugin entry: register(ctx) — wires tools, slash command, CLI subcommands.
Daemon entry: hermes_quant.daemon.main:main — the long-lived signal loop.
"""
from __future__ import annotations

import logging
from typing import Any

from .protocol import (
    Action,
    AggregatedSignal,
    Aggregator,
    Analyst,
    AnalystView,
    AssetClass,
    Calibrator,
    DataProvider,
    DataProviderError,
    DataQualityError,
    Direction,
    EpisodeOutcome,
    HaltRecord,
    HaltState,
    HermesQuantError,
    MarketContext,
    MarketState,
    Portfolio,
    Position,
    RateLimitError,
    RealizedOutcome,
    RiskGate,
    SignalTooLarge,
    StatefulAnalyst,
    Timeframe,
)

__version__ = "0.4.0"

__all__ = [
    "Action",
    "AggregatedSignal",
    "Aggregator",
    "Analyst",
    "AnalystView",
    "AssetClass",
    "Calibrator",
    "DataProvider",
    "DataProviderError",
    "DataQualityError",
    "Direction",
    "EpisodeOutcome",
    "HaltRecord",
    "HaltState",
    "HermesQuantError",
    "MarketContext",
    "MarketState",
    "Portfolio",
    "Position",
    "RateLimitError",
    "RealizedOutcome",
    "RiskGate",
    "SignalTooLarge",
    "StatefulAnalyst",
    "Timeframe",
    "register",
]

logger = logging.getLogger(__name__)


def register(ctx: Any) -> None:
    """Hermes plugin entry point. Called ONCE at gateway startup.

    Registers tools, slash command, CLI subcommand tree, hooks, and the
    bundled hermes-quant skill. Does NOT spawn the daemon — daemon is
    opt-in via `hermes quant start` (per ADR-0007).

    Heavy imports are lazy. register() should run in <50ms.
    """
    from . import schemas
    from . import tools as quant_tools

    # Tools — read-only views into daemon state (ADR-0007)
    ctx.register_tool(
        name="quant_status",
        toolset="quant",
        schema=schemas.QUANT_STATUS,
        handler=quant_tools.quant_status,
    )
    ctx.register_tool(
        name="quant_show_signals",
        toolset="quant",
        schema=schemas.QUANT_SHOW_SIGNALS,
        handler=quant_tools.quant_show_signals,
    )
    ctx.register_tool(
        name="quant_show_views",
        toolset="quant",
        schema=schemas.QUANT_SHOW_VIEWS,
        handler=quant_tools.quant_show_views,
    )
    ctx.register_tool(
        name="quant_recommend",
        toolset="quant",
        schema=schemas.QUANT_RECOMMEND,
        handler=quant_tools.quant_recommend,
    )
    # HITL React surface (ADR-0015) — propose / approve / reject / list / lookup
    ctx.register_tool(
        name="quant_propose",
        toolset="quant",
        schema=schemas.QUANT_PROPOSE,
        handler=quant_tools.quant_propose,
    )
    ctx.register_tool(
        name="quant_approve",
        toolset="quant",
        schema=schemas.QUANT_APPROVE,
        handler=quant_tools.quant_approve,
    )
    ctx.register_tool(
        name="quant_reject",
        toolset="quant",
        schema=schemas.QUANT_REJECT,
        handler=quant_tools.quant_reject,
    )
    ctx.register_tool(
        name="quant_pending",
        toolset="quant",
        schema=schemas.QUANT_PENDING,
        handler=quant_tools.quant_pending,
    )
    ctx.register_tool(
        name="quant_proposal",
        toolset="quant",
        schema=schemas.QUANT_PROPOSAL,
        handler=quant_tools.quant_proposal,
    )
    # Autonomous-mode surface (ADR-0016) — silence-bias-gated paper trading
    ctx.register_tool(
        name="quant_autonomous_tick",
        toolset="quant",
        schema=schemas.QUANT_AUTONOMOUS_TICK,
        handler=quant_tools.quant_autonomous_tick,
    )
    ctx.register_tool(
        name="quant_autonomous_status",
        toolset="quant",
        schema=schemas.QUANT_AUTONOMOUS_STATUS,
        handler=quant_tools.quant_autonomous_status,
    )
    ctx.register_tool(
        name="quant_watchlist_add",
        toolset="quant",
        schema=schemas.QUANT_WATCHLIST_ADD,
        handler=quant_tools.quant_watchlist_add,
    )
    ctx.register_tool(
        name="quant_watchlist_remove",
        toolset="quant",
        schema=schemas.QUANT_WATCHLIST_REMOVE,
        handler=quant_tools.quant_watchlist_remove,
    )
    ctx.register_tool(
        name="quant_watchlist_list",
        toolset="quant",
        schema=schemas.QUANT_WATCHLIST_LIST,
        handler=quant_tools.quant_watchlist_list,
    )
    ctx.register_tool(
        name="quant_doctor",
        toolset="quant",
        schema=schemas.QUANT_DOCTOR,
        handler=quant_tools.quant_doctor,
    )

    # Slash command (works in CLI + gateway-multiplexed)
    ctx.register_command(
        "quant",
        handler=quant_tools.handle_quant_slash,
        description="hermes-quant: /quant status | recommend <SYM> | propose <SYM> | approve <ID> | reject <ID> <reason> | pending | doctor",
    )

    # CLI subcommand tree — control plane (ADR-0007 §canonical CLI)
    try:
        from . import cli as quant_cli
        ctx.register_cli_command(
            name="quant",
            help="hermes-quant: trading daemon control plane",
            setup_fn=quant_cli.setup_argparse,
            handler_fn=quant_cli.dispatch,
        )
    except Exception as exc:
        logger.warning("hermes-quant: CLI registration skipped: %s", exc, exc_info=True)

    # Skill — voice-mode usage / troubleshooting playbook
    try:
        from pathlib import Path
        skill_md = Path(__file__).parent / "skills" / "hermes-quant" / "SKILL.md"
        if skill_md.exists():
            ctx.register_skill("hermes-quant", skill_md)
    except Exception as exc:
        logger.warning("hermes-quant: skill registration skipped: %s", exc, exc_info=True)

    # Discord /quant slash command — deferred install via pre_gateway_dispatch
    # (per references/plugin-authoring.md "Discord slash-command fingerprint-skip")
    try:
        from .discord_slash import install_quant_slash_on_pre_dispatch
        ctx.register_hook("pre_gateway_dispatch", install_quant_slash_on_pre_dispatch)
    except Exception as exc:
        logger.warning("hermes-quant: Discord slash hook registration skipped: %s",
                       exc, exc_info=True)

    logger.info("hermes-quant plugin v%s registered", __version__)
