#!/usr/bin/env bash
# .claude/hooks/secret_handling_guard.sh
#
# PreToolUse guard for Bash commands that may emit secret values to stdout
# (sops decrypt, docker compose logs against api/ib_gateway/webhook_pusher,
# psql against credential tables). Per memory feedback_secret_handling.md,
# all such output must be routed to a file with chmod 600 + verified via
# wc -l only — never head/cat/grep displayed.
#
# Behavior: informational warning if a known leak pattern is detected AND
# no obvious file redirect (`> /tmp/` or `> /root/` or `> /var/`) is present.
# Exit 0 (does NOT block).

set -euo pipefail

INPUT="$(cat)"

TOOL="$(printf '%s' "$INPUT" | jq -r '.tool_name // empty')"
[ "$TOOL" != "Bash" ] && exit 0

COMMAND="$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty')"
[ -z "$COMMAND" ] && exit 0

LEAK_PATTERNS=(
  "sops -d"
  "docker compose config"
  "docker compose logs.*api"
  "docker compose logs.*ib_gateway"
  "docker compose logs.*webhook_pusher"
  "docker compose logs.*discord_bot"
  "psql.*setup_tokens"
  "psql.*totp_secrets"
  "psql.*backup_codes"
  "psql.*sessions"
  "psql.*webauthn_credentials"
)

# Skip if command already redirects to a file
if printf '%s' "$COMMAND" | grep -qE '> *(/tmp/|/root/|/var/|/Users/)' ; then
  exit 0
fi

# Skip if command is a count-only operation (wc -l, COUNT(*) SQL)
if printf '%s' "$COMMAND" | grep -qE 'wc -l|COUNT\(\*\)' ; then
  exit 0
fi

for pattern in "${LEAK_PATTERNS[@]}"; do
  if printf '%s' "$COMMAND" | grep -qE "$pattern"; then
    cat >&2 <<EOF

[secret handling guard] Bash command may emit a secret to stdout:
  matched pattern: $pattern

Per memory feedback_secret_handling.md (Day 24 leak retrospective):
  route output to a file with chmod 600, verify via 'wc -l' only,
  never display content via head/cat/grep.

Safe form:
  <command> > /tmp/check && chmod 600 /tmp/check && wc -l /tmp/check && shred -u /tmp/check

(informational only; tool call will proceed)

EOF
    exit 0
  fi
done

exit 0
