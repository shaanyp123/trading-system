"""``python -m research.run --config <path>`` — the P1 run entrypoint.

Loads a run config, evaluates each instrument on the daily spine, and writes an
operator-legible report to ``research/runs/<UTC-ts>/``. Daily/backtest/``daily``
engine only in P1; other engines/resolutions/modes fail with a clear,
phase-pointing message (LEAN engine → P2, ``minute`` → P5, ``forward`` → P7).

House rule: no ``print`` / stdlib logging — structlog only (dev-guide §3.5).
"""

from __future__ import annotations

import argparse
import itertools
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import structlog

from research import __version__
from research.config.schema import RunConfig, load_run_config
from research.data.contract_specs import get_spec
from research.data.daily_loader import load_daily_series
from research.eval.compare import ComparisonRow, run_compare
from research.eval.report import (
    LeverageRow,
    ReportRow,
    write_leverage_report,
    write_report,
    write_validity_report,
)
from research.eval.reproduce_v1 import build_v1_run_spec
from research.eval.sweep import Candidate, run_sweep
from research.eval.walk_forward import generate_windows
from research.lean.driver import run_backtest
from research.lean.reference import REFERENCE_STRATEGIES, build_reference_run_spec
from research.lean.results import ParsedLeanResult
from research.risk.liquidation import estimate_intrabar_liquidation, margin_model_for
from research.risk.metrics import compute_risk_metrics, summarize
from research.risk.sizing_schemes import SizingScheme, simulate_sized_path
from research.screen.daily_eval import evaluate_daily
from research.strategy.buy_and_hold import BuyAndHold
from research.strategy.contract import ResearchStrategy
from research.strategy.donchian import DonchianBreakout
from research.strategy.v1_adapter import V1Adapter

_DEFAULT_RUNS_DIR = Path("research/runs")
_DEFAULT_VOL_LOOKBACK = 60
_DEFAULT_ATR_LOOKBACK = 20
_log = structlog.get_logger("research.run")


def _build_strategy(cfg: RunConfig) -> ResearchStrategy:
    if cfg.strategy_ref == "buy_and_hold":
        return BuyAndHold(cfg.contracts)
    if cfg.strategy_ref == "donchian":
        return DonchianBreakout(channel=cfg.channel or 20, contracts=abs(cfg.contracts))
    if cfg.strategy_ref == "v1_adapter":
        # Production V1 with its live default params (replays the real V1 logic).
        return V1Adapter()
    raise ValueError(f"unknown strategy.ref {cfg.strategy_ref!r}")


def _is_leverage_sweep(cfg: RunConfig) -> bool:
    """A run is a P3 leverage sweep when it carries a sizing scheme."""
    return cfg.sizing_scheme is not None


def _guard_phase(cfg: RunConfig) -> None:
    """Reject not-yet-built combinations with a clear pointer to their phase."""
    if cfg.resolution != "daily":
        raise NotImplementedError(
            f"resolution={cfg.resolution!r}: intraday is deferred (P5/P6, 2026-06-03 "
            "sign-off). P1 is daily only."
        )
    if cfg.mode != "backtest":
        raise NotImplementedError(f"mode={cfg.mode!r}: live/paper-forward is P7.")
    if cfg.engine == "vbt":
        raise NotImplementedError(
            "engine='vbt': the vectorbt sweep lands in P4. Use engine='daily' for the "
            "numpy evaluator."
        )
    if cfg.engine not in ("daily", "lean"):
        raise ValueError(f"unsupported engine {cfg.engine!r}")


