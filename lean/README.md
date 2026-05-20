# `lean/` — LEAN Local Algorithm

The LEAN algorithm wrapper for `v1_trend_following`. **Post-pivot 2026-05-12
architecture (Pivot-PR-A; DP-025 → Option 4)** + **2026-05-20 data-layer
sub-pivot:** this algorithm runs inside a Dockerized `lean_local` container
on the operator's VPS. It POSTs signal events to the backend at
`POST /api/internal/lean/signals` (shared-bearer auth) and reads market
data directly from IBKR via the same `ib_gateway` sidecar the api uses
for order placement — on a distinct `clientId=10` (api owns clientId=1).
**The api is the broker authority** — `services/execution/ibkr_adapter.py`
dispatches approved signals to IBKR via `ib-async` through the
`ib_gateway` container. **No QuantConnect Cloud involvement in production.
No seed files. No DataBento. No yfinance.**

**2026-05-20 data-layer sub-pivot (current):** LEAN's `lean.json`
`paper-internal` env binds `data-queue-handler` + `history-provider`
both to `InteractiveBrokersBrokerage`. The IBKR brokerage plugin DLL
(`QuantConnect.Brokerages.InteractiveBrokers` v2.5.17699 from NuGet)
is baked into the `lean_local` image via the multi-stage Dockerfile at
`infrastructure/lean_local/Dockerfile` so live-mode boot can resolve
the class. `live-mode-brokerage` stays `PaperBrokerage` (LEAN does
NOT place orders; the api owns the order path on clientId=1). The
strategy's `self.history(symbol, count, Resolution.DAILY)` calls
`reqHistoricalData` against IBKR; `self.on_data` is a no-op so delayed
quote ticks are dropped. See `Docs/decisions-log.md` 2026-05-20 entry
"Phase 1 data-layer pivot: IBKR delayed quotes replace seed-file
architecture".

