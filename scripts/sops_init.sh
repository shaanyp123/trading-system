#!/usr/bin/env bash
# scripts/sops_init.sh
#
# Substitutes generated age public keys into .sops.yaml. Run once, after
# `age-keygen` has produced the three env keys (Day 2 15:00 task per
# implementation-guide.md §11).
#
# Usage:
#   bash scripts/sops_init.sh <DEV_PUBKEY> <PAPER_PUBKEY> <LIVE_PUBKEY>
#
# Each pubkey is the `age1...` string from the `# public key:` line of the
# corresponding key file (~/dev-key.txt etc.) — ~62 characters. Convenience:
#
#   DEV=$(awk '/^# public key:/ {print $4}' ~/dev-key.txt)
#   PAPER=$(awk '/^# public key:/ {print $4}' ~/paper-key.txt)
#   LIVE=$(awk '/^# public key:/ {print $4}' ~/live-key.txt)
#   bash scripts/sops_init.sh "$DEV" "$PAPER" "$LIVE"
#
# Produces a ready-to-use .sops.yaml. Verify with:
#   git diff .sops.yaml      # placeholders -> real pubkeys
#
# Idempotency: safe to re-run with the same pubkeys (no-op). Re-running with
# different pubkeys overwrites — appropriate during annual rotation
# (backend-spec §8.1.2).

set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 <DEV_PUBKEY> <PAPER_PUBKEY> <LIVE_PUBKEY>" >&2
  echo "  each pubkey is the age1... string from the corresponding ~/<env>-key.txt" >&2
  exit 1
fi

DEV_PK=$1
PAPER_PK=$2
LIVE_PK=$3

# Sanity check: each pubkey starts with "age1" and is the right length-ish
# (62 chars typical; tolerate 50-100 to allow for future format changes).
for pair in "DEV:$DEV_PK" "PAPER:$PAPER_PK" "LIVE:$LIVE_PK"; do
  name=${pair%%:*}
  pk=${pair#*:}
  if [[ ! "$pk" =~ ^age1[a-z0-9]{40,100}$ ]]; then
    echo "error: $name pubkey doesn't look like an age recipient: $pk" >&2
    echo "  expected: starts with 'age1', then 40+ lowercase alphanumeric chars" >&2
    exit 2
  fi
done

REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"

# Cross-platform sed in-place: BSD sed (macOS) requires a backup-suffix arg;
# GNU sed (Linux) accepts `-i` alone. We use a tempfile pattern that works on both.
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT

sed \
  -e "s|<DEV_AGE_PUBKEY_PLACEHOLDER>|$DEV_PK|" \
  -e "s|<PAPER_AGE_PUBKEY_PLACEHOLDER>|$PAPER_PK|" \
  -e "s|<LIVE_AGE_PUBKEY_PLACEHOLDER>|$LIVE_PK|" \
  .sops.yaml > "$tmp"
mv "$tmp" .sops.yaml

# Verify substitution: no placeholders should remain.
if grep -q "_AGE_PUBKEY_PLACEHOLDER" .sops.yaml; then
  echo "error: placeholders remain in .sops.yaml after substitution" >&2
  grep "_AGE_PUBKEY_PLACEHOLDER" .sops.yaml >&2
  exit 3
fi

echo "ok: .sops.yaml populated with three env recipients."
echo "diff:"
git --no-pager diff -- .sops.yaml || true
