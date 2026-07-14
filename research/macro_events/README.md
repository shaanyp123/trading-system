# `research/macro_events/` — Tier-1 macro-event awareness study (research only)

Backtest study of scheduled-event (CPI / FOMC / NFP) entry-window filters on
the crypto-perps strategy, motivated by the 2026-07-14 incident (short opened
at 00:05 UTC, cold CPI print ~12.4 h later was on the public calendar but
invisible to the system). **No production code — the only consumers are the
operator's decision and, if directed, a later proposal doc.**

The reference backtest in `research/crypto_perps/` is the LOCKED authority.
Nothing here modifies it; the variant engine reproduces it exactly (parity
gate) before any filtered run, and variants are studies, never a new baseline.

| File | Purpose |
|---|---|
| `REPORT.md` | **Study report + operator decision prompt — read this first** |
| `build_calendar.py` | FMP economics-calendar dumps → `data/tier1_calendar.csv` |
| `data/tier1_calendar.csv` | 522 scheduled US release instances, 2016-06→2026-07, UTC (committed for reproducibility) |
| `variant_backtest.py` | Parity gate + entry-filter variant suite (`python3 variant_backtest.py`) |
| `results_variants.json` | Variant metrics from the last run |

Requires `pandas`/`numpy`/`structlog` (all in the repo venv).
