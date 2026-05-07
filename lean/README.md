# `lean/` — QuantConnect LEAN Project

The QC algorithm wrapper for `v1_trend_following`. Phase 1 architecture: this
algorithm is the only thing that talks to a broker; signals + broker state cross
to the backend via QC ObjectStore (no direct IBKR connection — see
`CLAUDE.md` and `Docs/backend-spec.md` §1).

| File | Purpose |
|---|---|
| `v1_qc_algorithm.py` | `QCAlgorithm` subclass. Daily resolution, 17:30 ET signal cycle, IBKR brokerage model, parameter map via `self.get_parameter`. Day 4 = paper-broker ready, heartbeat-only `on_daily_signal_cycle`. Strategy wiring lands Week 4. Uses QC's snake_case Python API (per `Docs/decisions-log.md` 2026-05-06 — QC migrated from PascalCase). |
| `lean.json` | LEAN project config. QC Cloud reads `algorithm-language` + `algorithm-type-name` + `parameters`. LEAN Local reads the `environments` map (`live-paper-qc` for paper trading; `live-paper-ibkr` is reserved for Phase 2). |

---

## Day 4 10:00 — Operator runbook: upload algorithm + start QC paper trading

This is a one-time manual flow in QC's web UI. **Claude Code cannot do this for
you.** Each numbered step is a single click or paste. Total time: ~10 min.

### Pre-flight (do this first)

