"""Unit tests for ``GET /api/internal/lean/parameters`` (PR-C, design §12).

The endpoint lets the nightly LEAN cycle read the active
``STRATEGY_DECOMMISSIONED`` from the DB head row so it can honor an operator's
runtime kill-switch flip. Tested in-process via ``httpx.AsyncClient`` +
``ASGITransport`` with the repo dependency overridden by a stub (no live DB) —
the same pattern ``test_api_lean_auth.py`` uses for the app, plus a
FastAPI ``dependency_overrides`` for the parameter-set read.

The consumer side (``lean/v1_strategy.py``'s fetch + fail-open override) is NOT
unit-testable — ``v1_strategy.py`` does ``from AlgorithmImports import *`` and
needs the LEAN runtime — so it is covered by the LEAN↔api smoke checklist
(A27, in ``deploy/lean_local/README.md``) + the risk-review pass.

Coverage:
  * empty table → 200, source='defaults', both flags 'False', null hash
  * seeded head row → 200, source='db_head', value passthrough + hash
  * non-flag params round-trip as strings
  * missing LEAN bearer → rejected (not 200)
  * §12.4 forward-guard: EXIT_AUTO_APPROVE default False (fail-open precondition)
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from services.api import config as api_config
from services.api.repos.phase1 import ParameterSetRow
from services.api.routes.internal import lean as lean_route
from strategies.v1_trend_following.parameters import V1_DEFAULTS, default_v1_parameters

_VALID_LEAN_BEARER = "lean-bearer-secret-test-32-bytes"
_LEAN_AUTH_HEADER = f"Bearer {_VALID_LEAN_BEARER}"
_PARAMS_PATH = "/api/internal/lean/parameters"


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
    """Minimal stand-in for PostgresPhase1QueryRepo — only the one method the
    endpoint calls. Returned via dependency override so no DB is touched."""

    def __init__(self, row: ParameterSetRow | None) -> None:
        self._row = row

    async def fetch_active_parameter_set(self) -> ParameterSetRow | None:
        return self._row


def _seeded_row(*, decommissioned: str = "False") -> ParameterSetRow:
    """A head row in the canonical to_canonical_dict() all-strings shape."""
    params = {k: str(v) for k, v in default_v1_parameters().to_canonical_dict().items()}
    params["STRATEGY_DECOMMISSIONED"] = decommissioned
    return ParameterSetRow(
        parameter_set_hash="a" * 64,
        parameters=params,
        first_active_at=datetime(2026, 5, 29, tzinfo=UTC),
        last_active_at=None,
    )


@pytest_asyncio.fixture
async def _app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    return _build_app(monkeypatch)


async def _get(app: FastAPI, *, with_bearer: bool = True) -> object:
    transport = ASGITransport(app=app)
    headers = {"Authorization": _LEAN_AUTH_HEADER} if with_bearer else {}
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(_PARAMS_PATH, headers=headers)


class TestGetLeanParameters:
    async def test_empty_table_returns_safe_defaults(self, _app: FastAPI) -> None:
        _app.dependency_overrides[lean_route._get_lean_query_repo] = lambda: _StubRepo(None)
        resp = await _get(_app)
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "defaults"
        assert body["parameter_set_hash"] is None
        assert body["parameters"]["STRATEGY_DECOMMISSIONED"] == "False"
        assert body["parameters"]["EXIT_AUTO_APPROVE"] == "False"

    async def test_seeded_head_row_passthrough(self, _app: FastAPI) -> None:
        _app.dependency_overrides[lean_route._get_lean_query_repo] = lambda: _StubRepo(
            _seeded_row(decommissioned="True")
        )
        resp = await _get(_app)
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "db_head"
        assert body["parameter_set_hash"] == "a" * 64
        assert body["parameters"]["STRATEGY_DECOMMISSIONED"] == "True"
        # Non-flag params survive as strings (canonical shape).
        assert body["parameters"]["LOOKBACK_DAYS_DONCHIAN"] == "60"

    async def test_seeded_false_flag_passthrough(self, _app: FastAPI) -> None:
        _app.dependency_overrides[lean_route._get_lean_query_repo] = lambda: _StubRepo(
            _seeded_row(decommissioned="False")
        )
        resp = await _get(_app)
        assert resp.status_code == 200
        assert resp.json()["parameters"]["STRATEGY_DECOMMISSIONED"] == "False"

    async def test_missing_lean_bearer_rejected(self, _app: FastAPI) -> None:
        _app.dependency_overrides[lean_route._get_lean_query_repo] = lambda: _StubRepo(None)
        resp = await _get(_app, with_bearer=False)
        assert resp.status_code in (401, 403)
        assert resp.status_code != 200


class TestFailOpenForwardGuard:
    """§12.4 forward-guard: the nightly kill-switch fails OPEN, which is safe
    ONLY while exit auto-approval is unbuilt + EXIT_AUTO_APPROVE defaults False
    (every emitted signal is operator-gated). If a future change flips that
    default True, this test fails — forcing a re-evaluation of fail-open
    (fail-closed for exits) before the kill-switch can be silently defeated."""

    def test_exit_auto_approve_defaults_false(self) -> None:
        assert default_v1_parameters().exit_auto_approve is False
        assert V1_DEFAULTS["EXIT_AUTO_APPROVE"] is False

    def test_endpoint_safe_default_exit_auto_approve_false(self) -> None:
        assert lean_route._SAFE_DEFAULT_LEAN_PARAMETERS["EXIT_AUTO_APPROVE"] == "False"
        assert lean_route._SAFE_DEFAULT_LEAN_PARAMETERS["STRATEGY_DECOMMISSIONED"] == "False"
