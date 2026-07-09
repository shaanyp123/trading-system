"""services/discord_bot/commands/cycle.py — `/cycle` slash command.

Crypto-pivot delta spec §3.8: on-demand digest of the latest 00:05 UTC
daily decision — per-asset trend score → target → action → est. costs,
plus the 30 s risk-loop heartbeat and the next Friday CDE close. Data
via ``GET /api/system/cycle`` (§3.7) through the same bearer-auth
:class:`~services.discord_bot.api_client.ApiClient` as ``/status``.

ANNOUNCE-ONLY (dev-guide §1.5 locked; operator mandate 2026-07-08): the
reply is a bare embed — NO buttons, NO confirm views, NO approval
affordances of any kind. The embed is rendered by the SHARED builder
``services.discord_shared.cycle_digest.build_cycle_digest_embed`` (the
same function behind the scheduled 00:10 UTC #daily-brief push), so the
on-demand and scheduled digests are identical.

IMPORT-GRAPH CONSTRAINT: this module (and the whole discord_bot package)
must import NOTHING under ``services.webhook_pusher.*`` — that package's
``__init__`` eagerly imports its sqlalchemy-touching dispatcher, and the
slim bot image ships no sqlalchemy (the exact ImportError that failed
the discord_bot docker-build CI job when the builder briefly lived
there). The shared builder therefore lives in the dependency-neutral
``services.discord_shared`` package (stdlib-only import graph; locked by
``tests/unit/test_discord_shared_import_hygiene.py``).

The no-decision-yet / worker-down states render as a digest that says so
(amber embed) — never an error reply.
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
from services.discord_shared.cycle_digest import build_cycle_digest_embed

log = structlog.get_logger()


def register_cycle(
    tree: app_commands.CommandTree[discord.Client],
    *,
    api_client: ApiClient,
    guild: discord.abc.Snowflake | None = None,
) -> None:
    """Attach the ``/cycle`` slash command to the given ``CommandTree``."""

    @tree.command(
        name="cycle",
        description="Show the latest daily-decision digest (announce-only).",
        guild=guild,
    )
    async def cycle(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        log.info("discord_command_invoked", command="/cycle", user_id=interaction.user.id)
        try:
            payload = await api_client.get_system_cycle()
        except ApiClientHTTPError as exc:
            log.warning(
                "discord_command_api_error",
                command="/cycle",
                status_code=exc.status_code,
                error_code=exc.error_code,
            )
            await interaction.followup.send(
                embed=discord.Embed(
                    title=f"❌ /cycle failed — {exc.error_code}",
                    description=(f"HTTP {exc.status_code}\n{exc.message}"),
                    color=EMBED_COLOR_CRITICAL,
                ),
                ephemeral=True,
            )
            return
        now = datetime.now(tz=UTC)
        embed = discord.Embed.from_dict(build_cycle_digest_embed(payload, now_utc=now))
        await interaction.followup.send(embed=embed, ephemeral=True)


__all__ = ["register_cycle"]
