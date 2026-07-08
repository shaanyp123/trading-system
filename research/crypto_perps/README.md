# `research/crypto_perps/` — standalone backtest for the crypto-perps pivot

Validates the strategy in `Docs/crypto-perps-strategy.md` (Coinbase CFM US
perpetual-style futures, vol-targeted trend on BTC/ETH) per its §9 backtest
plan. **Deliberately standalone** — it does not use the LEAN-based harness in
`research/` (that harness is CME-futures/LEAN-specific, and the pivot retires
LEAN), and it touches no `services/**` code.

| File | Purpose |
|---|---|
| `backtest.py` | Engine + scenario suite + §9 falsification verdicts (`python3 backtest.py`) |
| `validate_data.py` | Data sanity checks — run before any backtest |
| `data/*.csv` | Daily OHLCV, FMP `BTCUSD`/`ETHUSD`, 2016-06-01→2026-07-07 (committed for reproducibility) |
| `results.json` | Scenario metrics from the last run |
| `REPORT.md` | **Validation report + verdict — read this first** |

Requires `pandas`/`numpy` only.
