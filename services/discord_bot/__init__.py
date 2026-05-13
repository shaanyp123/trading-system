"""services/discord_bot package — Discord gateway client + slash commands.

Public surface (Phase 0 per implementation-guide §3 Week 6 Thu + frontend-spec
§6.1 LOCKED surface phasing):

- :class:`BotSettings` (``config.py``) — Pydantic-Settings BaseSettings reading
  ``DISCORD_BOT_TOKEN`` + ``DISCORD_GUILD_ID`` + ``API_BASE_URL`` +
  ``DISCORD_BOT_API_BEARER_TOKEN`` from the environment.
- :class:`ApiClient` (``api_client.py``) — typed httpx async client for the
  three api endpoints the Phase 0 commands hit (`/api/health`,
  `/api/positions/current`, `/api/system/kill-switch/invoke`).
- :func:`build_positions_embed`, :func:`build_halt_confirm_embed`,
  :func:`build_status_embed` (``embeds.py``) — pure-function embed builders;
  pure data-in / discord.Embed-out, no I/O.

Bot architecture matches frontend-spec §6.7 (gateway WS + REST calls to
backend over the internal Docker network) with an auth model that follows
spec §6.6 + §4.4 (shared sops-decrypted Bearer token) — see
``services/api/middleware.BotAuthMiddleware`` for the api-side validator.

Phase 0 commands (frontend-spec §6.1 LOCKED — only these three for Day 23):

- ``/positions`` — calls ``GET /api/positions/current``; renders embed
  table; ephemeral.
- ``/halt <reason>`` — confirmation flow: shows ephemeral confirm view +
  buttons, on ✓ POSTs ``/api/system/kill-switch/invoke`` with
  ``trigger="manual_judgment"``; surfaces 501 ``KILL_SWITCH_HANDLER_NOT_WIRED``
  envelope until Week 4 Wed dispatcher wires the real handler. ``/halt``
  is risk-tightening (no re-auth required per dev-guide §1.5 + spec §6.1).
- ``/status`` — calls ``GET /api/health``; renders embed with environment
  + version + db-connected + checks.

Phase 1 surface (NOT in Day 23 scope; deferred per spec §6.1): ``/pnl``,
``/exposure``, ``/calendar``, ``/last-fills``, ``/ratify``, ``/health``,
``/vacation start``, ``/close``, ``/diary``, ``/audit``, ``/today`` plus
the seven channel push paths (``#daily-brief``, ``#signals``, ``#fills``,
``#alerts``, ``#critical``, ``#ops``, ``#audit``).

A22 contract: tests must NOT hit the live Discord gateway or live api. The
unit test suite in ``tests/unit/test_discord_bot_*.py`` exercises the embed
builders + command-handler logic with a stubbed ``ApiClient`` and a
mocked ``discord.Interaction`` — pure Python, no live bot runtime.

A27 contract: Discord is a third-party platform. The Day 23 PR ships
``deploy/discord_bot/README.md`` as the operator-runbook smoke test per
dev-guide §6.8 alternative (b); the live operator smoke (typing
``/status`` in the dev guild) closes A27 at Day 23 carryover or Day 24
Fri operator ceremony (bundled with the Day 21 + Day 22 carryovers per
PR #71 deferral lock).
"""

from services.discord_bot.api_client import (
    ApiClient,
    ApiClientError,
    ApiClientHTTPError,
    HealthCheck,
    HealthResponse,
    KillSwitchInvokePayload,
    KillSwitchInvokeResponse,
    PositionRow,
    PositionsResponse,
    SignalApprovePayload,
    SignalApproveResponse,
)
from services.discord_bot.config import BotSettings
from services.discord_bot.embeds import (
    EMBED_COLOR_CRITICAL,
    EMBED_COLOR_INFO,
    EMBED_COLOR_OK,
    EMBED_COLOR_WARNING,
    build_approve_confirm_embed,
    build_approve_error_embed,
    build_approve_invalid_uuid_embed,
    build_approve_success_embed,
    build_halt_confirm_embed,
    build_halt_invoke_error_embed,
    build_halt_invoke_success_embed,
    build_positions_embed,
    build_status_embed,
    format_kill_switch_invoke_failure,
)

__all__ = [
    "EMBED_COLOR_CRITICAL",
    "EMBED_COLOR_INFO",
    "EMBED_COLOR_OK",
    "EMBED_COLOR_WARNING",
    "ApiClient",
    "ApiClientError",
    "ApiClientHTTPError",
    "BotSettings",
    "HealthCheck",
    "HealthResponse",
    "KillSwitchInvokePayload",
    "KillSwitchInvokeResponse",
    "PositionRow",
    "PositionsResponse",
    "SignalApprovePayload",
    "SignalApproveResponse",
    "build_approve_confirm_embed",
    "build_approve_error_embed",
    "build_approve_invalid_uuid_embed",
    "build_approve_success_embed",
    "build_halt_confirm_embed",
    "build_halt_invoke_error_embed",
    "build_halt_invoke_success_embed",
    "build_positions_embed",
    "build_status_embed",
    "format_kill_switch_invoke_failure",
]