- [ ] You are logged into [quantconnect.com](https://www.quantconnect.com) with
      the operator account.
- [ ] Your QC subscription is on the **Researcher** tier or higher (1 live
      trading node is required; the free tier has 0). See the Day 1 entry in
      `Docs/decisions-log.md`.
- [ ] You have `lean/v1_qc_algorithm.py` and `lean/lean.json` in front of you
      (or open in another tab on github.com).

### Step 1 — Create the QC project

1. Top nav → **Algorithm Lab**.
2. Click **+ New Project** (top-left of the project list).
3. Name: `v1_trend_following_paper`. Language: **Python**. Click **Create**.

### Step 2 — Upload `v1_qc_algorithm.py`

The new project ships with a `main.py` template. Replace it with our algorithm:

1. In the file tree on the left, click `main.py` to open it.
2. Right-click `main.py` → **Rename** → enter `v1_qc_algorithm.py` → **Save**.
3. Open `lean/v1_qc_algorithm.py` from this repo (github.com or your laptop)
   and copy the entire file contents.
4. Paste into the QC editor for `v1_qc_algorithm.py`. **Replace the entire
   file** — including the boilerplate QC put there.
5. Click the **Save** disk icon (or `Cmd+S` / `Ctrl+S`).

### Step 3 — Set the parameter map

QC's parameter map is project-level, not file-level. The values mirror
`lean/lean.json` `parameters` block; we have to enter them by hand because QC
Cloud doesn't read `lean.json` from a GitHub-mirrored repo.

1. In the project, click the **gear icon** (top-right of the editor) →
   **Parameters**.
2. For each row in the table below, click **+ Add Parameter** and enter the
   key/value as listed.

| Key                            | Value     |
|--------------------------------|-----------|
| `LOOKBACK_DAYS_DONCHIAN`       | `60`      |
| `MA_FAST_DAYS`                 | `50`      |
| `MA_SLOW_DAYS`                 | `200`     |
| `HURST_THRESHOLD`              | `0.55`    |
| `STOP_DISTANCE_ATR_MULT`       | `3.0`     |
| `ATR_LOOKBACK_DAYS`            | `20`      |
| `MIN_HOLDING_DAYS`             | `14`      |
| `VOL_TARGET_PCT_ANNUAL`        | `0.15`    |
| `INSTRUMENT_VOL_LOOKBACK_DAYS` | `60`      |
| `ROLL_DAYS_BEFORE_EXPIRY`      | `5`       |
| `STARTING_CASH_USD`            | `15000`   |

3. **Save** the parameter map (button at the bottom of the panel).

> If you typo a value, the algorithm falls back to its `V1_PARAMETER_DEFAULTS`
> hardcoded values (same as the table above). The Initialize log line at
> startup shows the effective `params_keys` — you can spot-check there.

### Step 4 — Quick smoke backtest (optional but recommended)

Before going live-paper, run a short backtest to confirm the algorithm boots:

1. Top of editor → **Build** button. Wait for "Build successful". If it errors,
   stop — fix the upload before proceeding.
2. Top of editor → **Backtest** dropdown → **Backtest**. Default window
   (the start/end dates set in `Initialize`) will run. ~30 seconds.
3. When complete, scroll to the **Logs** tab.
   - Look for: `v1_trend_following algorithm initialized (skeleton; live_mode=False; ...)`
   - Look for: ≥ 1 line of `signal_cycle_tick utc=... et=...` (note: backtest mode plays through warmup instantly, so the first tick lands ~200 sessions in; live mode waits real-time for warmup to complete)
4. If both lines appear → proceed. If not → check **Errors** tab; common cause
   is a missed parameter key in Step 3.

### Step 5 — Deploy to live paper trading

This is the step that starts the **paper-day clock** (30 CME-session counter).

1. Top of editor → **Live Trading** dropdown → **Deploy Live Algorithm**.
2. **Brokerage**: select **Quant Connect Paper Trading** (recommended for
   Phase 0 — no IBKR dependency). Do NOT select Interactive Brokers Paper
   yet; IBKR Pro is still pending and the IBKR-Paper option requires linking
   a real IBKR username.
3. **Node type**: the smallest live node available on the Researcher tier
   (typically "Live - Paper - L-MICRO"). Confirm 1 live node free; the
   Researcher tier has exactly 1.
4. **Algorithm parameters**: the parameters from Step 3 prefill here.
   Leave them as-is.
5. **Notifications**: optional. Discord webhook for `#critical` is configured
   on the backend, not in QC; skip QC's email/SMS notifications.
6. Click **Deploy**.

### Step 6 — Confirm "Running"

1. Top nav → **Live Trading** → find `v1_trend_following_paper` in the list.
2. Status pill should read **Running** (green) within ~30 seconds.
3. Click into the algorithm. Tabs at the top:
   - **Logs** → confirm `algorithm initialized` line appears
   - **Charts** → may be blank until first daily 17:30 ET tick fires
4. **Note the start timestamp shown on the Live Trading dashboard** — write it
   down. This is the Phase 1 paper-day clock start. Day 1 of the 30-CME-session
   counter starts at the next CME settlement (17:00 ET) after this timestamp.

### Step 7 — Capture artifacts for the audit trail

Per `Docs/decisions-log.md` discipline, log the deploy in the operator's notes:

- **QC Project ID**: visible in the URL when the project is open
  (`/algorithm-lab/projects/<ID>/`)
- **QC Live Algorithm ID**: visible in the URL when the live algorithm is open
  (`/live/<ID>/`)
- **Deploy timestamp** (UTC): from Step 6.
- **Broker**: `Quant Connect Paper Trading`
- **Starting cash**: `$15,000` (matches `STARTING_CASH_USD` parameter)

Paste these four values into a Day 4 close-out entry in `Docs/decisions-log.md`
when you next have a Claude Code session open.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Build errors like `"Resolution" has no attribute "Daily"` or `"V1TrendFollowingAlgorithm" has no attribute "SetStartDate"` | The pasted file is from a pre-2026-05-06 commit (PascalCase API). QC migrated its Python API to snake_case sometime ~2024 and the cloud editor's analyzer rejects PascalCase calls. | Re-copy the file from `main` (post-PR-#17). Method names are snake_case (`set_start_date`, `add_future`, `get_parameter`); enum values are SCREAMING_SNAKE (`Resolution.DAILY`, `DataMappingMode.OPEN_INTEREST`); class names stay PascalCase (`QCAlgorithm`, `Slice`). |
| Build error: `NameError: name 'AlgorithmImports' is not defined` | The first line `from AlgorithmImports import *` was deleted on paste | Re-copy the full file from the repo; ensure the import line is the first non-comment line |
| Build error: `cannot import V1TrendFollowing` | Strategy module wiring is enabled but Week 4 hasn't shipped yet | Confirm the `from v1_trend_following...` lines are still commented out (they should be on Day 4) |
| Live deploy: "No live nodes available" | Researcher tier's 1 live node is in use by another algorithm | Stop the other algorithm in Live Trading dashboard, or upgrade tier |
| Status flips from Running → Error | Often a parameter type mismatch | Check Errors tab; the algorithm reads parameters as strings then casts. A non-numeric value (e.g., `"sixty"`) crashes `int(...)` |
| `signal_cycle_tick` log never appears | Algorithm is still in warmup (200 daily bars ≈ 200 calendar days for futures) | Wait until QC's data feed has played through the warmup window. Backtest mode plays warmup instantly; live mode waits real-time |
| Algorithm boots but `initialize` never seems to run (no startup log line, no scheduled actions firing) | Method name typo — QC dispatches to `initialize` (snake_case). A method named `Initialize` (PascalCase) is silently ignored and the parent's no-op default runs instead. | Rename `def Initialize(self):` → `def initialize(self):`. Same for `OnData` → `on_data`. |

---

## Cross-references

- Strategy logic source-of-truth: `strategies/v1_trend_following/` (pure Python,
  unit-testable, no QC imports).
- Phase 1 architecture: `Docs/backend-spec.md` §1 (data flow), §2.10 (QC adapter),
  §1.6 (no direct IBKR).
- Parameter semantics + agent tighten directions: `Docs/backend-spec.md` §12.3
  + `strategies/v1_trend_following/parameters.py` `V1_DEFAULTS`.
- Day 4 implementation context: `implementation-guide.md` §11 Day 4.
- Phase 1 sub-universe lock: `Docs/decisions-log.md` 2026-05-05 entry.
- Why we use IBKR brokerage model in backtest but QC Paper in live: see
  `v1_qc_algorithm.py` Initialize docstring.
