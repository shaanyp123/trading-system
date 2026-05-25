#!/usr/bin/env bash
# .claude/hooks/risk_path_guard.sh
#
# PreToolUse guard for Edit/Write/MultiEdit on A02-forbidden paths
# (Docs/claude-dev-guide.md §11 [A02]).
#
# Behavior: informational warning to stderr (visible to Claude); exit 0
# (does NOT block the tool call — authoritative gate is the pre-merge
# linter on the PR; this hook is belt-and-suspenders to prevent wasted
# work in-session).
#
# To make this BLOCKING (exit 2) for paranoid mode, change the exit at
# the bottom of the match block.

set -euo pipefail

INPUT="$(cat)"

TOOL="$(printf '%s' "$INPUT" | jq -r '.tool_name // empty')"
case "$TOOL" in
  Edit|Write|MultiEdit) ;;
  *) exit 0 ;;
esac

FILE_PATH="$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty')"
[ -z "$FILE_PATH" ] && exit 0

FORBIDDEN_PATTERNS=(
  "services/risk/"
  "services/signal/"
  "services/audit/"
  "services/execution/"
  "services/reconciliation/"
  "services/calibration/"
  "services/agent/decisions/"
  "services/agent/risk_actions/"
  "services/agent/parameter_changes/"
  "services/agent/prompts/decision/"
  "alembic/"
)

for pattern in "${FORBIDDEN_PATTERNS[@]}"; do
  if printf '%s' "$FILE_PATH" | grep -q "$pattern"; then
    cat >&2 <<EOF

[risk-path guard] $FILE_PATH matches A02 forbidden-without-label pattern: $pattern

This change requires the 'risk-review-approved' PR label or the pre-merge
linter will block. See Docs/claude-dev-guide.md §11 [A02] for the canonical
whitelist and memory entry feedback_risk_paths_need_label.md for context.

(informational only; tool call will proceed)

EOF
    exit 0
  fi
done

exit 0
