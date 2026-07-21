"""v2 exploration backtest — Donchian + volume candidates (PREREGISTRATION.md).

Copy of the incumbent engine (research/crypto_perps/backtest.py, sections 4-7
semantics, identical cost/funding model) extended with feature switches that are
all OFF by default. With every switch off, this engine must reproduce the
incumbent `base` scenario exactly (parity gate) — checked at startup against the
committed research/crypto_perps/results.json.

v2 deltas vs the incumbent engine, each marked "V2:" inline:
  - load_asset keeps the volume column
  - optional 4th ensemble member s_d (Donchian latch or midline), TrendScore/4,
    score 0 -> flat
  - optional relative-volume entry confirmation (entries/increases only)
  - optional relative-volume size scaling (clipped, applied before risk caps)

Run:  python3 research/crypto_perps_v2/backtest_v2.py
Research-only. Nothing here touches the live system.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import dataclass, replace
from typing import Any

import numpy as np
import pandas as pd

HERE = os.path.dirname(__file__)
DATA_DIR = os.path.join(HERE, "..", "crypto_perps", "data")
INCUMBENT_RESULTS = os.path.join(HERE, "..", "crypto_perps", "results.json")
RESULTS_PATH = os.path.join(HERE, "results_v2.json")

ASSETS = ("BTC", "ETH")
CONTRACT_MULT = {"BTC": 0.01, "ETH": 0.10}
DAYS_YEAR = 365.0


@dataclass(frozen=True)
class Params:
    # S1 trend ensemble
    sma_fast: int = 100
    sma_slow: int = 200
    mom_lb: int = 20
    hysteresis_days: int = 2
    hysteresis_hold: bool = False
    # S2 vol estimate
    ewma_lambda: float = 0.94
    vol_floor: float = 0.20
    vol_cap: float = 1.50
    # S3 vol-regime filter
    volratio_slow_window: int = 90
    volratio_block: float = 2.0
    volratio_resume: float = 1.5
    # S4 funding gate
    funding_long_veto: float = 0.30
    funding_short_gate: float = -0.10
    # sizing (section 6)
    v_target: float = 0.40
    w_btc: float = 2.0 / 3.0
    per_asset_cap: float = 1.4
    gross_cap: float = 2.0
    deadband_abs: float = 200.0
    deadband_frac: float = 0.05
    band_edge_rebalance: bool = False
    cash_yield_ann: float = 0.0
    margin_frac: float = 0.25
    subscription_usd_month: float = 0.0
    # stops (section 5)
    atr_window: int = 14
    client_stop_atr: float = 2.0
    lockout_days: int = 2
    # risk framework (section 7)
    per_trade_risk_frac: float = 0.025
    daily_loss_limit: float = -0.04
    weekly_loss_limit: float = -0.08
    weekly_penalty_days: int = 7
    halt_frac: float = 0.50
    dd_tiers: tuple[tuple[float, float], ...] = ((0.10, 1.0), (0.20, 0.6), (0.35, 0.35), (9.9, 0.2))
    eth_min_price: float = 2000.0
    # costs (section 1.5) — per SIDE
    fee_bps: float = 5.0
    slip_bps: float = 4.0
    min_fee_per_contract: float = 0.20
    cost_mult: float = 1.0
    funding_ann: float = 0.1095
    initial_equity: float = 6000.0
    # ---- V2 feature switches (all off => incumbent-identical) ----
    don_mode: str = ""  # "" | "latch" | "mid"  (4th ensemble member)
    don_n: int = 55  # prior-day channel lookback
    volc_enabled: bool = False  # relative-volume entry confirmation
    volc_thresh: float = 1.25
    volc_window: int = 20
    vols_enabled: bool = False  # relative-volume size scaling
    vols_clip: tuple[float, float] = (0.7, 1.3)
    vols_fast: int = 5
    vols_slow: int = 60


def load_asset(symbol: str) -> pd.DataFrame:
    parts = []
    for i in (1, 2, 3):
        path = os.path.join(DATA_DIR, f"{symbol.lower()}usd_part{i}.csv")
        if os.path.exists(path):
            parts.append(pd.read_csv(path))
    df = pd.concat(parts, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates(subset="date").sort_values("date").set_index("date")
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="raise")
    return df[["open", "high", "low", "close", "volume"]]  # V2: keep volume


def compute_indicators(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    out = df.copy()
    c = out["close"]
    out["ret"] = np.log(c / c.shift(1))
    out["sma_fast"] = c.rolling(p.sma_fast).mean()
    out["sma_slow"] = c.rolling(p.sma_slow).mean()
    out["mom"] = out["ret"].rolling(p.mom_lb).sum()

    r = out["ret"].fillna(0.0).to_numpy()
    var = np.full(len(r), np.nan)
    seed_n = 30
    if len(r) > seed_n:
        v = float(np.nanvar(r[1 : seed_n + 1]))
        var[seed_n] = v
        lam = p.ewma_lambda
        for i in range(seed_n + 1, len(r)):
            v = lam * v + (1 - lam) * r[i] ** 2
            var[i] = v
    out["sigma_ann_raw"] = np.sqrt(var) * math.sqrt(DAYS_YEAR)
    out["sigma_ann"] = out["sigma_ann_raw"].clip(p.vol_floor, p.vol_cap)

    out["vol_slow"] = out["ret"].rolling(p.volratio_slow_window).std() * math.sqrt(DAYS_YEAR)
    out["vol_ratio"] = out["sigma_ann_raw"] / out["vol_slow"]

    hl = out["high"] - out["low"]
    hc = (out["high"] - c.shift(1)).abs()
    lc = (out["low"] - c.shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    out["atr"] = tr.ewm(alpha=1.0 / p.atr_window, adjust=False).mean()
    out["atrp"] = out["atr"] / c

    s_a = np.where(c > out["sma_fast"], 1.0, -1.0)
    s_b = np.where(c > out["sma_slow"], 1.0, -1.0)
    s_c = np.where(out["mom"] > 0, 1.0, -1.0)

    # V2: Donchian 4th ensemble member on the PRIOR don_n days' high/low channel
    nan_mask = out[["sma_slow", "mom", "sigma_ann", "vol_slow", "atr"]].isna().any(axis=1)
    if p.don_mode:
        ch_hi = out["high"].shift(1).rolling(p.don_n).max()
        ch_lo = out["low"].shift(1).rolling(p.don_n).min()
        cl = c.to_numpy()
        hi = ch_hi.to_numpy()
        lo = ch_lo.to_numpy()
        s_d = np.full(len(cl), np.nan)
        if p.don_mode == "mid":
            valid = ~np.isnan(hi)
            s_d[valid] = np.where(cl[valid] > (hi[valid] + lo[valid]) / 2.0, 1.0, -1.0)
        elif p.don_mode == "latch":
            cur = np.nan
            for i in range(len(cl)):
                if np.isnan(hi[i]):
                    continue
                if np.isnan(cur):  # seed at first valid bar: midline comparison
                    cur = 1.0 if cl[i] > (hi[i] + lo[i]) / 2.0 else -1.0
                if cl[i] > hi[i]:
                    cur = 1.0
                elif cl[i] < lo[i]:
                    cur = -1.0
                s_d[i] = cur
        else:
            raise ValueError(f"unknown don_mode {p.don_mode!r}")
        out["trend"] = (s_a + s_b + s_c + s_d) / 4.0
        nan_mask = nan_mask | pd.Series(np.isnan(s_d), index=out.index)
    else:
        out["trend"] = (s_a + s_b + s_c) / 3.0
    out.loc[nan_mask, "trend"] = np.nan

    # V2: relative-volume series (entry confirmation + size scaling)
    if p.volc_enabled:
        out["rel_vol"] = out["volume"] / out["volume"].rolling(p.volc_window).median()
    if p.vols_enabled:
        lo_clip, hi_clip = p.vols_clip
        out["size_mult"] = (
            out["volume"].rolling(p.vols_fast).mean() / out["volume"].rolling(p.vols_slow).median()
        ).clip(lo_clip, hi_clip)
    return out


@dataclass
class AssetState:
    contracts: float = 0.0
    entry_vwap: float = 0.0
    stop_level: float = np.nan
    applied_dir: int = 0
    pending_dir: int = 0
    pending_count: int = 0
    lockout_until: int = -1
    lockout_dir: int = 0
    vol_blocked: bool = False
    stopped_today: bool = False


def trade_cost(delta_contracts: float, price: float, mult: float, p: Params) -> float:
    n = abs(delta_contracts)
    if n == 0:
        return 0.0
    notional = n * mult * price
    fee = max(p.fee_bps / 1e4 * notional, p.min_fee_per_contract * n)
    slip = p.slip_bps / 1e4 * notional
    return (fee + slip) * p.cost_mult


def run_backtest(data: dict[str, pd.DataFrame], p: Params, start: str, end: str) -> dict[str, Any]:
    idx = data["BTC"].index
    for sym in ASSETS[1:]:
        idx = idx.union(data[sym].index)
    idx = idx[(idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end))]

    frames = {s: data[s].reindex(idx) for s in ASSETS}
    E = p.initial_equity
    hwm = E
    halted = False
    pause_until = -1
    weekly_mult_until = -1
    states = {s: AssetState() for s in ASSETS}

    equity_curve = []
    trades = []
    total_costs = 0.0
    total_funding = 0.0
    gross_expo = []

    equity_prev_close = E
    week_equity = []

    for i, ts in enumerate(idx):
        if halted:
            equity_curve.append((ts, E))
            continue
        day_realized = 0.0

        # ---- 1) intraday stop checks ----
        for s in ASSETS:
            st = states[s]
            st.stopped_today = False
            row = frames[s].loc[ts]
            if st.contracts == 0 or np.isnan(row["low"]) or np.isnan(st.stop_level):
                continue
            mult = CONTRACT_MULT[s]
            prev_close = frames[s]["close"].iloc[i - 1] if i > 0 else row["open"]
            hit = None
            if st.contracts > 0 and row["low"] <= st.stop_level:
                hit = min(row["open"], st.stop_level)
            elif st.contracts < 0 and row["high"] >= st.stop_level:
                hit = max(row["open"], st.stop_level)
            if hit is not None:
                pnl = st.contracts * mult * (hit - prev_close)
                cost = trade_cost(st.contracts, hit, mult, p)
                E += pnl - cost
                day_realized += pnl - cost
                total_costs += cost
                trades.append((str(ts.date()), s, -st.contracts, hit, cost, "stop"))
                st.lockout_until = i + p.lockout_days
                st.lockout_dir = 1 if st.contracts > 0 else -1
                st.contracts = 0.0
                st.stop_level = np.nan
                st.applied_dir = 0
                st.pending_dir = 0
                st.pending_count = 0
                st.stopped_today = True

        # ---- 2) mark-to-market at close + funding ----
        for s in ASSETS:
            st = states[s]
            row = frames[s].loc[ts]
            if st.contracts == 0 or np.isnan(row["close"]):
                continue
            mult = CONTRACT_MULT[s]
            prev_close = frames[s]["close"].iloc[i - 1] if i > 0 else row["open"]
            E += st.contracts * mult * (row["close"] - prev_close)
            f = st.contracts * mult * row["close"] * (p.funding_ann / DAYS_YEAR)
            E -= f
            total_funding += f

        # ---- 2b) yield on idle cash ----
        if p.cash_yield_ann > 0:
            gross_held = sum(
                abs(states[s].contracts) * CONTRACT_MULT[s] * frames[s]["close"].loc[ts]
                for s in ASSETS
                if not np.isnan(frames[s]["close"].loc[ts])
            )
            E += (p.cash_yield_ann / DAYS_YEAR) * max(0.0, E - p.margin_frac * gross_held)
            E -= p.subscription_usd_month * 12.0 / DAYS_YEAR

        # ---- 3) hard halt / bust ----
        if (p.halt_frac > 0 and E <= p.halt_frac * p.initial_equity) or E <= 0:
            for s in ASSETS:
                st = states[s]
                row = frames[s].loc[ts]
                if st.contracts != 0 and not np.isnan(row["close"]):
                    cost = trade_cost(st.contracts, row["close"], CONTRACT_MULT[s], p)
                    E -= cost
                    total_costs += cost
                    trades.append((str(ts.date()), s, -st.contracts, row["close"], cost, "halt"))
                    st.contracts = 0.0
            halted = True
            equity_curve.append((ts, E))
            continue

        # ---- 4) daily loss limit ----
        skip_decision = i <= pause_until
        if (E / equity_prev_close - 1.0) < p.daily_loss_limit:
            for s in ASSETS:
                st = states[s]
                row = frames[s].loc[ts]
                if st.contracts != 0 and not np.isnan(row["close"]):
                    cost = trade_cost(st.contracts, row["close"], CONTRACT_MULT[s], p)
                    E -= cost
                    total_costs += cost
                    trades.append(
                        (str(ts.date()), s, -st.contracts, row["close"], cost, "daily_loss")
                    )
                    st.contracts = 0.0
                    st.stop_level = np.nan
                    st.applied_dir = 0
            pause_until = i + 1
            skip_decision = True

        # ---- 5) weekly loss limit / drawdown tier / HWM ----
        week_equity.append(E)
        if len(week_equity) > 8:
            week_equity.pop(0)
        if len(week_equity) == 8 and (E / week_equity[0] - 1.0) < p.weekly_loss_limit:
            weekly_mult_until = i + p.weekly_penalty_days
        hwm = max(hwm, E)
        dd = 1.0 - E / hwm
        dd_mult = next(m for lvl, m in p.dd_tiers if dd <= lvl)
        v_target = p.v_target * (0.5 if i <= weekly_mult_until else 1.0)

        # ---- 6) decision at close ----
        if not skip_decision:
            targets = {}
            for s in ASSETS:
                st = states[s]
                row = frames[s].loc[ts]
                mult = CONTRACT_MULT[s]
                if np.isnan(row.get("trend", np.nan)):
                    targets[s] = st.contracts
                    continue

                vr = row["vol_ratio"]
                if not np.isnan(vr):
                    if st.vol_blocked and vr < p.volratio_resume:
                        st.vol_blocked = False
                    elif not st.vol_blocked and vr > p.volratio_block:
                        st.vol_blocked = True

                # V2: a 4-member ensemble can score exactly 0 -> flat immediately
                # (section 4: "flat is entered when TrendScore crosses 0 without
                # confirmation"); direction state resets so re-entry needs
                # fresh hysteresis confirmation.
                if p.don_mode and row["trend"] == 0.0:
                    st.applied_dir = 0
                    st.pending_dir = 0
                    st.pending_count = 0
                    targets[s] = 0.0
                    continue

                raw_dir = 1 if row["trend"] > 0 else -1
                if st.stopped_today:
                    targets[s] = 0.0
                    continue
                if raw_dir != st.applied_dir:
                    if raw_dir == st.pending_dir:
                        st.pending_count += 1
                    else:
                        st.pending_dir = raw_dir
                        st.pending_count = 1
                    if st.pending_count >= p.hysteresis_days:
                        st.applied_dir = raw_dir
                        st.pending_dir = 0
                        st.pending_count = 0
                    else:
                        targets[s] = st.contracts if p.hysteresis_hold else 0.0
                        continue
                else:
                    st.pending_dir = 0
                    st.pending_count = 0

                d = st.applied_dir
                # short gate: full-strength bearish score required (3- or 4-member)
                if d < 0 and (row["trend"] > -0.99 or p.funding_ann < p.funding_short_gate):
                    targets[s] = 0.0
                    continue
                funding_mult = 0.5 if (d > 0 and p.funding_ann > p.funding_long_veto) else 1.0
                if s == "ETH" and row["close"] < p.eth_min_price:
                    targets[s] = 0.0
                    continue
                if i < states[s].lockout_until and d == st.lockout_dir:
                    targets[s] = st.contracts if (st.contracts != 0) else 0.0
                    continue

                strength = abs(row["trend"])
                w = p.w_btc if s == "BTC" else 1.0 - p.w_btc
                notional = E * v_target * w * strength / row["sigma_ann"] * dd_mult * funding_mult
                # V2: relative-volume size scaling (before risk caps; caps win)
                if p.vols_enabled and not np.isnan(row.get("size_mult", np.nan)):
                    notional *= row["size_mult"]
                max_notional = p.per_trade_risk_frac * E / (p.client_stop_atr * row["atrp"])
                notional = min(notional, max_notional, p.per_asset_cap * E)
                n = math.floor(notional / (mult * row["close"]) + 0.5) * d
                if st.vol_blocked:
                    cur = st.contracts
                    half = math.floor(abs(cur) / 2.0) * (1 if cur > 0 else -1)
                    n = half if cur != 0 else 0.0
                targets[s] = float(n)

                # V2: relative-volume entry confirmation — unconfirmed bars defer
                # entries/increases (exits and reductions are never blocked; a
                # flip's exit leg executes, its entry leg waits for confirmation)
                if p.volc_enabled:
                    rv = row.get("rel_vol", np.nan)
                    if not np.isnan(rv) and rv < p.volc_thresh:
                        cur = st.contracts
                        tgt = targets[s]
                        if cur == 0 or tgt * cur < 0:
                            targets[s] = 0.0
                        elif abs(tgt) > abs(cur):
                            targets[s] = cur

            gross = sum(
                abs(targets[s]) * CONTRACT_MULT[s] * frames[s]["close"].loc[ts]
                for s in ASSETS
                if not np.isnan(frames[s]["close"].loc[ts])
            )
            if gross > p.gross_cap * E and gross > 0:
                scale = p.gross_cap * E / gross
                for s in ASSETS:
                    t = targets[s] * scale
                    targets[s] = float(math.floor(abs(t)) * (1 if t > 0 else -1))

            for s in ASSETS:
                st = states[s]
                row = frames[s].loc[ts]
                if np.isnan(row["close"]):
                    continue
                mult = CONTRACT_MULT[s]
                delta = targets[s] - st.contracts
                if delta == 0:
                    continue
                is_exit = targets[s] == 0 or st.contracts == 0 or (targets[s] * st.contracts < 0)
                band_notional = max(p.deadband_abs, p.deadband_frac * E)
                if not is_exit and abs(delta) * mult * row["close"] < band_notional:
                    continue
                if not is_exit and p.band_edge_rebalance:
                    band_n = math.floor(band_notional / (mult * row["close"]))
                    if band_n >= 1:
                        edge = targets[s] - band_n if delta > 0 else targets[s] + band_n
                        if (delta > 0 and edge > st.contracts) or (
                            delta < 0 and edge < st.contracts
                        ):
                            delta = edge - st.contracts
                cost = trade_cost(delta, row["close"], mult, p)
                E -= cost
                total_costs += cost
                trades.append((str(ts.date()), s, delta, row["close"], cost, "signal"))
                new_pos = st.contracts + delta
                if new_pos != 0 and (
                    st.contracts == 0
                    or st.contracts * new_pos < 0
                    or abs(new_pos) > abs(st.contracts)
                ):
                    if st.contracts * new_pos <= 0:
                        st.entry_vwap = row["close"]
                    else:
                        add = abs(delta)
                        st.entry_vwap = (
                            st.entry_vwap * abs(st.contracts) + row["close"] * add
                        ) / abs(new_pos)
                    sign = 1 if new_pos > 0 else -1
                    st.stop_level = st.entry_vwap * (1 - sign * p.client_stop_atr * row["atrp"])
                st.contracts = new_pos
                if new_pos == 0:
                    st.stop_level = np.nan

        gross_now = sum(
            abs(states[s].contracts) * CONTRACT_MULT[s] * frames[s]["close"].loc[ts]
            for s in ASSETS
            if not np.isnan(frames[s]["close"].loc[ts])
        )
        gross_expo.append(gross_now / E if E > 0 else 0.0)
        assert gross_now <= p.gross_cap * E * 1.10 + 1e-6, f"leverage cap breach at {ts}"
        equity_prev_close = E
        equity_curve.append((ts, E))

    curve = pd.Series({ts: e for ts, e in equity_curve}).sort_index()
    return summarize(curve, trades, total_costs, total_funding, gross_expo, halted, p)


def summarize(
    curve: pd.Series,
    trades: list[tuple[str, str, float, float, float, str]],
    total_costs: float,
    total_funding: float,
    gross_expo: list[float],
    halted: bool,
    p: Params,
) -> dict[str, Any]:
    rets = curve.pct_change().dropna()

    def sharpe(x: pd.Series) -> float:
        return (
            float(x.mean() / x.std() * math.sqrt(DAYS_YEAR)) if len(x) > 1 and x.std() > 0 else 0.0
        )

    def maxdd(c: pd.Series) -> float:
        return float((1 - c / c.cummax()).max())

    years = sorted(set(curve.index.year))
    per_year: dict[str, dict[str, float]] = {}
    for y in years:
        c = curve[curve.index.year == y]
        r = c.pct_change().dropna()
        if len(r) < 30:
            continue
        per_year[str(y)] = {
            "return": float(c.iloc[-1] / c.iloc[0] - 1),
            "sharpe": round(sharpe(r), 3),
            "maxdd": round(maxdd(c), 4),
        }
    sub = curve[curve.index >= "2023-01-01"]
    n_trades = len(trades)
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t[5]] = reasons.get(t[5], 0) + 1
    yrs_span = (curve.index[-1] - curve.index[0]).days / DAYS_YEAR
    return {
        "final_equity": round(float(curve.iloc[-1]), 2),
        "total_return": round(float(curve.iloc[-1] / curve.iloc[0] - 1), 4),
        "cagr": round(float((curve.iloc[-1] / curve.iloc[0]) ** (1 / yrs_span) - 1), 4),
        "sharpe": round(sharpe(rets), 3),
        "maxdd": round(maxdd(curve), 4),
        "sharpe_2023on": round(sharpe(sub.pct_change().dropna()), 3),
        "return_2023on": round(float(sub.iloc[-1] / sub.iloc[0] - 1), 4) if len(sub) else None,
        "worst_year_dd": round(max(v["maxdd"] for v in per_year.values()), 4),
        "per_year": per_year,
        "n_position_changes": n_trades,
        "trade_reasons": reasons,
        "trades_per_year": round(n_trades / yrs_span, 1),
        "total_costs": round(total_costs, 2),
        "total_funding_paid": round(total_funding, 2),
        "avg_gross_leverage": round(float(np.mean(gross_expo)), 3),
        "max_gross_leverage": round(float(np.max(gross_expo)), 3),
        "pct_days_in_market": round(float(np.mean(np.array(gross_expo) > 0)), 3),
        "halted": halted,
    }


def indicators_for(params: Params) -> dict[str, pd.DataFrame]:
    return {s: compute_indicators(load_asset(s), params) for s in ASSETS}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--end", default="2026-06-30")
    args = ap.parse_args()

    base = Params()

    # ---- PARITY GATE (PREREGISTRATION.md): features off must reproduce the
    # incumbent base scenario exactly, or every variant number below is void.
    parity = run_backtest(indicators_for(base), base, args.start, args.end)
    with open(INCUMBENT_RESULTS) as f:
        incumbent_base = json.load(f)["base"]
    for k in ("final_equity", "sharpe", "n_position_changes", "total_costs", "maxdd"):
        assert parity[k] == incumbent_base[k], (
            f"PARITY FAIL on {k}: v2={parity[k]} incumbent={incumbent_base[k]}"
        )
    print(
        f"parity gate PASS: v2 engine (features off) == incumbent base "
        f"(final ${parity['final_equity']}, sharpe {parity['sharpe']})"
    )

    # ---- pre-registered variant matrix ----
    variants: dict[str, Params] = {
        "D1_don_latch": replace(base, don_mode="latch"),
        "D2_don_mid": replace(base, don_mode="mid"),
        "V1_vol_confirm": replace(base, volc_enabled=True),
        "V2_vol_scale": replace(base, vols_enabled=True),
        "D1+V1": replace(base, don_mode="latch", volc_enabled=True),
        "D1+V2": replace(base, don_mode="latch", vols_enabled=True),
        "D2+V1": replace(base, don_mode="mid", volc_enabled=True),
        "D2+V2": replace(base, don_mode="mid", vols_enabled=True),
    }
    # pre-registered ±20% perturbations of the NEW parameters only
    perts: dict[str, Params] = {
        "D1_n44": replace(base, don_mode="latch", don_n=44),
        "D1_n66": replace(base, don_mode="latch", don_n=66),
        "D2_n44": replace(base, don_mode="mid", don_n=44),
        "D2_n66": replace(base, don_mode="mid", don_n=66),
        "V1_th1.00": replace(base, volc_enabled=True, volc_thresh=1.00),
        "V1_th1.50": replace(base, volc_enabled=True, volc_thresh=1.50),
        "V1_w16": replace(base, volc_enabled=True, volc_window=16),
        "V1_w24": replace(base, volc_enabled=True, volc_window=24),
        "V2_clip_tight": replace(base, vols_enabled=True, vols_clip=(0.76, 1.24)),
        "V2_clip_wide": replace(base, vols_enabled=True, vols_clip=(0.64, 1.36)),
        "V2_w_short": replace(base, vols_enabled=True, vols_fast=4, vols_slow=48),
        "V2_w_long": replace(base, vols_enabled=True, vols_fast=6, vols_slow=72),
    }

    results: dict[str, dict[str, Any]] = {"incumbent_base": parity}
    results["incumbent_base_2xcost"] = run_backtest(
        indicators_for(base), replace(base, cost_mult=2.0), args.start, args.end
    )
    all_runs = {
        **variants,
        **{f"{k}_2xcost": replace(v, cost_mult=2.0) for k, v in variants.items()},
        **perts,
    }
    for name, params in all_runs.items():
        results[name] = run_backtest(indicators_for(params), params, args.start, args.end)
        r = results[name]
        print(
            f"{name:24s} ret={r['total_return']:+8.1%} cagr={r['cagr']:+7.1%} "
            f"sharpe={r['sharpe']:+5.2f} dd={r['maxdd']:6.1%} "
            f"s23={r['sharpe_2023on']:+5.2f} trades/yr={r['trades_per_year']:5.1f} "
            f"costs=${r['total_costs']:8.0f} halted={r['halted']}"
        )

    # ---- pre-registered pass/fail evaluation (P1-P6) ----
    inc = results["incumbent_base"]
    inc2x = results["incumbent_base_2xcost"]
    pert_map = {
        "D1_don_latch": ["D1_n44", "D1_n66"],
        "D2_don_mid": ["D2_n44", "D2_n66"],
        "V1_vol_confirm": ["V1_th1.00", "V1_th1.50", "V1_w16", "V1_w24"],
        "V2_vol_scale": ["V2_clip_tight", "V2_clip_wide", "V2_w_short", "V2_w_long"],
    }
    verdicts: dict[str, dict[str, Any]] = {}
    print("\nPre-registered criteria (ALL must pass => PROMISING):")
    for name in variants:
        r = results[name]
        r2x = results[f"{name}_2xcost"]
        # combos inherit the perturbation sets of both parents
        pnames = [
            pn
            for single, pl in pert_map.items()
            for pn in pl
            if name == single or single.split("_")[0] in name.split("+")
        ]
        p_ok = (
            all(
                results[pn]["total_return"] > 0 and abs(results[pn]["sharpe"] - r["sharpe"]) <= 0.15
                for pn in pnames
            )
            if pnames
            else True
        )
        checks = {
            "P1_sharpe_bar": bool(r["sharpe"] >= inc["sharpe"] + 0.05),
            "P2_2xcost": bool(r2x["sharpe"] >= inc2x["sharpe"]),
            "P3_2023on": bool(r["sharpe_2023on"] >= inc["sharpe_2023on"]),
            "P4_worst_year_dd": bool(r["worst_year_dd"] <= 0.40),
            "P5_perturbations": bool(p_ok),
            "P6_costs": bool(r["total_costs"] <= 1.5 * inc["total_costs"]),
        }
        verdict = "PROMISING" if all(checks.values()) else "NOT ADOPTED"
        verdicts[name] = {"checks": checks, "verdict": verdict}
        failed = [k for k, v in checks.items() if not v]
        print(
            f"  {name:16s} {verdict:12s} "
            + (f"failed: {', '.join(failed)}" if failed else "all pass")
        )

    results["_verdicts"] = verdicts
    with open(RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nresults -> {RESULTS_PATH}")


if __name__ == "__main__":
    main()
