"""services/discord_bot/commands/status.py — `/status` slash command.

Frontend-spec §6.3 ``/status`` (Phase 0+):

  * Args: none
  * Response: ephemeral
  * Backend: ``GET /api/health`` → format embed

Phase 0 ships the health-only subset; the spec §6.3 examples include
risk_state + vacation + watchdog + recon + open positions count + pending
signals count + strategy version. Those expansions land Phase 1 when:
  - the bot also subscribes to SSE for real-time risk_state cache, OR
  - a dedicated ``GET /api/system/status`` projection lands (already
    shipped Day 15 — but we deliberately use ``/api/health`` for Day 23
    because the IG line 393 verification gate is "bot connected; backend
    API reachable from bot" and ``/api/health`` is the canonical liveness
    probe per backend-spec §7.3).
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
    build_status_embed,
)

log = structlog.get_logger()


def register_status(
    tree: app_commands.CommandTree[discord.Client],
    *,
    api_client: ApiClient,
    guild: discord.abc.Snowflake | None = None,
) -> None:
    """Attach the ``/status`` slash command to the given ``CommandTree``."""

    @tree.command(
        name="status",
        description="Show backend health (operator-only ephemeral reply).",
        guild=guild,
    )
    async def status(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        log.info("discord_command_invoked", command="/status", user_id=interaction.user.id)
        try:
            response = await api_client.get_health()
        except ApiClientHTTPError as exc:
            log.warning(
                "discord_command_api_error",
                command="/status",
                status_code=exc.status_code,
                error_code=exc.error_code,
            )
            await interaction.followup.send(
                embed=discord.Embed(
                    title=f"❌ /status failed — {exc.error_code}",
                    description=(f"HTTP {exc.status_code}\n{exc.message}"),
                    color=EMBED_COLOR_CRITICAL,
                ),
                ephemeral=True,
            )
            return
        now = datetime.now(tz=UTC)
        embed = build_status_embed(response, now=now)
        await interaction.followup.send(embed=embed, ephemeral=True)


__all__ = ["register_status"]