def run(cfg: RunConfig, *, runs_dir: Path = _DEFAULT_RUNS_DIR) -> Path:
    """Execute a daily run and return the path to the written ``report.html``."""
    _guard_phase(cfg)
    if not cfg.data_root.exists():
        raise FileNotFoundError(
            f"data_root {cfg.data_root} does not exist. Point it at a COPY of the "
            "on-disk LEAN bars (never the live trading_lean_data volume; see design "
            "§4.2)."
        )
    if cfg.engine == "lean":
        return run_lean(cfg, runs_dir=runs_dir)
    if cfg.validity_scheme is not None:
        return run_validity(cfg, runs_dir=runs_dir)
    if _is_leverage_sweep(cfg):
        return run_leverage_sweep(cfg, runs_dir=runs_dir)
    strategy = _build_strategy(cfg)
    rows: list[ReportRow] = []
    for symbol in cfg.universe:
        spec = get_spec(symbol)
        series = load_daily_series(
            cfg.data_root,
            symbol,
            expiry=cfg.expiries.get(symbol),
            start=cfg.start,
            end=cfg.end,
        )
        multiplier = float(spec.multiplier)
        starting_cash = (
            cfg.starting_cash
            if cfg.starting_cash is not None
            else abs(cfg.contracts) * multiplier * float(series.close[0])
        )
        result = evaluate_daily(
            series, strategy, multiplier=multiplier, starting_cash=starting_cash
        )
        rows.append(
            ReportRow(
                symbol=symbol,
                strategy_name=result.strategy_name,
                start=series.start,
                end=series.end,
                bars=len(series),
                start_price=float(series.close[0]),
                end_price=float(series.close[-1]),
                price_treatment=series.price_treatment,
                fill=result.fill,
                metrics=summarize(result),
                equity_curve=result.equity_curve,
            )
        )
        _log.info(
            "research_instrument_evaluated",
            symbol=symbol,
            bars=len(series),
            total_return=round(result.total_return, 6),
        )

    generated_at = datetime.now(UTC)
    run_dir = runs_dir / generated_at.strftime("%Y%m%dT%H%M%SZ")
    html_path = write_report(
        run_dir,
        run_name=cfg.name,
        harness_version=__version__,
        generated_at=generated_at,
        rows=rows,
    )
    _log.info(
        "research_run_complete",
        run_name=cfg.name,
        instruments=len(rows),
        report=str(html_path),
    )
    return html_path


def run_leverage_sweep(cfg: RunConfig, *, runs_dir: Path = _DEFAULT_RUNS_DIR) -> Path:
    """P3 leverage sweep → a ruin report (design §6, §9 P3).

    Single-instrument (the first symbol in ``universe`` — per-instrument is where the
    ruin story lives; multi-instrument portfolio leverage is P4). For each cap in
    ``leverage.sweep`` (or the single ``leverage.cap``) the strategy's per-bar
    DIRECTION is sized by ``sizing.scheme`` under that hard cap, the equity path rides
    through any wipeout, and the daily intrabar estimator + ruin metrics surface the
    liquidation. Writes ``report.html`` with a RED banner when any cap liquidates.
    """
    symbol = cfg.universe[0]
    if len(cfg.universe) > 1:
        _log.warning(
            "research_leverage_sweep_single_instrument",
            swept=symbol,
            ignored=list(cfg.universe[1:]),
            note="leverage sweep is per-instrument; run others separately (portfolio = P4)",
        )
    spec = get_spec(symbol)
    multiplier = float(spec.multiplier)
    series = load_daily_series(
        cfg.data_root, symbol, expiry=cfg.expiries.get(symbol), start=cfg.start, end=cfg.end
    )
    strategy = _build_strategy(cfg)
    directions = np.sign(strategy.target_positions(series)).astype(np.int64)
    assert cfg.sizing_scheme is not None  # guaranteed by _is_leverage_sweep
    scheme = SizingScheme(name=cfg.sizing_scheme, params=cfg.sizing_params)  # type: ignore[arg-type]
    margin = margin_model_for(spec, data_root=cfg.data_root)
    starting_cash = cfg.starting_cash if cfg.starting_cash is not None else 100_000.0
    vol_lookback = int(cfg.sizing_params.get("instrument_vol_lookback_days", _DEFAULT_VOL_LOOKBACK))
    atr_lookback = int(cfg.sizing_params.get("atr_lookback_days", _DEFAULT_ATR_LOOKBACK))
    caps = list(cfg.leverage_sweep) or ([cfg.leverage_cap] if cfg.leverage_cap is not None else [])
    if not caps:
        raise ValueError("a leverage sweep requires leverage.sweep [..] or leverage.cap")

    rows: list[LeverageRow] = []
    for cap in caps:
        sized = simulate_sized_path(
            series,
            directions,
            scheme,
            multiplier=multiplier,
            starting_cash=starting_cash,
            leverage_cap=cap,
            vol_lookback=vol_lookback,
            atr_lookback=atr_lookback,
        )
        estimate = estimate_intrabar_liquidation(sized.result, series, margin)
        first_flag = estimate.first_flag
        metrics = compute_risk_metrics(
            sized.result,
            peak_leverage=sized.leverage.peak,
            mean_leverage=sized.leverage.mean,
            n_margin_events=estimate.n_flagged,
            liquidated=estimate.liquidated or sized.leverage.wiped,
            first_liquidation_date=first_flag.flag_date if first_flag is not None else None,
        )
        rows.append(
            LeverageRow(
                leverage_cap=cap,
                scheme=scheme.name,
                metrics=metrics.to_report_dict(),
                liquidated=metrics.liquidated,
                first_liquidation_date=metrics.first_liquidation_date,
                n_liquidation_flags=estimate.n_flagged,
                bars_evaluated=estimate.bars_evaluated,
                lean_margin_events=0,  # numpy sweep; LEAN-native events arrive via the driver
                residual_uncertainty=estimate.residual_uncertainty,
                leverage_curve=sized.leverage.leverage,
            )
        )
        _log.info(
            "research_leverage_level_evaluated",
            symbol=symbol,
            leverage_cap=cap,
            peak_leverage=round(sized.leverage.peak, 3),
            liquidation_flags=estimate.n_flagged,
            liquidated=metrics.liquidated,
        )

    generated_at = datetime.now(UTC)
    run_dir = runs_dir / generated_at.strftime("%Y%m%dT%H%M%SZ")
    html_path = write_leverage_report(
        run_dir,
        run_name=cfg.name,
        harness_version=__version__,
        generated_at=generated_at,
        symbol=symbol,
        rows=rows,
    )
    _log.info(
        "research_leverage_sweep_complete",
        run_name=cfg.name,
        symbol=symbol,
        levels=len(rows),
        liquidation_detected=any(r.flagged for r in rows),
        report=str(html_path),
    )
    return html_path