**2026-05-12 post-ceremony context (historical):** LEAN-side broker
was originally planned as `InteractiveBrokersBrokerage` directly for
BOTH data and fills, but the bare `quantconnect/lean:latest` image
does NOT ship the IBKR brokerage plugin DLL — live-mode boot crashed
on LEAN's Composer broker-factory lookup with `Sequence contains no
matching element`. PR #120 (2026-05-12) swapped to `PaperBrokerage`
for both data + fills as the path of least resistance; on-disk seed
files (yfinance + DataBento) provided market data. **The 2026-05-20
sub-pivot bakes the IBKR DLL into the image** (multi-stage Dockerfile
pulls v2.5.17699 from NuGet at build time) and uses it for the DATA
path only — fills stay on PaperBrokerage. See `Docs/decisions-log.md`
2026-05-12 entries "Post-ceremony session — LEAN container's IBKR
DLL gap" + 2026-05-20 entry for the full backstory.

The pre-pivot architecture (algorithm hosted on QC Cloud; backend polled
QC ObjectStore for events; defensive trims via `/instructions/<n>.json`)
is retired — see `Docs/decisions-log.md` 2026-05-12 entry for the full
rationale.

| File | Purpose |
|---|---|
| `v1_strategy.py` | `QCAlgorithm` subclass — renamed from `v1_qc_algorithm.py` per Pivot-PR-A Q3 resolution. Daily resolution, 17:30 ET signal cycle, IBKR brokerage MODEL (fee/slippage simulation only — `BrokerageName.INTERACTIVE_BROKERS_BROKERAGE` enum ships in the base image and loads `InteractiveBrokersBrokerageModel` from `QuantConnect.Brokerages.dll`, distinct from the live brokerage plugin DLL), parameter map via `self.get_parameter`. Emits heartbeat + `signal_emitted` POSTs via `urllib.request`. Uses LEAN's snake_case Python API. `_log_universe_freshness` invocation in `initialize` removed by the 2026-05-20 sub-pivot (the on-disk path it walked no longer exists). |
| `lean.json` | LEAN project config. Reads `algorithm-language` + `algorithm-type-name` + `parameters` + `environments`. Post-pivot environments: `backtesting` (CI golden tests + parameter sweeps) and `paper-internal` (live-mode with `data-queue-handler` + `history-provider` = `InteractiveBrokersBrokerage` on clientId=10; `live-mode-brokerage` = `PaperBrokerage`; 9 ib-* config keys for the data-queue connection). Selected at runtime via `LEAN_LIVE_MODE`. |
| `infrastructure/lean_local/Dockerfile` | Multi-stage image build. Stage 1 (`mcr.microsoft.com/dotnet/sdk:10.0`) pulls `QuantConnect.Brokerages.InteractiveBrokers` v2.5.17699 from NuGet via a stub csproj + `dotnet publish`. Stage 2 (`quantconnect/lean:latest`) copies `QuantConnect.Brokerages.InteractiveBrokers.dll` + `QuantConnect.IBAutomater.dll` (~5MB combined) into `/Lean/Launcher/bin/Debug/` so live-mode boot's Composer broker-factory lookup resolves the data-queue-handler class name. Override the plugin version via `--build-arg IBKR_PLUGIN_VERSION=X.Y.Z` if upstream ships a newer release. |
| `infrastructure/lean_local/entrypoint.sh` | Container entrypoint. Reads sops bundle (`/run/secrets/decrypted.yaml`) for: (a) `lean.api_bearer_token` → `LEAN_LOCAL_BEARER_TOKEN` (for the `POST /api/internal/lean/signals` auth); (b) `ibkr.paper_username` / `.paper_password` / `.paper_account` → `IB_USER_NAME` / `IB_PASSWORD` / `IB_ACCOUNT` (for the `${...}` substitution in `lean.json`'s ib-* keys at the deep-merge step). Fail-closed on missing or placeholder values. Deep-merges `lean.json` on top of upstream `/Lean/Launcher/bin/Debug/config.json`. |
| `lean_data` (Docker named volume) | Deprecated as of the 2026-05-20 sub-pivot. Pre-sub-pivot held the on-disk seed files for the Phase 1 universe (yfinance ETFs + DataBento futures). Post-sub-pivot LEAN reads market data from IBKR via the data-queue-handler; the volume is **mounted but unused**. Operator can `docker volume rm trading_lean_data` after a 30-day soak (see `Docs/decisions-log.md` 2026-05-20 entry open follow-up #2). |

---

## Post-pivot operational runbook

The pre-pivot "operator uploads algorithm to QC Cloud" flow is retired.
The seed-file ceremony from `deploy/lean_local/seed-data.md` is also
retired (file DELETED 2026-05-20). Post-pivot, LEAN runs in a Docker
container on the operator's VPS; there is no QC Cloud project, no
manual algorithm upload, no QC's editor, no seed-data refresh ceremony.

See `deploy/lean_local/README.md` for the canonical post-2026-05-20
operator runbook (sops fields → docker compose build → docker compose
up → IBKR data smoke + bearer auth smoke).

### Brokerage configuration

LEAN's `lean.json` defines two environments:

* **`backtesting`** — runs historical backtests against QC's bundled
  bars. Used in CI golden tests + ad-hoc parameter sweeps. No broker
  dependency. (Note: post-2026-05-20, backtests against the bundled
  bars no longer have a "current trading day" extension; for current-day
  backtests, switch to `live-mode` against IBKR's `reqHistoricalData`.)
* **`paper-internal`** — live-mode operation. `data-queue-handler` +
  `history-provider` are both `[InteractiveBrokersBrokerage]`; the
  same plugin instance implements both interfaces. LEAN connects to
  the `ib_gateway` sidecar on `clientId=10` (distinct from the api's
  order-placement `clientId=1`); both connections multiplex over the
  same gateway → IBKR servers TCP socket per IBKR's multi-client-id
  design. `live-mode-brokerage` is `PaperBrokerage` — LEAN's strategy
  never places orders. The api owns the real IBKR broker contract via
  `services/execution/ibkr_adapter.py`. Handler bindings mirror
  upstream LEAN's `live-interactive` env shape for data + history,
  with `transaction-handler` overridden to `BacktestingTransactionHandler`
  because `PaperBrokerage` simulates fills internally (NOT
  `BrokerageTransactionHandler`, which would route fills through the
  data-queue-handler's brokerage instance + accidentally place orders
  at IBKR).

Environment selection is done at container-start via the `LEAN_LIVE_MODE`
env var (set in `deploy/.env` on the VPS): `false` → `backtesting`;
`true` → `paper-internal`. The `lean_local` container's entrypoint
resolves sops secrets → env vars → deep-merges `lean.json` on top of
the upstream LEAN config → launches LEAN's Launcher.

### IBKR clientId allocation (LOCKED 2026-05-20)

Per dev-guide §1.5 LOCKED:

| Client | clientId | Source of truth |
|---|---|---|
| api order-placement worker (`services/execution/ibkr_adapter.py`) | **1** | PR #101 (`DEFAULT_CLIENT_ID = 1`) |
| LEAN data-queue-handler (this PR's `lean.json`) | **10** | `lean.json::ib-client-id` |
| Operator probes + recovery tools (e.g., `scripts/operator_tools/replay_executions.py`) | **80-99** | dev-guide §1.5 LOCKED |
| Reserved for future expansion (multi-strategy, read-only telemetry) | **2-7** | dev-guide §1.5 LOCKED |

IBKR allows multiple distinct client-ids per gateway session (verified
2026-05-19 evening probe — simultaneous connections at api=1 + LEAN=10
+ probe=95 + browser TWS Desktop logged in). A clientId collision raises
Error 162 "Trading TWS session is connected from a different IP
address" and wedges the colliding client for ~30 min. Never overlap.

### Authentication to the backend

Every POST from `v1_strategy.py::_post_event` carries
`Authorization: Bearer <token>` where `<token>` is sourced from sops
`lean.api_bearer_token` → env var `LEAN_LOCAL_BEARER_TOKEN`. The backend's
`LeanAuthMiddleware` (Pivot-PR-A) constant-time-compares against
`APISettings.lean_local_bearer_token` and injects a service-account
`SessionContext`. CSRF is bypassed for LEAN-authenticated requests.

If `LEAN_LOCAL_BEARER_TOKEN` is missing or empty at container start, the
entrypoint fails closed (exit 2) — the algorithm never starts. Better
loud failure than silent unauthenticated POSTs that 401 at the backend.

The data-queue connection to IBKR (clientId=10) uses **separate**
credentials from the bearer auth: `ibkr.paper_username` /
`.paper_password` / `.paper_account` from sops, exported as
`IB_USER_NAME` / `IB_PASSWORD` / `IB_ACCOUNT` and substituted into
`lean.json`'s `${...}` placeholders at the deep-merge step. The
entrypoint fail-closes if any of the three is missing or carries the
sops template's `<TODO_...>` placeholder (NEW 2026-05-20 — pre-sub-pivot
LEAN didn't need IBKR credentials).

### Strategy code mounting

`strategies/v1_trend_following/` is mounted into the container at
`/Lean/strategies` (read-only) so the broker-agnostic strategy package
runs identically across backtest + paper + live envs. Strategy changes
do NOT require a container rebuild — `docker compose restart lean_local`
picks up new code after a `git pull`. The Dockerfile rebuild is only
needed when:
- The IBKR plugin version (`IBKR_PLUGIN_VERSION` ARG) bumps.
- The upstream `quantconnect/lean:latest` image is repulled.
- The structlog / tini / python3-yaml runtime deps change.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Boot crash: `LEAN_LOCAL_BEARER_TOKEN env var missing or empty` | sops field `lean.api_bearer_token` not populated, or `decrypted.yaml` is stale on the VPS | Run `sops secrets/<env>.enc.yaml`; add `lean.api_bearer_token: <32-byte-base64>` (generate via `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`); commit + push; re-decrypt on VPS (`sops -d secrets/<env>.enc.yaml > /opt/trading/secrets-decrypted/decrypted.yaml`); `docker compose restart lean_local` |
| Boot crash: `IB_USER_NAME is empty (sops field ibkr.paper_username...)` or `IB_PASSWORD` or `IB_ACCOUNT` | One of the 3 IBKR sops fields is missing or carries a `<TODO_...>` placeholder | Run `sops secrets/<env>.enc.yaml`; populate `ibkr.paper_username` / `ibkr.paper_password` / `ibkr.paper_account` with real paper-account credentials; if the `ib_gateway` sidecar is already authenticated against the same paper account, these are the SAME values it uses. Commit + push; re-decrypt on VPS; `docker compose restart lean_local`. **NEW 2026-05-20** — pre-sub-pivot LEAN didn't need IBKR credentials. |
| Boot crash: `lean.api_bearer_token still has placeholder value '<TODO_...>'` | sops template field is unedited | Same as above — replace the placeholder with a real value via `sops secrets/<env>.enc.yaml` |
| LEAN logs: `lean_signal_post_failed status=401 ...` | The token in `decrypted.yaml` does NOT match the api's `API_LEAN_LOCAL_BEARER_TOKEN` env var | Verify both containers were restarted after the last sops edit. Order of operations matters: edit sops → commit + push → on VPS, decrypt → restart api AND lean_local |
| LEAN logs: `lean_signal_post_url_error reason=Connection refused` | api container not running or `LEAN_LOCAL_API_BASE_URL` points at wrong host | `docker compose ps api`; if not Up, `docker compose up -d api`; if Up, verify the env var resolves correctly (`docker compose exec lean_local env | grep LEAN_LOCAL_API_BASE_URL`) |
| Backtest log: `Failed to load the algorithm. Algorithm class not found.` | `algorithm-location` in `lean.json` doesn't match the actual filename in `/Lean/Algorithm/` | Confirm `lean.json::algorithm-location` = `"v1_strategy.py"` (was `v1_qc_algorithm.py` pre-pivot) |
| Build errors like `"Resolution" has no attribute "Daily"` | The strategy uses PascalCase API; QC migrated to snake_case ~2024 | Method names are snake_case (`set_start_date`, `add_future`, `get_parameter`); enum values are SCREAMING_SNAKE (`Resolution.DAILY`); class names stay PascalCase. Check `v1_strategy.py` for any leftover PascalCase. |
| Build error: `NameError: name 'AlgorithmImports' is not defined` | The first line `from AlgorithmImports import *` was deleted | Restore the import line at the top of `v1_strategy.py` |
| LEAN boot crash with `Sequence contains no matching element` shortly after Launcher startup | The IBKR brokerage plugin DLL didn't make it into the final image — i.e., the Dockerfile builder stage failed to pull `QuantConnect.Brokerages.InteractiveBrokers` from NuGet, or the COPY into `/Lean/Launcher/bin/Debug/` was skipped. Pre-2026-05-20 this was the canonical "the IBKR DLL isn't in `quantconnect/lean:latest`" issue (PR #120). Post-2026-05-20 the multi-stage Dockerfile should bake it in, so this fingerprint now points to a build issue. | First check `docker compose exec lean_local ls /Lean/Launcher/bin/Debug/QuantConnect.Brokerages.InteractiveBrokers.dll` — if the file isn't there, rebuild: `docker compose --env-file deploy/.env build --no-cache lean_local` to force a fresh NuGet pull. If the builder stage fails (network issue reaching nuget.org from VPS), check egress firewall + retry. As a fallback, pin to an older `IBKR_PLUGIN_VERSION` via `--build-arg`. See `Docs/decisions-log.md` 2026-05-20 entry for the original gap discovery. |
| LEAN logs: `v1_history_unavailable session_date=... failed_markets=[...]` for one or more futures | LEAN's IBKR data-queue subscription failed or `reqHistoricalData` returned empty for the active contract on that session_date | (1) Check the IBKR-side: log into the operator's IBKR Client Portal; verify the futures-trading entitlement covers the affected market. (2) Check the gateway: `docker compose logs ib_gateway 2>&1 \| grep -i 162` — Error 162 means a clientId collision; verify LEAN's clientId=10 isn't being used elsewhere. (3) Check the 2026-05-19 probe known issue: if the operator's TWS Desktop OR browser session is logged in for the same paper account, IBKR enforces single-IP — close all other IBKR sessions + restart `lean_local`. |
| `Error 162: Trading TWS session is connected from a different IP address` in `lean_local` logs | The operator's TWS Desktop or browser session is logged in for the same paper account AND IBKR enforces single-IP on certain account tiers OR a clientId collision with the api worker (clientId=1) or a stale probe (clientId 80-99) | Close all other IBKR sessions (TWS Desktop, browser tabs at portal.interactivebrokers.com); restart `lean_local`. If recurring, verify clientId allocation per dev-guide §1.5 — api=1, LEAN=10, probes=80-99. |
| LEAN container boots but no data: `self.history(...)` returns empty for every market | LEAN's data-queue-handler isn't connecting to IBKR | Verify `lean.json::ib-host=ib_gateway` resolves on the internal Docker network: `docker compose exec lean_local getent hosts ib_gateway`. Verify port 4002 is reachable: `docker compose exec lean_local nc -zv ib_gateway 4002`. If the gateway is unreachable, check `docker compose ps ib_gateway` + `docker compose logs ib_gateway --tail=50`. |
| Algorithm boots but `initialize` never seems to run | Method name typo — LEAN dispatches to `initialize` (snake_case) | Rename `def Initialize(self):` → `def initialize(self):`. Same for `OnData` → `on_data`. |
| `signal_cycle_tick` log never appears | Algorithm is still in warmup (200 daily bars ≈ 200 calendar days). Note that with IBKR's `reqHistoricalData`, warmup is much faster than backtest mode used to be — bars arrive as fast as IBKR can serve them, typically < 60s for the full Phase 1 universe. | Wait until `is_warming_up` flips to False. If warmup hangs for > 5 min, check `lean_local` logs for IBKR auth errors. |

---

## Cross-references

- Strategy logic source-of-truth: `strategies/v1_trend_following/` (pure Python,
  unit-testable, no QC imports).
- Pivot rationale (foundational): `Docs/decisions-log.md` 2026-05-12 entry
  "Phase-1 architecture pivot — QC ObjectStore → LEAN Local + direct IBKR".
- Data-layer sub-pivot rationale: `Docs/decisions-log.md` 2026-05-20 entry
  "Phase 1 data-layer pivot: IBKR delayed quotes replace seed-file architecture".
- Post-pivot architecture: `Docs/backend-spec.md` §1.2 (current).
- Pre-pivot architecture (RETIRED): `Docs/backend-spec.md` §1.2-RETIRED.
- Operator runbook for the LEAN Local Docker container:
  `deploy/lean_local/README.md` (post-2026-05-20 rewrite).
- Operator runbook for the IBKR Gateway: `deploy/ibkr/README.md` (Pivot-PR-B).
- Parameter semantics + agent tighten directions: `Docs/backend-spec.md` §12.3
  + `strategies/v1_trend_following/parameters.py` `V1_DEFAULTS`.
- Phase 1 sub-universe lock: `Docs/decisions-log.md` 2026-05-05 entry.
- IBKR clientId allocation (LOCKED): `Docs/claude-dev-guide.md` §1.5.
- NuGet plugin: [QuantConnect.Brokerages.InteractiveBrokers](https://www.nuget.org/packages/QuantConnect.Brokerages.InteractiveBrokers)
  (pinned to v2.5.17699 in `infrastructure/lean_local/Dockerfile`).
