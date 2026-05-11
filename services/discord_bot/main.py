"""services/discord_bot/main.py — bot process entrypoint.

Wired in execution order:

  1. structlog configured per ``BotSettings.environment`` + log_level.
  2. Settings loaded from env (DISCORD_BOT_TOKEN, DISCORD_GUILD_ID,
     API_BASE_URL, DISCORD_BOT_API_BEARER_TOKEN).
  3. discord.py ``Client`` + ``CommandTree`` constructed with intents
     matching ``deploy/discord/manifest.json`` (GUILDS only — no
     MESSAGE_CONTENT, no GUILD_MEMBERS, no GUILD_PRESENCES).
  4. ``ApiClient`` instantiated with the bearer token and base URL.
  5. Three slash commands registered guild-scoped:
     ``/positions`` + ``/halt`` + ``/status``.
  6. ``client.run(token)`` connects to the Discord gateway WebSocket
     and blocks until SIGINT/SIGTERM. discord.py handles auto-reconnect
     internally; we don't add our own retry loop.

The bot is INTENDED to run as the only process inside the
``trading-discord-bot`` container — there's no FastAPI IPC listener
in Day 23. The Phase-1 backend → bot push path
(``POST /internal/discord/post`` per backend-spec §4.4) lands when the
Week 4 dispatcher + Phase-1 channel-fanout PR ships.

Cog-style organization is deliberately deferred — discord.py supports
both the cog-decorator style and the direct-tree-decorator style;
Phase 0's three commands are simple enough that the flat-tree style is
cleaner. Phase 1+ refactors to cogs when the command count crosses
~7-8.

Failure modes:

  * Missing env var → Pydantic raises at ``get_settings()`` (loud
    immediate exit).
  * Invalid bot token → discord.py raises ``LoginFailure`` at
    ``client.run`` (loud immediate exit).
  * Guild not found → discord.py raises ``NotFound`` at ``tree.sync``
    (loud immediate exit). The operator must invite the bot to the
    guild via the OAuth URL from ``deploy/discord/README.md`` Step 4.
  * api unreachable → first command invocation surfaces an
    ``ApiClientHTTPError`` to the operator via the embed; the bot stays
    connected.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import discord
import structlog
from discord import app_commands

from services.discord_bot.api_client import ApiClient
from services.discord_bot.commands import (
    register_halt,
    register_positions,
    register_status,
)
from services.discord_bot.config import BotSettings, get_settings

log = structlog.get_logger()


def _configure_structlog(settings: BotSettings) -> None:
    """Mirrors ``services/api/main._configure_structlog`` shape per dev-guide §3.5."""
    is_dev = settings.environment == "dev"
    level = getattr(logging, settings.log_level, logging.INFO)

    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp_utc"),
        structlog.processors.add_log_level,
        structlog.processors.CallsiteParameterAdder(
            [structlog.processors.CallsiteParameter.FUNC_NAME]
        ),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.dev.ConsoleRenderer() if is_dev else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _build_intents() -> discord.Intents:
    """Minimal intents matching deploy/discord/manifest.json.

    GUILDS is the only intent enabled. Slash commands fire via the
    interactions API which doesn't require MESSAGE_CONTENT or any
    privileged intent.
    """
    intents = discord.Intents.none()
    intents.guilds = True
    return intents


class TradingBotClient(discord.Client):
    """discord.py ``Client`` subclass wiring our ``CommandTree`` + lifecycle."""

    def __init__(
        self,
        *,
        api_client: ApiClient,
        settings: BotSettings,
    ) -> None:
        super().__init__(intents=_build_intents())
        self._api_client = api_client
        self._settings = settings
        self._tree = app_commands.CommandTree(self)
        self._guild = discord.Object(id=settings.discord_guild_id)
        self._register_commands()

    @property
    def tree(self) -> app_commands.CommandTree[TradingBotClient]:
        return self._tree

    def _register_commands(self) -> None:
        """Attach all Phase-0 slash commands to the tree, guild-scoped."""
        register_positions(
            self._tree,
            api_client=self._api_client,
            environment=self._settings.environment,
            guild=self._guild,
        )
        register_halt(
            self._tree,
            api_client=self._api_client,
            environment=self._settings.environment,
            guild=self._guild,
        )
        register_status(
            self._tree,
            api_client=self._api_client,
            guild=self._guild,
        )

    async def setup_hook(self) -> None:
        """discord.py lifecycle hook — runs once before gateway connect.

        ``tree.sync(guild=...)`` registers the slash commands with Discord's
        guild-scoped command registry. Guild-scoped registration propagates
        instantly (versus global which takes up to 1h). Single-operator
        deployment means we never need cross-guild fan-out.
        """
        synced = await self._tree.sync(guild=self._guild)
        log.info(
            "discord_commands_synced",
            guild_id=self._settings.discord_guild_id,
            command_count=len(synced),
            commands=[c.name for c in synced],
        )

    async def on_ready(self) -> None:
        # discord.py guarantees self.user is non-None inside on_ready
        user = self.user
        log.info(
            "discord_bot_ready",
            bot_user=str(user) if user else "<unknown>",
            guild_id=self._settings.discord_guild_id,
            environment=self._settings.environment,
        )

    async def close(self) -> None:
        log.info("discord_bot_closing")
        await self._api_client.aclose()
        await super().close()


async def _amain() -> int:
    settings = get_settings()
    _configure_structlog(settings)
    log.info(
        "discord_bot_starting",
        environment=settings.environment,
        api_base_url=settings.api_base_url,
        guild_id=settings.discord_guild_id,
    )
    api_client = ApiClient(
        base_url=settings.api_base_url,
        bearer_token=settings.discord_bot_api_bearer_token.get_secret_value(),
        timeout_seconds=settings.api_request_timeout_seconds,
    )
    client = TradingBotClient(api_client=api_client, settings=settings)
    try:
        await client.start(settings.discord_bot_token.get_secret_value())
    except discord.LoginFailure:
        log.error("discord_bot_login_failed")
        return 1
    except KeyboardInterrupt:
        log.info("discord_bot_keyboard_interrupt")
    finally:
        if not client.is_closed():
            await client.close()
    return 0


def main() -> int:
    """Synchronous entrypoint for ``python -m services.discord_bot.main``."""
    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