def lean_ruin_alert(symbol: str, parsed: ParsedLeanResult) -> str | None:
    """Return a ruin-banner line for ``symbol`` if the LEAN run shows ruin, else ``None``.

    Ruin must be impossible to miss (design §6.2), and there are TWO independent
    signals — the tag check alone is not enough:

    * a LEAN-native margin-call / forced-liquidation order (``parsed.liquidated``,
      detected by order tag); AND
    * the equity curve crossing ``<= $0`` — at high leverage LEAN may simply run the
      account negative and HALT the backtest WITHOUT emitting a tagged margin-call
      order (observed: 50x /MES wiped equity to -$7k with zero tagged events).
    """
    equity = parsed.result.equity_curve
    wiped = bool(equity.size) and bool(np.any(equity <= 0.0))
    if not (parsed.liquidated or wiped):
        return None
    reasons: list[str] = []
    if parsed.margin_events:
        first = parsed.margin_events[0].event_date.isoformat()
        reasons.append(f"{len(parsed.margin_events)} LEAN margin-call order(s) (first {first})")
    if wiped:
        reasons.append(f"equity wiped to ${parsed.result.final_equity:,.0f} (crossed <= $0)")
    return f"{symbol}: " + "; ".join(reasons)


def run_lean(cfg: RunConfig, *, runs_dir: Path = _DEFAULT_RUNS_DIR) -> Path:
    """Authoritative LEAN backtest path (design D1, §4.3) — ``engine: lean``.

    Drives REAL LEAN (the engine of record) over the on-disk bar COPY, isolated
    (``--network none`` + POST stub — :mod:`research.lean.driver`), and normalizes
    the output into the shared report. Reference strategies (``donchian`` /
    ``buy_and_hold``) run the self-contained :class:`ResearchRunnerAlgorithm` over a
    continuous future or ETF, one instrument each; ``v1_adapter`` runs the production
    ``V1TrendFollowingAlgorithm`` as a single portfolio backtest.

    LEAN-native margin-call / forced-liquidation orders surface as a RED report
    banner (design §6.2). Raises :class:`research.lean.images.LeanUnavailableError`
    (actionable) when no LEAN backend is available — the CLI operator sees the
    message; the integration test skips. The numpy screen (``engine: daily``)
    remains the fast, non-authoritative path.
    """
    if cfg.strategy_ref == "v1_adapter":
        # V1 subscribes to its whole universe internally → ONE portfolio backtest.
        # Its window is hard-coded in lean/v1_strategy.py initialize() and is NOT
        # renderable, so date_range is ignored here (reproduce-V1 semantics).
        _log.warning(
            "research_lean_v1_window_fixed",
            note="v1_adapter+lean runs V1's own hard-coded initialize() window; "
            "cfg.date_range is ignored (parameterizing V1's window is a follow-up)",
        )
        specs = [build_v1_run_spec(cfg.data_root)]
    elif cfg.strategy_ref in REFERENCE_STRATEGIES:
        specs = [build_reference_run_spec(cfg, symbol) for symbol in cfg.universe]
    else:
        raise ValueError(
            f"engine='lean' supports {(*REFERENCE_STRATEGIES, 'v1_adapter')}, "
            f"got strategy.ref={cfg.strategy_ref!r}"
        )

    generated_at = datetime.now(UTC)
    run_dir = runs_dir / generated_at.strftime("%Y%m%dT%H%M%SZ")
    rows: list[ReportRow] = []
    alerts: list[str] = []
    for index, spec in enumerate(specs):
        work_dir = run_dir / f"lean_{index:02d}_{spec.symbol.lstrip('/') or 'portfolio'}"
        work_dir.mkdir(parents=True, exist_ok=True)
        # Force the raw-docker (lean_local) backend: it is the ONLY one that mounts
        # our read-only data COPY at /Lean/Data (the CLI backend uses the CLI's own
        # data folder) AND carries the isolation env. So both references and V1 use
        # it — the production-faithful, data-wired path (design §4.3).
        parsed = run_backtest(spec, work_dir=work_dir, backend="docker")
        result = parsed.result
        treatment = (
            "continuous RAW · LEAN fills" if spec.symbol.startswith("/") else "RAW · LEAN fills"
        )
        rows.append(
            ReportRow(
                symbol=spec.symbol,
                strategy_name=result.strategy_name,
                start=result.dates[0],
                end=result.dates[-1],
                bars=len(result.dates),
                # LEAN's result carries the equity curve, not the instrument price
                # series — price columns are not parsed (a possible enhancement).
                start_price=0.0,
                end_price=0.0,
                price_treatment=treatment,
                fill=result.fill,
                metrics=summarize(result),
                equity_curve=result.equity_curve,
            )
        )
        alert = lean_ruin_alert(spec.symbol, parsed)
        if alert is not None:
            alerts.append(alert)
        _log.info(
            "research_lean_instrument_evaluated",
            symbol=spec.symbol,
            bars=len(result.dates),
            total_return=round(result.total_return, 6),
            liquidated=alert is not None,
        )

    html_path = write_report(
        run_dir,
        run_name=cfg.name,
        harness_version=__version__,
        generated_at=generated_at,
        rows=rows,
        alerts=tuple(alerts),
    )
    _log.info(
        "research_lean_run_complete",
        run_name=cfg.name,
        instruments=len(rows),
        liquidation_detected=bool(alerts),
        report=str(html_path),
    )
    return html_path


