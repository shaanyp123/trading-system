"""services/discord_bot/commands/positions.py — `/positions` slash command.

Frontend-spec §6.3 ``/positions`` (Phase 0+):

  * Args: none
  * Response: ephemeral (only operator sees the reply)
  * Backend: ``GET /api/positions/current`` → format embed

Phase 0 reality: the api returns ``positions=[]`` until the Week 4 Wed
dispatcher wires fills from QC ObjectStore into ``positions_current``.
The :func:`build_positions_embed` builder renders the empty-state UX
("No open positions.") with an explanatory parenthetical.
"""

from __future__ import annotations

import discord
import structlog
from discord import app_commands

from services.discord_bot.api_client import (
    ApiClient,
    ApiClientHTTPError,
)
from services.discord_bot.embeds import (
    EMBED_COLOR_CRITICAL,
    build_positions_embed,
)

log = structlog.get_logger()


def register_positions(
    tree: app_commands.CommandTree[discord.Client],
    *,
    api_client: ApiClient,
    environment: str,
    guild: discord.abc.Snowflake | None = None,
) -> None:
    """Attach the ``/positions`` slash command to the given ``CommandTree``.

    Pattern:
      * The handler defers the interaction (``ephemeral=True``) so the
        api round-trip can take >3s without Discord timing out.
      * On success: send the embed via ``followup.send`` (deferred
        responses must use ``followup``).
      * On api error: P2 toast-style error embed with the canonical
        envelope's error_code + message.
    """

    @tree.command(
        name="positions",
        description="Show open positions (operator-only ephemeral reply).",
        guild=guild,
    )
    async def positions(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        log.info("discord_command_invoked", command="/positions", user_id=interaction.user.id)
        try:
            response = await api_client.get_positions_current()
        except ApiClientHTTPError as exc:
            log.warning(
                "discord_command_api_error",
                command="/positions",
                status_code=exc.status_code,
                error_code=exc.error_code,
            )
            await interaction.followup.send(
                embed=discord.Embed(
                    title=f"❌ /positions failed — {exc.error_code}",
                    description=(f"HTTP {exc.status_code}\n{exc.message}"),
                    color=EMBED_COLOR_CRITICAL,
                ),
                ephemeral=True,
            )
            return
        embed = build_positions_embed(response, environment=environment)
        await interaction.followup.send(embed=embed, ephemeral=True)


__all__ = ["register_positions"]
