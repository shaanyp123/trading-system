#!/usr/bin/env bash
# deploy/day5-bringup.sh — idempotent end-to-end Day-5 bringup on the Ashburn VPS.
#
# Performs the full stack-up flow in one shot:
#   1. Sanity-check prerequisites (deploy/.env, sops binary, age key, etc.)
#   2. Decrypt sops yaml on the host (no sops container needed)
#   3. Build the api image (if not cached)
#   4. Bring up postgres + wait healthy
#   5. Run alembic migrations (idempotent — alembic skips applied)
#   6. ALTER ROLE app_service / app_owner with sops-stored passwords
#   7. Bring up api + caddy
#   8. Wait for api health + capture SETUP_TOKEN_EMITTED from logs
#   9. Verify /api/health locally
#
# Run as root from /opt/trading. Re-runnable from any state — each step
# probes for completion before doing work.
#
# Usage:
#   cd /opt/trading
#   bash deploy/day5-bringup.sh
#
# To force a clean rebuild from a destroyed state:
#   docker compose --env-file deploy/.env down -v
#   bash deploy/day5-bringup.sh

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants + sanity checks
# ---------------------------------------------------------------------------

REPO_ROOT="${REPO_ROOT:-/opt/trading}"
# Renamed from ENV_FILE to avoid collision with deploy/.env's own ENV_FILE
# variable (older runbooks set ENV_FILE=paper.enc.yaml). When the script
# `source`s deploy/.env, an ENV_FILE inside the file would clobber the path
# stored here. DEPLOY_ENV_PATH is unique enough to never collide.
DEPLOY_ENV_PATH="${REPO_ROOT}/deploy/.env"
SECRETS_DIR_HOST="${SECRETS_DIR_HOST:-/opt/trading/secrets-decrypted}"
DECRYPTED_YAML="${SECRETS_DIR_HOST}/decrypted.yaml"

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
: "${SOPS_AGE_KEY_FILE:?missing in ${DEPLOY_ENV_PATH}}"
: "${ENV_FILE_NAME:=${ENV_FILE_NAME:-paper.enc.yaml}}"

[[ -r "${SOPS_AGE_KEY_FILE}" ]] || die "age key file ${SOPS_AGE_KEY_FILE} not readable"
command -v sops >/dev/null || die "sops binary not on PATH; install per deploy/api/README.md"
command -v docker >/dev/null || die "docker not installed"

# Defensive: stale docker-compose.override.yml from prior debug sessions
# survives `git reset --hard` (gitignored, so untracked) and silently
# breaks api volume mounts. If one is present, warn loudly and remove it.
if [[ -f "${REPO_ROOT}/docker-compose.override.yml" ]]; then
  warn "stale docker-compose.override.yml found — removing (was from a prior debug session)"
  rm -f "${REPO_ROOT}/docker-compose.override.yml"
fi

ok "deploy/.env loaded"
ok "age key + sops + docker available"

# ---------------------------------------------------------------------------
# Step 1 — decrypt sops yaml on the host
# ---------------------------------------------------------------------------

step "Step 1 — decrypt sops yaml → ${DECRYPTED_YAML}"

mkdir -p "${SECRETS_DIR_HOST}"
SOPS_AGE_KEY_FILE="${SOPS_AGE_KEY_FILE}" \
  sops -d "${REPO_ROOT}/secrets/${ENV_FILE_NAME:-paper.enc.yaml}" > "${DECRYPTED_YAML}.new"
mv "${DECRYPTED_YAML}.new" "${DECRYPTED_YAML}"

# Container runs as uid 1000 (`trading` user in the api Dockerfile);
# bind-mount preserves host perms so file must be readable to that uid.
chown -R 1000:1000 "${SECRETS_DIR_HOST}"
chmod 0500 "${SECRETS_DIR_HOST}"
chmod 0400 "${DECRYPTED_YAML}"
ok "decrypted yaml ready (uid 1000, mode 0400)"

# Sanity-check: extract postgres app_service_password and confirm it's not a placeholder.
APP_SERVICE_PWD="$(SOPS_AGE_KEY_FILE="${SOPS_AGE_KEY_FILE}" \
  sops -d --extract '["postgres"]["app_service_password"]' \
  "${REPO_ROOT}/secrets/${ENV_FILE_NAME:-paper.enc.yaml}")"
APP_OWNER_PWD="$(SOPS_AGE_KEY_FILE="${SOPS_AGE_KEY_FILE}" \
  sops -d --extract '["postgres"]["app_owner_password"]' \
  "${REPO_ROOT}/secrets/${ENV_FILE_NAME:-paper.enc.yaml}")"

case "${APP_SERVICE_PWD}" in
  "<TODO"*|"")
    die "postgres.app_service_password is still a placeholder in secrets/${ENV_FILE_NAME:-paper.enc.yaml} — run 'sops secrets/${ENV_FILE_NAME:-paper.enc.yaml}' and fill it"
    ;;
esac
case "${APP_OWNER_PWD}" in
  "<TODO"*|"")
    die "postgres.app_owner_password is still a placeholder in secrets/${ENV_FILE_NAME:-paper.enc.yaml} — run 'sops secrets/${ENV_FILE_NAME:-paper.enc.yaml}' and fill it"
    ;;
esac
ok "postgres app-role passwords extracted (${#APP_SERVICE_PWD} + ${#APP_OWNER_PWD} chars)"

# ---------------------------------------------------------------------------
# Step 2 — build api image (idempotent; uses Docker layer cache)
# ---------------------------------------------------------------------------

step "Step 2 — build api image"

if docker image inspect "ghcr.io/${GHCR_OWNER}/trading-api:${RELEASE_SHA:-latest}" >/dev/null 2>&1; then
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
docker compose --env-file "${DEPLOY_ENV_PATH}" run --rm \
  -e DATABASE_URL="${PG_URL_SUPER}" \
  --entrypoint sh \
  api -c 'alembic upgrade head' \
  | tee /tmp/alembic-upgrade.log
grep -q "Running upgrade" /tmp/alembic-upgrade.log \
  && ok "migrations applied (or no-op if already at head)" \
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
