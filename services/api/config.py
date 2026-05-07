"""services/api/config.py — typed runtime configuration.

Pydantic-settings loads from environment variables (12-factor) with the
following precedence (highest → lowest):

  1. Explicit env vars set by docker-compose / systemd (incl. those exported
     from the sops-decrypted bundle by the entrypoint).
  2. `.env` file in the working directory (development only — never present
     on the production VPS).
  3. Field defaults defined here.

Phase 0 reads sensitive values (Postgres password, watchdog bearer) out of
sops by way of the `sops_init` sidecar (docker-compose.yml) which decrypts
`secrets/<env>.enc.yaml` to `/run/secrets/decrypted.yaml`. The api container's
entrypoint resolves yaml-keys → env vars before launching uvicorn so this
module never has to parse yaml at runtime.

See `deploy/api/README.md` for the canonical mapping (sops key → env var).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class APISettings(BaseSettings):
    """Frozen settings; instantiated once at startup via `get_settings()`."""

    model_config = SettingsConfigDict(
        env_prefix="API_",
        env_file=None,  # never auto-load a .env on the VPS
        case_sensitive=False,
        frozen=True,
        extra="ignore",
    )

    # --- environment & build identity -------------------------------------
    environment: Literal["dev", "paper", "live-small", "live-scale"] = Field(
        default="dev",
        description="Audit env tag (matches `audit_log.env` CHECK constraint).",
    )
    version: str = Field(
        default="dev",
        description="Git SHA or build tag; surfaced via /api/health.",
    )
    log_level: Literal["DEBUG", "INFO", "WARN", "ERROR"] = Field(default="INFO")

    # --- postgres ---------------------------------------------------------
    # Built by the entrypoint from sops keys postgres.app_service_password +
    # the docker-compose hostname `postgres`. Format:
    #   postgresql+asyncpg://app_service:<pwd>@postgres:5432/trading
    database_url: SecretStr = Field(
        ...,
        description="SQLAlchemy async URL for the app_service role.",
    )

    # --- bearer tokens (sops-sourced; SecretStr to keep them out of repr) -
    # Reserved for the watchdog POST endpoint (Week 5+; backend-spec §4.5.3).
    # If unset on Day 5, the future `/api/internal/watchdog` route fails closed
    # at the auth dependency.
    watchdog_bearer_token: SecretStr | None = Field(default=None)

    # --- session & CSRF ---------------------------------------------------
    session_cookie_name: str = Field(default="__Host-trading_session")
    csrf_cookie_name: str = Field(default="__Host-csrf_token")
    csrf_header_name: str = Field(default="X-CSRF-Token")
    session_idle_seconds: int = Field(default=30 * 60)  # 30 min
    session_absolute_seconds: int = Field(default=24 * 60 * 60)  # 24 h

    # --- CORS -------------------------------------------------------------
    # Tight by default — the frontend lives on the same apex; CORS only opens
    # if the dev environment uses a separate localhost port.
    cors_allow_origins: list[str] = Field(default_factory=list)


@lru_cache(maxsize=1)
def get_settings() -> APISettings:
    """Return the singleton settings instance (cached).

    Importing this lazily — never at module top-level — so unit tests can
    monkeypatch env vars before the first call.
    """
    return APISettings()  # type: ignore[call-arg]  # database_url comes from env
