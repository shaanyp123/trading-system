"""Parse a LEAN backtest output dir → the SHARED ``BacktestResult`` (+ trades).

This is a PARSER, not a second result type (design §4.1 note on
``research/eval/results.py``): it normalizes raw LEAN JSON into the same
:class:`research.eval.results.BacktestResult` the numpy screen produces, so
``research/eval/compare.py`` + the §6.6 parity rail compare apples-to-apples.

**Tolerant by construction.** LEAN's result JSON shape drifts across releases and
the operator's ``quantconnect/lean:latest`` image may differ from any single
documented schema, so the parser:

* finds the result file by CONTENT (a ``Statistics``/``Charts``/``TotalPerformance``
  signature), never by a guessed filename — ``lean backtest`` writes to
  ``<project>/backtests/<ts>/`` with a version-dependent name;
* reads the equity curve from ``Charts → "Strategy Equity" → Series → "Equity" →
  Values``, accepting both the line-series ``{"x","y"}`` and the candlestick
  ``{"x","open","high","low","close"}`` point shapes (LEAN switched the equity
  series to candlesticks in a later release);
* accepts both PascalCase and camelCase keys and both int- and string-encoded
  enums (``Direction``, ``Status``).

**Decimal at the §6.6 boundary (design D8):** trade prices / P&L / fees are parsed
as ``Decimal`` for the parity comparison. The float equity curve stays float for
numpy interop in ``BacktestResult`` (same exemption the numpy screen uses).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import numpy as np
import numpy.typing as npt
import structlog

from research.data.contract_specs import SPECS
from research.eval.results import BacktestResult

_log = structlog.get_logger(__name__)

#: Files in a LEAN output dir that are never the main result document.
_NON_RESULT_SUFFIXES = ("-order-events.json", "-data-monitor-report.json")
#: A parsed JSON object is "the result" if it carries any of these top-level keys.
_RESULT_SIGNATURE = ("Statistics", "statistics", "Charts", "charts", "TotalPerformance")


@dataclass(frozen=True, slots=True)
class LeanTrade:
    """One closed trade, money fields as ``Decimal`` (§6.6 comparison boundary)."""

    entry_time: datetime
    entry_price: Decimal
    exit_time: datetime
    exit_price: Decimal
    quantity: int  # absolute contract count (sign carried by ``direction``)
    direction: int  # +1 long, -1 short
    profit_loss: Decimal
    fees: Decimal


@dataclass(frozen=True, slots=True)
class OrderFill:
    """One filled order — the bridge from a LEAN result to entry decisions.

    ``market`` is the normalized symbol (futures carry a leading slash, e.g.
    ``/MES``; ETFs are bare). ``quantity`` is signed; ``direction`` is +1/-1.
    """

    market: str
    fill_date: date
    quantity: int
    direction: int


@dataclass(frozen=True, slots=True)
class ParsedLeanResult:
    """The normalized ``BacktestResult`` plus the raw trades + LEAN statistics."""

    result: BacktestResult
    trades: tuple[LeanTrade, ...]
    fills: tuple[OrderFill, ...]
    statistics: dict[str, str]


# --------------------------------------------------------------------------- #
# tolerant accessors
# --------------------------------------------------------------------------- #
def _get(obj: object, *names: str, default: object = None) -> object:
    """First present key among ``names`` (handles PascalCase/camelCase variants)."""
    if not isinstance(obj, dict):
        return default
    for name in names:
        if name in obj:
            return obj[name]
    return default


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _as_decimal(value: object, default: str = "0") -> Decimal:
    if isinstance(value, bool):
        return Decimal(default)
    if isinstance(value, (int, float, str)):
        try:
            return Decimal(str(value))
        except (ValueError, ArithmeticError):
            return Decimal(default)
    return Decimal(default)


def _unix_to_datetime(x: float) -> datetime:
    """LEAN chart ``x`` is unix seconds (sometimes ms); normalize to aware UTC."""
    seconds = x / 1000.0 if x > 1.0e12 else x
    return datetime.fromtimestamp(seconds, tz=UTC)


def _parse_datetime(value: object) -> datetime:
    """Parse a LEAN ISO timestamp; assume UTC if naive. Falls back to epoch."""
    if isinstance(value, (int, float)):
        return _unix_to_datetime(float(value))
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return datetime.fromtimestamp(0, tz=UTC)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return datetime.fromtimestamp(0, tz=UTC)


def _normalize_direction(value: object) -> int:
    """``Direction`` → +1 (long) / -1 (short). Accepts int enum or string."""
    if isinstance(value, bool):
        return 1
    if isinstance(value, (int, float)):
        # LEAN TradeDirection: Long=0, Short=1.
        return -1 if int(value) == 1 else 1
    if isinstance(value, str):
        return -1 if value.strip().lower() in ("short", "sell", "1") else 1
    return 1


# --------------------------------------------------------------------------- #
# result-file discovery
# --------------------------------------------------------------------------- #
def find_result_json(output_dir: Path) -> Path:
    """Locate the main result JSON in a LEAN output dir by CONTENT, not filename.

    ``lean backtest`` (and the raw Launcher) write a version-dependent filename;
    we scan for the JSON carrying a result signature, skipping the order-events /
    data-monitor side files. Picks the largest match (the result doc dwarfs config).
    """
    if not output_dir.exists():
        raise FileNotFoundError(f"LEAN output dir does not exist: {output_dir}")
    candidates: list[tuple[int, Path]] = []
    for path in sorted(output_dir.rglob("*.json")):
        name = path.name
        if any(name.endswith(suffix) for suffix in _NON_RESULT_SUFFIXES):
            continue
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if isinstance(obj, dict) and any(key in obj for key in _RESULT_SIGNATURE):
            candidates.append((path.stat().st_size, path))
    if not candidates:
        raise FileNotFoundError(
            f"no LEAN result JSON found under {output_dir} "
            f"(looked for a top-level {_RESULT_SIGNATURE} key)"
        )
    candidates.sort()
    return candidates[-1][1]


# --------------------------------------------------------------------------- #
# section parsers
# --------------------------------------------------------------------------- #
def _equity_series_values(result_obj: dict[str, object]) -> list[object]:
    """Extract the equity ``Values`` list from the Strategy Equity chart."""
    charts = _get(result_obj, "Charts", "charts", default={})
    chart = _get(charts, "Strategy Equity", "StrategyEquity", default=None)
    if chart is None and isinstance(charts, dict):
        # Fallback: any chart whose name mentions "Equity".
        for key, value in charts.items():
            if isinstance(key, str) and "equity" in key.lower():
                chart = value
                break
    series = _get(chart, "Series", "series", default={})
    equity = _get(series, "Equity", "equity", default=None)
    if equity is None and isinstance(series, list):
        for item in series:
            if str(_get(item, "Name", "name", default="")).lower() == "equity":
                equity = item
                break
    values = _get(equity, "Values", "values", default=[])
    return list(values) if isinstance(values, list) else []


def _parse_equity_curve(
    result_obj: dict[str, object],
) -> tuple[tuple[date, ...], npt.NDArray[np.float64]]:
    """Return (dates, equity_curve) from the Strategy Equity chart points."""
    points: list[tuple[float, float]] = []
    for raw in _equity_series_values(result_obj):
        if not isinstance(raw, dict):
            continue
        x = _get(raw, "x", "Time", "time")
        if x is None:
            continue
        y = _get(raw, "y", "close", "Close")  # line-series y, else candlestick close
        if y is None:
            continue
        points.append((_as_float(x), _as_float(y)))
    if not points:
        raise ValueError(
            "LEAN result has no Strategy Equity points — cannot build an equity curve"
        )
    points.sort(key=lambda p: p[0])
    dates = tuple(_unix_to_datetime(x).date() for x, _ in points)
    equity = np.asarray([v for _, v in points], dtype=np.float64)
    return dates, equity


def _parse_trades(result_obj: dict[str, object]) -> tuple[LeanTrade, ...]:
    """Parse ``TotalPerformance.ClosedTrades`` (empty if the strategy never exits)."""
    perf = _get(result_obj, "TotalPerformance", "totalPerformance", default={})
    closed = _get(perf, "ClosedTrades", "closedTrades", default=[])
    if not isinstance(closed, list):
        return ()
    trades: list[LeanTrade] = []
    for raw in closed:
        if not isinstance(raw, dict):
            continue
        qty = _as_decimal(_get(raw, "Quantity", "quantity"))
        trades.append(
            LeanTrade(
                entry_time=_parse_datetime(_get(raw, "EntryTime", "entryTime")),
                entry_price=_as_decimal(_get(raw, "EntryPrice", "entryPrice")),
                exit_time=_parse_datetime(_get(raw, "ExitTime", "exitTime")),
                exit_price=_as_decimal(_get(raw, "ExitPrice", "exitPrice")),
                quantity=abs(int(qty)),
                direction=_normalize_direction(_get(raw, "Direction", "direction")),
                profit_loss=_as_decimal(_get(raw, "ProfitLoss", "profitLoss")),
                fees=_as_decimal(_get(raw, "TotalFees", "totalFees")),
            )
        )
    return tuple(trades)


def _parse_statistics(result_obj: dict[str, object]) -> dict[str, str]:
    stats = _get(result_obj, "Statistics", "statistics", default={})
    if not isinstance(stats, dict):
        return {}
    return {str(k): str(v) for k, v in stats.items()}


def _orders_iter(result_obj: dict[str, object]) -> list[object]:
    orders = _get(result_obj, "Orders", "orders", default={})
    if isinstance(orders, dict):
        return list(orders.values())
    if isinstance(orders, list):
        return orders
    return []


# Futures roots (no slash) from the contract-spec universe — used to re-attach the
# leading slash to a LEAN order symbol so it matches the prod ``signals.market``.
_FUTURES_ROOTS = frozenset(
    spec.symbol.lstrip("/").upper() for spec in SPECS.values() if spec.asset_class == "future"
)


def _normalize_market(symbol_field: object) -> str:
    """LEAN order ``Symbol`` → prod ``signals.market`` form (``/MES``, ``TLT``)."""
    value = _get(symbol_field, "Value", "value", default=symbol_field)
    text = str(value).strip()
    root = text.split(" ", 1)[0].lstrip("/").upper()  # "MES YQB1..." / "/MES" → "MES"
    return f"/{root}" if root in _FUTURES_ROOTS else root


def _is_filled(status: object) -> bool:
    # LEAN OrderStatus.Filled=3, PartiallyFilled=2; accept string forms too.
    if isinstance(status, bool):
        return False
    if isinstance(status, (int, float)):
        return int(status) in (2, 3)
    if isinstance(status, str):
        return status.strip().lower() in ("filled", "partiallyfilled")
    return False


def parse_filled_orders(result_obj: dict[str, object]) -> tuple[OrderFill, ...]:
    """All FILLED orders as :class:`OrderFill` (the reproduce-V1 decision bridge)."""
    fills: list[OrderFill] = []
    for raw in _orders_iter(result_obj):
        if not isinstance(raw, dict) or not _is_filled(_get(raw, "Status", "status")):
            continue
        qty_raw = _get(raw, "Quantity", "quantity", "FillQuantity", "fillQuantity")
        when = _get(raw, "LastFillTime", "lastFillTime", "Time", "time", "CreatedTime")
        if qty_raw is None or when is None:
            continue
        qty = int(round(_as_float(qty_raw)))
        if qty == 0:
            continue
        fills.append(
            OrderFill(
                market=_normalize_market(_get(raw, "Symbol", "symbol")),
                fill_date=_parse_datetime(when).date(),
                quantity=qty,
                direction=1 if qty > 0 else -1,
            )
        )
    fills.sort(key=lambda f: (f.fill_date, f.market))
    return tuple(fills)


def load_result_object(output_dir: Path) -> dict[str, object]:
    """Find + load the LEAN result JSON from an output dir as a dict."""
    obj = json.loads(find_result_json(output_dir).read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError(f"{output_dir}: top-level LEAN result is not a JSON object")
    return obj


def _positions_from_fills(
    fills: tuple[OrderFill, ...], dates: tuple[date, ...]
) -> npt.NDArray[np.int64]:
    """Best-effort per-bar position from cumulative FILLED order quantity.

    Single-instrument backtests only (the harness evaluates one instrument per
    ``BacktestResult``); fills are summed across symbols. No fills ⇒ zeros: the §6.6
    parity criteria key off equity P&L + trades, not this series, so an approximate
    positions vector never corrupts a parity verdict.
    """
    positions = np.zeros(len(dates), dtype=np.int64)
    if not fills:
        return positions
    cumulative = 0
    fi = 0
    for i, day in enumerate(dates):
        while fi < len(fills) and fills[fi].fill_date <= day:
            cumulative += fills[fi].quantity
            fi += 1
        positions[i] = cumulative
    return positions


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
def parse_lean_result(
    output_dir: Path,
    *,
    symbol: str,
    multiplier: float,
    strategy_name: str,
    starting_cash: float | None = None,
    fill: str = "lean",
) -> ParsedLeanResult:
    """Parse a LEAN output dir into the SHARED :class:`BacktestResult` + trades.

    ``symbol`` / ``multiplier`` / ``strategy_name`` are run metadata LEAN's output
    does not carry; the driver supplies them. ``starting_cash`` defaults to the
    first equity point. ``fill="lean"`` marks the result as LEAN-fidelity (vs the
    numpy screen's ``"close"``) so a report never silently mixes the two.
    """
    result_path = find_result_json(output_dir)
    obj = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError(f"{result_path}: top-level LEAN result is not a JSON object")

    dates, equity = _parse_equity_curve(obj)
    fills = parse_filled_orders(obj)
    positions = _positions_from_fills(fills, dates)
    trades = _parse_trades(obj)
    statistics = _parse_statistics(obj)
    start_cash = float(starting_cash) if starting_cash is not None else float(equity[0])

    result = BacktestResult(
        symbol=symbol,
        dates=dates,
        equity_curve=equity,
        positions=positions,
        starting_cash=start_cash,
        multiplier=float(multiplier),
        fill=fill,
        strategy_name=strategy_name,
    )
    _log.info(
        "research_lean_result_parsed",
        result_file=str(result_path),
        symbol=symbol,
        bars=len(dates),
        trades=len(trades),
        fills=len(fills),
        final_equity=round(result.final_equity, 2),
    )
    return ParsedLeanResult(result=result, trades=trades, fills=fills, statistics=statistics)
