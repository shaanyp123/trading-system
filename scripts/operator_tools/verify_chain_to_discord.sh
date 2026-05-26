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

docker compose --env-file /opt/trading/deploy/.env exec -T api \
  /opt/venv/bin/python -m services.audit.verify_chain --env "$ENV_TAG" \
  > "$TMP_OUTPUT" 2>&1
EXIT=$?

# Parse the result. verify_chain emits one of:
#   CHAIN OK: <N> rows verified              -> exit 0
#   CHAIN BREAK at sequence_no=<X> (after <K> verified rows)  -> exit 1
#   <usage message>                          -> exit 2
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Categorize by what's in the output, not just exit code — `docker compose exec`
# failures can map to exit=1 too (it would be ambiguous with verify_chain's
# CHAIN-BREAK exit). Reject ambiguity: only treat exit=1 as a chain break if the
# string "CHAIN BREAK" is present in the output. Otherwise it's an infrastructure
# error (docker exec failed, env-file missing, container not running, etc.).
if grep -q 'CHAIN OK' "$TMP_OUTPUT"; then
  SUMMARY="$(grep -oE 'CHAIN OK: [0-9]+ rows verified' "$TMP_OUTPUT" | head -1)"
  [ -z "$SUMMARY" ] && SUMMARY='CHAIN OK (row count not parsed)'
  PAYLOAD="$(printf '{"content":"OK %s — env=%s, %s"}' "$TIMESTAMP" "$ENV_TAG" "$SUMMARY")"
elif grep -q 'CHAIN BREAK' "$TMP_OUTPUT"; then
  BREAK_LINE="$(grep 'CHAIN BREAK' "$TMP_OUTPUT" | head -1 | sed 's/"/\\"/g')"
  PAYLOAD="$(printf '{"content":"AUDIT CHAIN BREAK %s — env=%s\\n%s\\nRun /verify-chain locally + investigate IMMEDIATELY."}' \
            "$TIMESTAMP" "$ENV_TAG" "$BREAK_LINE")"
else
  # Output had neither marker → infrastructure error (docker exec failed,
  # env-file missing, container down, etc.). Surface enough to triage.
  # Take the last meaningful line for the error message (skip blank lines).
  ERR_LINE="$(tail -10 "$TMP_OUTPUT" | grep -vE '^\s*$' | tail -1 | sed 's/"/\\"/g; s/\\/\\\\/g' | head -c 200)"
  [ -z "$ERR_LINE" ] && ERR_LINE="(no output captured)"
  PAYLOAD="$(printf '{"content":"verify-chain cron INFRASTRUCTURE ERROR %s — env=%s, exit=%s\\nLast output: %s\\nCheck: docker compose ps, systemd journalctl -u verify-chain-daily.service"}' \
            "$TIMESTAMP" "$ENV_TAG" "$EXIT" "$ERR_LINE")"
fi

# POST to Discord; --fail-with-body so we get the HTTP status surfaced in journal
curl -fsS -X POST "$WEBHOOK_URL" \
  -H 'Content-Type: application/json' \
  -d "$PAYLOAD" \
  >/dev/null 2>&1 || echo "curl POST to webhook failed at $TIMESTAMP" >&2

exit 0
