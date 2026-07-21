#!/usr/bin/env bash
# deploy/day5-bringup.sh — idempotent end-to-end Day-5 bringup on the Ashburn VPS.
#
# Performs the full stack-up flow in one shot:
#   1. Sanity-check prerequisites (deploy/.env, host secrets file, etc.)
#   2. Validate the plain-YAML secrets file + set container-readable perms
#   3. Build the api image (if not cached)
#   4. Bring up postgres + wait healthy
#   5. Run alembic migrations (idempotent — alembic skips applied)
#   6. ALTER ROLE app_service / app_owner with secrets-file passwords
#   7. Bring up api + caddy
#   8. Wait for api health + capture SETUP_TOKEN_EMITTED from logs
#   9. Verify /api/health locally
#
# Run as root from /opt/trading. Re-runnable from any state — each step
# probes for completion before doing work.
#
# Usage:
#   cd /opt/trading
#   bash deploy/day5-bringup.sh              # first bringup / idempotent re-run
#   bash deploy/day5-bringup.sh --rebuild    # REDEPLOY: rebuild images + recreate
#
# --rebuild (added 2026-07-09 after the #364 hotfix deploy needed three
# hand-applied corrections): rebuilds ALL app images even when cached — so
# the alembic step runs inside the fresh image — and force-recreates every
# RUNNING app container afterwards (compose `restart` alone re-uses the old
# image; decisions-log 2026-07-09 "C1 night one"). This makes
#   git pull && bash deploy/day5-bringup.sh --rebuild
# the complete Appendix C redeploy ceremony.
#
# To force a clean rebuild from a destroyed state:
#   docker compose --env-file deploy/.env down -v
#   bash deploy/day5-bringup.sh

set -euo pipefail

REBUILD=0
for arg in "$@"; do
  case "${arg}" in
    --rebuild) REBUILD=1 ;;
    *) printf 'unknown argument: %s (supported: --rebuild)\n' "${arg}" >&2; exit 64 ;;
  esac
done

# ---------------------------------------------------------------------------
# Constants + sanity checks
# ---------------------------------------------------------------------------

REPO_ROOT="${REPO_ROOT:-/opt/trading}"
# DEPLOY_ENV_PATH (not ENV_FILE) so `source deploy/.env` can never clobber it.
DEPLOY_ENV_PATH="${REPO_ROOT}/deploy/.env"
SECRETS_DIR_HOST="${SECRETS_DIR_HOST:-/opt/trading-secrets}"
SECRETS_YAML="${SECRETS_DIR_HOST}/secrets.yaml"

cd "${REPO_ROOT}"

