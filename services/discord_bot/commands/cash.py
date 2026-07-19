"""services/discord_bot/commands/cash.py — ``/cash-recapture`` slash command.

Amendment C re-capture hook (C1→C2 build): after the operator converts
USDC↔USD, today's 00:20 UTC cash-balance snapshot is stale and the
hard-halt floor basis double-counts (or misses) the converted amount
for up to ~24 h (risk-review residual, decisions-log 2026-07-18 PR 1).
This command triggers ``POST /api/system/cash-capture``, which re-reads
the venue balances and UPSERTs today's ``cash_balance_snapshots`` row —
the operator runs it immediately after a conversion so the floor basis
tracks reality.

No confirm view (unlike ``/capital-deposit``): the re-capture is an
observation refresh — it moves no money and no risk knob; the worst
outcome of a stray invocation is an extra accurate snapshot. Mirrors
``/gates``' simple defer → call → embed shape. ANNOUNCE-ONLY (dev-guide
§1.5 locked): bare embeds, no components.

IMPORT-GRAPH CONSTRAINT: nothing under ``services.webhook_pusher.*``
may be imported here (slim bot image has no sqlalchemy — see
``commands/cycle.py`` for the full rationale).
"""

from __future__ import annotations

import discord
import structlog
from discord import app_commands

from services.discord_bot.api_client import (
    ApiClient,
    ApiClientHTTPError,
)
from services.discord_bot.embeds import EMBED_COLOR_CRITICAL, EMBED_COLOR_OK

log = structlog.get_logger()


def _fmt_usd(raw: object) -> str:
    """Decimal-as-string passthrough with a $ prefix ('—' when absent)."""
    if not isinstance(raw, str) or not raw:
        return "—"
    return f"-${raw[1:]}" if raw.startswith("-") else f"${raw}"


def build_cash_recapture_embed(payload: dict[str, object]) -> discord.Embed:
    """Success embed for a completed re-capture ([A05]: string passthrough)."""
    replaced = bool(payload.get("replaced"))
    action = "replaced today's snapshot" if replaced else "wrote today's FIRST snapshot"
    embed = discord.Embed(
        title="✅ Cash balances re-captured",
        description=(
            f"Venue balances re-read and {action} "
            f"({payload.get('snapshot_date_utc', '?')}). The hard-halt "
            "floor basis (Amendment C) now reads this capture."
        ),
        color=EMBED_COLOR_OK,
    )
    embed.add_field(name="Spot USD", value=_fmt_usd(payload.get("spot_usd")), inline=True)
    embed.add_field(name="CBI USDC", value=_fmt_usd(payload.get("cbi_usdc")), inline=True)
    return embed


def register_cash_recapture(
    tree: app_commands.CommandTree[discord.Client],
    *,
    api_client: ApiClient,
    guild: discord.abc.Snowflake | None = None,
) -> None:
    """Attach the ``/cash-recapture`` slash command to the given ``CommandTree``."""

    @tree.command(
        name="cash-recapture",
        description=(
            "Re-capture CBI cash balances after a USDC<->USD conversion "
            "(refreshes the halt-floor basis)."
        ),
        guild=guild,
    )
    async def cash_recapture(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        log.info("discord_command_invoked", command="/cash-recapture", user_id=interaction.user.id)
        try:
            payload = await api_client.invoke_cash_recapture()
        except ApiClientHTTPError as exc:
            log.warning(
                "discord_command_api_error",
                command="/cash-recapture",
                status_code=exc.status_code,
                error_code=exc.error_code,
            )
            await interaction.followup.send(
                embed=discord.Embed(
                    title=f"❌ /cash-recapture failed — {exc.error_code}",
                    description=(f"HTTP {exc.status_code}\n{exc.message}"),
                    color=EMBED_COLOR_CRITICAL,
                ),
                ephemeral=True,
            )
            return
        log.info(
            "discord_cash_recapture_completed",
            user_id=interaction.user.id,
            replaced=payload.get("replaced"),
        )
        await interaction.followup.send(embed=build_cash_recapture_embed(payload), ephemeral=True)


__all__ = ["build_cash_recapture_embed", "register_cash_recapture"]
