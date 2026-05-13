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

  * :mod:`services.discord_bot.commands.approve`   — ``/approve <signal_id>``
    (closes PR #138 follow-up #1; lets the operator approve a pending
    signal from any channel without opening the web app)

Phase 1+ command modules land in subsequent PRs (one slash command per
module per spec §6.7 layout).
"""

from services.discord_bot.commands.approve import register_approve
from services.discord_bot.commands.halt import register_halt
from services.discord_bot.commands.positions import register_positions
from services.discord_bot.commands.status import register_status

__all__ = [
    "register_approve",
    "register_halt",
    "register_positions",
    "register_status",
]
