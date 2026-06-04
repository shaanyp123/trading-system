#!/usr/bin/env bash
# scripts/operator_tools/notify_unit_failure.sh
#
# `OnFailure=` handler for host systemd units. Posts a P1 alert to the
# Discord #alerts channel naming the failed unit + a triage hint, so a
# silently-failing host unit can no longer go unnoticed.
#
# Wired via the template unit
#   deploy/audit/systemd/notify-unit-failure@.service
# which each monitored unit references with
#   OnFailure=notify-unit-failure@%n.service
# systemd then starts notify-unit-failure@<failed-unit>.service, whose
# ExecStart passes the failed unit name (%i) as $1 to this script.
#
# Closes the monitoring gap surfaced by PR #321: lean-universe-synthesis.service
# exited FAILURE every night for >=2 days unnoticed because nothing alerts on
# systemd unit failure. Generalises to all host units (lean-universe-synthesis,
# lean-local-daily-restart, recovery-agent-poll, verify-chain-daily).
#
# Design — mirrors scripts/operator_tools/verify_chain_to_discord.sh:
#   * Reads the webhook URL from a chmod-600 file (treated as a credential
#     per feedback_secret_handling.md); never echoes it.
#   * Pure curl POST — NO docker / postgres / api / sops dependency, so it
#     fires even when the failure IS an infrastructure outage (the whole
#     point; routing via the alerts table would need postgres+api up exactly
#     when they may be the thing that's down).
#   * Always exits 0. An OnFailure handler that itself failed would be doubly
#     silent; per-run problems are surfaced to the systemd journal instead
#     (journalctl -u notify-unit-failure@<unit>.service).
#   * Per-unit cooldown so a fast-cadence timer (recovery-agent-poll fires
#     every 60s) entering a failure loop cannot storm #alerts.
#
# Required state on the VPS:
#   /etc/trading/alerts-webhook.url — chmod 600, owned by `trading` user;
#                                     contains the Discord #alerts webhook URL
#                                     (one line, no quotes). Install ceremony in
#                                     deploy/audit/README.md.
#
# Args:
#   $1 — the failed unit name (systemd %i, e.g. lean-universe-synthesis.service)
#
# Env (optional):
#   NOTIFY_COOLDOWN_SECONDS — per-unit cooldown window (default 3600 = 1h)
#   NOTIFY_WEBHOOK_FILE     — override the webhook-file path (default
#                             /etc/trading/alerts-webhook.url); for local/CI
#                             testing + operator dry-runs against a test webhook
#   NOTIFY_STATE_DIR        — override the cooldown state dir (default
#                             /run/trading-unit-notify); for the same
#
# Exit codes:
#   0 — always (cron/OnFailure-friendly; outcomes flow through Discord + journal)

set -uo pipefail

RAW_UNIT="${1:-unknown.unit}"
WEBHOOK_FILE="${NOTIFY_WEBHOOK_FILE:-/etc/trading/alerts-webhook.url}"
COOLDOWN_SECONDS="${NOTIFY_COOLDOWN_SECONDS:-3600}"
STATE_DIR="${NOTIFY_STATE_DIR:-/run/trading-unit-notify}"

# Sanitise the unit name to a known-safe charset before it lands in a JSON
# body, a shell command hint, and a state-file path. systemd unit names are
# already restricted, but %i can carry escaped sequences — defence in depth.
UNIT="$(printf '%s' "$RAW_UNIT" | tr -cd 'A-Za-z0-9._@:-')"
[ -z "$UNIT" ] && UNIT="unknown.unit"

NOW="$(date -u +%s)"
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
HOSTNAME_SHORT="$(hostname -s 2>/dev/null || echo host)"

# --- Per-unit cooldown ------------------------------------------------------
# Suppress a repeat post for the same unit within the cooldown window. State
# lives in /run (tmpfs) so a reboot legitimately re-arms alerting. Best-effort:
# any read/write failure here must never block the post.
mkdir -p "$STATE_DIR" 2>/dev/null || true
STATE_FILE="$STATE_DIR/${UNIT}.last"
if [ -r "$STATE_FILE" ]; then
  LAST="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
  case "$LAST" in
    ''|*[!0-9]*) LAST=0 ;;
  esac
  if [ "$LAST" -gt 0 ] && [ "$((NOW - LAST))" -lt "$COOLDOWN_SECONDS" ]; then
    echo "notify_unit_failure: within cooldown for unit=$UNIT ($((NOW - LAST))s < ${COOLDOWN_SECONDS}s); suppressing duplicate post" >&2
    exit 0
  fi
fi

# --- Webhook URL ------------------------------------------------------------
if [ ! -r "$WEBHOOK_FILE" ]; then
  # The one failure we cannot escalate to Discord (no URL to post to).
  # Surface to the journal; the operator sees it via journalctl on the
  # notify-unit-failure@ instance.
  echo "notify_unit_failure: cannot read $WEBHOOK_FILE — unit=$UNIT failed but NO alert posted (run the install ceremony in deploy/audit/README.md)" >&2
  exit 0
fi
WEBHOOK_URL="$(cat "$WEBHOOK_FILE")"

# --- Post -------------------------------------------------------------------
# P1 host-unit failure. Single printf builds the JSON; \\n becomes a literal
# \n escape inside the JSON string (same idiom as verify_chain_to_discord.sh).
PAYLOAD="$(printf '{"content":"P1 HOST UNIT FAILURE %s — host=%s\\nunit=%s entered the failed state.\\nTriage: journalctl -u %s -n 50 --no-pager"}' \
          "$TIMESTAMP" "$HOSTNAME_SHORT" "$UNIT" "$UNIT")"

if curl -fsS -X POST "$WEBHOOK_URL" \
     -H 'Content-Type: application/json' \
     -d "$PAYLOAD" >/dev/null 2>&1; then
  # Record the post time for the cooldown only on a successful post, so a
  # transient webhook outage doesn't start a cooldown that swallows the
  # next real failure.
  echo "$NOW" > "$STATE_FILE" 2>/dev/null || true
  echo "notify_unit_failure: posted P1 #alerts for unit=$UNIT at $TIMESTAMP" >&2
else
  echo "notify_unit_failure: curl POST to #alerts webhook FAILED for unit=$UNIT at $TIMESTAMP" >&2
fi

exit 0
