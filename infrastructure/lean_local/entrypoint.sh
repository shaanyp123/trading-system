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
#
# Pivot-PR-F (2026-05-12): config rendering was rewritten. LEAN's
# Launcher.dll reads its config from /Lean/Launcher/bin/Debug/config.json
# (a full framework config with handler bindings, limits, paths) — NOT
# our partial /Lean/Algorithm/lean.json. Pre-PR-F the entrypoint wrote
# our template to /Lean/lean.json where LEAN never looked, so LEAN fell
# through to the upstream BasicTemplateFrameworkAlgorithm default and
# crash-looped. PR-F deep-merges our template on top of the upstream
# config and writes the result back to its canonical location, with
# absolute paths for algorithm-location + data-folder and the active
# environment selected from LEAN_LIVE_MODE.

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
# wires these to LEAN's brokerage config (lean.json). For backtest mode
# they're optional.
if [ -z "${LEAN_IBKR_USERNAME:-}" ]; then
    LEAN_IBKR_USERNAME="$(read_secret 'ibkr.paper_username')"
    export LEAN_IBKR_USERNAME
fi
if [ -z "${LEAN_IBKR_PASSWORD:-}" ]; then
    LEAN_IBKR_PASSWORD="$(read_secret 'ibkr.paper_password')"
    export LEAN_IBKR_PASSWORD
fi
# Account ID lives in sops too. Pivot-PR-A originally let it default
# to empty string for backtest; in paper/live mode IBKR rejects the
# connect handshake unless it's populated.
if [ -z "${LEAN_IBKR_ACCOUNT:-}" ]; then
    LEAN_IBKR_ACCOUNT="$(read_secret 'ibkr.paper_account')"
    export LEAN_IBKR_ACCOUNT
fi

# Deep-merge our template on top of upstream config.json. LEAN reads
# from /Lean/Launcher/bin/Debug/config.json (the upstream framework
# config with handler bindings + limits + paths) and ignores our
# partial /Lean/Algorithm/lean.json. We solve this by reading both,
# deep-merging (template overrides defaults), patching absolute paths,
# selecting the active environment from LEAN_LIVE_MODE, and writing
# the result back to its canonical location.
UPSTREAM_CONFIG="${LEAN_UPSTREAM_CONFIG:-/Lean/Launcher/bin/Debug/config.json}"
TEMPLATE_PATH="${LEAN_CONFIG_TEMPLATE:-/Lean/Algorithm/lean.json}"

if [ ! -f "$UPSTREAM_CONFIG" ]; then
    echo "[lean_local_entrypoint] FATAL: upstream LEAN config not found at $UPSTREAM_CONFIG" >&2
    echo "[lean_local_entrypoint] The image may have shifted launcher paths; check the Dockerfile CMD." >&2
    exit 3
fi
if [ ! -f "$TEMPLATE_PATH" ]; then
    echo "[lean_local_entrypoint] FATAL: lean.json template not found at $TEMPLATE_PATH" >&2
    echo "[lean_local_entrypoint] Verify the ./lean:/Lean/Algorithm volume mount in docker-compose.yml." >&2
    exit 3
fi

python3 - "$UPSTREAM_CONFIG" "$TEMPLATE_PATH" <<'PY_EOF'
"""Deep-merge our lean.json template on top of upstream LEAN config.

The upstream config at /Lean/Launcher/bin/Debug/config.json is JSONC
(with // line comments). We strip those, merge our template's keys
in (skipping $comment-* keys), patch absolute paths, pick the
environment from LEAN_LIVE_MODE, and write the result back to the
upstream location so LEAN's launcher picks it up.
"""

import json
import os
import re
import sys

upstream_path = sys.argv[1]
template_path = sys.argv[2]


def strip_jsonc_line_comments(s: str) -> str:
    """Strip // line comments from JSONC content, preserving // inside string literals."""
    out = []
    in_string = False
    escape_next = False
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if escape_next:
            out.append(c)
            escape_next = False
            i += 1
            continue
        if in_string and c == "\\":
            out.append(c)
            escape_next = True
            i += 1
            continue
        if c == '"':
            in_string = not in_string
            out.append(c)
            i += 1
            continue
        if not in_string and c == "/" and i + 1 < n and s[i + 1] == "/":
            # Skip until newline (preserve the newline so json parser keeps line numbers stable).
            while i < n and s[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def expand_env_vars(s: str) -> str:
    """Substitute ${VAR_NAME} → os.environ[VAR_NAME] (empty string if unset)."""
    return re.sub(r"\$\{([A-Z_][A-Z0-9_]*)\}", lambda m: os.environ.get(m.group(1), ""), s)


def deep_merge(base, overlay):
    """Merge overlay into base. Overlay wins for conflicts.

    Lists are NOT merged element-wise — overlay's list replaces base's list (matches
    LEAN handler-binding semantics where data-queue-handler etc. are unambiguous
    full replacements, not appends).
    """
    if not isinstance(base, dict) or not isinstance(overlay, dict):
        return overlay
    result = dict(base)
    for k, v in overlay.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def strip_comment_keys(obj):
    """Recursively drop any dict key whose name starts with '$comment' (e.g., $comment,
    $comment-1, $comment-algorithm-location). Applied to the final merged result so
    LEAN never reads our documentation hints."""
    if isinstance(obj, dict):
        return {
            k: strip_comment_keys(v)
            for k, v in obj.items()
            if not (isinstance(k, str) and k.startswith("$comment"))
        }
    if isinstance(obj, list):
        return [strip_comment_keys(x) for x in obj]
    return obj


with open(upstream_path, "r") as fh:
    upstream_raw = fh.read()
upstream = json.loads(strip_jsonc_line_comments(upstream_raw))

with open(template_path, "r") as fh:
    template_raw = fh.read()
template = json.loads(expand_env_vars(template_raw))

merged = strip_comment_keys(deep_merge(upstream, template))

# Absolute paths so LEAN can find them regardless of working directory.
merged["algorithm-location"] = "/Lean/Algorithm/v1_strategy.py"
merged["data-folder"] = "/Lean/Data/"

# Active environment: backtesting by default; paper-ibkr when LEAN_LIVE_MODE=true.
live_mode = os.environ.get("LEAN_LIVE_MODE", "false").strip().lower() == "true"
merged["environment"] = "paper-ibkr" if live_mode else "backtesting"

with open(upstream_path, "w") as fh:
    json.dump(merged, fh, indent=2)
    fh.write("\n")

print(
    "[lean_local_entrypoint] merged config written to "
    + upstream_path
    + " (environment="
    + merged["environment"]
    + ", algorithm-type-name="
    + str(merged.get("algorithm-type-name", "?"))
    + ")",
    file=sys.stderr,
)
PY_EOF

echo "[lean_local_entrypoint] api_base=${LEAN_LOCAL_API_BASE_URL} live_mode=${LEAN_LIVE_MODE:-false} env=${ENVIRONMENT:-paper}"
echo "[lean_local_entrypoint] launching: $*"

# exec replaces our shell so SIGTERM from tini propagates to LEAN.
exec "$@"
