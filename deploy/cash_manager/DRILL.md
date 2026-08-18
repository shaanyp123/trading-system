# A27 cash-manager venue drill — paste-ready ceremony

> **✅ EXECUTED 2026-08-18 (operator-run; full findings in
> `Docs/decisions-log.md` "2026-08-18 — A27 CASH-MANAGER VENUE DRILL").**
> Outcome: converts PROVEN both directions (currency-code accounts, $0
> fee, settlement ≤ 30 s; **quote TTL is short — quote + commit must run
> as ONE block**, which is why Steps 1+2 below must not be run minutes
> apart); Amendment C smoke PASS ×2; equity round-trip exact (FC-6);
> **Step 3's `schedule_futures_sweep` → HTTP 403 Missing Required
> Scopes** (caveat 1 confirmed — View+Trade key). Sweep disposition
> pending operator decision; the recorded recommendation is convert-only
> reclaim (venue auto-margining is proven by the July CFM statement),
> which would retire the sweep leg without widening key permissions.
> The `-T` flags on the exec blocks below were added post-drill (the
> heredoc form fails without them: "the input device is not a TTY").

**Status: PREPARED 2026-07-20. DO NOT RUN until the operator explicitly
triggers the drill.** Every step past Step 0 moves real (small) money.
The drill is the `README.md` 7-item fact-check checklist made
paste-ready; it is a **manual** exercise of the `SdkCashSweepVenueClient`
call contracts — `API_CASH_MANAGER_ENABLED` stays **false** throughout
and the worker stays DORMANT. Nothing here writes `cash_sweeps` rows or
audit events (an operator-manual venue action, ledger-external by
design).

**House rules for the drill:**

- Paste every raw output back into the session. Surprises are FINDINGS,
  not failures — the drill exists to replace [A27] assumptions with
  observed venue contracts (README fact-check 7: everything observed
  gets a decisions-log entry and any wrapper corrections land in a PR
  before activation).
- If any step errors, STOP at that step and paste the error. Do not
  improvise retries with different parameters.
- Pick the drill amount first. Suggested `DRILL_USD=25.00` (well under
  1-contract margin scale). Venue convert minimums are themselves
  [A27]-unverified — a minimum-amount rejection is a finding; raise the
  amount only after recording it.

**Known caveats going in (expect these; they are findings, not
emergencies):**

1. **Key permissions.** The CDP key was provisioned View + Trade ONLY
   (no Transfer — `deploy/secrets.template.yaml`). Whether
   convert/sweep endpoints need more than Trade is unverified: a
   403/permission error at Step 1 halts the drill pending an operator
   decision on widening key permissions (security tradeoff — record
   it, don't just do it).
2. **Convert funding source.** The wrapper assumes a USD→USDC convert
   draws from spot (CBI) USD. If the operator's USD sits almost
   entirely as CFM futures margin, the quote may reject for
   insufficient funds — which would mean the worker's `to_yield` leg
   needs a CFM→CBI movement first (wrapper change, pre-activation).
3. **Sweep direction.** `schedule_futures_sweep` is assumed to move
   spot USD INTO CFM margin (the `to_margin` leg). If the venue call
   sweeps the other way (CFM→spot), record it — direction fix required
   before activation.
4. **Statement classification.** These manual conversions have no
   `cash_sweeps` rows, so on the first CFM statement
   `reconcile_statement.py` will classify their lines `capital_event`
   (not `sweep`) — expected; eyeball them then.

All blocks run on the VPS from `/opt/trading`.

---

## Step 0 — pre-flight + baseline (read-only, safe to run anytime)

```bash
cd /opt/trading
# 0a. #405 passthrough mapping exists and resolves (both should print, value false)
docker compose --env-file deploy/.env config | grep -E "API_(CASH_MANAGER|BINANCE_FUNDING_PROXY)_ENABLED"
# 0b. the api container itself sees the flag as false (worker dormant)
docker compose --env-file deploy/.env exec api printenv API_CASH_MANAGER_ENABLED
# 0c. latest USDC capture (the Amendment C floor-basis input)
docker compose --env-file deploy/.env exec postgres psql -U app_service -d trading -c \
  "SELECT snapshot_date_utc, cbi_usdc, captured_at_utc FROM cash_balance_snapshots ORDER BY snapshot_date_utc DESC LIMIT 3;"
```

```bash
# 0d. venue baseline — futures balance summary (the equity_from_summary source)
cd /opt/trading
docker compose --env-file deploy/.env exec -T api python - <<'PY'
import json, yaml
sec = yaml.safe_load(open("/run/secrets/secrets.yaml"))
from coinbase.rest import RESTClient
rest = RESTClient(api_key=sec["coinbase"]["api_key_name"],
                  api_secret=sec["coinbase"]["api_private_key"], timeout=30)
body = rest.get_futures_balance_summary().to_dict()
print(json.dumps(body, indent=2, default=str))
PY
```

Record the baseline numbers (visible USD equity, USDC balance). They
anchor fact-check 6.

---

## Step 1 — FC-1: convert quote USD→USDC (creates a QUOTE only; commits nothing)

```bash
cd /opt/trading
DRILL_USD=25.00 docker compose --env-file deploy/.env exec -T -e DRILL_USD api python - <<'PY'
import json, os, yaml
sec = yaml.safe_load(open("/run/secrets/secrets.yaml"))
from coinbase.rest import RESTClient
rest = RESTClient(api_key=sec["coinbase"]["api_key_name"],
                  api_secret=sec["coinbase"]["api_private_key"], timeout=30)
q = rest.create_convert_quote(from_account="USD", to_account="USDC",
                              amount=os.environ["DRILL_USD"])
print(json.dumps(q.to_dict(), indent=2, default=str))
PY
```

**Verify (README FC-1):** the response carries `trade.id`; the
currency-code account identifiers (`"USD"`/`"USDC"`) were ACCEPTED (an
error demanding account UUIDs = wrapper fix). Note any fee field (the
wrapper assumes 1:1 no-fee). Quotes expire — proceed to Step 2
promptly.

---

## Step 2 — FC-2: commit the quote + settlement-latency observation

Paste the `trade.id` from Step 1 into `TRADE_ID`:

```bash
cd /opt/trading
TRADE_ID=PASTE_ME docker compose --env-file deploy/.env exec -T -e TRADE_ID api python - <<'PY'
import json, os, time, yaml
sec = yaml.safe_load(open("/run/secrets/secrets.yaml"))
from coinbase.rest import RESTClient
rest = RESTClient(api_key=sec["coinbase"]["api_key_name"],
                  api_secret=sec["coinbase"]["api_private_key"], timeout=30)
r = rest.commit_convert_trade(trade_id=os.environ["TRADE_ID"],
                              from_account="USD", to_account="USDC")
print(json.dumps(r.to_dict(), indent=2, default=str))
for wait in (0, 30, 120):
    time.sleep(wait)
    body = rest.get_futures_balance_summary().to_dict()
    bs = body.get("balance_summary", {})
    print(f"--- balance_summary after +{wait}s ---")
    print(json.dumps(bs, indent=2, default=str))
PY
```

**Verify (README FC-2):** required commit params were just
`trade_id` + accounts (extra required params = wrapper fix); whether
the balance reflects within the printed 0/30/150 s reads. **If
settlement is asynchronous** (balances lag the commit), record the lag —
it weakens the worker's crash-recovery re-plan argument (a restart
inside the window can double-sweep) and must be noted in the activation
decision.

