# `deploy/ibkr/` — IBKR Gateway operator runbook

Pivot-PR-B (post-pivot 2026-05-12). A27 satisfier for the IBKR platform
contract per dev-guide §6.8 alternative (b).

**This runbook is a precondition for merging Pivot-PR-B.** The
canonical "most-restricted endpoint smoke" for IBKR is `placeOrder` +
`cancelOrder` round-trip on the paper account (DP-025 lesson — smoke
the operations the integration actually depends on, not just auth). If
Step 5 below fails, the PR must not merge; if it passes, the IBKR
integration is operationally proven for the operator's account.

---

## Prereqs

* IBKR Pro account approved (operator's account `U25655583` cleared
  approval 2026-05-10 per DP-001 close-out).
* Futures-trading approval cleared (or paper-trading sim available
  pre-approval — IBKR paper accounts grant simulated full-feature
  access regardless of live futures status; the operator confirms in
  Step 0 below).
* Paper-account credentials available (the IBKR portal under "Paper
  Trading" → "Get Credentials" gives a separate username + password).
* sops decryption working on the VPS (`/etc/credstore.encrypted/age_key`).
* Stage 0 PR merged + Pivot-PR-A merged (the LEAN Local container is
  the canonical caller of this gateway).

---

## Step 0 — Verify futures-sim access on paper account

This is the precondition the brief flagged. If futures trading is not
available on the paper account, Pivot-PR-B must fall back to Option 2
(QC algorithm direct POST) until futures approval clears.

1. Log into the [IBKR Portal](https://www.interactivebrokers.com/).
2. Switch to the **Paper Account** (top-right user menu).
3. Navigate to **Trade → All Products** + search "MES" (Micro E-mini S&P 500).
4. Confirm the contract is tradable (shows bid/ask + "Buy" / "Sell"
   buttons). If trading is BLOCKED with a "permission required" message,
   futures-sim is NOT available — escalate to operator + halt Pivot-PR-B.

Expected: futures should be tradable on paper regardless of live
futures-approval status. Confirmed once the operator sees `/MES`
quotes + the buy/sell buttons are enabled.

---

## Step 1 — Generate VNC password + add paper credentials to sops

The `gnzsnz/ib-gateway` image runs IB Gateway inside a VNC session
(headless X11). The VNC password is set via env var so the operator can
debug a hung gateway by VNC-ing in. Generate one:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(16))'
```

Edit the paper-env sops bundle:

```bash
sops secrets/paper.enc.yaml
```

Fill in (replacing the `<TODO_FROM_PIVOT_PR_B_BRINGUP>` placeholders):

```yaml
ibkr:
  paper_username: <your-paper-username>     # from IBKR portal Paper Trading
  paper_password: <your-paper-password>     # from IBKR portal Paper Trading
  client_id: 1                              # leave default
```

Save + commit:

```bash
git add secrets/paper.enc.yaml
git commit -m "ops: paper env IBKR paper-trading credentials (Pivot-PR-B)"
git push
```

---

## Step 2 — Re-decrypt sops on VPS + set ib_gateway env vars

On the VPS:

```bash
ssh trading@<vps-host>
cd /opt/trading
git pull --ff-only
sops -d secrets/paper.enc.yaml > /opt/trading/secrets-decrypted/decrypted.yaml.tmp \
  && mv /opt/trading/secrets-decrypted/decrypted.yaml.tmp /opt/trading/secrets-decrypted/decrypted.yaml \
  && chmod 600 /opt/trading/secrets-decrypted/decrypted.yaml
```

The `ib_gateway` container reads `TWS_USERID` + `TWS_PASSWORD` from
docker-compose env directly (not via the sops bundle volume — IBC needs
them as env at startup). Export to `deploy/.env`:

```bash
echo "IB_GATEWAY_USERNAME=$(yq -r .ibkr.paper_username /opt/trading/secrets-decrypted/decrypted.yaml)" >> deploy/.env
echo "IB_GATEWAY_PASSWORD=$(yq -r .ibkr.paper_password /opt/trading/secrets-decrypted/decrypted.yaml)" >> deploy/.env
echo "IB_GATEWAY_MODE=paper" >> deploy/.env
echo "IB_GATEWAY_VNC_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(16))')" >> deploy/.env
chmod 600 deploy/.env
```

**Security note:** `deploy/.env` is NOT committed to git (excluded via
`.gitignore`). The IBKR credentials sit in plaintext on disk at
`/opt/trading/deploy/.env` and `/opt/trading/secrets-decrypted/decrypted.yaml`.
Both are 600-mode + owned by the `trading` user; only root + trading
can read.

---

## Step 3 — Pull + start the ib_gateway container

```bash
docker compose --env-file deploy/.env pull ib_gateway
docker compose --env-file deploy/.env up -d ib_gateway
```

Initial IBC login takes ~90 seconds (the healthcheck `start_period` is
120s for this reason). Watch logs:

```bash
docker compose --env-file deploy/.env logs ib_gateway 2>&1 | tail -100
```

Expected sequence (from `gnzsnz/ib-gateway`'s entrypoint + IBC):

```
[gateway] Starting IBC v3.x.x
[gateway] Starting TWS Gateway
[gateway] Login to TWS Gateway succeeded
[gateway] Server listening on port 4002
```

If you see `Login to TWS Gateway failed` or 2FA prompts, the operator
must complete 2FA via the IBKR mobile app — the gnzsnz image supports
YubiKey + IBKR Mobile push-notification 2FA. Check `TWOFA_DEVICE` env
matches the operator's actual 2FA mechanism.

Verify the healthcheck:

```bash
docker compose --env-file deploy/.env ps ib_gateway
```

Status should be `Up (healthy)` within 2-3 min of container start.

---

## Step 4 — Verify port 4002 is reachable from inside the network

From inside the `internal` Docker network (using a one-off `curl` /
`nc` container):

```bash
docker compose --env-file deploy/.env exec api nc -zv ib_gateway 4002
```

Expected: `Connection to ib_gateway 4002 port [tcp/*] succeeded!`

If this fails, the gateway didn't actually start its listener; check
`docker compose logs ib_gateway` for IBC errors.

---

## Step 5 — placeOrder + cancelOrder round-trip smoke (the A27 satisfier)

This is the canonical Pivot-PR-B "most-restricted endpoint smoke."
It exercises the full TWS API round-trip from inside the api container
using a Python REPL.

```bash
docker compose --env-file deploy/.env exec api /opt/venv/bin/python <<'EOF'
import asyncio
from decimal import Decimal
from services.execution.ibkr_adapter import IbAsyncIbkrClient
from services.execution.types import IbkrPlaceOrderRequest, IbkrContractRef

async def main():
    client = IbAsyncIbkrClient(host="ib_gateway", port=4002, client_id=1)
    state = await client.connect()
    print(f"connected: {state.is_connected} server_version={state.server_version}")
    contract = await client.resolve_contract("/MES")
    print(f"contract: {contract}")
    req = IbkrPlaceOrderRequest(
        client_order_id="smoke-001-abc-xyz-1",
        contract=contract,
        side="buy",
        quantity=Decimal("1"),
        order_type="limit_marketable",
        limit_price=Decimal("4000"),  # below market — won't fill immediately
        time_in_force="DAY",
    )
    result = await client.place_order(req)
    print(f"placed: client_order_id={result.client_order_id} broker_order_id={result.broker_order_id} status={result.status}")
    if result.status in ("submitted", "pending_submit"):
        cancel = await client.cancel_order(req.client_order_id)
        print(f"cancelled: broker_order_id={cancel.broker_order_id}")
    elif result.status == "rejected":
        print(f"REJECTED: category={result.rejection_category} detail={result.rejection_detail}")
    await client.disconnect()

asyncio.run(main())
EOF
```

Expected output:

```
connected: True server_version=176
contract: IbkrContractRef(market='/MES', ibkr_local_symbol='', ...)
placed: client_order_id=smoke-001-abc-xyz-1 broker_order_id=12345 status=submitted
cancelled: broker_order_id=12345
```

**This round-trip MUST succeed before Pivot-PR-B can be considered
deploy-ready.** If it fails:
* "REJECTED: category=outside_trading_hours" — wait for the next CME
  session (futures trade 17:00 ET → 16:00 ET next day, Sun–Fri) + retry.
* "REJECTED: category=invalid_contract" — futures-sim NOT available on
  the operator's paper account; escalate per Step 0 fallback.
* Connection error — check `docker compose logs ib_gateway` for the
  underlying IBC failure.

---

## Step 6 — Verify get_positions returns zero

The paper account starts with zero positions (this is the operator's
new paper account in IBKR's universe). Confirm:

```bash
docker compose --env-file deploy/.env exec api /opt/venv/bin/python -c "
import asyncio
from services.execution.ibkr_adapter import IbAsyncIbkrClient

async def main():
    client = IbAsyncIbkrClient(host='ib_gateway', port=4002, client_id=1)
    await client.connect()
    positions = await client.get_positions()
    print(f'positions: {len(positions)}')
    for p in positions:
        print(f'  {p.contract.market} qty={p.quantity}')
    await client.disconnect()

asyncio.run(main())
"
```

Expected: `positions: 0`.

If positions > 0, the operator may have placed test orders from a
previous IBKR portal session; cancel them via the portal or close the
positions before continuing.

---

## Step 7 — Audit chain integrity check

Pivot-PR-B does NOT write audit_log rows directly (the dispatcher in
Pivot-PR-D + the route handlers do). But run `verify_chain` to confirm
the existing chain still walks cleanly:

```bash
docker compose --env-file deploy/.env exec api /opt/venv/bin/python -m services.audit.verify_chain --env paper
```

Expected: `CHAIN OK: 2 rows verified` (unchanged from prior carryover state).

---

## Token rotation procedure

IBKR's paper-account credentials rotate manually via the IBKR portal
(no API). Live-account credentials similarly. The procedure:

1. Operator logs into IBKR portal + regenerates credentials.
2. Update `secrets/paper.enc.yaml` (or `live.enc.yaml`) via `sops`.
3. Commit + push.
4. On VPS: re-decrypt sops bundle + update `deploy/.env` (Step 2).
5. Restart `ib_gateway` container — `docker compose restart ib_gateway`.
6. Wait for `Login succeeded` log + healthcheck Up.
7. Repeat Step 5 placeOrder smoke to confirm the new credentials work.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Login to TWS Gateway failed` in logs | Bad credentials OR 2FA timeout | Re-check sops fields; complete 2FA via IBKR mobile app within 30s of container start |
| Port 4002 not listening | Container never finished IBC login | Wait 2-3 min; if still failing, check IBC logs for 2FA prompts |
| `IbkrPlacementError: ib_gateway connect failed` | TWS API session never came up | Step 4's `nc` should show whether the port is reachable; if not, IBC didn't fully start |
| `REJECTED category=outside_trading_hours` | CME market closed | Wait for next session (futures: Sun 17:00 ET → Fri 16:00 ET; daily 16:00-17:00 break) |
| `REJECTED category=invalid_contract` | Futures-sim unavailable on paper account | Escalate per Step 0; pivot fallback to Option 2 if persistent |
| `REJECTED category=duplicate_order` | Same client_order_id used twice | Smoke script uses a fixed ID (`smoke-001-abc-xyz-1`); cancel any existing order with that ID first |
| `REJECTED category=limit_too_far_from_market` | Limit price (4000) below the realistic /MES range | The smoke deliberately picks a low limit; if /MES is trading much higher (e.g., 5500+), raise the limit_price to within a reasonable band |
| Healthcheck `Up (unhealthy)` after 5 min | IBC login looped or hung | `docker compose restart ib_gateway`; if persistent, VNC in via `vncviewer <vps-ip>:5900` (password from `IB_GATEWAY_VNC_PASSWORD`) + debug manually |

---

## Cross-references

- Pivot-PR-B rationale: `Docs/decisions-log.md` 2026-05-12 entry.
- Forbidden whitelist binding: `services/execution/**` → `[A02]` requires
  `risk-review-approved` PR label.
- IBKR client Protocol: `services/execution/ibkr_client.py`.
- Adapter implementation: `services/execution/ibkr_adapter.py`.
- Pure-policy types: `services/execution/types.py`.
- LEAN Local runbook: `deploy/lean_local/README.md` (Pivot-PR-A).
- Reconciliation runbook: `deploy/reconciliation/README.md` (Pivot-PR-C).
- gnzsnz/ib-gateway-docker image docs: https://github.com/gnzsnz/ib-gateway-docker
- ib-async library: https://github.com/ib-api-reloaded/ib_async