def _build_swept_strategy(cfg: RunConfig, overrides: dict[str, float]) -> ResearchStrategy:
    """Build the run's strategy with sweep overrides applied (reference strategies only)."""
    if cfg.strategy_ref == "donchian":
        channel = int(overrides.get("channel", cfg.channel or 20))
        return DonchianBreakout(channel=channel, contracts=abs(cfg.contracts))
    if cfg.strategy_ref == "buy_and_hold":
        contracts = int(overrides.get("contracts", cfg.contracts))
        return BuyAndHold(abs(contracts))
    raise ValueError(
        f"strategy.ref={cfg.strategy_ref!r} is not sweepable for validity "
        "(use donchian or buy_and_hold; V1 has no free params on this path)"
    )


def _fmt_param(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _candidate_strategies(cfg: RunConfig) -> list[Candidate]:
    """Cartesian product of ``strategy.sweep`` → labeled candidates (else the base strategy)."""
    if not cfg.strategy_sweep:
        return [Candidate(label=cfg.strategy_ref, strategy=_build_strategy(cfg))]
    keys = sorted(cfg.strategy_sweep)
    candidates: list[Candidate] = []
    for combo in itertools.product(*(cfg.strategy_sweep[k] for k in keys)):
        overrides = dict(zip(keys, combo, strict=True))
        label = ", ".join(f"{k}={_fmt_param(v)}" for k, v in zip(keys, combo, strict=True))
        candidates.append(Candidate(label=label, strategy=_build_swept_strategy(cfg, overrides)))
    return candidates


def _benchmark_candidates(cfg: RunConfig) -> list[Candidate]:
    """Build the comparison benchmark strategies from ``cfg.benchmarks``."""
    contracts = abs(cfg.contracts)
    out: list[Candidate] = []
    for ref in cfg.benchmarks:
        if ref == "buy_and_hold":
            out.append(Candidate("buy_and_hold", BuyAndHold(contracts)))
        elif ref == "donchian":
            channel = cfg.channel or 20
            out.append(Candidate(f"donchian({channel})", DonchianBreakout(channel, contracts)))
        elif ref == "v1_adapter":
            out.append(Candidate("v1_adapter", V1Adapter()))
        else:
            raise ValueError(f"unknown benchmark ref {ref!r}")
    return out


def run_validity(cfg: RunConfig, *, runs_dir: Path = _DEFAULT_RUNS_DIR) -> Path:
    """P4 walk-forward OOS-ranked sweep → a validity report (design §7, §9 P4).

    Sweeps ``strategy.sweep`` over rolling IS/OOS windows on the fast numpy screen
    (NON-authoritative — confirm the winner in LEAN, design D1), ranks combos on the
    out-of-sample metric, surfaces IS→OOS degradation + a multiple-testing reality-
    check, and compares the top candidate against ``benchmarks``. Single-instrument
    (the first symbol; portfolio walk-forward is a later extension).
    """
    if cfg.is_months is None or cfg.oos_months is None:
        raise ValueError("validity needs validity.is_months and validity.oos_months")
    symbol = cfg.universe[0]
    if len(cfg.universe) > 1:
        _log.warning(
            "research_validity_single_instrument",
            evaluated=symbol,
            ignored=list(cfg.universe[1:]),
            note="walk-forward is per-instrument; run others separately",
        )
    spec = get_spec(symbol)
    multiplier = float(spec.multiplier)
    series = load_daily_series(
        cfg.data_root, symbol, expiry=cfg.expiries.get(symbol), start=cfg.start, end=cfg.end
    )
    starting_cash = cfg.starting_cash if cfg.starting_cash is not None else 100_000.0
    windows = generate_windows(
        series.dates,
        is_months=cfg.is_months,
        oos_months=cfg.oos_months,
        step_months=cfg.step_months,
    )
    if not windows:
        raise ValueError(
            f"no walk-forward windows fit {len(series)} bars ({series.start}..{series.end}) "
            f"with is_months={cfg.is_months} + oos_months={cfg.oos_months}. Use a longer "
            "date_range / deeper data or smaller windows (deep multi-year history is the "
            "parents-for-history follow-up)."
        )
    candidates = _candidate_strategies(cfg)
    sweep = run_sweep(
        series, candidates, windows, multiplier=multiplier, starting_cash=starting_cash
    )
    comparison: tuple[ComparisonRow, ...] = ()
    if cfg.benchmarks and sweep.best is not None:
        best = next(c for c in candidates if c.label == sweep.best.label)
        comparison = run_compare(
            series,
            best,
            _benchmark_candidates(cfg),
            multiplier=multiplier,
            starting_cash=starting_cash,
        )
    generated_at = datetime.now(UTC)
    run_dir = runs_dir / generated_at.strftime("%Y%m%dT%H%M%SZ")
    html_path = write_validity_report(
        run_dir,
        run_name=cfg.name,
        harness_version=__version__,
        generated_at=generated_at,
        symbol=symbol,
        sweep=sweep,
        comparison=comparison,
    )
    _log.info(
        "research_validity_complete",
        run_name=cfg.name,
        symbol=symbol,
        n_windows=len(windows),
        n_candidates=len(candidates),
        best=sweep.best.label if sweep.best else None,
        likely_overfit=sweep.likely_overfit,
        report=str(html_path),
    )
    return html_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="research.run", description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="path to a run YAML")
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=_DEFAULT_RUNS_DIR,
        help="output root (default research/runs)",
    )
    args = parser.parse_args(argv)
    cfg = load_run_config(args.config)
    run(cfg, runs_dir=args.runs_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