step() { printf "\n\033[1;36m==> %s\033[0m\n" "$*"; }
ok()   { printf "    \033[1;32m✓\033[0m %s\n" "$*"; }
warn() { printf "    \033[1;33m!\033[0m %s\n" "$*"; }
die()  { printf "\n    \033[1;31m✗\033[0m %s\n" "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Step 0 — prerequisites
# ---------------------------------------------------------------------------

step "Step 0 — prerequisites"

[[ "$(id -u)" -eq 0 ]] || die "must be run as root"
[[ -f "${DEPLOY_ENV_PATH}" ]] || die "${DEPLOY_ENV_PATH} does not exist; author it per deploy/.env.example"
[[ -r "${DEPLOY_ENV_PATH}" ]] || die "${DEPLOY_ENV_PATH} not readable"

# Source deploy/.env for our own use; compose loads it via --env-file.
set -a
# shellcheck disable=SC1090
source "${DEPLOY_ENV_PATH}"
set +a

: "${POSTGRES_SUPERUSER_PASSWORD:?missing in ${DEPLOY_ENV_PATH}}"

[[ -f "${SECRETS_YAML}" ]] || die "${SECRETS_YAML} does not exist; author it per deploy/secrets.template.yaml (see deploy/crypto-vps-bringup.md Step 3)"
command -v docker >/dev/null || die "docker not installed"
python3 -c 'import yaml' 2>/dev/null || die "python3-yaml not installed (apt-get install -y python3-yaml)"

# Defensive: stale docker-compose.override.yml from prior debug sessions
# survives `git reset --hard` (gitignored, so untracked) and silently
# breaks api volume mounts. If one is present, warn loudly and remove it.
if [[ -f "${REPO_ROOT}/docker-compose.override.yml" ]]; then
  warn "stale docker-compose.override.yml found — removing (was from a prior debug session)"
  rm -f "${REPO_ROOT}/docker-compose.override.yml"
fi

ok "deploy/.env loaded"
ok "secrets file + docker + python3-yaml available"

# ---------------------------------------------------------------------------
# Step 1 — validate the host secrets file + set container-readable perms
# ---------------------------------------------------------------------------

step "Step 1 — validate ${SECRETS_YAML}"

# Container runs as uid 1000 (`trading` user in the api Dockerfile);
# bind-mount preserves host perms so file must be readable to that uid.
chown -R 1000:1000 "${SECRETS_DIR_HOST}"
chmod 0500 "${SECRETS_DIR_HOST}"
chmod 0400 "${SECRETS_YAML}"
ok "secrets file perms set (uid 1000, mode 0400)"

# Parse-check the YAML + extract the postgres app-role passwords.
# Float leaves = YAML numeric coercion ate a long-digit string (quote it
# in the file); the api entrypoint enforces the same rule at boot.
mapfile -t PG_PWDS < <(python3 - "${SECRETS_YAML}" <<'PYVALIDATE'
import sys

import yaml

with open(sys.argv[1], encoding="utf-8") as fh:
    data = yaml.safe_load(fh) or {}


def find_floats(node, path=()):
    if isinstance(node, float):
        yield ".".join(path) or "<root>"
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from find_floats(value, (*path, str(key)))
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            yield from find_floats(value, (*path, f"[{idx}]"))


floats = sorted(find_floats(data))
if floats:
    sys.stderr.write(
        "float-typed value(s) at: " + ", ".join(floats)
        + " — wrap long all-digit values in double quotes\n"
    )
    raise SystemExit(1)

for key in ("app_service_password", "app_owner_password"):
    value = (data.get("postgres") or {}).get(key)
    if not value or str(value).startswith("<TODO"):
        sys.stderr.write(f"postgres.{key} missing or placeholder\n")
        raise SystemExit(1)
    print(value)
PYVALIDATE
) || die "secrets file invalid — fix ${SECRETS_YAML} per deploy/secrets.template.yaml"

APP_SERVICE_PWD="${PG_PWDS[0]}"
APP_OWNER_PWD="${PG_PWDS[1]}"
ok "postgres app-role passwords extracted (${#APP_SERVICE_PWD} + ${#APP_OWNER_PWD} chars)"

# ---------------------------------------------------------------------------
# Step 2 — build api image (idempotent; uses Docker layer cache)
# ---------------------------------------------------------------------------

step "Step 2 — build api image"

if [[ "${REBUILD}" -eq 1 ]]; then
  # Redeploy path: rebuild ALL app images unconditionally. The skip-if-cached
  # branch below is bringup-only — on a redeploy it reuses the stale image and
  # the alembic step (Step 4) then no-ops without the new migration files
  # while reporting success (the 2026-07-09 launch tripled on this).
  docker compose --env-file "${DEPLOY_ENV_PATH}" build api discord_bot webhook_pusher nextjs
  ok "app images rebuilt (--rebuild): api, discord_bot, webhook_pusher, nextjs"
elif docker image inspect "ghcr.io/${GHCR_OWNER}/trading-api:${RELEASE_SHA:-latest}" >/dev/null 2>&1; then
  ok "image already cached: ghcr.io/${GHCR_OWNER}/trading-api:${RELEASE_SHA:-latest}"
else
  docker compose --env-file "${DEPLOY_ENV_PATH}" build api
  ok "api image built"
fi

# ---------------------------------------------------------------------------
# Step 3 — bring up postgres + wait healthy
# ---------------------------------------------------------------------------

step "Step 3 — postgres up + healthy"

docker compose --env-file "${DEPLOY_ENV_PATH}" up -d postgres

deadline=$(( $(date +%s) + 60 ))
until docker compose --env-file "${DEPLOY_ENV_PATH}" exec -T postgres pg_isready -U postgres -d trading >/dev/null 2>&1; do
  [[ $(date +%s) -lt ${deadline} ]] || die "postgres did not become healthy within 60s"
  sleep 2
done
ok "postgres healthy"

# ---------------------------------------------------------------------------
# Step 4 — alembic migrations (idempotent; alembic detects applied)
# ---------------------------------------------------------------------------

step "Step 4 — alembic upgrade head"

PG_URL_SUPER="postgresql+psycopg2://postgres:${POSTGRES_SUPERUSER_PASSWORD}@postgres:5432/trading"
# alembic logs to STDERR — merge it into the tee'd stream, or the grep
# below never sees "Running upgrade" and misreports an applied migration
# as a no-op (observed on the 2026-07-20 batch deploy).
docker compose --env-file "${DEPLOY_ENV_PATH}" run --rm \
  -e DATABASE_URL="${PG_URL_SUPER}" \
  --entrypoint sh \
  api -c 'alembic upgrade head' 2>&1 \
  | tee /tmp/alembic-upgrade.log
grep -q "Running upgrade" /tmp/alembic-upgrade.log \
  && ok "migrations applied — see Running-upgrade line(s) above" \
  || ok "no new migrations (already at head)"

# ---------------------------------------------------------------------------
# Step 5 — ALTER ROLE app_service / app_owner (idempotent)
# ---------------------------------------------------------------------------

step "Step 5 — ALTER ROLE app_service + app_owner"

docker compose --env-file "${DEPLOY_ENV_PATH}" exec -T \
  -e PGPASSWORD="${POSTGRES_SUPERUSER_PASSWORD}" \
  postgres psql -U postgres -d trading <<SQL
ALTER ROLE app_service WITH LOGIN PASSWORD '${APP_SERVICE_PWD}';
ALTER ROLE app_owner   WITH LOGIN PASSWORD '${APP_OWNER_PWD}';
SQL
ok "app_service + app_owner passwords set"

# Verify app_service can authenticate.
docker compose --env-file "${DEPLOY_ENV_PATH}" exec -T \
  -e PGPASSWORD="${APP_SERVICE_PWD}" \
  postgres psql -U app_service -d trading -c "SELECT current_user;" >/dev/null
ok "app_service auth verified"

# ---------------------------------------------------------------------------
# Step 6 — api + caddy up
# ---------------------------------------------------------------------------

step "Step 6 — api + caddy up"

# Force-recreate api + caddy in case a stale container exists with the old
# (broken) volume config from a prior debug session. `up -d` alone won't
# always recreate; explicit stop+rm guarantees the new compose definition
# wins.
docker compose --env-file "${DEPLOY_ENV_PATH}" stop api caddy 2>/dev/null || true
docker compose --env-file "${DEPLOY_ENV_PATH}" rm -f api caddy 2>/dev/null || true
docker compose --env-file "${DEPLOY_ENV_PATH}" up -d api caddy

deadline=$(( $(date +%s) + 90 ))
until [[ "$(docker compose --env-file "${DEPLOY_ENV_PATH}" ps --format json api 2>/dev/null | grep -o '"Health":"healthy"' || true)" == '"Health":"healthy"' ]]; do
  [[ $(date +%s) -lt ${deadline} ]] || die "api did not become healthy within 90s; check 'docker compose logs api'"
  sleep 3
done
ok "api healthy"

# ---------------------------------------------------------------------------
# Step 7 — capture SETUP_TOKEN_EMITTED from api logs
# ---------------------------------------------------------------------------

step "Step 7 — setup token (one-time, capture into 1Password)"

set +e
SETUP_LINE="$(docker compose --env-file "${DEPLOY_ENV_PATH}" logs api 2>/dev/null | grep "SETUP_TOKEN_EMITTED" | head -1)"
set -e
if [[ -n "${SETUP_LINE}" ]]; then
  printf "\n\033[1;33m=== COPY THIS LINE TO 1PASSWORD ===\033[0m\n"
  printf "%s\n" "${SETUP_LINE}"
  printf "\033[1;33m===================================\033[0m\n"
  ok "setup token printed above"
else
  warn "no SETUP_TOKEN_EMITTED in api logs"
  warn "either: (a) a token was already consumed (no-op restart), OR"
  warn "        (b) the api couldn't reach postgres on first boot (check logs)"
fi

# ---------------------------------------------------------------------------
# Step 8 — verification gate
# ---------------------------------------------------------------------------

step "Step 8 — verification gate (curl /api/health)"

HEALTH="$(docker compose --env-file "${DEPLOY_ENV_PATH}" exec -T api curl -fsS http://localhost:8000/api/health || echo 'CURL_FAILED')"
if [[ "${HEALTH}" == "CURL_FAILED" ]]; then
  die "/api/health curl failed inside the container; check 'docker compose logs api'"
fi
printf "    %s\n" "${HEALTH}"

if echo "${HEALTH}" | grep -q '"status":"ok"' && echo "${HEALTH}" | grep -q '"db_connected":true'; then
  ok "verification gate PASSED — /api/health returned ok + db_connected:true"
else
  die "verification gate FAILED — health response above does not match expected shape"
fi

# ---------------------------------------------------------------------------
# Step 9 (--rebuild only) — force-recreate running app containers onto the
# fresh images. `restart` is NOT sufficient: it reboots the existing
# container on its OLD image (decisions-log 2026-07-09 "C1 night one").
# Only containers that are already running are recreated — this step never
# starts a service the operator hasn't brought up.
# ---------------------------------------------------------------------------

if [[ "${REBUILD}" -eq 1 ]]; then
  step "Step 9 — force-recreate running app containers (--rebuild)"
  for svc in strategy_worker discord_bot webhook_pusher nextjs; do
    if docker compose --env-file "${DEPLOY_ENV_PATH}" ps --status running "${svc}" 2>/dev/null | grep -q "${svc}"; then
      docker compose --env-file "${DEPLOY_ENV_PATH}" up -d --force-recreate "${svc}"
      ok "${svc} recreated on the fresh image"
    else
      ok "${svc} not running — skipped (start it explicitly when ready)"
    fi
  done
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

printf "\n\033[1;32m========================================\033[0m\n"
printf "\033[1;32m  Day 5 verification gate CLOSED\033[0m\n"
printf "\033[1;32m========================================\033[0m\n"
printf "\nNext steps:\n"
printf "  - From your laptop: curl -fsS https://%s/api/health | jq .\n" "${DOMAIN}"
printf "  - Capture the SETUP_TOKEN above into 1Password\n"
printf "  - Run the Ashburn ↔ Discord webhook test (deploy/api/README.md Step 10)\n"
printf "  - Mark Day 5 verification gate complete in implementation-guide.md §3\n"

unset APP_SERVICE_PWD APP_OWNER_PWD POSTGRES_SUPERUSER_PASSWORD
