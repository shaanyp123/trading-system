"""Unit tests for ``strategies.v1_trend_following.lean_history_adapter``.

Locks the PR #129 contract: the DataFrame branch of the history parser must
yield bars (not silently skip all rows). Pre-PR-#129, the legacy iteration
treated a pandas DataFrame as an iterable of column-name strings, all of
which skipped the ``end_time is None: continue`` branch and left ``bars``
empty — surfacing live as the 2026-05-12 cycle emitting
``v1_history_unavailable`` for every market in the universe.

These tests use duck-typed mock objects so we don't take a runtime pandas
dependency. pandas is only available inside LEAN's container (the
``quantconnect/lean:latest`` image ships it); the adapter only requires
``hasattr(history, "iterrows")``, ``getattr(history, "empty", False)``,
``row[key]``, and ``key in row`` — all of which work with simple Python
mocks. If the real pandas API ever changes shape, these tests miss it; the
counterweight is that the adapter file is pure-Python under
``strategies/`` (mypy + ruff + pytest all run on it), so any return-type
shift in pandas would surface as a parse error at boot, not silent skip.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from strategies.v1_trend_following.lean_history_adapter import (
    parse_history_to_bars,
    value_is_missing,
)
from strategies.v1_trend_following.signals import Bar

# ----------------------------------------------------------------------
# Mock objects — emulate the duck-typed surface the adapter relies on.
# ----------------------------------------------------------------------


class _FakeRow:
    """Duck-types pandas ``Series``: supports ``row[key]`` + ``key in row``."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __contains__(self, key: object) -> bool:
        return key in self._data


class _FakeDataFrame:
    """Duck-types pandas ``DataFrame``: ``iterrows()`` + ``.empty``."""

    def __init__(
        self,
        rows: list[tuple[Any, dict[str, Any]]],
        *,
        empty: bool = False,
    ) -> None:
        self._rows = [(ts, _FakeRow(data)) for ts, data in rows]
        self.empty = empty

    def iterrows(self) -> Any:
        return iter(self._rows)


class _FakeTimestamp:
    """Duck-types pandas ``Timestamp``: ``.date()`` returns ``datetime.date``."""

    def __init__(self, d: date) -> None:
        self._d = d

    def date(self) -> date:
        return self._d


class _FakeLegacyBar:
    """Duck-types a TradeBar — supports snake_case AND PascalCase attrs.

    The legacy branch tries snake_case first then falls back to PascalCase
    via ``getattr(raw, "open", None) or getattr(raw, "Open", None)``. To
    exercise the PascalCase fallback branch, set ``snake=False`` and the
    object exposes ONLY PascalCase attrs.
    """

    def __init__(
        self,
        end_time: datetime | None,
        *,
        open_v: float | None,
        high_v: float | None,
        low_v: float | None,
        close_v: float | None,
        volume_v: float | int | None = 0,
        snake: bool = True,
    ) -> None:
        if snake:
            if end_time is not None:
                self.end_time = end_time
            if open_v is not None:
                self.open = open_v
            if high_v is not None:
                self.high = high_v
            if low_v is not None:
                self.low = low_v
            if close_v is not None:
                self.close = close_v
            if volume_v is not None:
                self.volume = volume_v
        else:
            if end_time is not None:
                self.EndTime = end_time
            if open_v is not None:
                self.Open = open_v
            if high_v is not None:
                self.High = high_v
            if low_v is not None:
                self.Low = low_v
            if close_v is not None:
                self.Close = close_v
            if volume_v is not None:
                self.Volume = volume_v


# ----------------------------------------------------------------------
# value_is_missing
# ----------------------------------------------------------------------


