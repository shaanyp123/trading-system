"""Reproduce production V1 + cross-check against the live paper signals oracle.

The trust bridge's second proof (design §8): drive LEAN to run the production
``V1TrendFollowingAlgorithm`` on a fixed daily window with the live params, then
check that the harness-captured backtest AGREES, at the DECISION level (same market
+ direction + session_date), with what live paper V1 actually decided — the prod
``signals`` table (``env='paper'``, ``signal_type='entry'``).

Per the P2 kickoff: reproduce at the DECISION level, not the fill — ``decision_price
≠ backtest fill`` is expected (that gap is what the §6.6 tolerances cover), and the
``parameter_set_hash`` varies per signal (params were calibrating + the Kaufman ER
gate landed mid-window), so a clean cross-check picks a SINGLE-phash sub-window via
:func:`crosscheck_entries`'s ``window`` filter.

This module is LEAN-free + pure: it turns parsed :class:`OrderFill`s into entry
DECISIONS and diffs them against an oracle snapshot. The actual LEAN run is driven
by :func:`build_v1_run_spec` + ``research.lean.driver.run_backtest`` (isolated;
skip-gated).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import structlog

from research.lean.config_render import load_production_v1_parameters
from research.lean.driver import LeanRunSpec
from research.lean.results import OrderFill

_log = structlog.get_logger(__name__)

#: Repo paths (read-only inputs) for the production V1 algorithm + its package.
_V1_ALGORITHM = Path("lean/v1_strategy.py")
_STRATEGIES_PKG = Path("strategies")


@dataclass(frozen=True, slots=True)
class Entry:
    """An entry DECISION: a market taken from flat to a directional position."""

    session_date: date
    market: str
    direction: str  # "long" | "short"

    def key(self) -> tuple[date, str, str]:
        return (self.session_date, self.market, self.direction)


def _normalize_direction(value: object) -> str:
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("long", "buy"):
            return "long"
        if v in ("short", "sell"):
            return "short"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "long" if value > 0 else "short"
    return str(value).strip().lower()


def entries_from_fills(fills: list[OrderFill]) -> list[Entry]:
    """Reduce filled orders to entry decisions (flat → directional per market).

    V1 enters once per breakout (no pyramiding — anti-pyramiding guard, PR #250),
    so the first fill that opens a market is the entry. A subsequent fill that flips
    direction (flat-crossing) also counts as a new entry; fills that merely close to
    flat do not.
    """
    running: dict[str, int] = {}
    entries: list[Entry] = []
    for fill in sorted(fills, key=lambda f: (f.fill_date, f.market)):
        prev = running.get(fill.market, 0)
        cur = prev + fill.quantity
        if prev == 0 and cur != 0:
            entries.append(Entry(fill.fill_date, fill.market, "long" if cur > 0 else "short"))
        elif prev != 0 and cur != 0 and (prev > 0) != (cur > 0):
            entries.append(Entry(fill.fill_date, fill.market, "long" if cur > 0 else "short"))
        running[fill.market] = cur
    return entries


@dataclass(frozen=True, slots=True)
class OracleEntry:
    """One live paper entry decision from the prod ``signals`` table."""

    session_date: date
    market: str
    direction: str

    def key(self) -> tuple[date, str, str]:
        return (self.session_date, self.market, self.direction)


def load_oracle(path: Path) -> list[OracleEntry]:
    """Load an oracle snapshot (the reproduce-V1 SQL output, JSON-shaped).

    Accepts either ``{"entries": [...]}`` or a bare list. Each item needs
    ``session_date`` (``YYYY-MM-DD``), ``market``, ``direction``; a ``signal_type``
    other than ``entry``, if present, is filtered out.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get("entries", []) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise ValueError(f"{path}: oracle must be a list or have an 'entries' list")
    out: list[OracleEntry] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("signal_type", "entry")).lower() != "entry":
            continue
        out.append(
            OracleEntry(
                session_date=date.fromisoformat(str(item["session_date"])),
                market=str(item["market"]),
                direction=_normalize_direction(item.get("direction")),
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class CrossCheckReport:
    """Decision-level agreement between the V1 backtest and the live oracle."""

    matched: tuple[tuple[date, str, str], ...]
    missing_in_backtest: tuple[tuple[date, str, str], ...]  # oracle decided, backtest didn't
    extra_in_backtest: tuple[tuple[date, str, str], ...]  # backtest decided, oracle didn't
    window_start: date | None
    window_end: date | None

    @property
    def oracle_count(self) -> int:
        return len(self.matched) + len(self.missing_in_backtest)

    @property
    def match_rate(self) -> float:
        return len(self.matched) / self.oracle_count if self.oracle_count else 1.0

    def summary(self) -> str:
        window = (
            f"[{self.window_start}..{self.window_end}]"
            if self.window_start or self.window_end
            else "[full]"
        )
        return (
            f"V1 repro {window}: matched {len(self.matched)}/{self.oracle_count} "
            f"({self.match_rate:.0%}); missing {len(self.missing_in_backtest)}, "
            f"extra {len(self.extra_in_backtest)}"
        )


def crosscheck_entries(
    backtest_entries: list[Entry],
    oracle_entries: list[OracleEntry],
    *,
    window: tuple[date, date] | None = None,
) -> CrossCheckReport:
    """Diff backtest vs oracle entry decisions (market + direction + session_date).

    ``window`` (inclusive) restricts BOTH sides to a single-phash sub-window so a
    mid-window param change does not muddy the comparison (P2 kickoff guidance).
    """
    start, end = window if window is not None else (None, None)

    def _in_window(d: date) -> bool:
        return (start is None or d >= start) and (end is None or d <= end)

    bt = {e.key() for e in backtest_entries if _in_window(e.session_date)}
    orc = {e.key() for e in oracle_entries if _in_window(e.session_date)}
    report = CrossCheckReport(
        matched=tuple(sorted(bt & orc)),
        missing_in_backtest=tuple(sorted(orc - bt)),
        extra_in_backtest=tuple(sorted(bt - orc)),
        window_start=start,
        window_end=end,
    )
    _log.info(
        "research_v1_repro_crosscheck",
        matched=len(report.matched),
        missing=len(report.missing_in_backtest),
        extra=len(report.extra_in_backtest),
        match_rate=round(report.match_rate, 4),
    )
    return report


def build_v1_run_spec(
    data_root: Path,
    *,
    parameters: dict[str, str] | None = None,
    v1_algorithm: Path = _V1_ALGORITHM,
    strategies_pkg: Path = _STRATEGIES_PKG,
) -> LeanRunSpec:
    """A :class:`LeanRunSpec` that reproduces production V1 (isolated; POST-stubbed).

    Drives the REPO's ``lean/v1_strategy.py`` (a read-only input, not copied) with
    the live params from ``lean/lean.json`` and the ``strategies/`` package mounted.
    ``posts_to_api=True`` forces the docker backend + the isolation env so V1's
    per-cycle POSTs never reach the prod api (design §9 P2). V1's backtest WINDOW is
    its own hard-coded ``initialize()`` range — not renderable from here.
    """
    return LeanRunSpec(
        algorithm_type_name="V1TrendFollowingAlgorithm",
        algorithm_source=v1_algorithm,
        parameters=parameters or load_production_v1_parameters(),
        data_root=data_root,
        symbol="PORTFOLIO",  # V1 is multi-instrument; equity curve is account-level
        multiplier=1.0,
        strategy_name="v1_trend_following",
        extra_package_mounts=(strategies_pkg,),
        posts_to_api=True,
    )
