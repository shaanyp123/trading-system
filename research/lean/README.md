# `research/lean/` — the LEAN driver (P2)

Drives LEAN (the engine of record, design D1) to run daily backtests and normalizes
the output into the shared `research.eval.results.BacktestResult`. Full design:
`Docs/futures-backtester-design.md` §4.3 + the "P2 landed" subsection.

| Module | Role |
|---|---|
| `images.py` | availability probes (lean CLI exe + Docker daemon — never `importlib`) + invocation backends (LEAN CLI default, raw `docker run` fallback) |
| `config_render.py` | render a THROWAWAY per-run `lean.json` into a temp project dir — never edits production `lean/lean.json` |
| `driver.py` | assemble an ISOLATED run, launch LEAN (skip-gated), collect the output dir |
| `results.py` | tolerant parser: LEAN result JSON → `BacktestResult` (+ trades, fills) |
| `projects/` | LEAN `QCAlgorithm` source the harness drives (e.g. the Donchian parity twin) |

The parity rail (`tests/integration/test_vbt_lean_parity.py`) and the reproduce-V1
cross-check (`research/eval/reproduce_v1.py`) build on these.

## ⚠️ Safety — do not pollute production

`lean/v1_strategy.py` POSTs to `LEAN_LOCAL_API_BASE_URL` (default `http://api:8000`)
on **every** daily cycle. A research V1 backtest is therefore run ISOLATED three
ways (`driver.isolation_env()` + `--network none`): no prod network, an unreachable
`LEAN_LOCAL_API_BASE_URL` stub, and a dummy non-empty `LEAN_LOCAL_BEARER_TOKEN`. The
data mount is ALWAYS a read-only COPY (`research/data/cache/lean_bars`) — the live
`trading_lean_data` volume is never mounted (`driver._guard_data_root`). These are
unit-tested invariants. **Never** run a research V1 backtest joined to the prod
docker network.

## Authoritative V1 P&L — the backtest-only order path (PR #335)

The numpy / vbt screen (research/README.md) is a *daily screen*; LEAN is the
authority for fills, stops, margin, and the equity curve (design D1). PR #335
(2026-06-09, `a10013c`) makes that authoritative run the LIVE strategy itself:

- **Mechanism.** In a research backtest (`not self.live_mode`), `lean/v1_strategy.py`
  places real LEAN **market orders** mirroring its computed per-bar signals, sized by
  the **production Stage 0-5 sizer** (`services/risk/sizing.py`, loaded by file path
  inside the LEAN container — the harness mounts `services/` read-only). Orders fill
  next-bar; the resulting equity curve, drawdown, and margin events are LEAN's, not a
  reconstruction. Reconcile/roll logic lives in `_place_backtest_orders`.
- **Production is byte-for-byte unchanged.** The path is gated by
  `self._backtest_orders_enabled = not bool(self.live_mode)` (set once in
  `initialize`, never mutated). In paper/live V1 stays POST-only and places **ZERO**
  LEAN orders — a live-safety unit test (`tests/unit/test_v1_backtest_orders.py`)
  locks that the order path is provably unreachable when `live_mode` is true.
- **Isolation is identical to the POST path.** The acceptance run is `--network none`,
  POST stub `http://127.0.0.1:9`, dummy non-empty bearer, read-only data COPY — never
  the live `trading_lean_data` volume.

**Acceptance (real engine, isolated container, 2023-09-01 → 2026-06-08):** 1013 bars,
45 fills, 18 closed trades, **+4.10% total return**, Sharpe **−1.00**, max drawdown
**6.30%**, **0 margin events**, NON-FLAT equity **$100k → $104,104**, realized vol
**4.4% annualized**. Driver: `/tmp/v1_acceptance_run.py` (env `V1_START` / `V1_END` /
`V1_TIMEOUT`); run from a worktree with `cwd` AND `PYTHONPATH` set to that worktree.

**Follow-up findings (post-#335, in `Docs/futures-backtester-design.md` charter PR A
follow-up):** the two roll nits were investigated — nit (a) `invested_since`-reset is
*structurally inert* (the MIN_HOLDING gate reads a field LEAN doesn't populate, so it
never engages); nit (b)'s one-bar chain-edge gap root-causes to a price-source artifact
(the mapped front contract reads `.price == 0` for months, so the new leg can't be
ordered on the close bar) — a real but *separate* price-source fix, out of scope for a
roll-handler change. A READ-ONLY vol-deployment diagnostic explains the 4.4% « 15%
target: V1 is flat ~66% of bars (median 1 concurrent position); the binding constraint
is sparse time-in-market × the 25%/name cap at micro-contract granularity, **not** the
portfolio gross 3.0× / net 1.5× caps. Tuning caps upward does nothing for flat days;
the price-source fix (same root cause as nit b) is the higher-leverage lever.

