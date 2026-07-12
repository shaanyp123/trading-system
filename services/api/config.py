"""services/api/config.py — typed runtime configuration.

Pydantic-settings loads from environment variables (12-factor) with the
following precedence (highest → lowest):

  1. Explicit env vars set by docker-compose / systemd (incl. those exported
     from the host secrets file by the entrypoint).
  2. `.env` file in the working directory (development only — never present
     on the production VPS).
  3. Field defaults defined here.

Phase 0 reads sensitive values (Postgres password, watchdog bearer) out of
the host secrets file (bind-mounted by docker-compose.yml) which carries
`secrets/<env>.enc.yaml` to `/run/secrets/decrypted.yaml`. The api container's
entrypoint resolves yaml-keys → env vars before launching uvicorn so this
module never has to parse yaml at runtime.

See `deploy/api/README.md` for the canonical mapping (secrets key → env var).
"""

from __future__ import annotations

from decimal import Decimal
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
    # Built by the entrypoint from secrets keys postgres.app_service_password +
    # the docker-compose hostname `postgres`. Format:
    #   postgresql+asyncpg://app_service:<pwd>@postgres:5432/trading
    database_url: SecretStr = Field(
        ...,
        description="SQLAlchemy async URL for the app_service role.",
    )

    # --- bearer tokens (secrets-file-sourced; SecretStr to keep them out of repr) -
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
    # Three buckets (IG §3 Week 5 Wed, + sse added 2026-07-12):
    #   - general: 100 req / 10s on /api/** (excl. /api/auth/**, /api/health,
    #     /api/sse/events)
    #   - auth:      5 req / 10s on /api/auth/** (brute-force-conscious)
    #   - sse:      30 req / 10s on /api/sse/events (each connect costs a
    #     session-table lookup under real validation; sized well above the
    #     N=4-tab reconnect-storm worst case)
    rate_limit_window_seconds: float = Field(default=10.0)
    rate_limit_general_per_window: int = Field(default=100)
    rate_limit_auth_per_window: int = Field(default=5)
    rate_limit_sse_per_window: int = Field(default=30)

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
    # spec §8.5.2 ("separate storage; column-encrypted"). The key itself
    # ships via the host secrets file -- ``totp.encryption_key``
    # -- but the TOTP key file (age key) protects the file at rest;
    # operationally these are two different keys.
    #
    # Encoded as base64url-no-padding of a 32-byte key (AES-256-GCM key).
    # When unset, the TOTP-touching endpoints fail closed with
    # ``TOTP_KEY_MISSING`` — never silently fall back to plaintext storage.
    totp_encryption_key: SecretStr | None = Field(
        default=None,
        description=(
            "Base64url-encoded 32-byte AES-256-GCM key for column encryption "
            "of totp_secrets.encrypted_secret. Sourced from the per-env secrets file."
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
    # spec §6.6 + §4.4 ("shared secrets-file Bearer token; rotated
    # quarterly with 1h overlap"). When the bot makes an HTTP request to
    # the api over the trading_internal Docker network, it sets
    # ``Authorization: Bearer <token>``; ``BotAuthMiddleware`` validates
    # via constant-time compare and on match injects a service-account
    # ``SessionContext`` (username=``discord-bot``, role=``owner``,
    # auth_strength=``strong``, is_phase0_stub=False).
    #
    # When unset the middleware degrades to no-op (no bot path is
    # served). Deployed hosts must set this — the real SessionMiddleware
    # cookie validation outside dev would otherwise reject the bot's
    # calls (the bot holds no session cookie) before any handler runs.
    #
    # Sourced from secrets ``discord.api_bearer_token`` per
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
    # Worker-PR-3b follow-up (post-pivot 2026-05-12); fetch path swapped
    # to Coinbase in crypto-pivot C0 §3.5 (2026-07-09). Wires the
    # Coinbase EOD fetcher → recon planner → apply pipeline into the api
    # lifespan so the 00:15 UTC daily cycle (after the 00:05 UTC daily
    # decision) fires automatically.
    #
    # The scheduler starts at api boot only when BOTH
    # ``coinbase_api_key_name`` AND ``coinbase_api_private_key`` (the
    # same CDP key pair the execution layer uses — see the Coinbase
    # credentials block below) are configured AND
    # ``reconciliation_scheduler_enabled`` is True. Missing either
    # secret → scheduler is skipped + a structured warning is logged
    # so the operator knows to populate the secrets file + restart the api.
    reconciliation_scheduler_enabled: bool = Field(
        default=True,
        description=(
            "When False, the api lifespan skips ReconciliationScheduler "
            "startup. Use to pause the EOD recon cycle without affecting "
            "the rest of the api (e.g., during a Coinbase-side outage)."
        ),
    )
    # --- Discord + Resend for the recon-break alert dispatch hook --------
    #
    # When the reconciliation scheduler detects an actionable break, the
    # api lifespan-constructed `alert_dispatch_hook` INSERTs an alerts
    # row + invokes `services.webhook_pusher.dispatcher.dispatch_alert`
    # which fans out to the Discord webhook URLs + Resend email below.
    #
    # All four fields are secrets-file-sourced (per `deploy/webhook_pusher/README.md`
    # which already documents these for the standalone webhook_pusher
    # smoke; the api now consumes the SAME secrets fields):
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
    # before secrets fields are populated), the api skips constructing the
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
            "secrets `discord.webhook_urls.alerts`. Required for the "
            "reconciliation alert_dispatch_hook to fire; when unset the "
            "hook is skipped (recon still runs)."
        ),
    )
    discord_webhook_url_critical: SecretStr | None = Field(
        default=None,
        description=(
            "Discord webhook URL for the #critical channel. Sourced "
            "from secrets `discord.webhook_urls.critical`. Required for P0 "
            "escalation; if unset, P0 alerts will hit dispatcher "
            "validation (SEVERITY_TO_CHANNELS includes #critical for P0)."
        ),
    )
    resend_api_key: SecretStr | None = Field(
        default=None,
        description=(
            "Resend HTTP API key. Sourced from secrets `resend.api_key`. "
            "Required for P0 email escalation; missing → "
            "email_identity stays None + P0 alerts trip dispatcher "
            "validation."
        ),
    )
    resend_from_address: str | None = Field(
        default=None,
        description=(
            "Resend `from` address. Sourced from secrets "
            "`resend.from_address`. See resend_api_key for the "
            "missing-value semantics."
        ),
    )
    resend_to_address: str | None = Field(
        default=None,
        description=(
            "Resend `to` address. Sourced from secrets `resend.to_address`. "
            "See resend_api_key for the missing-value semantics."
        ),
    )

    # --- Coinbase market data worker (crypto-pivot C0-B2a, delta spec §3.2) --
    #
    # The `CoinbaseMarketDataWorker` (services/data/coinbase_market_data.py)
    # replaces the retired bar_sync worker as the market-data layer: public
    # WS ticker marks for the 30s risk loop, hourly CDE funding capture into
    # `funding_rates`, daily `product_metadata` snapshots, spot daily-bar
    # sampling at 00:00 UTC, and the 3-minute staleness watchdog (strategy
    # §7 outage detection). No API keys — all endpoints are public; auth
    # enters only with the §3.1 execution adapter.
    coinbase_market_data_enabled: bool = Field(
        default=True,
        description=(
            "When False, the api lifespan skips CoinbaseMarketDataWorker "
            "startup. Marks/funding/metadata capture stop; the risk loop's "
            "own staleness handling covers the trading side."
        ),
    )
    coinbase_rest_base_url: str = Field(
        default="https://api.coinbase.com",
        description=(
            "Base URL for the public Advanced Trade REST endpoints "
            "(/api/v3/brokerage/market/**). Override for tests/sandbox."
        ),
    )
    coinbase_ws_url: str = Field(
        default="wss://advanced-trade-ws.coinbase.com",
        description="Public Advanced Trade WebSocket URL (ticker channel).",
    )
    coinbase_market_data_tick_interval_seconds: float = Field(
        default=30.0,
        gt=0.0,
        le=300.0,
        description=(
            "Cadence of the worker's periodic tick (staleness check + "
            "hourly/daily job due-checks). 30s matches the risk-loop "
            "cadence the marks feed."
        ),
    )
    coinbase_market_data_stale_threshold_seconds: float = Field(
        default=180.0,
        gt=0.0,
        le=3600.0,
        description=(
            "Mark age past which a product counts as stale (strategy §7 "
            "locked: 3 minutes). Changing this is a strategy-doc amendment, "
            "not a tuning knob."
        ),
    )
    coinbase_market_data_stale_realert_cooldown_seconds: float = Field(
        default=900.0,
        gt=0.0,
        description=(
            "Minimum seconds between repeated P2 staleness alerts while "
            "marks stay continuously stale (A22-style alert throttling)."
        ),
    )
    coinbase_market_data_startup_grace_seconds: float = Field(
        default=180.0,
        ge=0.0,
        description=(
            "Grace period after worker start before never-ticked products "
            "count as stale — lets the first WS connect land without a "
            "boot-time false alarm."
        ),
    )
    coinbase_market_data_http_timeout_seconds: float = Field(
        default=30.0,
        gt=0.0,
        le=600.0,
        description="Per-request timeout on the public REST calls.",
    )

    # --- Coinbase execution credentials (crypto-pivot C0-B2b, §3.1) --------
    #
    # CDP API key for the AUTHENTICATED Advanced Trade surface (orders,
    # fills, positions, futures balance summary) consumed by
    # ``services/execution/coinbase_client.SdkCoinbaseBrokerClient``.
    # Sourced from secrets ``coinbase.api_key_name`` + ``coinbase.api_private_key``
    # (ES256 EC private key in PEM form, as issued by the CDP portal) and
    # mapped to env vars by ``services/api/entrypoint.py``. When unset,
    # nothing that trades can start — the strategy worker (§3.3, C0-B3)
    # fails closed at construction, and the api lifespan skips the recon
    # scheduler (same fail-closed contract the deleted flex-credential
    # pair used to carry). The market-data worker (§3.2) is unaffected
    # (public endpoints only).
    coinbase_api_key_name: SecretStr | None = Field(
        default=None,
        description=(
            "CDP API key name (organizations/{org}/apiKeys/{key}). "
            "Sourced from secrets `coinbase.api_key_name`."
        ),
    )
    coinbase_api_private_key: SecretStr | None = Field(
        default=None,
        description=(
            "CDP API EC private key (PEM). Sourced from secrets "
            "`coinbase.api_private_key`. SecretStr keeps it out of repr/logs."
        ),
    )

    # --- USDC rewards + cash-balance capture (decisions-log 2026-07-10) ----
    #
    # Daily api-lifespan job (services/data/usdc_rewards.py, fired at
    # 00:20 UTC by the generic once-per-UTC-day scheduler): polls the
    # Coinbase v2 USDC ledger for candidate reward transactions and
    # snapshots the CBI spot USD + USDC balances. Reuses the SAME CDP
    # key pair as the execution/recon surfaces (coinbase_api_key_name +
    # coinbase_api_private_key above) — when those are unset the job
    # skips with a warning, same fail-closed contract as the recon
    # scheduler. Operator kill switch for the capture alone:
    # API_USDC_REWARDS_CAPTURE_ENABLED=false.
    usdc_rewards_capture_enabled: bool = Field(
        default=True,
        description=(
            "Start the daily USDC rewards + cash-balance capture job in "
            "the api lifespan (00:20 UTC). Requires the coinbase.* CDP "
            "credentials in the secrets file."
        ),
    )

    # --- Cash-yield display rate (crypto-pivot §3.6/§3.9) ------------------
    #
    # Interim MANUAL value surfaced by ``GET /api/system/funding`` as
    # ``yield_apy`` until the §3.6 cash_manager worker (C2 activation)
    # lands and owns the real rate. The Coinbase One Basic USDC rewards
    # rate (3.50% as of 2026-07, delta spec §3.6) is operator-entered via
    # ``API_CASH_YIELD_APY=0.035``; None renders as "—" in the UI (no
    # sweep worker → no yield claim). Decimal per [A05] — never float,
    # serialized as a string on the wire.
    cash_yield_apy: Decimal | None = Field(
        default=None,
        description=(
            "Annualized cash-yield rate as a fraction (e.g. 0.035 for "
            "3.5%). Interim manual value until the §3.6 cash_manager "
            "worker lands; None renders as '—' in the UI."
        ),
    )


@lru_cache(maxsize=1)
def get_settings() -> APISettings:
    """Return the singleton settings instance (cached).

    Importing this lazily — never at module top-level — so unit tests can
    monkeypatch env vars before the first call.
    """
    return APISettings()  # type: ignore[call-arg]  # database_url comes from env
