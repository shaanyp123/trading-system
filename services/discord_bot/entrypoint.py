"""services/discord_bot/entrypoint.py — container entrypoint.

Reads the sops-decrypted secrets bundle (``/run/secrets/decrypted.yaml``,
written by the host-side sops -d step in ``deploy/day5-bringup.sh``),
exports the fields the bot needs as bare ``DISCORD_*`` / ``API_*`` env
vars, then exec()s ``python -m services.discord_bot.main`` so signals
(SIGTERM from docker stop) flow correctly to the asyncio gateway loop.

Why a separate entrypoint (mirrors ``services/api/entrypoint.py``):

  * The bot reads ``DISCORD_BOT_TOKEN``, ``DISCORD_GUILD_ID``, and
    ``DISCORD_BOT_API_BEARER_TOKEN`` as bare env vars per its
    ``BotSettings`` pydantic-settings config (no env_prefix). The sops
    bundle stores these under ``discord.bot_token``,
    ``discord.guild_id``, ``discord.api_bearer_token`` — yaml-to-env
    translation happens here so the bot module never has to parse yaml.
  * Failing fast at startup (missing required key → exit code 2) is
    operator-friendlier than the gateway connecting + then dying on the
    first command because of a missing api bearer.

This file deliberately uses stdlib + pyyaml only — same constraint as
the api's entrypoint.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import yaml

DEFAULT_SECRETS_PATH = Path("/run/secrets/decrypted.yaml")


def _looks_like_placeholder(value: str | int | None) -> bool:
    if value is None:
        return True
    s = str(value)
    if not s:
        return True
    return s.startswith("<TODO") or s == "null"


def _load_secrets(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    return loaded or {}


def _exit(message: str, code: int = 2) -> int:
    print(f"[discord-bot-entrypoint] {message}", file=sys.stderr, flush=True)
    return code


def main(argv: list[str] | None = None) -> int:
    secrets_path = Path(os.environ.get("BOT_SECRETS_PATH", str(DEFAULT_SECRETS_PATH)))
    secrets = _load_secrets(secrets_path)
    discord_secrets = secrets.get("discord") or {}

    # --- Required: DISCORD_BOT_TOKEN ----------------------------------------
    if "DISCORD_BOT_TOKEN" not in os.environ:
        bot_token = discord_secrets.get("bot_token")
        if _looks_like_placeholder(bot_token):
            return _exit(
                f"discord.bot_token missing or placeholder in {secrets_path}; "
                "fill via `sops secrets/<env>.enc.yaml` per deploy/discord_bot/README.md",
            )
        os.environ["DISCORD_BOT_TOKEN"] = str(bot_token)

    # --- Required: DISCORD_GUILD_ID ----------------------------------------
    if "DISCORD_GUILD_ID" not in os.environ:
        guild_id = discord_secrets.get("guild_id")
        if _looks_like_placeholder(guild_id):
            return _exit(
                f"discord.guild_id missing or placeholder in {secrets_path}; "
                "fill via `sops secrets/<env>.enc.yaml` per deploy/discord_bot/README.md",
            )
        os.environ["DISCORD_GUILD_ID"] = str(guild_id)

    # --- Required: DISCORD_BOT_API_BEARER_TOKEN ----------------------------
    if "DISCORD_BOT_API_BEARER_TOKEN" not in os.environ:
        api_bearer = discord_secrets.get("api_bearer_token")
        if _looks_like_placeholder(api_bearer):
            return _exit(
                f"discord.api_bearer_token missing or placeholder in {secrets_path}; "
                "this token must match API_DISCORD_BOT_BEARER_TOKEN on the api side; "
                "generate via `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` "
                "and add to both deploy paths per deploy/discord_bot/README.md",
            )
        os.environ["DISCORD_BOT_API_BEARER_TOKEN"] = str(api_bearer)

    # --- Optional env vars (default to dev/INFO/http://api:8000) ------------
    os.environ.setdefault("ENVIRONMENT", os.environ.get("ENVIRONMENT", "dev"))
    os.environ.setdefault("LOG_LEVEL", os.environ.get("LOG_LEVEL", "INFO"))
    os.environ.setdefault("API_BASE_URL", os.environ.get("API_BASE_URL", "http://api:8000"))

    cmd = argv or ["python", "-m", "services.discord_bot.main"]
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    sys.exit(main())
