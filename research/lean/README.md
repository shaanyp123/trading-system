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
