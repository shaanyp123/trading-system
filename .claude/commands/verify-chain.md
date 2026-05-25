---
description: Generate the audit-chain verification ceremony for a specific environment
argument-hint: <env: dev|paper|live-small|live-scale>
allowed-tools: [Bash, Read]
---

# Verify audit chain

Generates the exact operator-side SSH ceremony for running `services/audit/verify_chain.py` against the specified environment. The verification itself runs INSIDE the api container ON THE VPS — Claude Code locally cannot execute it, but this command outputs the canonical commands per `deploy/audit/README.md` so the operator can copy-paste into their SSH session.

## Steps

1. **Validate env arg.** Must be one of: `dev`, `paper`, `live-small`, `live-scale`. If missing or invalid: output usage error referencing the canonical envs from dev-guide §1.5.

2. **Read `deploy/audit/README.md`** for the latest ceremony version. If the README contradicts this command's output, the README wins — surface the discrepancy and recommend updating one or the other.

3. **Output the ceremony** in a copy-paste-ready block:

```bash
# Step 1: Decrypt sops + extract app_service password
export SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt"
APP_SERVICE_PW=$(sops -d secrets/<env>.enc.yaml | yq '.postgres.app_service_password' -r)

# Step 2: Stage DATABASE_URL (subshell-local; never displayed)
export DATABASE_URL="postgresql://app_service:${APP_SERVICE_PW}@postgres:5432/trading"
echo "DATABASE_URL host: postgres, role: app_service"  # sanity print — never the password

# Step 3: Run verify_chain inside api container
docker compose exec -T -e DATABASE_URL="$DATABASE_URL" api \
  /opt/venv/bin/python -m services.audit.verify_chain --env <env>

# Expected outputs:
#   CHAIN OK: <N> rows verified           (exit 0 — chain intact)
#   CHAIN BREAK at sequence_no=<X> ...    (exit 1 — INCIDENT — escalate to operator)
#   Usage error                            (exit 2 — bad invocation)

# Step 4 (optional): psql cross-check
PSQL_PW="$APP_SERVICE_PW" docker compose exec -T \
  -e PGPASSWORD="$APP_SERVICE_PW" \
  postgres psql -U app_service -d trading \
  -c "SELECT COUNT(*), COALESCE(MAX(sequence_no), 0) FROM audit_log WHERE env = '<env>';"

# Step 5: Cleanup
unset APP_SERVICE_PW DATABASE_URL PSQL_PW
```

4. **Substitute `<env>`** with the actual env arg in all 5 occurrences.

5. **Anti-pattern reminders:**
   - Per memory `feedback_secret_handling.md`: NEVER display the sops decrypt output. NEVER `echo $APP_SERVICE_PW`. NEVER `cat /tmp/decrypted`.
   - Per memory `feedback_no_destructive_shortcuts.md`: if `verify_chain` exits 1 (CHAIN BREAK), DO NOT try to "fix" the chain — escalate immediately. Audit-chain breaks are incident-level events per `Docs/backend-spec.md` §2.10.

6. **Output expected timing:** for a clean chain at current row count (~64 rows as of 2026-05-18 per file-index.md), verify_chain completes in <2s. For 100K+ rows, expect ~30s.

## Phase 1+ extensions

- Add a `--from-sequence` / `--to-sequence` arg pass-through once the operator has reason to verify a sub-range
- Optionally wire to a scheduled-task cron that runs verify_chain daily at 02:00 ET and POSTs result to `#audit` — see Workstream #8 in `Docs/claude-setup-overhaul.md`

## Cross-refs

- Canonical runbook: `deploy/audit/README.md`
- CLI module: `services/audit/verify_chain.py`
- Walker: `services/audit/chain.py::verify_chain`
- Memory: `reference_verification_ceremonies.md`
