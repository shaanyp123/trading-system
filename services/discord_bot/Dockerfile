# services/discord_bot/Dockerfile — Discord bot container.
#
# Mirrors the shape of `services/webhook_pusher/Dockerfile`: two-stage
# build (fat builder + slim runtime), non-root user, minimal apt
# surface. Ships in Day 23 (Week 6 Thu — IG line 383) per
# implementation-guide §3 Week 6 Thu + frontend-spec §6.7.
#
# Why this container exists
# -------------------------
#
# The Discord bot is a separate process from the api so it can:
#   * Maintain a persistent gateway WebSocket connection without sharing
#     uvicorn's event loop with HTTP request handlers.
#   * Live on `trading_internal + trading_egress` (gateway WS needs
#     egress; api calls go over `trading_internal` to `http://api:8000`).
#   * Get auto-restarted independently when discord.py crashes (rare but
#     possible during gateway resume storms).
#
# Auth model (per backend-spec §6.6 + §4.4):
#   The bot calls api with `Authorization: Bearer <token>` where the
#   token is sourced from sops (`secrets/<env>.enc.yaml`
#   `discord.api_bearer_token`). The api validates via
#   `services/api/middleware.BotAuthMiddleware` and on match injects a
#   service-account `SessionContext` (username=`discord-bot`,
#   role=`owner`, auth_strength=`strong`). Token rotates quarterly with
#   a 1h overlap window; the bot doesn't need any cookie or CSRF state.
#
# Phase 0 vs Phase 1 entrypoint
# -----------------------------
#
# Day 23 ships THREE slash commands: /positions + /halt + /status. These
# are the read + risk-tighten subset per frontend-spec §6.1 LOCKED.
# Phase 1 adds /pnl + /exposure + /calendar + /last-fills + /ratify +
# /health + /vacation start + /close + /diary + /audit + /today PLUS
# the seven channel push paths (#daily-brief, #signals, #fills, #alerts,
# #critical, #ops, #audit). Phase 1 expansion lands when the Week 4 Wed
# dispatcher + signal emission pipeline land — the bot needs no
# Dockerfile change for that PR (commands are pure-Python additions
# under services/discord_bot/commands/).

# ---- builder ---------------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build deps for the small set of native extensions we touch (httpx via
# certifi/trustme on some platforms; discord.py is pure Python at 2.4).
# Mirrors webhook_pusher's set; smaller than the api's because we don't
# need argon2-cffi / asyncpg / uuid-utils.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Layer 1: copy pyproject + readme + stub package __init__.py files so
# pip-install can resolve deps before the actual source code (Layer 2)
# changes. Same caching pattern as services/api/Dockerfile +
# services/webhook_pusher/Dockerfile.
COPY pyproject.toml README.md ./
COPY services/__init__.py services/
COPY infrastructure/__init__.py infrastructure/
COPY strategies/__init__.py strategies/

# Pre-resolve runtime deps into the venv.
#
# discord_bot's runtime deps are a STRICT SUBSET of the api's. Keep
# this list narrow — the bot's image-pull frequency is highest in the
# fleet (Discord library updates + the bot is Phase 0 surface that
# evolves first). Version pins MUST match `pyproject.toml`'s
# `[project] dependencies` block (verified manually since
# scripts/check_dockerfile_deps_against_pyproject.py only checks the
# api Dockerfile per its docstring).
#
# Why each dep:
#   - discord.py             — gateway WS client + slash command tree
#   - httpx                  — api-client for /api/health,
#                              /api/positions/current,
#                              /api/system/kill-switch/invoke
#   - structlog              — canonical logging per dev-guide §3.5
#   - pydantic               — typed BotSettings + api response models
#   - pydantic-settings      — env-var loader for BotSettings
#   - pyyaml                 — entrypoint reads /run/secrets/decrypted.yaml
#                              and exports DISCORD_*/API_* env vars
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip wheel \
    && /opt/venv/bin/pip install \
        "discord.py>=2.4" \
        "httpx>=0.28" \
        "structlog>=24.4" \
        "pydantic>=2.9" \
        "pydantic-settings>=2.6" \
        "pyyaml>=6.0"

# Layer 2: copy the source tree. discord_bot is self-contained.
# strategies/ + infrastructure/ + the rest of services/ are NOT needed
# at runtime today; included only for symmetry with webhook_pusher (drop
# if image size becomes a concern Phase 1+).
COPY services/ services/

# Sanity check: importable module + slash-command registration intact.
# ModuleNotFoundError fires here at build time (NOT at first gateway
# connect), closing the same blind spot that bit Day 11 PR #47 with the
# api image's transitive-import gap.
RUN /opt/venv/bin/python -c \
    "from services.discord_bot.main import TradingBotClient; \
     from services.discord_bot.entrypoint import main as ep_main; \
     from services.discord_bot.commands import register_positions, register_halt, register_status; \
     from services.discord_bot.api_client import ApiClient; \
     from services.discord_bot.embeds import build_positions_embed, build_halt_confirm_embed, build_status_embed; \
     import discord; print(f'discord_bot import sanity OK; discord.py={discord.__version__}')"

# ---- runtime ---------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Runtime apt deps: libssl/libffi for httpx + discord.py SSL handshakes
# (gateway WSS requires TLS); ca-certificates for httpx's verify chain;
# tini for proper PID-1 signal handling so docker stop terminates the
# bot cleanly (else discord.py's gateway WS may not close gracefully and
# Discord's session takes ~60s to clean up server-side). NO curl (no
# healthcheck endpoint to hit — discord.py exposes no HTTP listener; the
# Phase-1 IPC listener PR adds one).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libffi8 \
        libssl3 \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 trading \
    && useradd  --uid 1000 --gid trading --no-create-home --shell /usr/sbin/nologin trading

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /build/services /app/services

USER trading

# tini reaps zombies + forwards signals; python entrypoint reads sops
# secrets → env vars then exec()s the bot main loop (asyncio.run).
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "services.discord_bot.entrypoint"]
