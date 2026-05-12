"""services/qc_adapter/config.py — typed runtime configuration.

Pydantic-settings loads from environment variables (12-factor) with the
same precedence rules as ``services/api/config.py``:

  1. Explicit env vars set by docker-compose / systemd (incl. those
     exported from the sops-decrypted bundle by ``entrypoint.py``).
  2. ``.env`` file in the working directory (development only).
  3. Field defaults defined here.

The qc_adapter container's entrypoint resolves sops yaml keys → env vars
before launching the orchestrator loop so this module never has to parse
yaml at runtime. See ``deploy/qc_adapter/README.md`` for the canonical
mapping (sops key → env var).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class QCAdapterSettings(BaseSettings):
    """Frozen settings; instantiated once at startup via :func:`get_settings`."""

    model_config = SettingsConfigDict(
        env_prefix="QC_ADAPTER_",
        env_file=None,
        case_sensitive=False,
        frozen=True,
        extra="ignore",
    )

    # --- environment & build identity -------------------------------------
    environment: Literal["dev", "paper", "live-small", "live-scale"] = Field(
        default="dev",
        description="Audit env tag (matches audit_log.env CHECK constraint).",
    )
    version: str = Field(
        default="dev",
        description="Git SHA or build tag; surfaced in structlog records.",
    )
    log_level: Literal["DEBUG", "INFO", "WARN", "ERROR"] = Field(default="INFO")
    # Docker compose env vars are always strings; pydantic-settings doesn't
    # auto-coerce ``"0"`` to ``Literal[0, 1, 2, 3]``. ``before`` validator
    # coerces incoming strings to int before Literal validation runs.
    phase_at_emit: Literal[0, 1, 2, 3] = Field(default=0)

    @field_validator("phase_at_emit", mode="before")
    @classmethod
    def _coerce_phase_int(cls, v: object) -> object:
        if isinstance(v, str):
            return int(v)
        return v

    # --- postgres ---------------------------------------------------------
    # Built by the entrypoint from sops keys postgres.app_service_password +
    # the docker-compose hostname `postgres`. Format:
    #   postgresql+asyncpg://app_service:<pwd>@postgres:5432/trading
    database_url: SecretStr = Field(
        ...,
        description="SQLAlchemy async URL for the app_service role.",
    )

    # --- quantconnect -----------------------------------------------------
    # The QC public REST API at https://www.quantconnect.com/api/v2/
    # authenticates via HTTP Basic with a hash-stamped password:
    #   Authorization: Basic base64(<qc_user_id>:<sha256(<api_token>:<ts>)>)
    #   Timestamp: <ts>
    # ``qc_user_id`` is the numeric "User ID" from the QC account page —
    # sourced from sops yaml ``quantconnect.organization_id`` per
    # ``deploy/qc_adapter/README.md`` (QC's docs ambiguously call this
    # field "user_id" in the auth section + "organization_id" elsewhere;
    # the operator's secret schema persists it under ``organization_id``).
    qc_user_id: str = Field(
        ...,
        description="QC numeric user ID for HTTP Basic auth.",
        min_length=1,
    )
    qc_api_token: SecretStr = Field(
        ...,
        description="QC API token (rotated by operator out-of-band).",
    )
    qc_objectstore_base_url: str = Field(
        default="https://www.quantconnect.com/api/v2",
        description="QC REST API base URL (no trailing slash).",
    )
    qc_http_timeout_seconds: float = Field(default=30.0, gt=0.0)

    # --- account / phase-0 single-operator -------------------------------
    # The active account FK every audit / signal row carries. Resolved by
    # the orchestrator at startup via the same pattern as
    # ``services/api/repos/phase1.py:fetch_active_account_id``:
    #   SELECT id FROM accounts WHERE active_to IS NULL
    #   ORDER BY active_from ASC LIMIT 1
    # An explicit override via ``QC_ADAPTER_ACCOUNT_ID`` is supported for
    # testing + multi-account environments Phase 3+.
    account_id: UUID | None = Field(
        default=None,
        description=(
            "Optional override for the active account. Phase 0 leaves "
            "this unset + the orchestrator resolves at startup."
        ),
    )

    # --- poll cadence overrides (defaults match CursorDirectory) ---------
    # Useful for tests + smoke runs (override to 1s); production leaves
    # the dev-guide §1.5 LOCKED 60/60/5 cadence.
    poll_events_seconds: int = Field(default=60, gt=0)
    poll_state_portfolio_seconds: int = Field(default=60, gt=0)
    poll_instruction_acks_seconds: int = Field(default=5, gt=0)

    # --- consecutive-failure escalation (backend-spec §6.6.1) ------------
    # Wall-clock seconds-of-failures thresholds:
    #   < 300s  → log.warning only
    #   300..600s → P1 alert (Phase 1 wiring; PR-A logs only)
    #   ≥ 600s → HALT_NEW + qc_objstore_stale_10m trigger (Phase 1+;
    #            PR-A surfaces structlog event + flag)
    failure_alert_threshold_seconds: int = Field(default=300, gt=0)
    failure_halt_threshold_seconds: int = Field(default=600, gt=0)


@lru_cache(maxsize=1)
def get_settings() -> QCAdapterSettings:
    """Return the singleton settings instance (cached).

    Lazy import keeps unit tests free to monkeypatch env vars before the
    first call.
    """
    return QCAdapterSettings()  # type: ignore[call-arg]


__all__ = ["QCAdapterSettings", "get_settings"]
