"""services/discord_bot/config.py — typed runtime configuration.

Pydantic-Settings BaseSettings reading from environment variables. The
container entrypoint (``services/api/entrypoint.py`` style — secrets
secrets bundle → env vars at compose-up time) sets these before the bot
process starts.

Required env vars per Day 23 scope:

  * ``DISCORD_BOT_TOKEN`` — Discord application bot token (sourced from
    ``secrets/<env>.enc.yaml`` ``discord.bot_token``).
  * ``DISCORD_GUILD_ID`` — operator's single dev/paper/live guild ID
    (sourced from ``secrets/<env>.enc.yaml`` ``discord.guild_id``). The
    bot registers slash commands GUILD-scoped to this single guild — fast
    propagation (instant) versus global registration's 1h delay. The
    single-operator system never needs cross-guild fan-out.
  * ``API_BASE_URL`` — base URL for the api service. On the ``trading_internal``
    Docker network this is ``http://api:8000`` (no TLS, no Caddy hop). Phase 2+
    when the bot moves out-of-network it becomes ``https://<your-domain>``;
    Day 23 ships only the internal-network mode per backend-spec §6.7
    "REST calls for command responses (over internal Docker network;
    localhost-bound)".
  * ``DISCORD_BOT_API_BEARER_TOKEN`` — shared bearer token between bot and
    api. The api validates this header (``Authorization: Bearer <token>``)
    via ``services/api/middleware.BotAuthMiddleware`` and on match injects
    a service-account ``SessionContext`` (username=``discord-bot``,
    role=``owner``, auth_strength=``strong``). Sourced from
    ``secrets/<env>.enc.yaml`` ``discord.api_bearer_token``; rotated
    quarterly per spec §6.6 with a 1h overlap window during rotation
    (operator deploys both sides with the new token then revokes the old).

Operational env vars (defaults sufficient for Day 23):

  * ``LOG_LEVEL`` — structlog filter level. Default ``INFO``.
  * ``ENVIRONMENT`` — environment tag echoed in embed footers. One of
    ``dev`` / ``paper`` / ``live-small`` / ``live-scale``. Default ``dev``.

Phase 1+ adds: replay buffer cursor (``LAST_DELIVERED_SEQUENCE``), local
IPC listener bind address (``BOT_IPC_BIND``), per-channel-ID overrides
under ``CHANNEL_<NAME>_ID``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotSettings(BaseSettings):
    """Frozen settings for the discord bot process; instantiated once at startup."""

    model_config = SettingsConfigDict(
        env_prefix="",  # No prefix — env vars use bare DISCORD_*/API_BASE_URL names per spec §6.7
        env_file=None,  # never auto-load .env on the VPS
        case_sensitive=False,
        frozen=True,
        extra="ignore",
    )

    # --- Discord credentials --------------------------------------------------
    discord_bot_token: SecretStr = Field(
        ...,
        description=(
            "Discord application bot token. Sourced from secrets/<env>.enc.yaml discord.bot_token."
        ),
    )
    discord_guild_id: int = Field(
        ...,
        description=(
            "Operator's Discord guild ID. Slash commands are guild-scoped to "
            "this single guild for instant propagation."
        ),
    )

    # --- Backend communication ------------------------------------------------
    api_base_url: str = Field(
        default="http://api:8000",
        description=(
            "Base URL for the api service. Internal Docker network default; no trailing slash."
        ),
    )
    discord_bot_api_bearer_token: SecretStr = Field(
        ...,
        description=(
            "Shared bearer token between bot and api. Must match "
            "API_DISCORD_BOT_BEARER_TOKEN on the api side."
        ),
    )
    api_request_timeout_seconds: float = Field(
        default=10.0,
        gt=0.0,
        description=(
            "Per-request httpx timeout for api calls. discord.py interactions "
            "must respond within 3s OR be deferred; the bot defers all api "
            "calls so a 10s ceiling is comfortable."
        ),
    )

    # --- Operational ----------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARN", "ERROR"] = Field(default="INFO")
    environment: Literal["dev", "paper", "live-small", "live-scale"] = Field(
        default="dev",
        description="Environment tag echoed in embed footers.",
    )


@lru_cache(maxsize=1)
def get_settings() -> BotSettings:
    """Return the singleton settings instance (cached).

    Importing this lazily — never at module top-level — so unit tests can
    monkeypatch env vars before the first call.
    """
    return BotSettings()  # type: ignore[call-arg]  # all required fields come from env


__all__ = ["BotSettings", "get_settings"]
