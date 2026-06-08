"""V1 backtest-window param parsing (lean/v1_strategy._parse_backtest_date).

The single most safety-critical line in the window-parameterization change: a missing
or malformed BACKTEST_START_DATE / BACKTEST_END_DATE param MUST fall back to the
original hard-coded paper window, so production — which sets neither param — is
provably byte-for-byte unchanged. lean/v1_strategy.py does
``from AlgorithmImports import *`` (LEAN runtime only); we stub QCAlgorithm to import
the pure helper (same pattern as test_lean_live_positions.py).
"""

from __future__ import annotations

import sys
import types

import pytest

# ---- Stub the LEAN runtime symbol the class definition needs, then import. ----
_stub = types.ModuleType("AlgorithmImports")


class _QCAlgorithm:  # minimal base; _parse_backtest_date is a pure module function.
    ...


_stub.QCAlgorithm = _QCAlgorithm  # type: ignore[attr-defined]
_stub.__all__ = ["QCAlgorithm"]  # type: ignore[attr-defined]
sys.modules.setdefault("AlgorithmImports", _stub)

from lean import v1_strategy  # noqa: E402

_DEFAULT = (2026, 5, 1)  # the original hard-coded V1 start date


def test_parses_clean_yyyymmdd() -> None:
    assert v1_strategy._parse_backtest_date("20230901", *_DEFAULT) == (2023, 9, 1)
    assert v1_strategy._parse_backtest_date("  20260603  ", *_DEFAULT) == (
        2026,
        6,
        3,
    )  # whitespace ok


@pytest.mark.parametrize(
    "token",
    [
        None,  # production: param unset ⇒ get_parameter returns None
        "",
        "   ",
        "2023",  # too short
        "2023090",  # 7 chars
        "202309011",  # 9 chars
        "2023-09-01",  # dashed
        "2023sep1",  # non-digit
        "abcdefgh",
    ],
)
def test_malformed_falls_back_to_default(token: object) -> None:
    # The live-safety property: anything but a clean 8-digit token → the default
    # window, so production (which sets no param) is byte-for-byte unchanged.
    assert v1_strategy._parse_backtest_date(token, *_DEFAULT) == _DEFAULT
