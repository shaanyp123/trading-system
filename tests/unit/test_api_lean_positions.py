"""Unit tests for ``GET /api/internal/lean/positions`` (PR-A2).

The endpoint supplies the nightly LEAN cycle with the api's REAL open positions
(plus the in-flight-exit set) so ``generate_signals`` + ``generate_exit_candidates``
run against reality instead of LEAN's always-empty ``self.portfolio`` under
PaperBrokerage (Docs/decisions-log.md 2026-05-31). Tested in-process via
``httpx.AsyncClient`` + ``ASGITransport`` with the repo dependency overridden by a
stub (no live DB) — same pattern as ``test_api_lean_parameters.py``.

The consumer side (``lean/v1_strategy.py`` fetch + mapping) needs the LEAN runtime
(``from AlgorithmImports import *``); the pure mapper ``_position_from_api_row`` is
covered by ``test_lean_live_positions.py``.

Coverage:
  * no active account → 200, empty positions + exits
  * positions map (signed qty, Decimal-as-string avg_cost, opened_at → ET date)
  * opened_at_utc NULL → opened_at_session_date None
  * ET conversion is DST-aware + can cross the UTC date boundary
  * exits_in_flight passthrough (sorted)
  * missing LEAN bearer → rejected (not 200)
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from services.api import config as api_config
from services.api.routes.internal import lean as lean_route

_VALID_LEAN_BEARER = "lean-bearer-secret-test-32-bytes"
_LEAN_AUTH_HEADER = f"Bearer {_VALID_LEAN_BEARER}"
_POSITIONS_PATH = "/api/internal/lean/positions"
_ACCOUNT_ID = UUID("00000000-0000-0000-0000-0000000000a1")


def _stub_env() -> dict[str, str]:
    return {
        "API_DATABASE_URL": "postgresql+asyncpg://stub:stub@127.0.0.1:0/stub",
        "API_ENVIRONMENT": "dev",
        "API_VERSION": "test",
        "API_LOG_LEVEL": "INFO",
        "API_LEAN_LOCAL_BEARER_TOKEN": _VALID_LEAN_BEARER,
        "API_DISCORD_BOT_BEARER_TOKEN": "bot-bearer-secret-test-32-bytes",
    }


def _build_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    for k, v in _stub_env().items():
        monkeypatch.setenv(k, v)
    api_config.get_settings.cache_clear()
    main_mod = importlib.import_module("services.api.main")
    app: FastAPI = main_mod.create_app()
    return app


class _StubRepo:
    """Stand-in for PostgresPhase1QueryRepo — only the three methods the
    positions endpoint calls. Returned via dependency override (no DB)."""

    def __init__(
        self,
        *,
        account_id: UUID | None,
        positions: list[dict[str, object]] | None = None,
        exits_in_flight: set[str] | None = None,
    ) -> None:
        self._account_id = account_id
        self._positions = positions or []
        self._exits = exits_in_flight or set()

    async def fetch_active_account_id(self) -> UUID | None:
        return self._account_id

    async def fetch_positions_for_lean_cycle(self, account_id: UUID) -> list[dict[str, object]]:
        assert account_id == self._account_id
        return self._positions

    async def fetch_inflight_exit_markets(self, account_id: UUID, env: str) -> set[str]:
        assert account_id == self._account_id
        assert env == "paper"
        return self._exits


@pytest_asyncio.fixture
async def _app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    return _build_app(monkeypatch)


async def _get(app: FastAPI, *, with_bearer: bool = True) -> object:
    transport = ASGITransport(app=app)
    headers = {"Authorization": _LEAN_AUTH_HEADER} if with_bearer else {}
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(_POSITIONS_PATH, headers=headers)


def _override(app: FastAPI, repo: _StubRepo) -> None:
    app.dependency_overrides[lean_route._get_lean_query_repo] = lambda: repo


class TestGetLeanPositions:
    async def test_no_active_account_returns_empty(self, _app: FastAPI) -> None:
        _override(_app, _StubRepo(account_id=None))
        resp = await _get(_app)
        assert resp.status_code == 200
        body = resp.json()
        assert body["positions"] == []
        assert body["exits_in_flight"] == []

    async def test_positions_mapped(self, _app: FastAPI) -> None:
        _override(
            _app,
            _StubRepo(
                account_id=_ACCOUNT_ID,
                positions=[
                    {
                        "market": "/MES",
                        "quantity": 3,
                        "avg_cost": "5123.25",
                        "opened_at_utc": datetime(2026, 5, 20, 14, 0, tzinfo=UTC),
                    },
                    {
                        "market": "TLT",
                        "quantity": -2,
                        "avg_cost": "92.10",
                        "opened_at_utc": datetime(2026, 5, 22, 18, 0, tzinfo=UTC),
                    },
                ],
            ),
        )
        resp = await _get(_app)
        assert resp.status_code == 200
        positions = {p["market"]: p for p in resp.json()["positions"]}
        assert positions["/MES"]["quantity"] == 3
        assert positions["/MES"]["avg_cost"] == "5123.25"  # Decimal-as-string (A05)
        assert positions["/MES"]["opened_at_session_date"] == "2026-05-20"
        assert positions["TLT"]["quantity"] == -2  # signed: short
        assert positions["TLT"]["opened_at_session_date"] == "2026-05-22"

    async def test_null_opened_at_maps_to_none(self, _app: FastAPI) -> None:
        _override(
            _app,
            _StubRepo(
                account_id=_ACCOUNT_ID,
                positions=[
                    {"market": "/MGC", "quantity": 1, "avg_cost": "2400.0", "opened_at_utc": None}
                ],
            ),
        )
        resp = await _get(_app)
        assert resp.status_code == 200
        assert resp.json()["positions"][0]["opened_at_session_date"] is None

    async def test_opened_at_is_dst_aware_and_crosses_utc_date(self, _app: FastAPI) -> None:
        # 2026-05-28T02:30:00Z is 2026-05-27 22:30 ET (EDT, UTC-4 in May) →
        # session date 2026-05-27, NOT the UTC calendar date 2026-05-28.
        _override(
            _app,
            _StubRepo(
                account_id=_ACCOUNT_ID,
                positions=[
                    {
                        "market": "/M2K",
                        "quantity": 1,
                        "avg_cost": "2050.0",
                        "opened_at_utc": datetime(2026, 5, 28, 2, 30, tzinfo=UTC),
                    }
                ],
            ),
        )
        resp = await _get(_app)
        assert resp.json()["positions"][0]["opened_at_session_date"] == "2026-05-27"

    async def test_exits_in_flight_passthrough_sorted(self, _app: FastAPI) -> None:
        _override(
            _app,
            _StubRepo(
                account_id=_ACCOUNT_ID,
                positions=[],
                exits_in_flight={"/MES", "/M2K", "TLT"},
            ),
        )
        resp = await _get(_app)
        assert resp.json()["exits_in_flight"] == ["/M2K", "/MES", "TLT"]

    async def test_missing_lean_bearer_rejected(self, _app: FastAPI) -> None:
        _override(_app, _StubRepo(account_id=_ACCOUNT_ID))
        resp = await _get(_app, with_bearer=False)
        assert resp.status_code in (401, 403)
        assert resp.status_code != 200
