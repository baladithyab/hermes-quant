"""hermes_quant.discord_slash — Deferred /quant slash command install.

Per references/plugin-authoring.md (Discord slash-command fingerprint-skip):

1. register(ctx) runs BEFORE bot login — the tree is None, so register-time
   walker fails silently.
2. Hermes' adapter computes a slash fingerprint over its own commands and
   short-circuits the implicit re-sync on subsequent restarts.
3. Plugin slashes need an explicit `await tree.sync()` after a late
   `add_command` or they stay invisible to Discord users.

The fix is a deferred install via `pre_gateway_dispatch` hook + forced
explicit sync. Lifted near-verbatim from hermes-s2s `voice/slash.py` —
attribution + similar pattern.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_QUANT_COMMAND_INSTALLED = "_hermes_quant_command_installed"


def install_quant_slash_on_pre_dispatch(**kwargs: Any) -> None:
    """Hook handler. Runs on every inbound message via pre_gateway_dispatch.
    Idempotent via sentinel attribute on the tree."""
    state = _get_state()
    if state["installed"]:
        return None
    gateway = kwargs.get("gateway")
    if gateway is None:
        return None
    adapters = getattr(gateway, "adapters", {}) or {}
    adapter = adapters.get("discord") if isinstance(adapters, dict) else None
    if adapter is None:
        return None
    try:
        installed = _install_quant_command_on_adapter(adapter)
    except Exception as exc:
        logger.warning("hermes-quant: deferred /quant install failed: %s", exc, exc_info=True)
        return None
    if installed:
        state["installed"] = True
        logger.info("hermes-quant: /quant slash installed on first gateway-dispatch")
    return None


def _get_state() -> dict:
    """Per-process state for idempotency. Module-global is fine since
    register() runs once per process."""
    global _STATE
    try:
        return _STATE
    except NameError:
        _STATE = {"installed": False}
        return _STATE


def _install_quant_command_on_adapter(adapter: Any) -> bool:
    """Install /quant on a live DiscordAdapter. Forces tree.sync() to bypass
    the fingerprint-skip. Returns True if install fired this dispatch."""
    client = getattr(adapter, "_client", None) or getattr(adapter, "client", None)
    if client is None:
        return False
    tree = getattr(client, "tree", None)
    if tree is None:
        return False
    if getattr(tree, _QUANT_COMMAND_INSTALLED, False):
        return False    # already installed by some other path

    # Defer the discord.py import — keeps register-time fast
    try:
        from discord import app_commands
    except ImportError:
        logger.debug("hermes-quant: discord.py not available; /quant not installed")
        return False

    @app_commands.command(name="quant",
                          description="hermes-quant: status, signals, doctor")
    @app_commands.describe(subcommand="status | signals | doctor",
                            arg="optional: asset symbol or N")
    async def quant_cmd(interaction, subcommand: str = "status", arg: str = ""):
        from .tools import handle_quant_slash
        sub_args = [subcommand]
        if arg:
            sub_args.append(arg)
        try:
            result = handle_quant_slash(sub_args)
            await interaction.response.send_message(
                f"```json\n{result[:1900]}\n```", ephemeral=True
            )
        except Exception as exc:
            await interaction.response.send_message(
                f"hermes-quant error: {exc}", ephemeral=True
            )

    tree.add_command(quant_cmd)
    setattr(tree, _QUANT_COMMAND_INSTALLED, True)

    # Force re-sync — the adapter's fingerprint-skip would otherwise hide
    # this from Discord's UI
    loop = getattr(client, "loop", None)
    if loop is not None and not loop.is_closed():
        async def _resync():
            try:
                await tree.sync()
            except Exception as exc:
                logger.warning("hermes-quant: post-install tree.sync() failed: %s", exc)
        asyncio.run_coroutine_threadsafe(_resync(), loop)

    return True
