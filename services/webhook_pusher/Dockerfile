# services/webhook_pusher/Dockerfile — alerts-delivery container.
#
# Mirrors the shape of `services/api/Dockerfile`: two-stage build (fat
# builder + slim runtime), non-root user, minimal apt surface, image
# hardening per backend-spec §8.11. Smaller than the api image because
# webhook_pusher's runtime deps are a strict subset (httpx + structlog
# + sqlalchemy[asyncio] + asyncpg — see the pip-install list below).
#
# Why this container exists
# -------------------------
#
# Backend architecture (backend-spec §8.11) keeps the api service on
# `trading_internal` (Docker `Internal: true` — no external internet) to
# limit blast radius if the api is compromised. Outbound HTTP for
# webhooks (Discord) and email (Resend) routes through a dedicated
# worker on `trading_internal` + `trading_egress`. Day 11 carryover #2
# (decisions-log 2026-05-12) documented the temporary Phase 0 workaround
# of attaching the api container to `trading_egress`; this container
# replaces that workaround with the architecturally correct deployment.
#
# Phase 0 vs Phase 1 entrypoint
# -----------------------------
#
# The Week 4 risk dispatcher will eventually invoke `dispatch_alert(...)`
# either via HTTP RPC (webhook_pusher exposes a small FastAPI app), via
# postgres LISTEN/NOTIFY (webhook_pusher polls a channel for new alert
# IDs), or via a job queue. That design lands separately. For now this
# container is a "ready-to-be-exec'd" placeholder: it starts and stays
# alive (logging once at boot) so the operator runbook can `docker
# compose exec webhook_pusher python -m services.webhook_pusher.cli ...`
# to run the smoke against an egress-enabled container.
#
# When the Week 4 dispatcher PR lands, swap the CMD to the real worker
# entrypoint and add a healthcheck. The Dockerfile's Python deps + image
# layout are already correct for that future state.

# ---- builder ---------------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build deps for asyncpg native extensions. (No argon2 / no uuid-utils
# native bits needed in this container — webhook_pusher doesn't touch
# auth or generate uuid7s; alerts row INSERTs are the api's job.)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Layer 1: copy pyproject + readme + stub package __init__.py files so
# pip-install can resolve deps without invalidating the cache when the
# actual source code (Layer 2 below) changes. Same caching pattern as
# services/api/Dockerfile.
COPY pyproject.toml README.md ./
COPY services/__init__.py services/
COPY infrastructure/__init__.py infrastructure/
COPY strategies/__init__.py strategies/

# Pre-resolve runtime deps into the venv.
#
# webhook_pusher's runtime deps are a STRICT SUBSET of the api's. Keep
# this list narrow — a smaller image surface means a smaller attack
# surface and faster pulls. The version pins MUST match
# `pyproject.toml`'s `[project] dependencies` block (same dep-drift
# discipline as services/api/Dockerfile per Day 11 carryover #2 +
# `scripts/check_dockerfile_deps_against_pyproject.py`).
#
# Why each dep:
#   - httpx                 — Discord webhook POST + Resend HTTP API
#   - structlog             — canonical logging per dev-guide §3.5
#   - sqlalchemy[asyncio]   — AsyncSession for the dispatcher's SELECT/UPDATE
#                             on the alerts table
#   - asyncpg               — async Postgres driver (sqlalchemy[asyncio]
#                             needs an async DB driver; asyncpg is the
#                             spec-locked one per dev-guide §7.2)
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip wheel \
    && /opt/venv/bin/pip install \
        "httpx>=0.28" \
        "structlog>=24.4" \
        "sqlalchemy[asyncio]>=2.0.36" \
        "asyncpg>=0.30"

# Layer 2: copy the source tree. webhook_pusher is self-contained but
# `services/__init__.py` may eventually re-export from sibling packages,
# so we copy the full services/ tree to be safe (pattern-match with the
# api Dockerfile). Strategies/ + infrastructure/ are NOT needed by
# webhook_pusher today; included only for symmetry — drop if image size
# becomes a concern.
COPY services/ services/

# Sanity check: importable module surface intact. ModuleNotFoundError
# would fire here at build time before deploy, NOT at first request from
# inside the container (the gap that bit Day 11 / PR #47 with the api
# image's transitive-import blind spot).
RUN /opt/venv/bin/python -c \
    "import services.webhook_pusher.cli, services.webhook_pusher.dispatcher, services.webhook_pusher.payloads, services.webhook_pusher.sender; import httpx; print('webhook_pusher import sanity OK')"

# ---- runtime ---------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Runtime apt deps: libssl/libffi for asyncpg native bits; tini for
# proper PID-1 signal handling so docker stop terminates the container
# cleanly. NO curl (no healthcheck endpoint to hit yet — added when the
# Week 4 dispatcher PR exposes a real /health). NO ca-certificates? —
# httpx uses certifi via Python; we still need ca-certificates for the
# system OpenSSL that asyncpg uses for TLS to Postgres if Phase 1+ ever
# turns on TLS in transit.
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

# Phase 0 entrypoint: stay alive so the operator runbook can `docker
# compose exec webhook_pusher /opt/venv/bin/python -m services.webhook_pusher.cli ...`.
# When the Week 4 dispatcher PR lands, replace CMD with the worker
# entrypoint and add a real healthcheck.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "echo 'webhook_pusher container started (Phase 0 placeholder; CLI-only invocation pattern — see deploy/webhook_pusher/README.md). Will be replaced by a real worker process in the Week 4 risk dispatcher PR.'; exec sleep infinity"]