## Operator acceptance (the real-engine gate)

The code + parser + parity-logic + V1 cross-check are CI-green on committed fixtures
WITHOUT Docker/LEAN. The items below need a real engine and are the P2 acceptance
gate; until they run, the integration tests SKIP visibly (never a silent pass).

### 0. Prereqs

```bash
pip install lean            # the CLI (provides the `lean` executable)
# + Docker Desktop running, OR build/tag the production-faithful image:
docker build -t trading-lean-local:latest -f infrastructure/lean_local/Dockerfile .
```

### 1. Snapshot the on-disk bars (a COPY — never the live volume)

```bash
# on the VPS (read-only; doesn't touch running containers):
ssh root@178.156.239.84 'docker run --rm -v trading_lean_data:/src:ro -v /tmp:/out \
  alpine tar czf /out/lean_bars_snapshot.tgz -C /src .'
scp root@178.156.239.84:/tmp/lean_bars_snapshot.tgz /tmp/
mkdir -p research/data/cache/lean_bars
tar xzf /tmp/lean_bars_snapshot.tgz -C research/data/cache/lean_bars
ssh root@178.156.239.84 'rm /tmp/lean_bars_snapshot.tgz'
```

### 2. Run the §6.6 parity rail (Donchian, a clean ETF series)

```bash
make test-integration            # or:
python -m pytest tests/integration/test_vbt_lean_parity.py -m integration -rs
# Knobs (env): RESEARCH_PARITY_SYMBOL (default TLT), RESEARCH_PARITY_CHANNEL (20),
# RESEARCH_PARITY_START/END, RESEARCH_PARITY_CASH.
```

If the per-trade slippage criterion fails on the first real run, tune the fill model
in `projects/donchian_reference.py` (LEAN fills next-open; the numpy screen is
close-to-close) — a one-line change. The aggregate-P&L + trade-count criteria are
robust to that gap.

### 3. Reproduce production V1 + cross-check against the live oracle

Capture the live paper decision oracle (read-only; prints no secrets):

```bash
ssh root@178.156.239.84 'docker exec -i -w /app trading-api-1 python -' <<'PY' > /tmp/v1_oracle.json
import asyncio, json, os
from pathlib import Path
import asyncpg
from services.api import entrypoint
_sec = entrypoint._load_secrets(Path(os.environ.get("API_SECRETS_PATH", str(entrypoint.DEFAULT_SECRETS_PATH))))
_pw = (_sec.get("postgres") or {}).get("app_service_password"); assert isinstance(_pw, str)
_url = entrypoint._build_database_url(_pw).replace("+asyncpg", "")
async def main():
    conn = await asyncpg.connect(_url)
    try:
        rows = await conn.fetch("""
            SELECT to_char(session_date,'YYYY-MM-DD') AS session_date, market,
                   lower(direction) AS direction, left(parameter_set_hash,12) AS phash
            FROM signals WHERE env='paper' AND signal_type='entry'
            ORDER BY session_date, market""")
        print(json.dumps({"env": "paper", "signal_type": "entry",
                          "entries": [dict(r) for r in rows]}, indent=2))
    finally:
        await conn.close()
asyncio.run(main())
PY
```

Then run the reproduce-V1 test, picking a SINGLE-phash sub-window (the
`parameter_set_hash` varies per signal — params calibrated mid-window + the Kaufman
ER gate landed 2026-06-02, so a clean cross-check restricts to one phash):

```bash
RESEARCH_V1_ORACLE=/tmp/v1_oracle.json \
RESEARCH_V1_WINDOW=2026-05-27:2026-05-29 \
python -m pytest tests/integration/test_reproduce_v1.py -m integration -rs
```

Reproduce at the DECISION level (same market + direction + session_date);
`decision_price ≠ backtest fill` is expected (the §6.6 tolerances cover that). When
the real run passes, replace the representative fixtures
(`tests/fixtures/lean_output/v1_repro/` + `tests/fixtures/v1_oracle/`) with the
captured artifacts so the golden test guards the real result.
