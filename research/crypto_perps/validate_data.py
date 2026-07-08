"""Sanity checks on the agent-fetched OHLCV CSVs. Run before any backtest."""
import glob
import os
import sys

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def validate(symbol: str) -> pd.DataFrame:
    parts = sorted(glob.glob(os.path.join(DATA_DIR, f"{symbol}usd_part*.csv")))
    df = pd.concat([pd.read_csv(p) for p in parts], ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    dupes = df["date"].duplicated().sum()
    df = df.drop_duplicates("date").sort_values("date").set_index("date")

    problems = []
    gaps = df.index.to_series().diff().dt.days.dropna()
    big_gaps = gaps[gaps > 1]
    if len(big_gaps):
        problems.append(f"{len(big_gaps)} calendar gaps, max {big_gaps.max():.0f}d at {big_gaps.idxmax().date()}")
    bad_hl = ((df.high < df[["open", "close"]].max(axis=1) - 1e-9)
              | (df.low > df[["open", "close"]].min(axis=1) + 1e-9)).sum()
    if bad_hl:
        problems.append(f"{bad_hl} rows violate high/low vs open/close consistency")
    nonpos = (df[["open", "high", "low", "close"]] <= 0).any(axis=1).sum()
    if nonpos:
        problems.append(f"{nonpos} rows with non-positive prices")
    r = np.log(df.close / df.close.shift(1)).dropna()
    extreme = r[abs(r) > 0.60]
    if len(extreme):
        problems.append(f"{len(extreme)} daily |log return| > 60%: {[str(d.date()) for d in extreme.index]}")

    print(f"{symbol.upper()}: {len(df)} rows {df.index[0].date()}..{df.index[-1].date()}, dupes_dropped={dupes}")
    print(f"  close min={df.close.min():.2f} max={df.close.max():.2f}; "
          f"ann vol={r.std() * np.sqrt(365):.1%}")
    for p in problems:
        print(f"  WARN: {p}")
    return df


if __name__ == "__main__":
    ok = True
    for sym in ("btc", "eth"):
        validate(sym)
    sys.exit(0 if ok else 1)
