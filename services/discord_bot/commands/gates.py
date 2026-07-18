"""services/discord_bot/commands/gates.py — `/gates` slash command.

Strategy §10 acceptance-gate tracker (C1→C2 build): on-demand status of
gates A1-A4 / B1-B3 — value vs target per gate, days live vs the 45-day
Phase-A minimum, and honest evidence notes (A1 statement matching and A3
manual drills are operator-attested, not computed). Data via
``GET /api/system/gates`` through the same bearer-auth
:class:`~services.discord_bot.api_client.ApiClient` as ``/cycle``.

ANNOUNCE-ONLY (dev-guide §1.5 locked): the reply is a bare embed — no
buttons, no approval affordances. The embed is rendered by the SHARED
builder ``services.discord_shared.gates_digest.build_gates_embed``
(whose one-line sibling feeds the 00:10 UTC nightly digest's "§10
gates" field), keeping on-demand and scheduled renders consistent.

IMPORT-GRAPH CONSTRAINT: nothing under ``services.webhook_pusher.*``
may be imported here (slim bot image has no sqlalchemy — see
``commands/cycle.py`` for the full rationale).
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
from services.discord_bot.embeds import EMBED_COLOR_CRITICAL
from services.discord_shared.gates_digest import build_gates_embed

log = structlog.get_logger()


def register_gates(
    tree: app_commands.CommandTree[discord.Client],
    *,
    api_client: ApiClient,
    guild: discord.abc.Snowflake | None = None,
) -> None:
    """Attach the ``/gates`` slash command to the given ``CommandTree``."""

    @tree.command(
        name="gates",
        description="Show §10 acceptance-gate status (A1-A4, B1-B3; announce-only).",
        guild=guild,
    )
    async def gates(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        log.info("discord_command_invoked", command="/gates", user_id=interaction.user.id)
        try:
            payload = await api_client.get_system_gates()
        except ApiClientHTTPError as exc:
            log.warning(
                "discord_command_api_error",
                command="/gates",
                status_code=exc.status_code,
                error_code=exc.error_code,
            )
            await interaction.followup.send(
                embed=discord.Embed(
                    title=f"❌ /gates failed — {exc.error_code}",
                    description=(f"HTTP {exc.status_code}\n{exc.message}"),
                    color=EMBED_COLOR_CRITICAL,
                ),
                ephemeral=True,
            )
            return
        now = datetime.now(tz=UTC)
        embed = discord.Embed.from_dict(build_gates_embed(payload, now_utc=now))
        await interaction.followup.send(embed=embed, ephemeral=True)


__all__ = ["register_gates"]
