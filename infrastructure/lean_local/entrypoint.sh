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
#
# DATA-LAYER PIVOT v2 (Option C; 2026-05-20 evening): the v1 attempt's
# `IB_USER_NAME` / `IB_PASSWORD` / `IB_ACCOUNT` / `QC_USER_ID` /
# `QC_API_TOKEN` sops reads were dropped — LEAN no longer talks to
# IBKR or QC's subscription validator. Bar data is now api-managed
# (services/data/bar_sync.py reads via ib-async on clientId=2 and
# writes to the shared lean_data Docker volume); LEAN reads on-disk
# via FakeDataQueue + SubscriptionDataReaderHistoryProvider. See
# Docs/decisions-log.md 2026-05-20 evening entry + the v2 landing entry.

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

# Deep-merge our template on top of upstream config.json. The upstream
# config at /Lean/Launcher/bin/Debug/config.json is a full framework
# config (handler bindings + limits + paths + 30+ env stubs); our
# /Lean/Algorithm/lean.json is partial. LEAN's Config class loads the
# active config from CWD (Directory.GetCurrentDirectory() + "config.json")
# — NOT BaseDirectory + "config.json". Discovered Pivot-PR-F deploy
# 2026-05-12 via a sentinel-value injection test: an algorithm-location
# value written to /Lean/Launcher/bin/Debug/config.json was IGNORED by
# LEAN, while the same value at /Lean/config.json was picked up. So we
# write the merged result to BOTH locations: /Lean/config.json (the
# location LEAN actually reads) and /Lean/Launcher/bin/Debug/config.json
# (in case a future LEAN version or sub-launcher reads BaseDirectory).
UPSTREAM_CONFIG="${LEAN_UPSTREAM_CONFIG:-/Lean/Launcher/bin/Debug/config.json}"
RUNTIME_CONFIG="${LEAN_RUNTIME_CONFIG:-/Lean/config.json}"
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

python3 - "$UPSTREAM_CONFIG" "$TEMPLATE_PATH" "$RUNTIME_CONFIG" <<'PY_EOF'
"""Deep-merge our lean.json template on top of upstream LEAN config.

The upstream config at /Lean/Launcher/bin/Debug/config.json is JSONC
(with // line comments). We strip those, merge our template's keys
in (skipping $comment-* keys), patch absolute paths, pick the
environment from LEAN_LIVE_MODE, and write the result to BOTH
/Lean/config.json (the CWD-relative path LEAN's Config class actually
reads) AND /Lean/Launcher/bin/Debug/config.json (overwriting the
upstream default so we have a single source of truth for the merged
config; future LEAN versions or sub-launchers that read BaseDirectory
also get our config).
"""

import json
import os
import re
import sys

upstream_path = sys.argv[1]
template_path = sys.argv[2]
runtime_path = sys.argv[3]


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

# Active environment: backtesting by default; paper-internal when
# LEAN_LIVE_MODE=true. (Post-ceremony 2026-05-12 rename: `paper-ibkr` →
# `paper-internal` because the "ibkr" suffix was misleading — LEAN
# itself no longer talks to IBKR; the api owns that contract via
# services/execution/ibkr_adapter.py.)
live_mode = os.environ.get("LEAN_LIVE_MODE", "false").strip().lower() == "true"
merged["environment"] = "paper-internal" if live_mode else "backtesting"

serialized = json.dumps(merged, indent=2) + "\n"
for out_path in (runtime_path, upstream_path):
    with open(out_path, "w") as fh:
        fh.write(serialized)

print(
    "[lean_local_entrypoint] merged config written to "
    + runtime_path
    + " + "
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

# LEAN's Python algorithms `from AlgorithmImports import *` at module
# top. AlgorithmImports.py lives at /Lean/Launcher/bin/Debug/ alongside
# the .NET launcher assemblies. Since our WORKDIR is /Lean (so LEAN's
# Config class reads /Lean/config.json from CWD), Python's default
# sys.path doesn't include the launcher directory. Without this prefix,
# the import fails with "No module named 'AlgorithmImports'".
# /Lean/Algorithm appears second so v1_strategy.py's siblings (none today,
# but room to grow) are resolvable.
# /Lean appears third — exposes the broker-agnostic `strategies`
# package (mounted from repo ./strategies at /Lean/strategies via
# docker-compose.yml) so `from strategies.v1_trend_following.X import Y`
# resolves. Pivot-PR-D 2026-05-12 initial commit added /Lean/Strategies
# directly; that exposed `v1_trend_following` at top level but the
# package's internal absolute imports use the `strategies.*` namespace,
# so we expose the parent path instead.
export PYTHONPATH="/Lean/Launcher/bin/Debug:/Lean/Algorithm:/Lean${PYTHONPATH:+:$PYTHONPATH}"

echo "[lean_local_entrypoint] PYTHONPATH=${PYTHONPATH}"
echo "[lean_local_entrypoint] launching: $*"

# exec replaces our shell so SIGTERM from tini propagates to LEAN.
exec "$@"