**Then, immediately: run `/cash-recapture` in Discord** — the first
live smoke of the Amendment C hook. Verify:

```bash
cd /opt/trading
docker compose --env-file deploy/.env logs api --since 10m 2>&1 | grep -E "usdc_rewards_recapture_completed|cash_capture"
docker compose --env-file deploy/.env exec postgres psql -U app_service -d trading -c \
  "SELECT snapshot_date_utc, cbi_usdc, captured_at_utc FROM cash_balance_snapshots ORDER BY snapshot_date_utc DESC LIMIT 1;"
```

Expect `usdc_rewards_recapture_completed`, today's row UPSERTed
(`captured_at_utc` fresh, `cbi_usdc` up by ~the drill amount).

---

## Step 3 — FC-3 + FC-4: reclaim leg — convert USDC→USD, then sweep into CFM. TIME THIS

Note the UTC clock time you start. Quote + commit (same two-block
pattern, directions flipped — reuse Step 1 then Step 2 with
`from_account="USDC", to_account="USD"`). Then:

```bash
cd /opt/trading
DRILL_USD=25.00 docker compose --env-file deploy/.env exec -T -e DRILL_USD api python - <<'PY'
import json, os, yaml
sec = yaml.safe_load(open("/run/secrets/secrets.yaml"))
from coinbase.rest import RESTClient
rest = RESTClient(api_key=sec["coinbase"]["api_key_name"],
                  api_secret=sec["coinbase"]["api_private_key"], timeout=30)
r = rest.schedule_futures_sweep(usd_amount=os.environ["DRILL_USD"])
print(json.dumps(r.to_dict(), indent=2, default=str))
PY
```

**Verify (README FC-3/FC-4):** the `usd_amount` param name was accepted;
the sweep's DIRECTION is spot→CFM (caveat 3); then poll —

```bash
# re-run every few minutes until the CFM side reflects the sweep; note elapsed time
cd /opt/trading
docker compose --env-file deploy/.env exec -T api python - <<'PY'
import json, yaml
sec = yaml.safe_load(open("/run/secrets/secrets.yaml"))
from coinbase.rest import RESTClient
rest = RESTClient(api_key=sec["coinbase"]["api_key_name"],
                  api_secret=sec["coinbase"]["api_private_key"], timeout=30)
print(json.dumps(rest.get_futures_balance_summary().to_dict(), indent=2, default=str))
try:
    print(json.dumps(rest.list_futures_sweeps().to_dict(), indent=2, default=str))
except Exception as exc:  # endpoint contract itself is [A27]-unverified
    print("list_futures_sweeps failed:", repr(exc))
PY
```

**FC-4 verdict (delta-spec open question #1):** was the reclaimed USD
usable as CFM margin the SAME UTC day, and how long did it take? If
same-day cannot be verified, **the worker stays dormant** (README
hard rule). Run `/cash-recapture` again after the USDC→USD convert
(symmetric Amendment C smoke — basis should tighten back down).

---

## Step 5 — FC-5: rewards-accrual residual (next-day observation)

After the drill day's 00:20 UTC capture: confirm USDC rewards accrue on
the full CBI USDC balance (Coinbase UI rewards ledger, or the
usdc_rewards ledger poll). No paste block — observational; report what
the venue shows.

---

## Step 6 — FC-6: equity-visibility check

From the Step 0 baseline vs the reads after each leg:
`balance_summary` visible USD equity should drop by EXACTLY the
converted amount after Step 2 (USD→USDC) and recover after Step 3's
sweep lands. Any deviation (fees, rounding, lag) is a finding —
quantify it.

---

## Step 7 — record + fold

Paste the full drill transcript back into the session. Claude then:
writes the decisions-log drill entry (every observed contract detail,
per README FC-7), folds any wrapper corrections into a PR, and updates
this file + `README.md` with the observed reality. Only after that —
and the C2 operator decision — does the enable ceremony
(`README.md` §Enable) become eligible.
