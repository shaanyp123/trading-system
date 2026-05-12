"""Pure-Python adapter — LEAN ``self.history(...)`` result → ``list[Bar]``.

Extracted from ``lean/v1_strategy.py`` per the PR #129 follow-up. The wrapper
in ``lean/v1_strategy.py`` is intentionally OFF the mypy/ruff/pytest target
set (``pyproject.toml`` excludes ``lean/``) because LEAN's ``AlgorithmImports``
injects symbols that mypy can't resolve. By extracting the parsing logic into
``strategies/`` we get full lint + type + test coverage on the contract that
silently broke in the 2026-05-12 seed ceremony (DataFrame iteration was
treating column names as bars; PR #129 fixed at the surface; this PR locks
the fix with a regression test).

The wrapper still handles:

* The ``self.history(symbol, count, Resolution.DAILY)`` LEAN call (and its
  try/except + structured log lines).
* Sort + dedup of parsed bars by ``session_date``.
* ``BarSeries`` construction + its strictly-increasing-date validation.

This module handles:

* ``value_is_missing`` — NaN-safe missing-value check (None / NaN / non-numeric).
* ``parse_history_to_bars`` — duck-typed iteration over either a pandas
  ``DataFrame`` (modern QC Python API, lowercase column names + MultiIndex
  ``(symbol, time)``) or a legacy iterable-of-``TradeBar`` (defensive: kept
  in case a future LEAN release reverts, or warmup/backtest replay yields
  a different shape).

Why split here vs. ``signals.py``: ``signals.py`` is wire-types-only
(``Bar`` / ``BarSeries`` / ``CandidateSignal`` dataclasses). The adapter is
LEAN-specific (assumes ``iterrows`` / ``end_time`` shapes). Keeping it
adjacent in the package but in its own module preserves the wire-types-only
hygiene of ``signals.py`` while keeping the import graph flat.

Why ``strategies/v1_trend_following/`` and not under ``services/signal/``:
``services/signal/**`` is on the FORBIDDEN whitelist per dev-guide §2.2 and
this is pure-Python adapter logic with no signal-service authority. It runs
inside LEAN's process (via the ``lean/v1_strategy.py`` algorithm wrapper) and
never executes server-side. ``strategies/`` is the canonical home for pure
strategy logic; ``strategies/v1_trend_following/parameters.py`` is the only
forbidden-list file inside it (operator-only parameter changes).
"""

from __future__ import annotations

import math
from datetime import date as _date
from decimal import Decimal
from typing import Any

from strategies.v1_trend_following.signals import Bar


