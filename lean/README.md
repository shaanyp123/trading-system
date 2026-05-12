# `lean/` — LEAN Local Algorithm

The LEAN algorithm wrapper for `v1_trend_following`. **Post-pivot 2026-05-12
architecture (Pivot-PR-A; DP-025 → Option 4):** this algorithm runs inside
a Dockerized `lean_local` container on the operator's VPS. It POSTs signal
events to the backend at `POST /api/internal/lean/signals` (shared-bearer
auth). **The api is the broker authority** — `services/execution/ibkr_adapter.py`
dispatches approved signals to IBKR via `ib-async` through the `ib_gateway`
container. **No QuantConnect Cloud involvement in production.**

**Post-ceremony 2026-05-12:** LEAN-side broker swapped from
`InteractiveBrokersBrokerage` → `PaperBrokerage`. The bare
`quantconnect/lean:latest` image does NOT ship
`QuantConnect.Brokerages.InteractiveBrokers.dll`, so live-mode boot with
the IBKR broker crash-looped on LEAN's Composer broker-factory lookup
(`Sequence contains no matching element`). PaperBrokerage is LEAN's
built-in zero-dependency simulator; it satisfies LEAN's internal
`IBrokerage` contract well enough for the strategy's subscription +
scheduling machinery to function. The strategy never calls
broker-mutating APIs — it only emits signals via HTTP POST — so the
LEAN-side broker is purely vestigial. See `Docs/decisions-log.md`
2026-05-12 entry "Post-ceremony session — LEAN container's IBKR DLL gap".

The pre-pivot architecture (algorithm hosted on QC Cloud; backend polled
QC ObjectStore for events; defensive trims via `/instructions/<n>.json`)
is retired — see `Docs/decisions-log.md` 2026-05-12 entry for the full
rationale.

| File | Purpose |
|---|---|
| `v1_strategy.py` | `QCAlgorithm` subclass — renamed from `v1_qc_algorithm.py` per Pivot-PR-A Q3 resolution. Daily resolution, 17:30 ET signal cycle, IBKR brokerage MODEL (fee/slippage simulation only — `BrokerageName.INTERACTIVE_BROKERS_BROKERAGE` enum ships in the base image; the missing DLL is the LIVE broker, not the model), parameter map via `self.get_parameter`. Emits heartbeat + `signal_emitted` POSTs via `urllib.request`. Uses LEAN's snake_case Python API. |
| `lean.json` | LEAN project config. Reads `algorithm-language` + `algorithm-type-name` + `parameters` + `environments`. Post-pivot environments: `backtesting` (CI golden tests + parameter sweeps) and `paper-internal` (live-mode against PaperBrokerage; selected when `LEAN_LIVE_MODE=true`). |
| `lean_data` (Docker named volume) | Seed daily bars for the Phase 1 universe (`/MES /MNQ /MYM /M2K /MGC /MCL /MBT` futures + `TLT IEF SHY TIP` ETFs) mounted into the container at `/Lean/Data/`. Populated lazily by the operator — the boot smoke does NOT require this volume to be non-empty (the strategy survives empty `active_universe` by returning no signals; heartbeats still POST, the daily cadence still fires). To seed: download QC's public daily bars + `docker cp` into the volume via a transient container; see `deploy/lean_local/README.md`. NOT bind-mounted from this repo (compose uses a named volume — anything in `lean/Data/` in git would be ignored). |

---

## Post-pivot operational runbook

The pre-pivot "operator uploads algorithm to QC Cloud" flow is retired.
Post-pivot, LEAN runs in a Docker container on the operator's VPS; there
is no QC Cloud project, no manual algorithm upload, no QC's editor.

See `deploy/lean_local/README.md` for the canonical Pivot-PR-A operator
runbook (sops fields → docker compose build → docker compose up →
smoke).

### Brokerage configuration

LEAN's `lean.json` defines two environments:

* **`backtesting`** — runs historical backtests against QC's bundled bars
  (downloaded + cached in the `lean_data` Docker volume on first run).
  Used in CI golden tests + ad-hoc parameter sweeps. No broker
  dependency.
* **`paper-internal`** — live-mode operation under LEAN's built-in
  `PaperBrokerage` simulator. No real exchange connection — LEAN's
  strategy never places orders. The api owns the real IBKR broker
  contract via `services/execution/ibkr_adapter.py` (paper or live IBKR
  port depending on the api's own configuration; LEAN's behavior is
  identical either way). Used for the 30-CME-session paper clock + for
  live operation. Handler bindings mirror upstream LEAN's `live-paper`
  env with one substitution: `data-queue-handler` uses `FakeDataQueue`
  (real built-in that aggregates fake ticks via `IDataQueueHandler` +
  `IDataQueueUniverseProvider`) instead of upstream's `LiveDataQueue`
  (which is a stub that throws `NotImplementedException` on Subscribe).
  The strategy's `on_data` is a no-op so the fake ticks are dropped;
  daily-resolution history comes from `SubscriptionDataReaderHistoryProvider`
  reading `/Lean/Data/` on disk.

