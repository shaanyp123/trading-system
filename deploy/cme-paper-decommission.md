# CME paper system decommission — operator runbook (Phase C0 start)

**Authority:** `Docs/crypto-pivot-delta-spec.md` §1 "Decommission timing" +
operator directive 2026-07-08 ("CME paper system shutdown approved — execute
at C0 start"). This is the one-time VPS-side ceremony that retires the
IBKR/LEAN/CME paper stack. The repo-side deletions land in the C0-D1 PR;
this runbook is the physical-world half.

Run top to bottom in one sitting (~20 minutes). Every step is copy-paste.
If any step errors, STOP and paste the output into a Claude session — do
not improvise.

---

## Step 0 — Preconditions

* The C0-D1 decommission PR is merged to `main`.
* You are SSH'd into the VPS: `ssh <your-vps-alias>` and in `/opt/trading`.

```bash
cd /opt/trading
```

## Step 1 — Cancel any open paper orders

Open the IBKR portal → switch to the **Paper Account** → Orders. Cancel
every open/working order (protective stops included — the positions they
guard are being closed out of scope; the account is being retired, not
managed). If the portal shows zero working orders, this step is done.

Belt-and-braces check from the VPS (lists working orders via the api's DB
view of orders in non-terminal states):

```bash
docker compose exec postgres psql -U trading -d trading -c \
  "SELECT id, market, side, order_type, status FROM orders WHERE status IN ('pending_submit','submitted','accepted','partially_filled');"
```

Any rows here should correspond to orders you just cancelled (status
flips arrive via the recon cycle in Step 2; stale rows are fine).

## Step 2 — One last EOD reconciliation (archives final account state)

```bash
docker compose exec api python -m services.reconciliation.eod_cycle --manual
```

Expected: the run completes with `recon_break_found=false` (or documents
final breaks). This is the archived terminal snapshot of the paper account.
If it errors because the ib_gateway is already down, restart the gateway
once (`docker compose up -d ib_gateway`), re-run, then continue.

## Step 3 — Emit the `strategy_retired` audit event

Uses the EXISTING `system_stopped` audit enum with a free-text payload (no
new enum per delta spec §1 / anti-pattern [A04]):

```bash
docker compose exec api python - <<'PY'
import asyncio

from sqlalchemy import text

from services.api.config import get_settings
from services.api.db import close_pool, init_pool, session_scope
from services.audit.event_types import AuditEventType
from services.audit.writer import append_audit_event


async def main() -> None:
    settings = get_settings()
    await init_pool(settings)
    try:
        async with session_scope() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT id FROM accounts WHERE active_to IS NULL "
                        "ORDER BY active_from DESC LIMIT 1"
                    )
                )
            ).first()
            assert row is not None, "no active accounts row"
            account_id = row.id
        async with session_scope() as session:
            record = await append_audit_event(
                session,
                AuditEventType.SYSTEM_STOPPED,
                {
                    "reason": "strategy_retired",
                    "detail": (
                        "CME micro-futures paper system retired at crypto-pivot "
                        "Phase C0 start per Docs/crypto-pivot-delta-spec.md §1 "
                        "(operator directive 2026-07-08). Final state archived "
                        "via manual EOD recon immediately prior."
                    ),
                },
                account_id=account_id,
                env=settings.environment,
                phase_at_emit=1,
            )
            print("audit event:", record.event_uuid)
    finally:
        await close_pool()


asyncio.run(main())
PY
```

Expected: prints `audit event: <uuid>`. (If the `append_audit_event`
signature complains, STOP and paste the error into a Claude session —
do not hand-edit the call.)

## Step 4 — Stop + remove the retired systemd timers

```bash
sudo systemctl disable --now lean-universe-synthesis.timer lean-local-daily-restart.timer
sudo rm -f /etc/systemd/system/lean-universe-synthesis.{timer,service} \
           /etc/systemd/system/lean-local-daily-restart.{timer,service}
sudo systemctl daemon-reload
systemctl list-timers | grep -i lean || echo "OK: no lean timers remain"
```

## Step 5 — Pull the merged decommission commit + restart the stack

```bash
git pull origin main
docker compose up -d --remove-orphans
```

`--remove-orphans` removes the now-undefined `lean_local`, `ib_gateway`,
`autoheal` (and, if present, `qc_adapter`) containers. Verify:

```bash
docker compose ps
docker ps -a | grep -Ei "lean|gateway|autoheal|qc" || echo "OK: retired containers gone"
```

Expected `docker compose ps` set: caddy, api, postgres, nextjs,
discord_bot, webhook_pusher (+ gitea if you run it).

## Step 6 — Remove the LEAN data volume (optional archive first)

The bars are re-downloadable market data (nothing account-specific), so
archiving is optional. To archive first:

```bash
docker run --rm -v trading_lean_data:/data -v /opt/trading/archive:/backup alpine \
  tar czf /backup/lean_data_final_$(date +%Y%m%d).tar.gz -C /data .
```

Then remove:

```bash
docker volume rm trading_lean_data
```

(Postgres data is NOT touched — the audit chain and full trade history
stay.)

## Step 7 — Confirm the api is healthy post-decommission

```bash
curl -fsS http://localhost:8000/api/health && echo OK
docker compose logs api --since 5m | grep -E "api_ready|error" | tail -20
```

Expected: `api_ready` present; no bar_sync/LEAN startup lines (those code
paths no longer exist). The order-placement worker + recon scheduler will
log connection warnings against the now-absent gateway until their
Coinbase replacements land (delta spec §3.1/§3.5) — expected and
harmless during the C0 build window.

## Step 8 — Operator prerequisite for the C0 build (do this week)

* **Subscribe to Coinbase One Basic ($4.99/mo)** — required for the
  Amendment B cash-yield layer (USDC rewards are subscriber-exclusive
  since 2025-12-15; decision 2026-07-08, delta spec §3.6).
* Keep IBKR paper credentials until the 6-month review, then close per
  your preference — nothing in the system reads them after today.

---

*After this runbook completes, the CME paper system is fully retired.
The crypto build proceeds per delta spec §5 (C0 build order); small-live
begins when the C0 offline gates pass (no shadow phase, operator
directive 2026-07-08).*
