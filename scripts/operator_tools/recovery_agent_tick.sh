#!/usr/bin/env bash
# scripts/operator_tools/recovery_agent_tick.sh
#
# Per-tick wrapper for the recovery agent. Designed for systemd timer
# use (fires every 60s via `deploy/audit/systemd/recovery-agent-poll.timer`).
#
# Mirrors `scripts/operator_tools/verify_chain_to_discord.sh` structure
# verbatim: sops decrypt → DATABASE_URL → docker exec api python -m … →
# exit 0. The recovery agent itself handles its own Discord posts via
# the dedicated /etc/trading/critical-webhook.url file, so the wrapper
# doesn't need to parse output or post.
#
# Independent of api's webhook_pusher by design — if the api is
# partially up (Postgres reachable but webhook_pusher down) the
# recovery agent's audit + alert UPDATE writes still land + its own
# Discord post fires. The 24/7 safety-net pattern from drill 5/6
# retrospectives.
#
# Required state on the VPS:
#   /etc/trading/critical-webhook.url — chmod 600, owned by `trading` user;
#                                       Discord #critical webhook URL (one line)
#   /opt/trading/                      — the trading-system repo checkout
#   /opt/trading/deploy/.env           — chmod 600 root:root; carries GHCR_OWNER etc.
#   /opt/trading/secrets/<env>.enc.yaml — sops-encrypted; carries postgres.app_service_password
#   docker access for the running user (systemd unit runs as root)
#
# Per memory feedback_secret_handling.md:
#   - Webhook URL is treated as a credential (chmod 600)
#   - sops-decrypted password lives in tmpfs only (mktemp + shred -u)
#   - DATABASE_URL is unset after the docker exec returns
#   - CRITICAL_WEBHOOK_URL is unset after the docker exec returns
#
# Exit codes:
#   0 — always (cron-friendly; per-tick outcomes flow through Discord +
#       structlog/journal). The recovery agent's own exit code is
#       captured in the journal but not propagated.

set -uo pipefail

ENV_TAG="${1:-paper}"
WEBHOOK_FILE="/etc/trading/critical-webhook.url"
TRADING_ROOT="/opt/trading"
PW_TMP="$(mktemp /tmp/recovery-agent-pw.XXXXXX)"

cleanup() {
  if [ -f "$PW_TMP" ]; then
    shred -u "$PW_TMP" 2>/dev/null || rm -f "$PW_TMP"
  fi
}
trap cleanup EXIT

# Sanity check the webhook URL is readable. Log + continue: the
# recovery agent handles missing webhook URL gracefully (audit +
# alert UPDATE still land; Discord post skipped with a WARNING).
if [ ! -r "$WEBHOOK_FILE" ]; then
  echo "WARNING: cannot read $WEBHOOK_FILE; recovery agent will skip Discord post" >&2
  CRITICAL_WEBHOOK_URL=""
else
  CRITICAL_WEBHOOK_URL="$(cat "$WEBHOOK_FILE")"
fi

cd "$TRADING_ROOT" || {
  echo "ERROR: cannot cd to $TRADING_ROOT — host config drift" >&2
  exit 0
}

# Source deploy/.env to get SOPS_AGE_KEY_FILE (matches verify-chain pattern).
set -a
# shellcheck disable=SC1091
. /opt/trading/deploy/.env 2>/dev/null
set +a

# Decrypt sops + extract app_service password into tmpfs file (chmod 600).
chmod 600 "$PW_TMP"
if ! sops -d "/opt/trading/secrets/${ENV_TAG}.enc.yaml" 2>/dev/null \
     | yq '.postgres.app_service_password' -r > "$PW_TMP" 2>/dev/null \
     || ! [ -s "$PW_TMP" ]; then
  echo "ERROR: sops decrypt failed for /opt/trading/secrets/${ENV_TAG}.enc.yaml; check SOPS_AGE_KEY_FILE" >&2
  exit 0
fi

APP_SERVICE_PW="$(cat "$PW_TMP")"
shred -u "$PW_TMP" 2>/dev/null || rm -f "$PW_TMP"

# +asyncpg driver scheme required — recovery_agent uses sqlalchemy async.
# Matches verify_chain_to_discord.sh contract verbatim.
export DATABASE_URL="postgresql+asyncpg://app_service:${APP_SERVICE_PW}@postgres:5432/trading"
unset APP_SERVICE_PW
export CRITICAL_WEBHOOK_URL

# Run the recovery agent inside the api container. The container's
# image bakes the scripts/ directory (services/api/Dockerfile:98) so
# `python -m scripts.operator_tools.recovery_agent` resolves cleanly.
#
# stdout + stderr flow through systemd journal via the .service unit's
# StandardOutput/StandardError=journal directives.
docker compose --env-file /opt/trading/deploy/.env exec -T \
  -e DATABASE_URL="$DATABASE_URL" \
  -e CRITICAL_WEBHOOK_URL="$CRITICAL_WEBHOOK_URL" \
  api /opt/venv/bin/python -m scripts.operator_tools.recovery_agent --env "$ENV_TAG"

# Always unset secrets on exit (defense-in-depth; the shell process is
# about to exit anyway but the unset is cheap insurance against a
# future where this wrapper grows additional steps).
unset DATABASE_URL
unset CRITICAL_WEBHOOK_URL

exit 0