Environment selection is done at container-start via the `LEAN_LIVE_MODE`
env var (set in `deploy/.env` on the VPS): `false` → `backtesting`;
`true` → `paper-internal`. The `lean_local` container's entrypoint
resolves sops secrets → `LEAN_LOCAL_BEARER_TOKEN` env var → deep-merges
`lean.json` on top of the upstream LEAN config → launches LEAN's Launcher.

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

### Strategy code mounting

`strategies/v1_trend_following/` is mounted into the container at
`/Lean/Strategies` (read-only) so the broker-agnostic strategy package
runs identically across backtest + paper + live envs. Strategy changes
do NOT require a container rebuild — `docker compose restart lean_local`
picks up new code after a `git pull`.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Boot crash: `LEAN_LOCAL_BEARER_TOKEN env var missing or empty` | sops field `lean.api_bearer_token` not populated, or `decrypted.yaml` is stale on the VPS | Run `sops secrets/<env>.enc.yaml`; add `lean.api_bearer_token: <32-byte-base64>` (generate via `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`); commit + push; re-decrypt on VPS (`sops -d secrets/<env>.enc.yaml > /opt/trading/secrets-decrypted/decrypted.yaml`); `docker compose restart lean_local` |
| Boot crash: `lean.api_bearer_token still has placeholder value '<TODO_...>'` | sops template field is unedited | Same as above — replace the placeholder with a real value via `sops secrets/<env>.enc.yaml` |
| LEAN logs: `lean_signal_post_failed status=401 ...` | The token in `decrypted.yaml` does NOT match the api's `API_LEAN_LOCAL_BEARER_TOKEN` env var | Verify both containers were restarted after the last sops edit. Order of operations matters: edit sops → commit + push → on VPS, decrypt → restart api AND lean_local |
| LEAN logs: `lean_signal_post_url_error reason=Connection refused` | api container not running or `LEAN_LOCAL_API_BASE_URL` points at wrong host | `docker compose ps api`; if not Up, `docker compose up -d api`; if Up, verify the env var resolves correctly (`docker compose exec lean_local env | grep LEAN_LOCAL_API_BASE_URL`) |
| Backtest log: `Failed to load the algorithm. Algorithm class not found.` | `algorithm-location` in `lean.json` doesn't match the actual filename in `/Lean/Algorithm/` | Confirm `lean.json::algorithm-location` = `"v1_strategy.py"` (was `v1_qc_algorithm.py` pre-pivot) |
| Build errors like `"Resolution" has no attribute "Daily"` | The strategy uses PascalCase API; QC migrated to snake_case ~2024 | Method names are snake_case (`set_start_date`, `add_future`, `get_parameter`); enum values are SCREAMING_SNAKE (`Resolution.DAILY`); class names stay PascalCase. Check `v1_strategy.py` for any leftover PascalCase. |
| Build error: `NameError: name 'AlgorithmImports' is not defined` | The first line `from AlgorithmImports import *` was deleted | Restore the import line at the top of `v1_strategy.py` |
| LEAN boot crash with `Sequence contains no matching element` | LEAN was bound to a brokerage assembly that isn't in the `quantconnect/lean:latest` image (e.g., `InteractiveBrokersBrokerage`) | Confirm `lean.json` has `live-mode-brokerage = "PaperBrokerage"` under the `paper-internal` env. The bare LEAN image ships PaperBrokerage but NOT the IBKR brokerage — see `Docs/decisions-log.md` 2026-05-12 'Post-ceremony session' entry. |
| LEAN container boots but no historical data + no signals generated | `/Lean/Data/` empty — `SubscriptionDataReaderHistoryProvider` returns nothing | Expected during initial boot smoke. Populate `lean/Data/` with QC's public daily-bars datasets (CME micros + bond ETFs, ~90 days) when ready. The boot smoke succeeds without data — the daily cadence still fires + heartbeats still POST. |
| Algorithm boots but `initialize` never seems to run | Method name typo — LEAN dispatches to `initialize` (snake_case) | Rename `def Initialize(self):` → `def initialize(self):`. Same for `OnData` → `on_data`. |
| `signal_cycle_tick` log never appears | Algorithm is still in warmup (200 daily bars ≈ 200 calendar days) | Wait until LEAN's data feed has played through the warmup window. Backtest mode plays warmup instantly; live mode waits real-time |

---

## Cross-references

- Strategy logic source-of-truth: `strategies/v1_trend_following/` (pure Python,
  unit-testable, no QC imports).
- Pivot rationale: `Docs/decisions-log.md` 2026-05-12 entry "Phase-1 architecture
  pivot — QC ObjectStore → LEAN Local + direct IBKR".
- Post-pivot architecture: `Docs/backend-spec.md` §1.2 (current).
- Pre-pivot architecture (RETIRED): `Docs/backend-spec.md` §1.2-RETIRED.
- Operator runbook for the LEAN Local Docker container: `deploy/lean_local/README.md`.
- Operator runbook for the IBKR Gateway: `deploy/ibkr/README.md` (Pivot-PR-B).
- Parameter semantics + agent tighten directions: `Docs/backend-spec.md` §12.3
  + `strategies/v1_trend_following/parameters.py` `V1_DEFAULTS`.
- Phase 1 sub-universe lock: `Docs/decisions-log.md` 2026-05-05 entry.
