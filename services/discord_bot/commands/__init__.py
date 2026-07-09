"""services/discord_bot/commands package — slash command modules.

Each module exposes a single async ``register(tree, ...)`` function that
attaches its slash command to the discord.py ``CommandTree``. The bot's
``main.py`` calls ``register`` for each command in :data:`ALL_COMMANDS`
during startup, then ``tree.sync(guild=...)`` registers them with
Discord (guild-scoped per ``BotSettings.discord_guild_id`` for instant
propagation).

Phase 0 commands (per implementation-guide §3 Week 6 Thu):

  * :mod:`services.discord_bot.commands.positions` — ``/positions``
  * :mod:`services.discord_bot.commands.halt`      — ``/halt <reason>``
  * :mod:`services.discord_bot.commands.status`    — ``/status``

Post-pivot commands:

  * ``/approve`` — RETIRED (crypto-pivot C0-B4, delta spec §3.8:
    announce-only Discord; no per-trade approval
    signal from any channel without opening the web app)
  * :mod:`services.discord_bot.commands.cycle` — ``/cycle`` (crypto-pivot
    §3.8: latest daily-decision digest; announce-only)

Phase 1+ command modules land in subsequent PRs (one slash command per
module per spec §6.7 layout).
"""

from services.discord_bot.commands.capital import register_capital_event_commands
from services.discord_bot.commands.cycle import register_cycle
from services.discord_bot.commands.halt import register_halt
from services.discord_bot.commands.positions import register_positions
from services.discord_bot.commands.status import register_status

__all__ = [
    "register_capital_event_commands",
    "register_cycle",
    "register_halt",
    "register_positions",
    "register_status",
]