class TestValueIsMissing:
    def test_none_returns_true(self) -> None:
        assert value_is_missing(None) is True

    def test_python_nan_returns_true(self) -> None:
        assert value_is_missing(float("nan")) is True

    def test_string_nan_returns_true(self) -> None:
        # float("nan") works on the string "nan", so math.isnan(float("nan")) is True.
        assert value_is_missing("nan") is True

    def test_finite_float_returns_false(self) -> None:
        assert value_is_missing(1.5) is False

    def test_numeric_string_returns_false(self) -> None:
        assert value_is_missing("1.5") is False

    def test_int_returns_false(self) -> None:
        assert value_is_missing(42) is False

    def test_zero_returns_false(self) -> None:
        assert value_is_missing(0) is False
        assert value_is_missing(0.0) is False

    def test_negative_returns_false(self) -> None:
        assert value_is_missing(-1.5) is False

    def test_non_numeric_string_returns_true(self) -> None:
        assert value_is_missing("abc") is True

    def test_dict_returns_true(self) -> None:
        # float({}) raises TypeError → treat as missing.
        assert value_is_missing({"a": 1}) is True

    def test_list_returns_true(self) -> None:
        assert value_is_missing([1, 2]) is True

    def test_inf_returns_false(self) -> None:
        # inf is not NaN — math.isnan(inf) is False. The adapter can still
        # form a Bar from it (Decimal("inf") raises later inside Bar
        # construction; not our concern here).
        assert value_is_missing(float("inf")) is False

    def test_negative_inf_returns_false(self) -> None:
        assert value_is_missing(float("-inf")) is False

    def test_bool_true_returns_false(self) -> None:
        # bool is a numeric subtype in Python; float(True)=1.0, not NaN.
        assert value_is_missing(True) is False

    def test_bool_false_returns_false(self) -> None:
        assert value_is_missing(False) is False


# ----------------------------------------------------------------------
# parse_history_to_bars — DataFrame branch (the PR #129 contract)
# ----------------------------------------------------------------------


