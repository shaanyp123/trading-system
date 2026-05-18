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
[gateway] Server listening on port 4002 (then socat forwards 4004 → 4002)
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

## Step 4 — Verify port 4004 is reachable from inside the network

The gnzsnz image publishes the externally-facing TWS API port via socat
on **4004 (paper)** / **4003 (live)** — NOT the internal gateway port
4002/4001 (those are 127.0.0.1-only inside the container). Discovered
Pivot-PR-B Step 5 smoke 2026-05-12.

From inside the `internal` Docker network (using a one-off `curl` /
`nc` container):

```bash
docker compose --env-file deploy/.env exec api nc -zv ib_gateway 4004
```

Expected: `Connection to ib_gateway 4004 port [tcp/*] succeeded!`

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
    client = IbAsyncIbkrClient(host="ib_gateway", port=4004, client_id=1)
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
    client = IbAsyncIbkrClient(host='ib_gateway', port=4004, client_id=1)
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

## Existing session collisions with TWS Desktop

IBKR allows only one logged-in client per account at a time. If the
operator's TWS Desktop is open against the same paper account
(`DUQ825170`) and the gateway tries to log in — typically after IBKR's
overnight maintenance window restarts the gateway around 00:18 UTC —
IBC pops a headless "Existing session detected" dialog that hangs
indefinitely on the VNC display. While the dialog is open, IBKR
emits Error 1100 ("connectivity lost") to the api and orderStatus
events stop propagating, even though the api's TWS API socket stays
open and `place_order` calls succeed at the protocol layer (the orders
sit in the gateway but never roundtrip a fill event).

**Canonical fix:** `docker-compose.yml` sets
`EXISTING_SESSION_DETECTED_ACTION=primary` on the `ib_gateway` service.
IBC writes this to its `config.ini` `ExistingSessionDetectedAction`
setting at container start. With `primary`, the gateway WINS the
collision — the other client (TWS Desktop) is kicked.

**Documented tradeoff:** every gateway restart silently kicks the
operator out of TWS Desktop. The operator must manually reconnect
TWS Desktop after each restart. This is the expected behavior — the
backend is authoritative for the trading account; TWS Desktop is for
manual operator review only.

**Manual smoke procedure** (do this after any `docker-compose.yml`
edit that touches the IBC env block, or after a fresh VPS deploy):

1. **Pre-state:** open TWS Desktop on the operator workstation and
   log into `DUQ825170` paper. Confirm `Connected` in the TWS title
   bar.

2. **Trigger a gateway restart:** on the VPS:
   ```
   docker compose restart ib_gateway
   ```

3. **Watch the gateway logs for ~120s** (IBC's normal cold-boot
   window):
   ```
   docker compose logs -f ib_gateway
   ```

4. **Expected log lines (success path):**
   - `Got main window from future`
   - `Login succeeded`
   - `TWS API connection accepted` (when api reconnects)

5. **Expected TWS Desktop behavior:** within ~10s of the gateway's
   `Login succeeded`, the TWS Desktop session disconnects with a
   dialog "You have been logged in from another location." This is
   the expected `primary` behavior.

6. **Anti-pattern to watch for:** if logs show
   `Existing session detected ... User must choose` and IBC hangs,
   the env var did not propagate. Verify with:
   ```
   docker compose exec ib_gateway grep ExistingSessionDetectedAction /home/ibgateway/ibc/config.ini
   ```
   Should print `ExistingSessionDetectedAction=primary`. If missing
   or different, re-check `docker-compose.yml` env block + recreate
   the container (`docker compose up -d --force-recreate ib_gateway`).

7. **Post-state:** confirm the api reconnects within ~30s:
   ```
   docker compose logs api 2>&1 | grep -E 'ibkr_(connected|reconnected)' | tail -3
   ```

8. **Cleanup:** operator may reconnect TWS Desktop if they want
   continued manual visibility into the paper account. The kick is
   one-shot — TWS Desktop can rejoin without affecting the gateway.

Integration-testing this end-to-end in CI is impractical because it
requires a real IBKR auth round-trip + a second IBKR client to drive
the collision. The above manual smoke is the A27 satisfier.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Login to TWS Gateway failed` in logs | Bad credentials OR 2FA timeout | Re-check sops fields; complete 2FA via IBKR mobile app within 30s of container start |
| Port 4004 not listening | Container never finished IBC login OR socat process died | Wait 2-3 min for login; check `docker exec trading-ib_gateway-1 ps auxf` for socat process; restart container if socat is gone |
| `IbkrPlacementError: ib_gateway connect failed` | TWS API session never came up | Step 4's `nc` should show whether the port is reachable; if not, IBC didn't fully start |
| `REJECTED category=outside_trading_hours` | CME market closed | Wait for next session (futures: Sun 17:00 ET → Fri 16:00 ET; daily 16:00-17:00 break) |
| `REJECTED category=invalid_contract` | Futures-sim unavailable on paper account | Escalate per Step 0; pivot fallback to Option 2 if persistent |
| `REJECTED category=duplicate_order` | Same client_order_id used twice | Smoke script uses a fixed ID (`smoke-001-abc-xyz-1`); cancel any existing order with that ID first |
| `REJECTED category=limit_too_far_from_market` | Limit price (4000) below the realistic /MES range | The smoke deliberately picks a low limit; if /MES is trading much higher (e.g., 5500+), raise the limit_price to within a reasonable band |
| Healthcheck `Up (unhealthy)` after 5 min | IBC login looped or hung | `docker compose restart ib_gateway`; if persistent, VNC in via `vncviewer <vps-ip>:5900` (password from `IB_GATEWAY_VNC_PASSWORD`) + debug manually |
| `Existing session detected ... User must choose` in logs + IBC hangs + Error 1100 to api | `EXISTING_SESSION_DETECTED_ACTION` env var not propagating to IBC config | See "Existing session collisions with TWS Desktop" section above; verify `docker compose exec ib_gateway grep ExistingSessionDetectedAction /home/ibgateway/ibc/config.ini` prints `primary`; recreate container if not |
| Orders placed but no fills propagate after IBKR maintenance restart (~00:18 UTC) | IBC hit "Existing session detected" dialog because TWS Desktop was open against same account | Workaround: close TWS Desktop + `docker compose restart ib_gateway`. Canonical fix shipped 2026-05-18 (`EXISTING_SESSION_DETECTED_ACTION=primary`); if symptom recurs, verify env propagation per row above |

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
