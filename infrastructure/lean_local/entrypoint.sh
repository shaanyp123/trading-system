#!/bin/sh
# infrastructure/lean_local/entrypoint.sh — LEAN Local container entrypoint.
#
# Pivot-PR-A (post-pivot 2026-05-12). Reads sops-decrypted secrets bundle
# at /run/secrets/decrypted.yaml and exports the keys LEAN needs as env
# vars before launching LEAN's runtime.
#
# Fail-closed: missing LEAN_LOCAL_BEARER_TOKEN → exit 2 with operator-
# readable error. The bearer is load-bearing for the LEAN → backend POST
# path; without it the algorithm boots but every POST returns 401 + the
# signals never land in audit_log.
#
# Idempotent: env vars already set by docker-compose take precedence
# over sops values. This means the operator can override sops fields
# at the docker-compose layer for local debugging without re-encrypting
# secrets.

set -eu

SECRETS_PATH="${LEAN_SECRETS_PATH:-/run/secrets/decrypted.yaml}"

# Helper: read a sops yaml key via Python (no jq or yq required — Python
# + PyYAML is already in the base image because LEAN uses it).
read_secret() {
    key_path="$1"
    if [ ! -f "$SECRETS_PATH" ]; then
        echo ""
        return 0
    fi
    python3 -c "
import sys
import yaml
with open('$SECRETS_PATH', 'r') as fh:
    data = yaml.safe_load(fh) or {}
keys = '$key_path'.split('.')
val = data
for k in keys:
    val = (val or {}).get(k) if isinstance(val, dict) else None
if val is None:
    val = ''
print(val)
"
}

# Resolve LEAN_LOCAL_BEARER_TOKEN from sops if not already set.
if [ -z "${LEAN_LOCAL_BEARER_TOKEN:-}" ]; then
    LEAN_LOCAL_BEARER_TOKEN="$(read_secret 'lean.api_bearer_token')"
    export LEAN_LOCAL_BEARER_TOKEN
fi

# Fail closed if still empty.
if [ -z "${LEAN_LOCAL_BEARER_TOKEN}" ]; then
    echo "[lean_local_entrypoint] FATAL: lean.api_bearer_token missing or empty in $SECRETS_PATH" >&2
    echo "[lean_local_entrypoint] Fill via: sops secrets/<env>.enc.yaml (add lean.api_bearer_token: <32-byte-base64>)" >&2
    echo "[lean_local_entrypoint] See deploy/lean_local/README.md Step 1." >&2
    exit 2
fi

# Detect obvious placeholder values from the sops template.
case "${LEAN_LOCAL_BEARER_TOKEN}" in
    "<TODO"*|"null"|"")
        echo "[lean_local_entrypoint] FATAL: lean.api_bearer_token still has placeholder value '${LEAN_LOCAL_BEARER_TOKEN}'" >&2
        echo "[lean_local_entrypoint] Replace with a real 32-byte URL-safe base64 token." >&2
        exit 2
        ;;
esac

# Default API base URL points at the api container on the internal
# Docker network. Operator can override at the compose layer.
: "${LEAN_LOCAL_API_BASE_URL:=http://api:8000}"
export LEAN_LOCAL_API_BASE_URL

# Resolve IBKR credentials from sops for live-mode brokerage. Pivot-PR-B
# wires these to LEAN's brokerage config (lean.json). For Pivot-PR-A
# (backtest-mode only) they're optional.
if [ -z "${LEAN_IBKR_USERNAME:-}" ]; then
    LEAN_IBKR_USERNAME="$(read_secret 'ibkr.paper_username')"
    export LEAN_IBKR_USERNAME
fi
if [ -z "${LEAN_IBKR_PASSWORD:-}" ]; then
    LEAN_IBKR_PASSWORD="$(read_secret 'ibkr.paper_password')"
    export LEAN_IBKR_PASSWORD
fi

# Render lean.json from the template by substituting env vars. Done via
# python so we don't need envsubst (not in the LEAN base image by
# default). The rendered config goes to /Lean/lean.json which LEAN
# Launcher picks up at start.
TEMPLATE_PATH="${LEAN_CONFIG_TEMPLATE:-/Lean/Algorithm/lean.json}"
RENDERED_PATH="${LEAN_CONFIG_RENDERED:-/Lean/lean.json}"

if [ -f "$TEMPLATE_PATH" ]; then
    python3 -c "
import os
import re
with open('$TEMPLATE_PATH', 'r') as fh:
    content = fh.read()
def sub(match):
    var_name = match.group(1)
    return os.environ.get(var_name, '')
rendered = re.sub(r'\\\${([A-Z_]+)}', sub, content)
with open('$RENDERED_PATH', 'w') as fh:
    fh.write(rendered)
"
    echo "[lean_local_entrypoint] rendered lean.json from $TEMPLATE_PATH"
fi

echo "[lean_local_entrypoint] api_base=${LEAN_LOCAL_API_BASE_URL} live_mode=${LEAN_LIVE_MODE:-false} env=${ENVIRONMENT:-paper}"
echo "[lean_local_entrypoint] launching: $*"

# exec replaces our shell so SIGTERM from tini propagates to LEAN.
exec "$@"
