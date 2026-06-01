"""Unit tests for the LEAN-side PR-A2 live-position sourcing.

``lean/v1_strategy.py`` does ``from AlgorithmImports import *`` (LEAN runtime
only). PR-A2 added ``from __future__ import annotations`` so the module imports
outside the runtime with only ``QCAlgorithm`` stubbed — letting us unit-test the
pure mapper + the fetch/suppression logic directly (rather than only via the
A27 smoke checklist).

Coverage:
  * ``_position_from_api_row`` — quantity sign → direction, Decimal avg_cost,
    opened_at_session_date parse, None / bad-date handling
  * ``_fetch_live_positions`` — success shape, FLAT filtering, HTTP/malformed
    failure → None (caller fails safe), per-row skip on a bad row
  * ``_emit_exits`` — suppresses markets in ``exits_in_flight`` (idempotency)
"""

from __future__ import annotations

import json
import sys
import types
from decimal import Decimal

import pytest

# ---- Stub the LEAN runtime symbol the class definition needs, then import. ----
_stub = types.ModuleType("AlgorithmImports")


class _QCAlgorithm:  # minimal base; the methods under test don't call into it.
    ...


_stub.QCAlgorithm = _QCAlgorithm
_stub.__all__ = ["QCAlgorithm"]
sys.modules.setdefault("AlgorithmImports", _stub)

from lean import v1_strategy  # noqa: E402
from strategies.v1_trend_following.signals import Direction  # noqa: E402

# ---------------------------------------------------------------------------
# _position_from_api_row (pure)
# ---------------------------------------------------------------------------


class TestPositionFromApiRow:
    def test_long(self) -> None:
        p = v1_strategy._position_from_api_row(
            {
                "market": "/MES",
                "quantity": 3,
                "avg_cost": "5123.25",
                "opened_at_session_date": "2026-05-20",
            }
        )
        assert p.market == "/MES"
        assert p.direction is Direction.LONG
        assert p.quantity == 3
        assert p.avg_cost == Decimal("5123.25")
        assert p.opened_at_session_date is not None
        assert p.opened_at_session_date.isoformat() == "2026-05-20"

    def test_short(self) -> None:
        p = v1_strategy._position_from_api_row(
            {
                "market": "TLT",
                "quantity": -2,
                "avg_cost": "92.10",
                "opened_at_session_date": "2026-05-22",
            }
        )
        assert p.direction is Direction.SHORT
        assert p.quantity == -2

    def test_none_opened_at_skips_min_holding(self) -> None:
        p = v1_strategy._position_from_api_row(
            {"market": "/MGC", "quantity": 1, "avg_cost": "2400.0", "opened_at_session_date": None}
        )
        assert p.opened_at_session_date is None  # strategy then skips MIN_HOLDING

    def test_bad_date_string_is_tolerated_as_none(self) -> None:
        p = v1_strategy._position_from_api_row(
            {
                "market": "/MNQ",
                "quantity": 1,
                "avg_cost": "18000",
                "opened_at_session_date": "not-a-date",
            }
        )
        assert p.opened_at_session_date is None

    def test_missing_opened_key(self) -> None:
        p = v1_strategy._position_from_api_row(
            {"market": "/MYM", "quantity": 1, "avg_cost": "40000"}
        )
        assert p.direction is Direction.LONG
        assert p.opened_at_session_date is None


# ---------------------------------------------------------------------------
# _fetch_live_positions (network I/O monkeypatched)
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *_a: object) -> bool:
        return False


def _bare_algo() -> object:
    algo = object.__new__(v1_strategy.V1TrendFollowingAlgorithm)
    algo._api_base_url = "http://api:8000"
    algo._api_bearer_token = "tok"
    algo.log = lambda *_a, **_k: None  # swallow LEAN log lines
    return algo


def _patch_urlopen(monkeypatch: pytest.MonkeyPatch, *, status: int, body: str) -> None:
    def _open(_req: object, timeout: float | None = None) -> _FakeResp:
        return _FakeResp(status, body)

    monkeypatch.setattr(v1_strategy.urllib.request, "urlopen", _open)


