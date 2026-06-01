"""services/discord_bot/commands/bar_sync.py — `/barsync` slash command.

Answers "how did the last bar_sync cycle go?" from Discord without the
operator SSHing to the VPS to grep ``bar_sync_cycle_completed`` in
journald.

  * Args: none
  * Response: ephemeral (operator-only)
  * Backend: ``GET /api/system/bar-sync`` → :func:`build_bar_sync_embed`

A ``status="pending"`` body (no cycle since the api last restarted) is a
valid 200 and renders as an info embed, not an error — only a transport /
HTTP failure produces the red error embed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import discord
import structlog
from discord import app_commands

from services.discord_bot.api_client import (
    ApiClient,
    ApiClientHTTPError,
)
from services.discord_bot.embeds import (
    EMBED_COLOR_CRITICAL,
    build_bar_sync_embed,
)

log = structlog.get_logger()


def register_bar_sync(
    tree: app_commands.CommandTree[discord.Client],
    *,
    api_client: ApiClient,
    environment: str,
    guild: discord.abc.Snowflake | None = None,
) -> None:
    """Attach the ``/barsync`` slash command to the given ``CommandTree``."""

    @tree.command(
        name="barsync",
        description="Show the last bar_sync cycle outcome (operator-only ephemeral reply).",
        guild=guild,
    )
    async def barsync(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        log.info("discord_command_invoked", command="/barsync", user_id=interaction.user.id)
        try:
            response = await api_client.get_bar_sync_status()
        except ApiClientHTTPError as exc:
            log.warning(
                "discord_command_api_error",
                command="/barsync",
                status_code=exc.status_code,
                error_code=exc.error_code,
            )
            await interaction.followup.send(
                embed=discord.Embed(
                    title=f"❌ /barsync failed — {exc.error_code}",
                    description=(f"HTTP {exc.status_code}\n{exc.message}"),
                    color=EMBED_COLOR_CRITICAL,
                ),
                ephemeral=True,
            )
            return
        now = datetime.now(tz=UTC)
        embed = build_bar_sync_embed(response, environment=environment, now=now)
        await interaction.followup.send(embed=embed, ephemeral=True)


__all__ = ["register_bar_sync"]
