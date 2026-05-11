# services/api/Dockerfile — FastAPI api container.
#
# Two-stage build: a fat builder layer for the wheel + venv, then a slim
# runtime image that excludes build tools. Image hardening per backend-spec
# §8.11 (non-root user, --no-cache pip, minimal apt surface).
#
# Build context is the repo root so the builder can `pip install -e .`
# against pyproject.toml + the services/, infrastructure/, strategies/,
# alembic/ trees.
#
#   docker build -f services/api/Dockerfile -t local/trading-api:dev .

# ---- builder ---------------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build deps for argon2-cffi + asyncpg + uuid-utils.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Layer 1: copy pyproject + readme so pip-install can resolve deps before
# the rest of the source tree changes (faster rebuilds on code-only edits).
COPY pyproject.toml README.md ./
COPY services/__init__.py services/
COPY infrastructure/__init__.py infrastructure/
COPY strategies/__init__.py strategies/

# Pre-resolve runtime deps into the venv.
#
# Keep this list in sync with `pyproject.toml`'s `[project] dependencies`
# block. Drift here breaks runtime imports — see Day 11 carryover entry
# in `Docs/decisions-log.md` (2026-05-12) for the incident this comment
# commemorates: webhook_pusher (PR #44, Day 10) added httpx + resend to
# pyproject.toml but not to this list, and the Dockerfile build's import
# sanity check did NOT catch it because services.api.main does not
# transitively import services.webhook_pusher today. Discovered Day 11
# when the operator runbook smoke (deploy/webhook_pusher/README.md)
# tried to run `python -m services.webhook_pusher.cli` from inside the
# api container and ImportError'd on httpx.
#
# Drift between this list and pyproject.toml's [project] dependencies
# is enforced by `scripts/check_dockerfile_deps_against_pyproject.py`,
# wired into CI as the `dep-drift-check` job in `.github/workflows/ci.yml`.
# The workflow when adding a new runtime dep is:
#   1. Add to `pyproject.toml` `[project] dependencies`
#   2. Add to this list (with the same version pin)
#   3. `python3 scripts/check_dockerfile_deps_against_pyproject.py` (local check)
#   4. `docker compose --env-file deploy/.env build api` to verify the build
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip wheel \
    && /opt/venv/bin/pip install hatchling \
    && /opt/venv/bin/pip install \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.32" \
        "pydantic>=2.9" \
        "pydantic-settings>=2.6" \
        "sqlalchemy[asyncio]>=2.0.36" \
        "asyncpg>=0.30" \
        "alembic>=1.14" \
        "structlog>=24.4" \
        "argon2-cffi>=23.1" \
        "pyotp>=2.9" \
        "webauthn>=2.0" \
        "uuid-utils>=0.10" \
        "numpy>=2.1" \
        "discord.py>=2.4" \
        "httpx>=0.28" \
        "apscheduler>=3.11" \
        "resend>=2.4" \
        "anthropic>=0.39" \
        "pyyaml>=6.0" \
        "psycopg2-binary>=2.9"

# Layer 2: copy the rest of the source tree we actually run.
COPY services/ services/
COPY infrastructure/ infrastructure/
COPY strategies/ strategies/
COPY alembic/ alembic/
COPY alembic.ini ./

# Sanity check: importable module + route surface intact. Stub env var for
# the pydantic-settings load; real values come from sops at runtime.
RUN API_DATABASE_URL='postgresql+asyncpg://stub:stub@127.0.0.1:0/stub' \
    /opt/venv/bin/python -c "from services.api.main import create_app; create_app()"

# ---- runtime ---------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Runtime deps only: libssl/libffi for argon2-cffi + asyncpg native bits;
# curl is needed for the docker-compose healthcheck (`curl -fsS .../health`).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libffi8 \
        libssl3 \
        curl \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 trading \
    && useradd  --uid 1000 --gid trading --no-create-home --shell /usr/sbin/nologin trading

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /build/services /app/services
COPY --from=builder /build/infrastructure /app/infrastructure
COPY --from=builder /build/strategies /app/strategies
COPY --from=builder /build/alembic /app/alembic
COPY --from=builder /build/alembic.ini /app/alembic.ini

USER trading

EXPOSE 8000

# tini reaps zombies (stdlib uvicorn workers don't); python entrypoint
# resolves sops secrets → env vars then exec()s uvicorn.
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "services.api.entrypoint"]