class TestFetchLivePositions:
    def test_success_maps_positions_and_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = json.dumps(
            {
                "positions": [
                    {
                        "market": "/MES",
                        "quantity": 3,
                        "avg_cost": "5123.25",
                        "opened_at_session_date": "2026-05-20",
                    },
                    {
                        "market": "TLT",
                        "quantity": -2,
                        "avg_cost": "92.10",
                        "opened_at_session_date": "2026-05-22",
                    },
                ],
                "exits_in_flight": ["/MES"],
                "server_now": "2026-05-31T21:30:00Z",
            }
        )
        _patch_urlopen(monkeypatch, status=200, body=body)
        result = _bare_algo()._fetch_live_positions()
        assert result is not None
        positions, exits = result
        assert set(positions) == {"/MES", "TLT"}
        assert positions["/MES"].direction is Direction.LONG
        assert positions["TLT"].direction is Direction.SHORT
        assert exits == frozenset({"/MES"})

    def test_flat_rows_are_filtered_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = json.dumps(
            {
                "positions": [
                    {
                        "market": "/MES",
                        "quantity": 0,
                        "avg_cost": "5123.25",
                        "opened_at_session_date": None,
                    },
                ],
                "exits_in_flight": [],
            }
        )
        _patch_urlopen(monkeypatch, status=200, body=body)
        positions, exits = _bare_algo()._fetch_live_positions()
        assert positions == {}  # quantity 0 → FLAT → excluded
        assert exits == frozenset()

    def test_http_error_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_urlopen(monkeypatch, status=503, body="")
        assert _bare_algo()._fetch_live_positions() is None  # caller fails safe

    def test_malformed_body_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_urlopen(monkeypatch, status=200, body=json.dumps(["not", "a", "dict"]))
        assert _bare_algo()._fetch_live_positions() is None

    def test_bad_row_is_skipped_not_fatal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = json.dumps(
            {
                "positions": [
                    {"market": "/MES", "quantity": "oops", "avg_cost": "x"},  # bad → skipped
                    {
                        "market": "TLT",
                        "quantity": -1,
                        "avg_cost": "92.0",
                        "opened_at_session_date": None,
                    },
                ],
                "exits_in_flight": [],
            }
        )
        _patch_urlopen(monkeypatch, status=200, body=body)
        positions, _ = _bare_algo()._fetch_live_positions()
        assert set(positions) == {"TLT"}  # the good row survives


# ---------------------------------------------------------------------------
# _emit_exits suppression
# ---------------------------------------------------------------------------


class _ExitSig:
    def __init__(self, market: str) -> None:
        self.market = market
        self.exit_reason = "trend_flip"


class _ExitResult:
    def __init__(self, markets: list[str]) -> None:
        self.signals = [_ExitSig(m) for m in markets]


class TestEmitExitsSuppression:
    def test_in_flight_markets_are_not_posted(self) -> None:
        algo = _bare_algo()
        posted: list[str] = []
        algo._build_exit_signal_payload = lambda **kw: {"market": kw["exit_signal"].market}
        algo._post_event = lambda event_type, extra=None: posted.append(extra["market"])

        algo._emit_exits(
            exit_result=_ExitResult(["/MES", "/M2K", "TLT"]),
            session_date="2026-05-31",
            equity=Decimal("20000"),
            exits_in_flight=frozenset({"/MES", "TLT"}),
        )
        assert posted == ["/M2K"]  # only the not-in-flight market is emitted

    def test_no_in_flight_emits_all(self) -> None:
        algo = _bare_algo()
        posted: list[str] = []
        algo._build_exit_signal_payload = lambda **kw: {"market": kw["exit_signal"].market}
        algo._post_event = lambda event_type, extra=None: posted.append(extra["market"])

        algo._emit_exits(
            exit_result=_ExitResult(["/MES", "/M2K"]),
            session_date="2026-05-31",
            equity=Decimal("20000"),
        )
        assert posted == ["/MES", "/M2K"]
