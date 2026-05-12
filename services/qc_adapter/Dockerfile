# services/qc_adapter/Dockerfile — QC ObjectStore poll worker.
#
# Mirrors the shape of `services/webhook_pusher/Dockerfile` +
# `services/discord_bot/Dockerfile`: two-stage build (fat builder +
# slim runtime), non-root user, minimal apt surface, tini PID-1. Ships
# in Day 28 (Week 7 Thu) per implementation-guide §3 Week 7 Thu — the
# Phase-1 pipeline build PR-A of 5.
#
# Why this container exists
# -------------------------
#
# Phase 1 backend has NO direct IBKR connection (dev-guide §1.5 LOCKED).
# QC algorithm pushes signals + state to QC ObjectStore; this container
# polls that REST surface and writes parsed events to the canonical
# audit_log + signals tables. Network shape:
#   internal — Postgres writes via app_service role
#   egress   — outbound TLS to https://www.quantconnect.com/api/v2
#
# Auth model (per backend-spec §4.4 + QC's docs):
#   HTTP Basic with username = QC numeric user_id, password =
#   sha256("{api_token}:{timestamp}") hex. Both credentials come from
#   sops `quantconnect.{organization_id,api_token}`. See
#   `deploy/qc_adapter/README.md` for the operator runbook.

# ---- builder ---------------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build deps for asyncpg native extensions + uuid-utils + cryptography
# (httpx pulls cryptography transitively on some platforms).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Layer 1: copy pyproject + readme + stub __init__.py files so pip-install
# can resolve deps without invalidating the cache when the actual source
# tree (Layer 2) changes. Same pattern as services/webhook_pusher/.
COPY pyproject.toml README.md ./
COPY services/__init__.py services/
COPY infrastructure/__init__.py infrastructure/
COPY strategies/__init__.py strategies/

# Pre-resolve runtime deps into the venv.
#
# qc_adapter's runtime deps:
#   - httpx                 — QC ObjectStore REST + presigned S3 GETs
#   - structlog             — canonical logging per dev-guide §3.5
#   - sqlalchemy[asyncio]   — AsyncSession for cursor + signals writes
#   - asyncpg               — async Postgres driver
#   - pydantic              — typed QCAdapterSettings
#   - pydantic-settings     — env-var loader for QCAdapterSettings
#   - pyyaml                — entrypoint reads /run/secrets/decrypted.yaml
#   - uuid-utils            — UUIDv7 for the audit writer's event_uuid mint
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip wheel \
    && /opt/venv/bin/pip install \
        "httpx>=0.28" \
        "structlog>=24.4" \
        "sqlalchemy[asyncio]>=2.0.36" \
        "asyncpg>=0.30" \
        "pydantic>=2.9" \
        "pydantic-settings>=2.6" \
        "pyyaml>=6.0" \
        "uuid-utils>=0.9"

# Layer 2: copy the source tree. We need services.audit (audit writer
# + JCS chain) at runtime; copying the full services/ tree is the
# safest pattern.
COPY services/ services/

# Sanity check: importable module surface intact. ModuleNotFoundError
# fires here at build time, NOT at the first poll cycle — closes the
# same blind spot that bit Day 11 / PR #47 with the api image.
RUN /opt/venv/bin/python -c \
    "import services.qc_adapter.main, \
            services.qc_adapter.orchestrator, \
            services.qc_adapter.qc_objectstore_client, \
            services.qc_adapter.signal_ingestion, \
            services.qc_adapter.db, \
            services.qc_adapter.config, \
            services.qc_adapter.entrypoint, \
            services.audit.writer, \
            services.audit.chain, \
            services.audit.event_types, \
            httpx; print('qc_adapter import sanity OK')"

# ---- runtime ---------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Runtime apt deps: libssl/libffi for asyncpg + httpx SSL handshakes;
# ca-certificates for httpx's verify chain (TLS to quantconnect.com +
# presigned S3 URLs); tini for proper PID-1 signal handling. NO curl
# (no /health endpoint to hit — the orchestrator's structlog
# `orchestrator_cycle_completed` event is the liveness signal; future
# /healthz lands when we add one).
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

# tini reaps zombies + forwards signals; entrypoint reads sops secrets
# → env vars then exec()s the orchestrator main loop (asyncio.run).
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "services.qc_adapter.entrypoint"]