def value_is_missing(value: object) -> bool:
    """Return True if ``value`` is None / NaN / non-numeric.

    pandas represents missing OHLCV cells as ``float('nan')`` and missing
    timestamps as ``pd.NaT`` — both pass ``v is not None``, so a naive
    ``v is None`` guard would let bad rows through and explode later inside
    ``Decimal(str(nan))``. We coerce to ``float()`` to catch NaN cleanly;
    non-numeric inputs short-circuit to True since we cannot make a valid
    Bar from them anyway.

    The function is duck-typed so it works against either real pandas
    objects (``np.float64`` NaN, ``pd.NaT``) or plain Python types
    (``float('nan')``, ``None``, ``"abc"``).

    Returns:
        True if value should be treated as missing; False otherwise.
    """
    if value is None:
        return True
    try:
        return math.isnan(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return True


def _extract_session_date_from_dataframe_ts(ts: object) -> _date:
    """For DataFrame iterrows tuple/single index → bar session date.

    Modern QC API yields a MultiIndex ``(symbol, Timestamp)`` so ``ts`` is
    a 2-tuple; the time component is the last element. A single-index
    DataFrame yields the Timestamp directly. Both branches resolve to a
    ``.date()`` call when the bar_time supports it.

    The ``_date.today()`` fallback is defensive — production LEAN never
    yields a bar_time without a ``.date()`` method. Preserving the
    fallback keeps behavior identical to pre-extraction; changing it
    (e.g. raising) is a strategy-logic change requiring risk-review.
    """
    if isinstance(ts, tuple) and len(ts) >= 1:
        bar_time: Any = ts[-1]
    else:
        bar_time = ts
    if hasattr(bar_time, "date"):
        result = bar_time.date()
        if isinstance(result, _date):
            return result
    return _date.today()


def _parse_dataframe_branch(history: Any) -> list[Bar]:
    """Parse the modern QC Python API DataFrame shape into ``list[Bar]``.

    The duck-typing contract:

    * ``history.iterrows()`` yields ``(ts, row)`` tuples where ``ts`` is a
      Timestamp (single-index) or a 2-tuple ``(symbol, Timestamp)``
      (MultiIndex).
    * ``row[key]`` returns the cell value for lowercase OHLCV columns
      (``open / high / low / close / volume``).
    * ``key in row`` returns True iff the column exists.

    A missing 'volume' column defaults to 0 (some QC data providers omit
    it for symbology that doesn't have it, e.g. continuous futures).

    Per-row rejection:

    * Any of open/high/low/close missing → skip the row entirely.
    * KeyError on missing 'open' / 'high' / 'low' / 'close' columns →
      skip (defensive; this should never happen on valid LEAN data).

    Typed as ``Any`` because the duck-typing contract spans real pandas
    DataFrames (production), test mocks (``tests/unit/test_lean_history_adapter.py``),
    and potential future variants. Caller (``parse_history_to_bars``)
    gates entry by ``hasattr(history, "iterrows")`` so this branch never
    sees a non-conforming input at runtime.
    """
    bars: list[Bar] = []
    if getattr(history, "empty", False):
        return bars
    for ts, row in history.iterrows():
        session = _extract_session_date_from_dataframe_ts(ts)
        try:
            open_v = row["open"]
            high_v = row["high"]
            low_v = row["low"]
            close_v = row["close"]
            volume_v: Any = row["volume"] if "volume" in row else 0
        except (KeyError, IndexError):
            continue
        if any(value_is_missing(v) for v in (open_v, high_v, low_v, close_v)):
            continue
        bars.append(
            Bar(
                session_date=session,
                open=Decimal(str(open_v)),
                high=Decimal(str(high_v)),
                low=Decimal(str(low_v)),
                close=Decimal(str(close_v)),
                volume=int(volume_v) if not value_is_missing(volume_v) else 0,
            )
        )
    return bars


def _parse_legacy_branch(history: Any) -> list[Bar]:
    """Parse the legacy iterable-of-``TradeBar`` shape into ``list[Bar]``.

    Preserved verbatim from the pre-DataFrame era of LEAN's Python API
    in case a future release reverts (or a different code path — warmup,
    backtest replay — yields bar objects rather than a DataFrame). The
    DataFrame branch in ``_parse_dataframe_branch`` is the primary path
    in current LEAN.

    Per-bar fields use snake_case first with PascalCase fallback (QC
    migrated the Python API ~2024; older custom code may still emit
    PascalCase attribute names). The ``end_time`` / ``EndTime`` lookup
    short-circuits to None on missing — those rows are skipped.

    Per-bar rejection:

    * ``end_time`` is None → skip (no session-date anchor available).
    * Any of open/high/low/close is None → skip (cannot make a valid
      Bar; this differs slightly from the DataFrame branch which uses
      ``value_is_missing`` for NaN-safety — the legacy iterable produces
      None or values, not NaN floats, so the simpler check applies here).
    """
    bars: list[Bar] = []
    for raw in history:
        end_time = getattr(raw, "end_time", None) or getattr(raw, "EndTime", None)
        if end_time is None:
            continue
        session = end_time.date() if hasattr(end_time, "date") else _date.today()
        open_v = getattr(raw, "open", None)
        if open_v is None:
            open_v = getattr(raw, "Open", None)
        high_v = getattr(raw, "high", None)
        if high_v is None:
            high_v = getattr(raw, "High", None)
        low_v = getattr(raw, "low", None)
        if low_v is None:
            low_v = getattr(raw, "Low", None)
        close_v = getattr(raw, "close", None)
        if close_v is None:
            close_v = getattr(raw, "Close", None)
        volume_v = getattr(raw, "volume", None)
        if volume_v is None:
            volume_v = getattr(raw, "Volume", 0)
        if any(v is None for v in (open_v, high_v, low_v, close_v)):
            continue
        bars.append(
            Bar(
                session_date=session,
                open=Decimal(str(open_v)),
                high=Decimal(str(high_v)),
                low=Decimal(str(low_v)),
                close=Decimal(str(close_v)),
                volume=int(volume_v) if volume_v is not None else 0,
            )
        )
    return bars


def parse_history_to_bars(history: object) -> list[Bar]:
    """Convert a LEAN ``self.history(...)`` result into ``list[Bar]``.

    Detection is via duck-typing on ``iterrows``:

    * DataFrame path: ``hasattr(history, "iterrows")`` is True (pandas).
    * Legacy path: otherwise — treat as iterable of TradeBar-shaped objects.

    Returns a possibly-empty list. The caller is responsible for:

    * Wrapping in try/except for parse-time exceptions (callers in
      ``lean/v1_strategy.py`` log a structured ``v1_history_parse_failed``
      line on any uncaught exception).
    * Sorting + dedup-ing by ``session_date`` (LEAN returns bars in order
      but defending against accidental reverse-iteration or duplicate
      dates is cheap insurance).
    * Constructing ``BarSeries`` and handling its
      ``__post_init__`` strictly-increasing-dates validation.

    Args:
        history: A LEAN ``self.history(...)`` return value — either a
            pandas DataFrame, an iterable of TradeBar-shaped objects, or
            None (treated as empty).

    Returns:
        List of ``Bar`` instances. Empty list on:

        * ``history is None``.
        * DataFrame is empty (``.empty=True``).
        * Iterable yields no parseable items (all rows rejected for
          missing or malformed cells).
    """
    if history is None:
        return []
    if hasattr(history, "iterrows"):
        return _parse_dataframe_branch(history)
    return _parse_legacy_branch(history)


__all__ = [
    "parse_history_to_bars",
    "value_is_missing",
]
