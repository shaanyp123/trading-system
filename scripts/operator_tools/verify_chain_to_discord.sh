#!/usr/bin/env bash
# scripts/operator_tools/verify_chain_to_discord.sh
#
# Daily audit-chain verification ceremony, designed for systemd timer use.
# Runs `verify_chain --env paper` inside the api container, then posts the
# result to a dedicated Discord webhook (the #audit channel).
#
# Independent of api's webhook_pusher service by design — if the audit chain
# is broken AND api is down, this script's curl path still works as long as
# docker + the postgres container are running. Avoids compounded-failure
# silence.
#
# Wired into systemd via:
#   deploy/audit/systemd/verify-chain-daily.{service,timer}
#
# Install ceremony in deploy/audit/README.md.
#
# Exit codes:
#   0 — always (cron-friendly; non-zero only if the script itself is broken)
#       The verify_chain exit code is communicated via the Discord post.
#
# Required state on the VPS:
#   /etc/trading/audit-webhook.url — chmod 600, owned by `trading` user;
#                                    contains the Discord webhook URL (one line, no quotes)
#   /opt/trading/                  — the trading-system repo checkout (cwd for docker compose)
#   docker access for the trading user
#
# Per memory feedback_secret_handling.md:
#   - The webhook URL is treated as a credential (file with chmod 600)
#   - verify_chain output is captured to a temp file, parsed for the count/break line,
#     then shred-deleted. Never echoed.

set -uo pipefail

ENV_TAG="${1:-paper}"
WEBHOOK_FILE="/etc/trading/audit-webhook.url"
TRADING_ROOT="/opt/trading"
TMP_OUTPUT="$(mktemp /tmp/verify-chain.XXXXXX.txt)"

cleanup() {
  if [ -f "$TMP_OUTPUT" ]; then
    shred -u "$TMP_OUTPUT" 2>/dev/null || rm -f "$TMP_OUTPUT"
  fi
}
trap cleanup EXIT

# Sanity check: webhook URL must be readable
if [ ! -r "$WEBHOOK_FILE" ]; then
  # Can't post a Discord alert if we don't have the webhook URL.
  # Log to systemd journal (visible via `journalctl -u verify-chain-daily.service`).
  echo "ERROR: cannot read $WEBHOOK_FILE; skip + abort" >&2
  exit 0
fi
WEBHOOK_URL="$(cat "$WEBHOOK_FILE")"

# Run verify_chain inside the api container. The api container's environment
# carries DATABASE_URL (set via docker-compose env_file -> sops decrypt at
# deploy time). If the api container is down, this fails — captured in EXIT.
cd "$TRADING_ROOT" || {
  curl -fsS -X POST "$WEBHOOK_URL" \
    -H 'Content-Type: application/json' \
    -d "{\"content\":\"verify-chain cron: cannot cd to $TRADING_ROOT — host config drift\"}" \
    >/dev/null 2>&1
  exit 0
}

docker compose exec -T api \
  /opt/venv/bin/python -m services.audit.verify_chain --env "$ENV_TAG" \
  > "$TMP_OUTPUT" 2>&1
EXIT=$?

# Parse the result. verify_chain emits one of:
#   CHAIN OK: <N> rows verified              -> exit 0
#   CHAIN BREAK at sequence_no=<X> (after <K> verified rows)  -> exit 1
#   <usage message>                          -> exit 2
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

case "$EXIT" in
  0)
    # Parse the row count for the success message
    SUMMARY="$(grep -oE 'CHAIN OK: [0-9]+ rows verified' "$TMP_OUTPUT" | head -1)"
    [ -z "$SUMMARY" ] && SUMMARY='CHAIN OK (row count not parsed)'
    PAYLOAD="$(printf '{"content":"OK %s — env=%s, %s"}' "$TIMESTAMP" "$ENV_TAG" "$SUMMARY")"
    ;;
  1)
    # CHAIN BREAK — louder message; include the break line
    BREAK_LINE="$(grep 'CHAIN BREAK' "$TMP_OUTPUT" | head -1 | sed 's/"/\\"/g')"
    [ -z "$BREAK_LINE" ] && BREAK_LINE='(break line not captured)'
    PAYLOAD="$(printf '{"content":"AUDIT CHAIN BREAK %s — env=%s\\n%s\\nRun /verify-chain locally + investigate IMMEDIATELY."}' \
              "$TIMESTAMP" "$ENV_TAG" "$BREAK_LINE")"
    ;;
  2)
    PAYLOAD="$(printf '{"content":"verify-chain cron usage error (exit 2) %s — env=%s. Check systemd journal."}' "$TIMESTAMP" "$ENV_TAG")"
    ;;
  *)
    # Unexpected exit code — likely api container down or docker daemon issue
    PAYLOAD="$(printf '{"content":"verify-chain cron unexpected exit=%s %s — env=%s. api container or docker daemon may be down."}' "$EXIT" "$TIMESTAMP" "$ENV_TAG")"
    ;;
esac

# POST to Discord; --fail-with-body so we get the HTTP status surfaced in journal
curl -fsS -X POST "$WEBHOOK_URL" \
  -H 'Content-Type: application/json' \
  -d "$PAYLOAD" \
  >/dev/null 2>&1 || echo "curl POST to webhook failed at $TIMESTAMP" >&2

exit 0
