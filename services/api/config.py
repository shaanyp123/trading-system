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

    # --- rate limiting (Day 17 — IG §3 Week 5 Wed) ------------------------
    # Fixed-window counters per (client_ip, bucket). The api enforces these
    # rather than Caddy because the stock `caddy:2-alpine` image lacks the
    # `mholt/caddy-ratelimit` community module; rather than fork the Caddy
    # build (xcaddy + custom image push) for Phase 0, the spec contract is
    # met via in-process Python middleware. Phase 1 can revisit if Caddy-edge
    # filtering is justified by real load. See Docs/decisions-log.md
    # "2026-05-10 — Day 17 09:00" entry for the rationale.
    #
    # Two buckets per IG §3 Week 5 Wed:
    #   - general: 100 req / 10s on /api/** (excl. /api/auth/**, /api/health,
    #     /api/internal/watchdog, /api/sse/events)
    #   - auth:      5 req / 10s on /api/auth/** (brute-force-conscious)
    rate_limit_window_seconds: float = Field(default=10.0)
    rate_limit_general_per_window: int = Field(default=100)
    rate_limit_auth_per_window: int = Field(default=5)

    # --- WebAuthn (Day 21 — IG §3 Week 6 Tue) -----------------------------
    # Relying-party identifier per spec §5.1 — apex registrable domain.
    # Default is intentionally an obvious placeholder so a live deploy that
    # forgets to override it fails the WebAuthn ceremony at verify time
    # rather than silently registering credentials against the placeholder.
    webauthn_rp_id: str = Field(
        default="localhost",
        description=(
            "WebAuthn RP ID — operator's registrable apex domain. "
            "Set per env via API_WEBAUTHN_RP_ID. 'localhost' is dev-only."
        ),
    )
    webauthn_rp_name: str = Field(
        default="trading-system",
        description="Human-readable RP name shown in the authenticator UI.",
    )
    webauthn_origin: str = Field(
        default="http://localhost:3000",
        description=(
            "Expected origin for WebAuthn verifications "
            "(scheme + host + optional port; no trailing slash)."
        ),
    )

    # --- TOTP encryption key (Day 21) -------------------------------------
    # Column-encryption key for ``totp_secrets.encrypted_secret`` per backend-
    # spec §8.5.2 ("separate from sops; column-encrypted"). The key itself
    # ships via sops -- ``totp.encryption_key`` in ``secrets/<env>.enc.yaml``
    # -- but the sops master key (age key) protects the file at rest;
    # operationally these are two different keys.
    #
    # Encoded as base64url-no-padding of a 32-byte key (AES-256-GCM key).
    # When unset, the TOTP-touching endpoints fail closed with
    # ``TOTP_KEY_MISSING`` — never silently fall back to plaintext storage.
    totp_encryption_key: SecretStr | None = Field(
        default=None,
        description=(
            "Base64url-encoded 32-byte AES-256-GCM key for column encryption "
            "of totp_secrets.encrypted_secret. Sourced from sops per env."
        ),
    )

    # --- Operator username (Phase 0 single-operator constant) -------------
    # Backend-spec §8.5 is silent on username convention because the system
    # is single-operator. Locking it to ``operator`` keeps the TOTP login +
    # recovery flow deterministic (operator types this username on /login
    # fallback + /recover). Configurable via API_OPERATOR_USERNAME for
    # multi-operator environments in Phase 3+.
    operator_username: str = Field(default="operator", min_length=1, max_length=64)

    # --- Discord bot bearer auth (Day 23 — IG §3 Week 6 Thu) --------------
    # Shared bearer token between the api and the discord bot per backend-
    # spec §6.6 + §4.4 ("shared sops-decrypted Bearer token; rotated
    # quarterly with 1h overlap"). When the bot makes an HTTP request to
    # the api over the trading_internal Docker network, it sets
    # ``Authorization: Bearer <token>``; ``BotAuthMiddleware`` validates
    # via constant-time compare and on match injects a service-account
    # ``SessionContext`` (username=``discord-bot``, role=``owner``,
    # auth_strength=``strong``, is_phase0_stub=False).
    #
    # When unset the middleware degrades to no-op (no bot path is
    # served). Production must set this — the SessionStubMiddleware fail-
    # close in live envs would otherwise reject the bot's calls before
    # any handler runs.
    #
    # Sourced from sops yaml ``discord.api_bearer_token`` per
    # ``deploy/discord_bot/README.md``. Token format: 32-byte URL-safe
    # base64 (``secrets.token_urlsafe(32)``); operator can rotate by
    # generating a fresh value, deploying both api + discord_bot with the
    # new value, then revoking the old.
    discord_bot_bearer_token: SecretStr | None = Field(
        default=None,
        description=(
            "Shared bearer token for Discord bot → api authentication. "
            "When set, BotAuthMiddleware accepts requests bearing this "
            "token in Authorization header and injects a service-account "
            "SessionContext."
        ),
    )

    # --- LEAN Local bearer auth (Pivot-PR-A — post-pivot 2026-05-12) ------
    # Shared bearer token between the api and the `lean_local` Docker
    # container running LEAN's algorithm engine on the operator's VPS. When
    # LEAN's `v1_strategy.py` POSTs a signal event to `POST
    # /api/internal/lean/signals`, it sets `Authorization: Bearer <token>`;
    # `LeanAuthMiddleware` validates via constant-time compare and on match
    # injects a service-account `SessionContext` (username=``lean-local``,
    # role=``owner``, auth_strength=``strong``, is_phase0_stub=False).
    #
    # Distinct from `discord_bot_bearer_token`: separate sops fields so that
    # compromise of one container doesn't grant access via the other (a
    # compromised Discord bot can't impersonate LEAN, and vice-versa).
    #
    # When unset the middleware degrades to no-op (no LEAN path is served).
    # Production must set this — the SessionStubMiddleware fail-close in
    # live envs would otherwise reject LEAN's POSTs before any handler runs.
    #
    # Sourced from sops yaml `lean.api_bearer_token` per
    # `deploy/lean_local/README.md`. Token format: 32-byte URL-safe base64
    # (`secrets.token_urlsafe(32)`); rotates with the quarterly secrets
    # rotation alongside the Discord bot token.
    lean_local_bearer_token: SecretStr | None = Field(
        default=None,
        description=(
            "Shared bearer token for LEAN Local → api authentication. "
            "When set, LeanAuthMiddleware accepts requests bearing this "
            "token in Authorization header and injects a service-account "
            "SessionContext."
        ),
    )

    # --- IBKR client (Worker-PR-1 follow-up — post-pivot 2026-05-12) ------
    # IB Gateway connection settings for the api-process-resident
    # `OrderPlacementWorker` background task. The worker drains
    # ``signals.status='approved'`` rows into IBKR via the
    # ``services.execution.ibkr_adapter.IbAsyncIbkrClient`` adapter.
    #
    # Defaults match the gnzsnz/ib-gateway externally-published socat
    # ports (4004 paper, 4003 live; the internal gateway listens on
    # 127.0.0.1:4002/:4001 but only the socat ports are reachable from
    # other containers). The IBKR account number is sops-sourced —
    # operator populates ``ibkr.paper_account`` in
    # ``secrets/<env>.enc.yaml`` and the api entrypoint maps it to
    # ``API_IBKR_ACCOUNT``.
    #
    # When ``ibkr_account`` is unset OR the api's lifespan can't fetch
    # an active account_id from the ``accounts`` table, the worker
    # **does not start** — the api still serves requests (the lifespan
    # logs the skip but doesn't crash). This keeps a fresh deploy with
    # no accounts row bootable.
    ibkr_host: str = Field(
        default="ib_gateway",
        description=(
            "Hostname of the ib_gateway Docker container. Defaults to "
            "the Docker DNS name on the internal network."
        ),
    )
    ibkr_port: int = Field(
        default=4004,
        ge=1,
        le=65535,
        description=(
            "TWS API port. 4004 for paper, 4003 for live (gnzsnz socat "
            "externally-published ports). Internal gateway listens on "
            "127.0.0.1:4002/:4001 but socat publishes the externally-"
            "reachable ports."
        ),
    )
    ibkr_client_id: int = Field(
        default=1,
        ge=1,
        le=7,
        description=(
            "TWS API clientId; 1-7 per IBKR's docs. The worker uses a "
            "fixed clientId so that reconnects after transient "
            "ib_gateway restarts surface as the same TWS API session."
        ),
    )
    ibkr_account: str | None = Field(
        default=None,
        description=(
            "IBKR account number (e.g., 'DUQ825170' for paper, "
            "'U25655583' for live). Sourced from sops "
            "`ibkr.paper_account` / `ibkr.live_account` per env. When "
            "unset, IbAsyncIbkrClient uses the default account on the "
            "TWS session."
        ),
    )
    # Operator escape hatch for emergency disable of the
    # OrderPlacementWorker without touching the docker-compose
    # configuration. Defaults to True (worker starts when an
    # IBKR-bound account_id resolves). Operator can set
    # `API_ORDER_PLACEMENT_WORKER_ENABLED=false` in `deploy/.env` to
    # disable while keeping the rest of the api running — useful
    # during the paper-clock period if an order placement issue
    # surfaces and the operator needs to pause the broker leg without
    # taking the web UI offline.
    order_placement_worker_enabled: bool = Field(
        default=True,
        description=(
            "When False, the api lifespan skips OrderPlacementWorker "
            "startup. Approved signals will queue in the signals "
            "table until the worker is re-enabled and the api restarts."
        ),
    )
    # Phase 1 poll cadence override. Defaults match
    # `services.risk.order_placement_worker.DEFAULT_POLL_INTERVAL_SECONDS`
    # (5.0s). Operator can tune via API_ORDER_PLACEMENT_POLL_INTERVAL
    # if signal cadence changes Phase 2+.
    order_placement_poll_interval_seconds: float = Field(
        default=5.0,
        gt=0.0,
        le=600.0,
        description=(
            "Seconds between OrderPlacementWorker.run_once() iterations. "
            "Lower = faster manual-approve → broker latency at the cost "
            "of marginal DB pressure."
        ),
    )
    # -- Async task liveness monitor ----------------------------------------
    #
    # 2026-05-17 follow-up to the silent-worker-death pattern observed
    # across three production drills. The lifespan spawns 3+ long-lived
    # asyncio tasks (OrderPlacementWorker, ReconciliationScheduler,
    # HeartbeatProbe). When a task dies via an uncaught exception, asyncio
    # marks it `.done()` but the exception is only surfaced if something
    # `await`s the task or calls `.exception()`. Without an explicit
    # observer, dead tasks are invisible until the operator notices a
    # secondary symptom (no fills, no recon, etc.).
    #
    # The monitor is a 4th lifespan task that ticks every
    # ``async_task_monitor_interval_seconds`` and inspects each tracked
    # task's `.done()` / `.exception()`. On unexpected completion it logs
    # a `async_task_died` event at ERROR with the task name + exception
    # repr (or "done without exception" if the task exited cleanly). The
    # monitor does NOT attempt to restart dead tasks — restart semantics
    # depend on the task (worker is restart-safe via api restart; the
    # heartbeat probe is too) but a blind restart could mask real bugs.
    # Phase 1+ may add per-task restart policies behind opt-in flags.
    async_task_monitor_enabled: bool = Field(
        default=True,
        description=(
            "When False, the api lifespan skips the async-task liveness "
            "monitor. Tests inject False to avoid spawning the monitor "
            "in unit-test contexts; production should leave True."
        ),
    )
    async_task_monitor_interval_seconds: float = Field(
        default=30.0,
        gt=0.0,
        le=600.0,
        description=(
            "Seconds between async-task liveness probes. Default 30s "
            "balances log noise against detection latency for silent "
            "task death (the operator sees the next health-check + "
            "log scan within ~1 minute of failure)."
        ),
    )
    # -- EOD reconciliation scheduler ---------------------------------------
    #
    # Worker-PR-3b follow-up (post-pivot 2026-05-12). Wires the
    # FlexQuery → recon planner → apply pipeline into the api lifespan
    # so the 18:30 ET daily cycle fires automatically.
    #
    # The scheduler starts at api boot only when BOTH
    # ``flex_query_id`` AND ``flex_query_token`` are configured AND
    # ``reconciliation_scheduler_enabled`` is True. Missing either
    # secret → scheduler is skipped + a structured warning is logged
    # so the operator knows to populate sops + restart the api.
    #
    # Operator workflow: pre-create the FlexQuery template in IBKR's
    # portal (Reports → Flex Queries → Create), record the numeric ID +
    # auto-generated token, then `sops secrets/<env>.enc.yaml` and
    # set ``ibkr.flex_query_id`` + ``ibkr.flex_query_token``. The
    # api's entrypoint (services/api/entrypoint.py) maps those onto
    # ``API_FLEX_QUERY_ID`` + ``API_FLEX_QUERY_TOKEN`` env vars at
    # container start.
    reconciliation_scheduler_enabled: bool = Field(
        default=True,
        description=(
            "When False, the api lifespan skips ReconciliationScheduler "
            "startup. Use to pause the EOD recon cycle without affecting "
            "the rest of the api (e.g., during an IBKR-side outage)."
        ),
    )
    flex_query_id: int | None = Field(
        default=None,
        gt=0,
        description=(
            "IBKR FlexQuery template ID. Sourced from sops "
            "`ibkr.flex_query_id`. When unset, the reconciliation "
            "scheduler does not start (the api still serves requests; "
            "a warning is logged at boot)."
        ),
    )
    flex_query_token: SecretStr | None = Field(
        default=None,
        description=(
            "IBKR FlexQuery auth token (per-template). Sourced from "
            "sops `ibkr.flex_query_token`. When unset, the "
            "reconciliation scheduler does not start."
        ),
    )

    # --- Discord + Resend for the recon-break alert dispatch hook --------
    #
    # When the reconciliation scheduler detects an actionable break, the
    # api lifespan-constructed `alert_dispatch_hook` INSERTs an alerts
    # row + invokes `services.webhook_pusher.dispatcher.dispatch_alert`
    # which fans out to the Discord webhook URLs + Resend email below.
    #
    # All four fields are sops-sourced (per `deploy/webhook_pusher/README.md`
    # which already documents these for the standalone webhook_pusher
    # smoke; the api now consumes the SAME sops fields):
    #
    #   - discord.webhook_urls.alerts    → Discord #alerts channel
    #     (P2 + P1 + P0 alerts ALL hit this channel)
    #   - discord.webhook_urls.critical  → Discord #critical channel
    #     (P0-only escalation; missing → P0 alerts skip the #critical leg
    #      with a structured warning, the dispatcher will raise on the
    #      missing channel since SEVERITY_TO_CHANNELS includes it for P0)
    #   - resend.api_key                 → Resend HTTP API auth bearer
    #   - resend.from_address            → "From" header on the email
    #   - resend.to_address              → "To" header on the email
    #
    # When `discord.webhook_urls.alerts` is unset (Phase 1 day-1 boot
    # before sops fields are populated), the api skips constructing the
    # hook — recon still runs end-to-end + alerts log a WARNING from
    # `services.reconciliation.apply._dispatch_alerts` ("hook not wired").
    #
    # P0 escalation (Resend) requires ALL of api_key + from_address +
    # to_address. If any one is missing, `email_identity` stays None +
    # the dispatcher's planner raises on a P0 break (the operator sees
    # a `reconciliation_alert_dispatch_failed` log line + the alerts
    # row's `delivery_status` JSONB shows the channel-level failure).
    discord_webhook_url_alerts: SecretStr | None = Field(
        default=None,
        description=(
            "Discord webhook URL for the #alerts channel. Sourced from "
            "sops `discord.webhook_urls.alerts`. Required for the "
            "reconciliation alert_dispatch_hook to fire; when unset the "
            "hook is skipped (recon still runs)."
        ),
    )
    discord_webhook_url_critical: SecretStr | None = Field(
        default=None,
        description=(
            "Discord webhook URL for the #critical channel. Sourced "
            "from sops `discord.webhook_urls.critical`. Required for P0 "
            "escalation; if unset, P0 alerts will hit dispatcher "
            "validation (SEVERITY_TO_CHANNELS includes #critical for P0)."
        ),
    )
    resend_api_key: SecretStr | None = Field(
        default=None,
        description=(
            "Resend HTTP API key. Sourced from sops `resend.api_key`. "
            "Required for P0 email escalation; missing → "
            "email_identity stays None + P0 alerts trip dispatcher "
            "validation."
        ),
    )
    resend_from_address: str | None = Field(
        default=None,
        description=(
            "Resend `from` address. Sourced from sops "
            "`resend.from_address`. See resend_api_key for the "
            "missing-value semantics."
        ),
    )
    resend_to_address: str | None = Field(
        default=None,
        description=(
            "Resend `to` address. Sourced from sops `resend.to_address`. "
            "See resend_api_key for the missing-value semantics."
        ),
    )


@lru_cache(maxsize=1)
def get_settings() -> APISettings:
    """Return the singleton settings instance (cached).

    Importing this lazily — never at module top-level — so unit tests can
    monkeypatch env vars before the first call.
    """
    return APISettings()  # type: ignore[call-arg]  # database_url comes from env
