"""``python -m research.run --config <path>`` — the P1 run entrypoint.

Loads a run config, evaluates each instrument on the daily spine, and writes an
operator-legible report to ``research/runs/<UTC-ts>/``. Daily/backtest/``daily``
engine only in P1; other engines/resolutions/modes fail with a clear,
phase-pointing message (LEAN engine → P2, ``minute`` → P5, ``forward`` → P7).

House rule: no ``print`` / stdlib logging — structlog only (dev-guide §3.5).
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

import structlog

from research import __version__
from research.config.schema import RunConfig, load_run_config
from research.data.contract_specs import get_spec
from research.data.daily_loader import load_daily_series
from research.eval.metrics import summarize
from research.eval.report import ReportRow, write_report
from research.screen.daily_eval import evaluate_daily
from research.strategy.buy_and_hold import BuyAndHold
from research.strategy.contract import ResearchStrategy

_DEFAULT_RUNS_DIR = Path("research/runs")
_log = structlog.get_logger("research.run")


def _build_strategy(cfg: RunConfig) -> ResearchStrategy:
    if cfg.strategy_ref == "buy_and_hold":
        return BuyAndHold(cfg.contracts)
    raise ValueError(f"unknown strategy.ref {cfg.strategy_ref!r}")


def _guard_phase(cfg: RunConfig) -> None:
    """Reject not-yet-built combinations with a clear pointer to their phase."""
    if cfg.resolution != "daily":
        raise NotImplementedError(
            f"resolution={cfg.resolution!r}: intraday is deferred (P5/P6, 2026-06-03 "
            "sign-off). P1 is daily only."
        )
    if cfg.mode != "backtest":
        raise NotImplementedError(f"mode={cfg.mode!r}: live/paper-forward is P7.")
    if cfg.engine == "lean":
        raise NotImplementedError("engine='lean': the LEAN driver lands in P2.")
    if cfg.engine == "vbt":
        raise NotImplementedError(
            "engine='vbt': the vectorbt sweep lands in P4. Use engine='daily' for the "
            "numpy evaluator."
        )
    if cfg.engine != "daily":
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
