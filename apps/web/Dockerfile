# apps/web/Dockerfile — Next.js 14 (App Router) frontend container.
#
# Mirrors the two-stage shape of services/api/Dockerfile and
# services/webhook_pusher/Dockerfile: builder compiles the Next.js
# standalone bundle, runtime exposes only the minimum Node + bundle.
# Non-root user (the upstream `node:20-alpine` image ships `node` at
# uid/gid 1000; we reuse it rather than colliding by creating
# `trading` at the same gid), tini entrypoint, /-based healthcheck.
#
# Build context is `apps/web/` (NOT the repo root). The api +
# webhook_pusher containers need repo-root context for pyproject.toml +
# cross-package source; this container only consumes apps/web. When
# Phase 1+ adds packages/api-types as a shared dependency, the context
# bumps back to the repo root.
#
#   docker build -f apps/web/Dockerfile -t local/trading-web:dev apps/web

# ---- builder ---------------------------------------------------------------
FROM node:20-alpine AS builder

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1

# pnpm via corepack; version pinned by package.json's packageManager field.
# Corepack installs the pinned pnpm release on first invocation.
RUN corepack enable

WORKDIR /build

# Layer 1: lockfile + package.json only — dep install caches independently
# from source edits.
COPY package.json pnpm-lock.yaml ./

RUN pnpm install --frozen-lockfile

# Layer 2: source tree + Tailwind/Next/TS configs.
COPY . .

RUN pnpm build

# ---- runtime ---------------------------------------------------------------
FROM node:20-alpine AS runtime

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# tini reaps zombies; curl is needed for the docker-compose healthcheck.
# The base node:20-alpine image already provides the `node` user at
# uid/gid 1000 — reuse it for the runtime (mirrors backend-spec §8.11's
# non-root principle without colliding with the upstream image).
RUN apk add --no-cache tini curl

WORKDIR /app

# Next.js standalone output ships its own server.js + minimal node_modules
# (only the deps actually referenced by the built bundle). Static assets
# live under .next/static. public/ is the source-controlled assets dir.
COPY --from=builder --chown=node:node /build/.next/standalone ./
COPY --from=builder --chown=node:node /build/.next/static ./.next/static
COPY --from=builder --chown=node:node /build/public ./public

USER node

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:3000/ || exit 1

ENTRYPOINT ["/sbin/tini", "--", "node", "server.js"]