class TestParseHistoryToBarsDataFramePath:
    """The branch the 2026-05-12 ceremony broke and PR #129 fixed."""

    def test_none_history_returns_empty(self) -> None:
        assert parse_history_to_bars(None) == []

    def test_empty_dataframe_returns_empty(self) -> None:
        df = _FakeDataFrame(rows=[], empty=True)
        assert parse_history_to_bars(df) == []

    def test_basic_multiindex_parses_in_order(self) -> None:
        # MultiIndex (symbol, Timestamp) is the modern QC Python API shape.
        # ``ts`` is a 2-tuple; the adapter extracts ts[-1].date().
        df = _FakeDataFrame(
            rows=[
                (
                    ("/MES", _FakeTimestamp(date(2026, 5, 1))),
                    {
                        "open": 5800.0,
                        "high": 5825.5,
                        "low": 5795.0,
                        "close": 5810.25,
                        "volume": 12345,
                    },
                ),
                (
                    ("/MES", _FakeTimestamp(date(2026, 5, 2))),
                    {
                        "open": 5810.0,
                        "high": 5830.0,
                        "low": 5805.5,
                        "close": 5820.0,
                        "volume": 9876,
                    },
                ),
            ],
        )
        bars = parse_history_to_bars(df)
        assert len(bars) == 2
        assert bars[0].session_date == date(2026, 5, 1)
        assert bars[0].open == Decimal("5800.0")
        assert bars[0].close == Decimal("5810.25")
        assert bars[0].volume == 12345
        assert bars[1].session_date == date(2026, 5, 2)
        assert bars[1].close == Decimal("5820.0")

    def test_single_index_dataframe_parses(self) -> None:
        # Defensive: single-index DataFrame yields the Timestamp directly
        # (not a tuple). Should still parse cleanly.
        df = _FakeDataFrame(
            rows=[
                (
                    _FakeTimestamp(date(2026, 5, 1)),
                    {"open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5, "volume": 1000},
                ),
            ],
        )
        bars = parse_history_to_bars(df)
        assert len(bars) == 1
        assert bars[0].session_date == date(2026, 5, 1)

    def test_nan_row_is_skipped(self) -> None:
        # An OHLCV cell with NaN should drop the whole row but preserve
        # other valid rows in the same DataFrame.
        df = _FakeDataFrame(
            rows=[
                (
                    ("TLT", _FakeTimestamp(date(2026, 5, 1))),
                    {"open": 85.0, "high": 86.0, "low": 84.5, "close": 85.5, "volume": 100},
                ),
                (
                    ("TLT", _FakeTimestamp(date(2026, 5, 2))),
                    {"open": float("nan"), "high": 86.5, "low": 85.0, "close": 86.0, "volume": 200},
                ),
                (
                    ("TLT", _FakeTimestamp(date(2026, 5, 3))),
                    {"open": 86.0, "high": 87.0, "low": 85.5, "close": 86.5, "volume": 300},
                ),
            ],
        )
        bars = parse_history_to_bars(df)
        # Bar at 5/2 dropped because open is NaN.
        assert len(bars) == 2
        assert bars[0].session_date == date(2026, 5, 1)
        assert bars[1].session_date == date(2026, 5, 3)

    def test_missing_volume_column_defaults_to_zero(self) -> None:
        # Some QC data providers omit volume entirely (e.g., continuous
        # futures bars). Adapter must default to 0, not crash.
        df = _FakeDataFrame(
            rows=[
                (
                    _FakeTimestamp(date(2026, 5, 1)),
                    {"open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5},
                ),
            ],
        )
        bars = parse_history_to_bars(df)
        assert len(bars) == 1
        assert bars[0].volume == 0

    def test_keyerror_on_missing_ohlc_skips_row(self) -> None:
        # If a row genuinely lacks 'open' (defensive — shouldn't happen
        # on real LEAN data), drop the row but keep others.
        df = _FakeDataFrame(
            rows=[
                (
                    _FakeTimestamp(date(2026, 5, 1)),
                    {"high": 101.0, "low": 99.5, "close": 100.5},  # no 'open' key
                ),
                (
                    _FakeTimestamp(date(2026, 5, 2)),
                    {"open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5},
                ),
            ],
        )
        bars = parse_history_to_bars(df)
        assert len(bars) == 1
        assert bars[0].session_date == date(2026, 5, 2)

    def test_decimal_precision_preserved(self) -> None:
        # The Decimal(str(value)) idiom must preserve the printed precision
        # of the source value — Decimal must not roundtrip through a float
        # binary representation (which would change e.g. 0.1 → 0.1000000...01).
        df = _FakeDataFrame(
            rows=[
                (
                    _FakeTimestamp(date(2026, 5, 1)),
                    {
                        "open": "85.567",
                        "high": "85.999",
                        "low": "85.123",
                        "close": "85.456",
                        "volume": 1000,
                    },
                ),
            ],
        )
        bars = parse_history_to_bars(df)
        assert bars[0].open == Decimal("85.567")
        assert bars[0].close == Decimal("85.456")

    def test_nan_volume_defaults_to_zero(self) -> None:
        # A NaN volume cell should not crash; it should default to 0
        # rather than dropping the row (OHLC are the only required cells).
        df = _FakeDataFrame(
            rows=[
                (
                    _FakeTimestamp(date(2026, 5, 1)),
                    {
                        "open": 100.0,
                        "high": 101.0,
                        "low": 99.5,
                        "close": 100.5,
                        "volume": float("nan"),
                    },
                ),
            ],
        )
        bars = parse_history_to_bars(df)
        assert len(bars) == 1
        assert bars[0].volume == 0

    def test_three_valid_one_nan_yields_three_bars(self) -> None:
        """PR #129 regression contract.

        Pre-PR #129, the entire DataFrame was treated as an iterable of
        column-name strings (so 5 strings → 5 iterations, each falling
        through the legacy `end_time is None: continue` branch). Post-fix,
        the DataFrame path is taken via `hasattr(history, "iterrows")` and
        produces N bars for N parseable rows.
        """
        df = _FakeDataFrame(
            rows=[
                (
                    ("/MES", _FakeTimestamp(date(2026, 5, 1))),
                    {
                        "open": 5800.0,
                        "high": 5820.0,
                        "low": 5790.0,
                        "close": 5810.0,
                        "volume": 1000,
                    },
                ),
                (
                    ("/MES", _FakeTimestamp(date(2026, 5, 2))),
                    {
                        "open": 5810.0,
                        "high": 5830.0,
                        "low": 5805.0,
                        "close": 5820.0,
                        "volume": 2000,
                    },
                ),
                (
                    ("/MES", _FakeTimestamp(date(2026, 5, 3))),
                    {
                        "open": float("nan"),
                        "high": 5840.0,
                        "low": 5815.0,
                        "close": 5830.0,
                        "volume": 3000,
                    },
                ),
                (
                    ("/MES", _FakeTimestamp(date(2026, 5, 4))),
                    {
                        "open": 5830.0,
                        "high": 5850.0,
                        "low": 5825.0,
                        "close": 5840.0,
                        "volume": 4000,
                    },
                ),
            ],
        )
        bars = parse_history_to_bars(df)
        assert len(bars) == 3
        assert [b.session_date for b in bars] == [
            date(2026, 5, 1),
            date(2026, 5, 2),
            date(2026, 5, 4),
        ]


# ----------------------------------------------------------------------
# parse_history_to_bars — legacy iterable-of-TradeBar branch (defensive)
# ----------------------------------------------------------------------


class TestParseHistoryToBarsLegacyPath:
    """Pre-DataFrame era of LEAN's Python API. Preserved as a defensive
    fallback for warmup/backtest-replay edge cases or future LEAN reverts."""

    def test_empty_iterable_returns_empty(self) -> None:
        assert parse_history_to_bars(iter([])) == []

    def test_end_time_none_skipped(self) -> None:
        bar = _FakeLegacyBar(end_time=None, open_v=100.0, high_v=101.0, low_v=99.0, close_v=100.5)
        assert parse_history_to_bars(iter([bar])) == []

    def test_snake_case_attrs_parses(self) -> None:
        # Modern legacy-bar shape (post ~2024 snake_case migration).
        bar = _FakeLegacyBar(
            end_time=datetime(2026, 5, 1, 16, 0, 0),
            open_v=85.0,
            high_v=86.0,
            low_v=84.5,
            close_v=85.5,
            volume_v=1000,
            snake=True,
        )
        bars = parse_history_to_bars(iter([bar]))
        assert len(bars) == 1
        assert bars[0].session_date == date(2026, 5, 1)
        assert bars[0].open == Decimal("85.0")
        assert bars[0].close == Decimal("85.5")
        assert bars[0].volume == 1000

    def test_pascalcase_attrs_fallback_parses(self) -> None:
        # Pre-snake_case migration shape — PascalCase fallback via the
        # `getattr(raw, "open", None) or getattr(raw, "Open", None)`
        # idiom in the legacy branch.
        bar = _FakeLegacyBar(
            end_time=datetime(2026, 5, 1, 16, 0, 0),
            open_v=85.0,
            high_v=86.0,
            low_v=84.5,
            close_v=85.5,
            volume_v=1000,
            snake=False,
        )
        bars = parse_history_to_bars(iter([bar]))
        assert len(bars) == 1
        assert bars[0].session_date == date(2026, 5, 1)
        assert bars[0].open == Decimal("85.0")
        assert bars[0].volume == 1000

    def test_open_none_skipped(self) -> None:
        # If `open` is missing (None on snake_case attr + None on PascalCase
        # attr), the bar is dropped entirely.
        bar = _FakeLegacyBar(
            end_time=datetime(2026, 5, 1, 16, 0, 0),
            open_v=None,
            high_v=86.0,
            low_v=84.5,
            close_v=85.5,
        )
        assert parse_history_to_bars(iter([bar])) == []

    def test_mixed_valid_and_invalid(self) -> None:
        bars_in = [
            _FakeLegacyBar(
                end_time=datetime(2026, 5, 1, 16, 0, 0),
                open_v=85.0,
                high_v=86.0,
                low_v=84.5,
                close_v=85.5,
                volume_v=1000,
            ),
            _FakeLegacyBar(  # end_time=None → skip
                end_time=None,
                open_v=86.0,
                high_v=87.0,
                low_v=85.5,
                close_v=86.5,
            ),
            _FakeLegacyBar(  # close=None → skip
                end_time=datetime(2026, 5, 3, 16, 0, 0),
                open_v=86.0,
                high_v=87.0,
                low_v=85.5,
                close_v=None,
            ),
            _FakeLegacyBar(
                end_time=datetime(2026, 5, 4, 16, 0, 0),
                open_v=87.0,
                high_v=88.0,
                low_v=86.5,
                close_v=87.5,
                volume_v=4000,
            ),
        ]
        bars = parse_history_to_bars(iter(bars_in))
        assert len(bars) == 2
        assert [b.session_date for b in bars] == [date(2026, 5, 1), date(2026, 5, 4)]

    def test_volume_none_defaults_to_zero(self) -> None:
        bar = _FakeLegacyBar(
            end_time=datetime(2026, 5, 1, 16, 0, 0),
            open_v=85.0,
            high_v=86.0,
            low_v=84.5,
            close_v=85.5,
            volume_v=None,
        )
        bars = parse_history_to_bars(iter([bar]))
        assert len(bars) == 1
        assert bars[0].volume == 0


# ----------------------------------------------------------------------
# Dispatch detection — duck-typing on `iterrows`
# ----------------------------------------------------------------------


class TestParseHistoryToBarsDispatchDetection:
    def test_object_with_iterrows_takes_dataframe_path(self) -> None:
        # If the input quacks like a DataFrame (has `iterrows`), the
        # adapter uses the DataFrame branch — even if it's not a real
        # pandas DataFrame.
        df = _FakeDataFrame(
            rows=[
                (
                    _FakeTimestamp(date(2026, 5, 1)),
                    {"open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5, "volume": 1000},
                ),
            ],
        )
        assert hasattr(df, "iterrows")
        bars = parse_history_to_bars(df)
        assert len(bars) == 1

    def test_object_without_iterrows_takes_legacy_path(self) -> None:
        # An iterable of bar objects has no `iterrows` attribute → falls
        # through to the legacy branch.
        bar = _FakeLegacyBar(
            end_time=datetime(2026, 5, 1, 16, 0, 0),
            open_v=85.0,
            high_v=86.0,
            low_v=84.5,
            close_v=85.5,
        )
        assert not hasattr(iter([bar]), "iterrows")
        bars = parse_history_to_bars(iter([bar]))
        assert len(bars) == 1


# ----------------------------------------------------------------------
# Session-date extraction edge cases (DataFrame branch only — legacy
# path uses raw.end_time.date() and is covered by the cases above).
# ----------------------------------------------------------------------


class TestExtractSessionDateFromDataframeTs:
    """Cover the internal helper through its public surface.

    The helper is module-private; we exercise it indirectly by feeding
    different ``ts`` shapes through ``parse_history_to_bars`` and asserting
    on the resulting bar's ``session_date``.
    """

    def test_tuple_multiindex_returns_last_element_date(self) -> None:
        df = _FakeDataFrame(
            rows=[
                (
                    ("MES", _FakeTimestamp(date(2026, 5, 7))),
                    {"open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5},
                ),
            ],
        )
        bars = parse_history_to_bars(df)
        assert bars[0].session_date == date(2026, 5, 7)

    def test_three_element_tuple_uses_last(self) -> None:
        # Defensive: if QC ever adds a third level to the MultiIndex
        # (e.g., (universe, symbol, time)), the adapter still takes the
        # last element. Locks the `ts[-1]` contract.
        df = _FakeDataFrame(
            rows=[
                (
                    ("uni", "MES", _FakeTimestamp(date(2026, 5, 7))),
                    {"open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5},
                ),
            ],
        )
        bars = parse_history_to_bars(df)
        assert bars[0].session_date == date(2026, 5, 7)

    def test_single_timestamp_returns_date(self) -> None:
        df = _FakeDataFrame(
            rows=[
                (
                    _FakeTimestamp(date(2026, 5, 7)),
                    {"open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5},
                ),
            ],
        )
        bars = parse_history_to_bars(df)
        assert bars[0].session_date == date(2026, 5, 7)

    def test_no_date_method_falls_back_to_today(self) -> None:
        # Defensive: if a bar_time object somehow doesn't have a `.date()`
        # method, the helper falls back to `_date.today()`. Production
        # LEAN never yields this shape; the fallback exists to keep the
        # algorithm running rather than crash on a malformed bar.
        class _NoDate:
            pass

        df = _FakeDataFrame(
            rows=[
                (
                    _NoDate(),
                    {"open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5},
                ),
            ],
        )
        bars = parse_history_to_bars(df)
        # Don't pin to a specific date (test would flake at midnight);
        # just confirm the row landed AND has today's session_date.
        assert len(bars) == 1
        assert bars[0].session_date == date.today()


# ----------------------------------------------------------------------
# Module contract smoke
# ----------------------------------------------------------------------


class TestModuleContract:
    def test_public_surface_is_two_names(self) -> None:
        from strategies.v1_trend_following import lean_history_adapter as mod

        assert sorted(mod.__all__) == ["parse_history_to_bars", "value_is_missing"]

    def test_parse_returns_list_of_bar(self) -> None:
        df = _FakeDataFrame(
            rows=[
                (
                    _FakeTimestamp(date(2026, 5, 1)),
                    {"open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5, "volume": 1000},
                ),
            ],
        )
        result = parse_history_to_bars(df)
        assert isinstance(result, list)
        assert all(isinstance(b, Bar) for b in result)


# Sanity import — ensures the adapter module loads without LEAN runtime.
def test_adapter_imports_cleanly_without_lean_runtime() -> None:
    import strategies.v1_trend_following.lean_history_adapter as mod

    assert callable(mod.parse_history_to_bars)
    assert callable(mod.value_is_missing)


# `pytest` import — keep the linter happy by referencing it.
_ = pytest
