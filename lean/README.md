# `lean/` — LEAN Local Algorithm

The LEAN algorithm wrapper for `v1_trend_following`. **Post-pivot 2026-05-12
architecture (Pivot-PR-A; DP-025 → Option 4):** this algorithm runs inside
a Dockerized `lean_local` container on the operator's VPS. It POSTs signal
events to the backend at `POST /api/internal/lean/signals` (shared-bearer
auth) and routes orders to IBKR via `ib-async` through the `ib_gateway`
container (Pivot-PR-B). **No QuantConnect Cloud involvement in production.**

The pre-pivot architecture (algorithm hosted on QC Cloud; backend polled
QC ObjectStore for events; defensive trims via `/instructions/<n>.json`)
is retired — see `Docs/decisions-log.md` 2026-05-12 entry for the full
rationale.

| File | Purpose |
|---|---|
| `v1_strategy.py` | `QCAlgorithm` subclass — renamed from `v1_qc_algorithm.py` per Pivot-PR-A Q3 resolution. Daily resolution, 17:30 ET signal cycle, IBKR brokerage model, parameter map via `self.get_parameter`. Pivot-PR-A scaffold: emits heartbeat POSTs via `urllib.request`. Strategy wiring (assemble bars, call generate_signals, POST each signal) lands in Pivot-PR-D. Uses LEAN's snake_case Python API. |
| `lean.json` | LEAN project config. Reads `algorithm-language` + `algorithm-type-name` + `parameters` + `environments`. Post-pivot environments: `backtesting`, `paper-ibkr` (Pivot-PR-B), `live-ibkr` (Week 8). |

---

## Post-pivot operational runbook

The pre-pivot "operator uploads algorithm to QC Cloud" flow is retired.
Post-pivot, LEAN runs in a Docker container on the operator's VPS; there
is no QC Cloud project, no manual algorithm upload, no QC's editor.

See `deploy/lean_local/README.md` for the canonical Pivot-PR-A operator
runbook (sops fields → docker compose build → docker compose up →
smoke).

### Brokerage configuration

LEAN's `lean.json` defines three environments:

* **`backtesting`** — runs historical backtests against QC's bundled bars
  (downloaded + cached in the `lean_data` Docker volume on first run).
  Used in CI golden tests + ad-hoc parameter sweeps. No broker
  dependency.
* **`paper-ibkr`** — paper trading against IBKR's paper account via the
  `ib_gateway` sidecar container (Pivot-PR-B). Connects to
  `ib_gateway:4004` (the externally-published socat port; the internal
  gateway listens on 127.0.0.1:4002 inside the container — see
  `deploy/ibkr/README.md` Step 4). Use this for the 30-CME-session paper
  clock before live cutover (Week 8).
* **`live-ibkr`** — live trading against IBKR's live account via
  `ib_gateway`. Connects to `ib_gateway:4003` (the externally-published
  socat port; internal gateway on 127.0.0.1:4001). Production env after
  the Week 8 cutover ceremony.

Environment selection is done at container-start via the `LEAN_LIVE_MODE`
env var (set in `deploy/.env`). The `lean_local` container's entrypoint
resolves sops secrets → IBKR credentials → renders `lean.json` → launches
LEAN's Launcher.

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
| Live (paper-ibkr) login fails | IBKR paper credentials missing or wrong port | See `deploy/ibkr/README.md` (Pivot-PR-B) — `ib_gateway` must boot healthy + accept the TWS API session BEFORE `lean_local` is started |
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
