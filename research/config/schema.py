"""``RunConfig`` — the parsed, validated representation of a run YAML.

Forward-compatible: P1 consumes ``name / engine / mode / resolution / universe /
date_range / strategy / data_root / expiries``; later phases add sizing,
leverage, costs, and validity blocks to the same schema. Unknown engines /
resolutions are accepted at parse time but rejected with a clear, phase-pointing
error at run time (e.g. ``lean`` → P2, ``minute`` → P5).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Final

import yaml

#: Engines known to the schema. Only ``daily`` runs in P1.
KNOWN_ENGINES: Final = ("daily", "vbt", "lean")
#: Resolutions known to the schema. Only ``daily`` runs now (intraday deferred).
KNOWN_RESOLUTIONS: Final = ("daily", "minute", "tick")
#: Strategy refs the registry can build (P1 buy-and-hold + P2/P3 Donchian +
#: the v1_adapter, which replays the production V1 logic — backtest "our strategy").
KNOWN_STRATEGIES: Final = ("buy_and_hold", "donchian", "v1_adapter")

_DEFAULT_DATA_ROOT_ENV = "RESEARCH_DATA_ROOT"
_DEFAULT_DATA_ROOT = "research/data/cache/lean_bars"


@dataclass(frozen=True, slots=True)
class RunConfig:
    """A single validated research run."""

    name: str
    engine: str
    mode: str
    resolution: str
    universe: tuple[str, ...]
    start: date
    end: date | None
    strategy_ref: str
    contracts: int
    starting_cash: float | None
    data_root: Path
    #: Per-future expiry (``YYYYMM``) for P1 single-expiry loads; futures absent
    #: here must have exactly one expiry on disk or the load errors.
    expiries: dict[str, str]
    #: Donchian channel length (``strategy.channel``); ``None`` for buy-and-hold.
    channel: int | None = None
    #: P3 sizing scheme (``sizing.scheme``) + its params (everything else under
    #: ``sizing``, as floats). ``None`` ⇒ no leverage sweep (P1/P2 fixed path).
    sizing_scheme: str | None = None
    sizing_params: dict[str, float] = field(default_factory=dict)
    #: P3 leverage cap + optional sweep (``leverage.cap`` / ``leverage.sweep``).
    #: ``leverage_sweep`` non-empty (or ``sizing_scheme`` set) routes to the sweep.
    leverage_cap: float | None = None
    leverage_sweep: tuple[float, ...] = ()
    #: Cost model (``costs.model``) — ``ibkr`` (realistic IBKR commission/slippage,
    #: the default) or ``zero`` (clean comparison). Consumed by the LEAN engine path.
    costs_model: str = "ibkr"
    #: P4 validity / walk-forward (``validity.scheme`` = ``walk_forward`` + integer
    #: ``is_months`` / ``oos_months`` / optional ``step_months``). ``validity_scheme``
    #: set ⇒ the run is an OOS-ranked sweep, not a single backtest.
    validity_scheme: str | None = None
    is_months: int | None = None
    oos_months: int | None = None
    step_months: int | None = None
    #: P4 parameter grid (``strategy.sweep``: ``{param: [values]}``) swept per combo,
    #: and the comparison ``benchmarks`` (strategy refs, e.g. ``buy_and_hold``).
    strategy_sweep: dict[str, tuple[float, ...]] = field(default_factory=dict)
    benchmarks: tuple[str, ...] = ()


def _as_str(value: object, ctx: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{ctx}: expected a string, got {type(value).__name__}")
    return value


def _as_date(value: object, ctx: str) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ValueError(f"{ctx}: expected a date (YYYY-MM-DD), got {type(value).__name__}")


def _require_mapping(value: object, ctx: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{ctx}: expected a mapping, got {type(value).__name__}")
    return {str(k): v for k, v in value.items()}


def parse_run_config(raw: dict[str, object]) -> RunConfig:
    """Build + validate a :class:`RunConfig` from a parsed YAML mapping."""
    name = _as_str(raw.get("name", ""), "name")
    if not name:
        raise ValueError("run config requires a non-empty 'name'")

    engine = _as_str(raw.get("engine", "daily"), "engine")
    if engine not in KNOWN_ENGINES:
        raise ValueError(f"engine={engine!r} not in {KNOWN_ENGINES}")
    mode = _as_str(raw.get("mode", "backtest"), "mode")
    if mode not in ("backtest", "forward"):
        raise ValueError(f"mode={mode!r} not in ('backtest', 'forward')")
    resolution = _as_str(raw.get("resolution", "daily"), "resolution")
    if resolution not in KNOWN_RESOLUTIONS:
        raise ValueError(f"resolution={resolution!r} not in {KNOWN_RESOLUTIONS}")

    universe_raw = raw.get("universe")
    if not isinstance(universe_raw, list) or not universe_raw:
        raise ValueError("'universe' must be a non-empty list of symbols")
    universe = tuple(_as_str(s, "universe[]") for s in universe_raw)

    date_range = _require_mapping(raw.get("date_range", {}), "date_range")
    if "start" not in date_range:
        raise ValueError("date_range requires 'start'")
    start = _as_date(date_range["start"], "date_range.start")
    end = _as_date(date_range["end"], "date_range.end") if date_range.get("end") else None
    if end is not None and end < start:
        raise ValueError(f"date_range.end {end} precedes start {start}")

    strategy = _require_mapping(raw.get("strategy", {}), "strategy")
    strategy_ref = _as_str(strategy.get("ref", "buy_and_hold"), "strategy.ref")
    if strategy_ref not in KNOWN_STRATEGIES:
        raise ValueError(f"strategy.ref={strategy_ref!r} not in {KNOWN_STRATEGIES}")
    contracts_raw = strategy.get("contracts", 1)
    # NB: bool is an int subclass — reject it explicitly so `contracts: true`
    # doesn't silently become 1.
    if isinstance(contracts_raw, bool) or not isinstance(contracts_raw, int) or contracts_raw == 0:
        raise ValueError(f"strategy.contracts must be a non-zero int, got {contracts_raw!r}")
    channel_raw = strategy.get("channel")
    channel = (
        int(channel_raw)
        if isinstance(channel_raw, int) and not isinstance(channel_raw, bool)
        else None
    )

    # P4 parameter grid (``strategy.sweep: {param: [values]}``) — absent for non-sweep runs.
    sweep_grid_raw = strategy.get("sweep", {})
    strategy_sweep: dict[str, tuple[float, ...]] = {}
    if sweep_grid_raw:
        grid = _require_mapping(sweep_grid_raw, "strategy.sweep")
        for key, values in grid.items():
            if not isinstance(values, list) or not values:
                raise ValueError(f"strategy.sweep.{key} must be a non-empty list of values")
            nums = tuple(
                float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)
            )
            if not nums:
                raise ValueError(f"strategy.sweep.{key} must contain numbers")
            strategy_sweep[key] = nums

    # P3 sizing + leverage blocks (absent for P1/P2 fixed runs).
    sizing = _require_mapping(raw.get("sizing", {}), "sizing")
    sizing_scheme = _as_str(sizing["scheme"], "sizing.scheme") if "scheme" in sizing else None
    sizing_params = {
        k: float(v)
        for k, v in sizing.items()
        if k != "scheme" and isinstance(v, (int, float)) and not isinstance(v, bool)
    }
    leverage = _require_mapping(raw.get("leverage", {}), "leverage")
    cap_raw = leverage.get("cap")
    leverage_cap = (
        float(cap_raw)
        if isinstance(cap_raw, (int, float)) and not isinstance(cap_raw, bool)
        else None
    )
    sweep_raw = leverage.get("sweep", [])
    if not isinstance(sweep_raw, list):
        raise ValueError("leverage.sweep must be a list of leverage caps")
    leverage_sweep = tuple(
        float(x) for x in sweep_raw if isinstance(x, (int, float)) and not isinstance(x, bool)
    )

    costs = _require_mapping(raw.get("costs", {}), "costs")
    costs_model = _as_str(costs.get("model", "ibkr"), "costs.model")
    if costs_model not in ("ibkr", "zero"):
        raise ValueError(f"costs.model={costs_model!r} not in ('ibkr', 'zero')")

    # P4 validity (walk-forward) block + comparison benchmarks (absent otherwise).
    validity = _require_mapping(raw.get("validity", {}), "validity")
    validity_scheme = (
        _as_str(validity["scheme"], "validity.scheme") if "scheme" in validity else None
    )
    if validity_scheme is not None and validity_scheme != "walk_forward":
        raise ValueError(f"validity.scheme={validity_scheme!r} not in ('walk_forward',)")

    def _opt_int(mapping: dict[str, object], key: str) -> int | None:
        value = mapping.get(key)
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else None

    is_months = _opt_int(validity, "is_months")
    oos_months = _opt_int(validity, "oos_months")
    step_months = _opt_int(validity, "step_months")

    benchmarks_raw = raw.get("benchmarks", [])
    if not isinstance(benchmarks_raw, list):
        raise ValueError("benchmarks must be a list of strategy refs")
    benchmarks = tuple(_as_str(b, "benchmarks[]") for b in benchmarks_raw)

    starting_cash_raw = raw.get("starting_cash")
    starting_cash = (
        float(starting_cash_raw) if isinstance(starting_cash_raw, (int, float)) else None
    )

    data_root_raw = raw.get("data_root") or os.environ.get(
        _DEFAULT_DATA_ROOT_ENV, _DEFAULT_DATA_ROOT
    )
    data_root = Path(_as_str(data_root_raw, "data_root")).expanduser()

    expiries_raw = _require_mapping(raw.get("expiries", {}), "expiries")
    expiries = {k: _as_str(v, f"expiries.{k}") for k, v in expiries_raw.items()}

    return RunConfig(
        name=name,
        engine=engine,
        mode=mode,
        resolution=resolution,
        universe=universe,
        start=start,
        end=end,
        strategy_ref=strategy_ref,
        contracts=contracts_raw,
        starting_cash=starting_cash,
        data_root=data_root,
        expiries=expiries,
        channel=channel,
        sizing_scheme=sizing_scheme,
        sizing_params=sizing_params,
        leverage_cap=leverage_cap,
        leverage_sweep=leverage_sweep,
        costs_model=costs_model,
        validity_scheme=validity_scheme,
        is_months=is_months,
        oos_months=oos_months,
        step_months=step_months,
        strategy_sweep=strategy_sweep,
        benchmarks=benchmarks,
    )


def load_run_config(path: Path) -> RunConfig:
    """Load + validate a run config from a YAML file."""
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")
    return parse_run_config({str(k): v for k, v in raw.items()})
