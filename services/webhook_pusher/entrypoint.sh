#!/bin/sh
# services/webhook_pusher/entrypoint.sh — webhook_pusher container entrypoint.
#
# Reads the host secrets file at /run/secrets/secrets.yaml and
# exports the keys the SSE subscriber needs as env vars, then exec()s
# python -m services.webhook_pusher (or whatever argv is passed).
#
# Mirrors the api entrypoint pattern (secrets yaml → env via
# stdlib python + pyyaml, then exec). Fail-closed: if a required field
# is missing or placeholder, exit 2 with operator-readable diagnostics
# in docker logs.
#
# Required secrets fields:
#   discord.api_bearer_token        → WEBHOOK_PUSHER_API_BEARER_TOKEN
#   discord.webhook_urls.signals    → WEBHOOK_PUSHER_SIGNALS_WEBHOOK_URL
#   discord.webhook_urls.fills      → WEBHOOK_PUSHER_FILLS_WEBHOOK_URL
#
# Optional:
#   WEBHOOK_PUSHER_API_BASE_URL   (defaults to http://api:8000)

set -eu

SECRETS_PATH="${WEBHOOK_PUSHER_SECRETS_PATH:-/run/secrets/secrets.yaml}"

# Helper: read a secrets yaml key via python (pyyaml is in the runtime
# image). dot-notation supports nested keys (e.g., discord.webhook_urls.signals).
read_secret() {
    key_path="$1"
    if [ ! -f "$SECRETS_PATH" ]; then
        echo ""
        return 0
    fi
    /opt/venv/bin/python -c "
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

_looks_placeholder() {
    case "$1" in
        ""|"null"|"<TODO"*) return 0;;
        *) return 1;;
    esac
}

_require() {
    name="$1"
    key_path="$2"
    # If already exported (e.g., docker-compose env override), keep it.
    current=$(eval "echo \${$name:-}")
    if [ -n "$current" ] && ! _looks_placeholder "$current"; then
        return 0
    fi
    value="$(read_secret "$key_path")"
    if _looks_placeholder "$value"; then
        echo "[webhook_pusher_entrypoint] FATAL: $key_path missing or placeholder in $SECRETS_PATH" >&2
        echo "[webhook_pusher_entrypoint] populate the host secrets file (deploy/secrets.template.yaml)" >&2
        echo "[webhook_pusher_entrypoint] see deploy/webhook_pusher/README.md." >&2
        exit 2
    fi
    eval "export $name=\"$value\""
}

# Install pyyaml if it's not already there (cli.py doesn't need it but
# this entrypoint does). The Dockerfile installs it at build time so
# this is a defensive check.
if ! /opt/venv/bin/python -c "import yaml" >/dev/null 2>&1; then
    echo "[webhook_pusher_entrypoint] FATAL: pyyaml not installed in /opt/venv" >&2
    echo "[webhook_pusher_entrypoint] rebuild the image — pyyaml should be in the Dockerfile RUN block" >&2
    exit 3
fi

# Required fields. Each one fails-closed if missing.
_require WEBHOOK_PUSHER_API_BEARER_TOKEN     "discord.api_bearer_token"
_require WEBHOOK_PUSHER_SIGNALS_WEBHOOK_URL  "discord.webhook_urls.signals"
_require WEBHOOK_PUSHER_FILLS_WEBHOOK_URL    "discord.webhook_urls.fills"

# Optional defaults.
: "${WEBHOOK_PUSHER_API_BASE_URL:=http://api:8000}"
export WEBHOOK_PUSHER_API_BASE_URL

: "${WEBHOOK_PUSHER_LOG_LEVEL:=INFO}"
export WEBHOOK_PUSHER_LOG_LEVEL

echo "[webhook_pusher_entrypoint] api_base=${WEBHOOK_PUSHER_API_BASE_URL} log_level=${WEBHOOK_PUSHER_LOG_LEVEL}"
echo "[webhook_pusher_entrypoint] launching: $*"

# exec replaces our shell so SIGTERM from tini propagates to the python
# process (which has its own signal handler in __main__.py).
exec "$@"
