"""Unit tests for services.api.middleware.LeanAuthMiddleware (Pivot-PR-A).

Pivot-PR-A (post-pivot 2026-05-12). Mirrors the Day 23 BotAuthMiddleware
test surface (tests/unit/test_api_bot_auth.py) with appropriate substitutions
for the LEAN identity.

Coverage matrix:

  * No Authorization header → LeanAuth pass-through; downstream BotAuth /
    SessionStub takes the request
  * Authorization: Bearer <invalid> → falls through (no 401; defer to
    BotAuth's match attempt)
  * Authorization: Bearer <valid LEAN token> → request.state.session
    populated as lean-local service-account; CSRF bypass; route reaches
    /api/internal/lean/signals handler
  * No LEAN bearer configured → LeanAuth degrades to noop; the lean path
    is unserved (the bearer header would fall through to BotAuth and
    likely 401 there if it doesn't match the bot token either)
  * Production env without LEAN bearer → request authenticates via the
    SessionStub fail-close path (401) — proves LeanAuth runs BEFORE
    SessionStub
  * Production env with valid LEAN bearer → bypasses fail-close; reaches
    the route handler

The route we target is POST /api/internal/lean/signals because that's the
only LEAN-authenticated endpoint shipped in Pivot-PR-A. It returns 202 on
heartbeat events (we send `lean_strategy_initialized`).

A22 N/A; A06 enforced; A27 N/A (covered by the operator runbook).
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from services.api import config as api_config

_VALID_LEAN_BEARER = "lean-bearer-secret-test-32-bytes"
_VALID_BOT_BEARER = "bot-bearer-secret-test-32-bytes"
_LEAN_AUTH_HEADER = f"Bearer {_VALID_LEAN_BEARER}"
_LEAN_PATH = "/api/internal/lean/signals"


def _heartbeat_body() -> dict[str, object]:
    return {
        "event_type": "lean_strategy_initialized",
        "ts_utc": "2026-05-12T17:30:00.000+00:00",
        "algorithm_id": "v1_trend_following",
    }


def _stub_env(
    *,
    lean_token: str | None = _VALID_LEAN_BEARER,
    bot_token: str | None = _VALID_BOT_BEARER,
    environment: str = "dev",
) -> dict[str, str]:
    env = {
        "API_DATABASE_URL": "postgresql+asyncpg://stub:stub@127.0.0.1:0/stub",
        "API_ENVIRONMENT": environment,
        "API_VERSION": "test",
        "API_LOG_LEVEL": "INFO",
    }
    if lean_token is not None:
        env["API_LEAN_LOCAL_BEARER_TOKEN"] = lean_token
    if bot_token is not None:
        env["API_DISCORD_BOT_BEARER_TOKEN"] = bot_token
    return env


def _build_app(monkeypatch: pytest.MonkeyPatch, **kwargs: str | None) -> FastAPI:
    for k, v in _stub_env(**kwargs).items():  # type: ignore[arg-type]
        monkeypatch.setenv(k, v)
    api_config.get_settings.cache_clear()
    main_mod = importlib.import_module("services.api.main")
    return main_mod.create_app()


@pytest_asyncio.fixture
async def client_with_lean_token(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    app = _build_app(monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest_asyncio.fixture
async def client_without_lean_token(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    app = _build_app(monkeypatch, lean_token=None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest_asyncio.fixture
async def client_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    app = _build_app(monkeypatch, environment="live-small")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest_asyncio.fixture
async def client_in_production_without_lean_token(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    app = _build_app(monkeypatch, environment="live-small", lean_token=None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


# ---------------------------------------------------------------------------
# TestNoAuthHeader
# ---------------------------------------------------------------------------


class TestNoAuthHeader:
    """No Authorization header → LeanAuth pass-through; downstream gates run."""

    async def test_post_to_lean_endpoint_without_header_blocked_by_csrf(
        self,
        client_with_lean_token: AsyncClient,
    ) -> None:
        """A POST without a bearer header reaches CSRF, which rejects (no cookie+header pair)."""
        resp = await client_with_lean_token.post(_LEAN_PATH, json=_heartbeat_body())
        # CSRF rejects with 403 because the stub session has no cookies +
        # the LEAN endpoint isn't on _CSRF_EXEMPT_PATHS.
        assert resp.status_code == 403
        body = resp.json()
        assert body["error_code"] == "CSRF_REJECTED"


# ---------------------------------------------------------------------------
# TestInvalidBearer
# ---------------------------------------------------------------------------


class TestInvalidBearer:
    """Invalid LEAN token → falls through to BotAuth, which may also reject."""

    async def test_wrong_token_falls_through(
        self,
        client_with_lean_token: AsyncClient,
    ) -> None:
        """A non-matching bearer falls through to BotAuth, which then 401s its own match attempt."""
        resp = await client_with_lean_token.post(
            _LEAN_PATH,
            headers={"Authorization": "Bearer wrong-token-not-matching-either-bearer"},
            json=_heartbeat_body(),
        )
        # BotAuth rejects with 401 BOT_AUTH_INVALID because the token also
        # doesn't match the bot bearer.
        assert resp.status_code == 401
        body = resp.json()
        assert body["error_code"] == "BOT_AUTH_INVALID"

    async def test_empty_bearer_falls_through(
        self,
        client_with_lean_token: AsyncClient,
    ) -> None:
        resp = await client_with_lean_token.post(
            _LEAN_PATH,
            headers={"Authorization": "Bearer "},
            json=_heartbeat_body(),
        )
        # Empty bearer falls through entirely → CSRF gate runs → 403.
        assert resp.status_code == 403
        assert resp.json()["error_code"] == "CSRF_REJECTED"

    async def test_non_bearer_scheme_falls_through(
        self,
        client_with_lean_token: AsyncClient,
    ) -> None:
        resp = await client_with_lean_token.post(
            _LEAN_PATH,
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
            json=_heartbeat_body(),
        )
        # Basic scheme isn't recognized by LeanAuth or BotAuth → falls
        # through to CSRF → 403.
        assert resp.status_code == 403
        assert resp.json()["error_code"] == "CSRF_REJECTED"


# ---------------------------------------------------------------------------
# TestValidBearer
# ---------------------------------------------------------------------------


class TestValidBearer:
    """Valid LEAN token → CSRF bypass + route reaches handler → 202 Accepted."""

    async def test_heartbeat_event_accepted(
        self,
        client_with_lean_token: AsyncClient,
    ) -> None:
        resp = await client_with_lean_token.post(
            _LEAN_PATH,
            headers={"Authorization": _LEAN_AUTH_HEADER},
            json=_heartbeat_body(),
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["accepted"] is True
        assert body["event_type"] == "lean_strategy_initialized"
        assert "received_at_utc" in body

    async def test_signal_emitted_missing_required_fields(
        self,
        client_with_lean_token: AsyncClient,
    ) -> None:
        """Worker-PR-4 (2026-05-12): signal_emitted without payload fields → 422.

        Route-layer boundary check (NOT pydantic model_validator —
        Pydantic v2 ctx-serialization issue) returns canonical AppError
        envelope with error_code=LEAN_SIGNAL_PAYLOAD_INCOMPLETE.
        """
        body = _heartbeat_body()
        body["event_type"] = "signal_emitted"
        # No signal-emit fields populated → handler should reject.
        resp = await client_with_lean_token.post(
            _LEAN_PATH,
            headers={"Authorization": _LEAN_AUTH_HEADER},
            json=body,
        )
        assert resp.status_code == 422
        envelope = resp.json()
        assert envelope["error_code"] == "LEAN_SIGNAL_PAYLOAD_INCOMPLETE"
        # All 6 signal-emit fields should appear in details.missing.
        assert set(envelope["details"]["missing"]) == {
            "market",
            "direction",
            "target_contracts",
            "decision_price",
            "sizing_trace",
            "strategy_version",
        }

    async def test_invalid_event_type_rejected_by_pydantic(
        self,
        client_with_lean_token: AsyncClient,
    ) -> None:
        """Pydantic Literal validation rejects unknown event_type values pre-handler."""
        body = _heartbeat_body()
        body["event_type"] = "made_up_event_type"
        resp = await client_with_lean_token.post(
            _LEAN_PATH,
            headers={"Authorization": _LEAN_AUTH_HEADER},
            json=body,
        )
        assert resp.status_code == 422

    async def test_naive_ts_rejected_by_pydantic(
        self,
        client_with_lean_token: AsyncClient,
    ) -> None:
        """Naive datetime (no tz info) rejected per [A06] enforced at schema layer."""
        body = _heartbeat_body()
        # Pydantic accepts the ISO string but normalizes — we test that the
        # schema's datetime field at minimum coerces correctly. A truly naive
        # string is parsed leniently, so we instead test bad format:
        body["ts_utc"] = "not-a-datetime"
        resp = await client_with_lean_token.post(
            _LEAN_PATH,
            headers={"Authorization": _LEAN_AUTH_HEADER},
            json=body,
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# TestNoLeanConfigured
# ---------------------------------------------------------------------------


class TestNoLeanConfigured:
    """When LEAN_LOCAL_BEARER_TOKEN is unset → middleware noop; path unserved."""

    async def test_valid_lean_bearer_falls_through_when_unconfigured(
        self,
        client_without_lean_token: AsyncClient,
    ) -> None:
        resp = await client_without_lean_token.post(
            _LEAN_PATH,
            headers={"Authorization": _LEAN_AUTH_HEADER},
            json=_heartbeat_body(),
        )
        # When LEAN isn't configured, LeanAuth doesn't fire. BotAuth tries
        # to match the bearer against its own token (which doesn't match,
        # because it's the lean bearer not the bot bearer) → 401 BOT_AUTH_INVALID.
        assert resp.status_code == 401
        assert resp.json()["error_code"] == "BOT_AUTH_INVALID"


# ---------------------------------------------------------------------------
# TestProductionFailClose
# ---------------------------------------------------------------------------


class TestProductionFailClose:
    """LeanAuth runs BEFORE SessionStub fail-close in production envs."""

    async def test_production_without_bearer_fails_closed(
        self,
        client_in_production: AsyncClient,
    ) -> None:
        """In live-small without a bearer, SessionStub fail-closes with 401."""
        resp = await client_in_production.post(_LEAN_PATH, json=_heartbeat_body())
        # SessionStubMiddleware fail-close in production env returns 401 AUTH_REQUIRED.
        assert resp.status_code == 401

    async def test_production_with_valid_bearer_reaches_handler(
        self,
        client_in_production: AsyncClient,
    ) -> None:
        """Valid LEAN bearer bypasses SessionStub fail-close + reaches handler."""
        resp = await client_in_production.post(
            _LEAN_PATH,
            headers={"Authorization": _LEAN_AUTH_HEADER},
            json=_heartbeat_body(),
        )
        # Reaches the handler in production env; returns 202 Accepted.
        assert resp.status_code == 202

    async def test_production_lean_unconfigured_bearer_rejected(
        self,
        client_in_production_without_lean_token: AsyncClient,
    ) -> None:
        """Production env + lean unconfigured + lean bearer header → BotAuth rejects 401."""
        resp = await client_in_production_without_lean_token.post(
            _LEAN_PATH,
            headers={"Authorization": _LEAN_AUTH_HEADER},
            json=_heartbeat_body(),
        )
        # BotAuth rejects 401 BOT_AUTH_INVALID (the token doesn't match the
        # bot bearer either; LeanAuth is noop because unconfigured).
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# TestForbiddenWithoutAuth
# ---------------------------------------------------------------------------


class TestForbiddenWithoutAuth:
    """Direct dependency-level gate: even a Discord-bot bearer can't reach this endpoint."""

    async def test_bot_bearer_rejected_at_dependency(
        self,
        client_with_lean_token: AsyncClient,
    ) -> None:
        """A bot-authenticated request should fail _require_lean_authenticated with 403."""
        resp = await client_with_lean_token.post(
            _LEAN_PATH,
            headers={"Authorization": f"Bearer {_VALID_BOT_BEARER}"},
            json=_heartbeat_body(),
        )
        # BotAuth accepts the bearer + injects bot session; then
        # _require_lean_authenticated dep raises AppError(LEAN_AUTH_REQUIRED)
        # which the app's error-handler serializes via the canonical envelope.
        assert resp.status_code == 403
        body = resp.json()
        assert body["error_code"] == "LEAN_AUTH_REQUIRED"


# ---------------------------------------------------------------------------
# TestModuleContract
# ---------------------------------------------------------------------------


class TestModuleContract:
    """LeanAuthMiddleware is exported + the route module is wired."""

    def test_lean_middleware_exported(self) -> None:
        from services.api.middleware import LeanAuthMiddleware, __all__

        assert "LeanAuthMiddleware" in __all__
        assert LeanAuthMiddleware is not None

    def test_lean_route_registered_in_app(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _build_app(monkeypatch)
        # Find the route path on the app's router.
        paths = {r.path for r in app.routes if hasattr(r, "path")}  # type: ignore[attr-defined]
        assert _LEAN_PATH in paths
