"""Unit tests for ``services.api.middleware.BotAuthMiddleware`` (Day 23).

A22 N/A; A06 enforced (every datetime tz-aware UTC); A27 N/A here (the
api ↔ bot HTTP contract is exercised via the test below + at the Day 23
carryover via the operator runbook).

Strategy: tests target POST ``/api/system/kill-switch/invoke`` because
its handler depends only on ``get_session_context`` (no DB-touching repo
dep). This lets us verify the FULL middleware chain — BotAuth + CSRF
bypass + SessionStub injection or skip + route reaches handler — without
needing a live Postgres or testcontainer.

Coverage matrix:

  * No Authorization header → falls through to SessionStub; POST without
    CSRF gets 403 (the stub session has no cookies)
  * Invalid Authorization header → 401 BOT_AUTH_INVALID + canonical
    envelope
  * Empty/non-bearer Authorization → falls through unchanged
  * Valid bearer token → request.state.session populated as
    discord-bot service-account; CSRF bypass; route reaches its 501 stub
  * Valid bearer + bad body → 422 (validation runs after auth+CSRF)
  * No bot bearer configured (None setting) → bot path unserved; bearer
    header falls through unchanged
  * Production env without bearer → 401 AUTH_REQUIRED (SessionStub
    fail-close)
  * Production env with valid bearer → bypasses fail-close; reaches 501
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from services.api import config as api_config


def _stub_env(
    *,
    bot_token: str | None = "bot-bearer-secret-test",
    environment: str = "dev",
) -> dict[str, str]:
    env = {
        "API_DATABASE_URL": "postgresql+asyncpg://stub:stub@127.0.0.1:0/stub",
        "API_ENVIRONMENT": environment,
        "API_VERSION": "test",
        "API_LOG_LEVEL": "INFO",
    }
    if bot_token is not None:
        env["API_DISCORD_BOT_BEARER_TOKEN"] = bot_token
    return env


def _build_app(monkeypatch: pytest.MonkeyPatch, **kwargs: str | None) -> FastAPI:
    for k, v in _stub_env(**kwargs).items():  # type: ignore[arg-type]
        monkeypatch.setenv(k, v)
    # Clear cached settings + force re-import of main so the new env takes
    api_config.get_settings.cache_clear()
    main_mod = importlib.import_module("services.api.main")
    return main_mod.create_app()


_VALID_BODY = {"trigger": "manual_judgment", "reason": "test halt from unit test"}
_INVOKE_PATH = "/api/system/kill-switch/invoke"
_VALID_BEARER = "Bearer bot-bearer-secret-test"


# Day 25: ``/api/system/kill-switch/invoke`` is no longer a 501 stub — it's
# a real handler that requires the ``get_session`` DB dep + a Phase1 repo.
# These overrides let the bot-auth tests reach the route handler without a
# live Postgres; the route then hits its 409 NO_ACTIVE_ACCOUNT branch (the
# stub repo returns ``None`` from ``fetch_active_account_id``). The
# semantic the tests care about — "BotAuth bypassed CSRF + the route ran" —
# is preserved; the response code changes from 501 to 409.
async def _fake_get_session() -> AsyncIterator[object]:
    class _NoopSession:
        async def execute(self, *args: object, **kwargs: object) -> object:  # pragma: no cover
            raise RuntimeError("fake session.execute called — route reached real DB unexpectedly")

    yield _NoopSession()


class _NoAccountRepo:
    """Stub :class:`Phase1QueryRepo` returning no active account.

    Routes call ``fetch_active_account_id`` first; receiving ``None`` is
    enough to short-circuit before any other repo method runs. The other
    methods are intentionally unimplemented so test coverage stays honest —
    if a route relies on something else, we'll notice.
    """

    async def fetch_active_account_id(self) -> object:
        return None

    def __getattr__(self, name: str) -> object:  # pragma: no cover
        raise NotImplementedError(
            f"_NoAccountRepo.{name} called — bot-auth tests should hit the "
            "NO_ACTIVE_ACCOUNT branch before any other repo method"
        )


def _install_route_overrides(app: FastAPI) -> None:
    """Install ``get_session`` + ``_get_repo`` overrides for the bot-auth POST tests.

    Day 25: the kill-switch route is wired end-to-end so the tests need the
    full dep graph satisfied. We bypass with a fake session + no-account
    repo so the route reaches its 409 branch deterministically.
    """
    from services.api.db import get_session
    from services.api.routes import system as system_route

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[system_route._get_repo] = lambda: _NoAccountRepo()


@pytest_asyncio.fixture
async def client_with_bot_token(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    app = _build_app(monkeypatch)
    _install_route_overrides(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest_asyncio.fixture
async def client_without_bot_token(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    app = _build_app(monkeypatch, bot_token=None)
    _install_route_overrides(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest_asyncio.fixture
async def client_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    app = _build_app(monkeypatch, environment="live-small")
    _install_route_overrides(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


# ---------------------------------------------------------------------------
# TestNoAuthHeader
# ---------------------------------------------------------------------------


class TestNoAuthHeader:
    """No Authorization header → BotAuth pass-through; CSRF gate runs."""

    async def test_health_endpoint_works_without_header(
        self,
        monkeypatch: pytest.MonkeyPatch,
        client_with_bot_token: AsyncClient,
    ) -> None:
        # /api/health is exempt from session entirely; verifies BotAuth
        # noop on missing header.
        from services.api import db as api_db

        async def _stub_ping() -> tuple[bool, str | None]:
            return (True, None)

        monkeypatch.setattr(api_db, "ping", _stub_ping)
        resp = await client_with_bot_token.get("/api/health")
        assert resp.status_code == 200

    async def test_post_without_header_blocked_by_csrf(
        self,
        client_with_bot_token: AsyncClient,
    ) -> None:
        # Without bearer header, CSRF gate fires (no cookie + no header).
        # 403 CSRF_REJECTED proves the bot middleware passed through and
        # CSRF ran normally (i.e. the bot bypass didn't accidentally
        # apply to non-bot requests).
        resp = await client_with_bot_token.post(_INVOKE_PATH, json=_VALID_BODY)
        assert resp.status_code == 403
        body = resp.json()
        assert body["error_code"] == "CSRF_REJECTED"


# ---------------------------------------------------------------------------
# TestInvalidBearer
# ---------------------------------------------------------------------------


class TestInvalidBearer:
    async def test_invalid_bearer_returns_401(
        self,
        client_with_bot_token: AsyncClient,
    ) -> None:
        resp = await client_with_bot_token.get(
            "/api/health",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["error_code"] == "BOT_AUTH_INVALID"
        assert "did not match" in body["message"]

    async def test_invalid_bearer_does_not_reach_route(
        self,
        client_with_bot_token: AsyncClient,
    ) -> None:
        # POST with invalid bearer → 401 short-circuit, NOT the route's 501.
        resp = await client_with_bot_token.post(
            _INVOKE_PATH,
            headers={"Authorization": "Bearer wrong-token"},
            json=_VALID_BODY,
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["error_code"] == "BOT_AUTH_INVALID"

    async def test_empty_bearer_falls_through(
        self,
        client_with_bot_token: AsyncClient,
    ) -> None:
        # "Bearer " with no token → BotAuth treats as no-auth, falls through.
        # CSRF then rejects the POST (403, not 401).
        resp = await client_with_bot_token.post(
            _INVOKE_PATH,
            headers={"Authorization": "Bearer "},
            json=_VALID_BODY,
        )
        assert resp.status_code == 403
        body = resp.json()
        assert body["error_code"] == "CSRF_REJECTED"

    async def test_non_bearer_scheme_falls_through(
        self,
        client_with_bot_token: AsyncClient,
    ) -> None:
        # Basic auth or any non-bearer scheme is ignored by BotAuth.
        resp = await client_with_bot_token.post(
            _INVOKE_PATH,
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
            json=_VALID_BODY,
        )
        assert resp.status_code == 403
        body = resp.json()
        assert body["error_code"] == "CSRF_REJECTED"


# ---------------------------------------------------------------------------
# TestValidBearer
# ---------------------------------------------------------------------------


class TestValidBearer:
    async def test_valid_bearer_bypasses_csrf_and_reaches_handler(
        self,
        client_with_bot_token: AsyncClient,
    ) -> None:
        # POST without CSRF cookie/header normally returns 403. With bot
        # bearer, CSRFMiddleware skips → request reaches the route which
        # (Day 25 onward) returns 409 NO_ACTIVE_ACCOUNT — the stub repo
        # returns ``None`` from ``fetch_active_account_id``. Prior to
        # Day 25 the route returned 501 KILL_SWITCH_HANDLER_NOT_WIRED.
        # The semantic the test asserts — "BotAuth bypassed CSRF + the
        # route handler ran" — is preserved by the 409.
        resp = await client_with_bot_token.post(
            _INVOKE_PATH,
            headers={"Authorization": _VALID_BEARER},
            json=_VALID_BODY,
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["error_code"] == "NO_ACTIVE_ACCOUNT"

    async def test_valid_bearer_validates_request_body_after_csrf_bypass(
        self,
        client_with_bot_token: AsyncClient,
    ) -> None:
        # Bad body should still 422 even with bot auth; CSRF bypass
        # doesn't bypass body validation.
        resp = await client_with_bot_token.post(
            _INVOKE_PATH,
            headers={"Authorization": _VALID_BEARER},
            json={"trigger": "not-a-real-trigger", "reason": "test"},
        )
        assert resp.status_code == 422

    async def test_valid_bearer_on_get_health(
        self,
        monkeypatch: pytest.MonkeyPatch,
        client_with_bot_token: AsyncClient,
    ) -> None:
        # GET /api/health with valid bearer → 200 (auth accepted, route
        # runs normally). Confirms BotAuth doesn't break the public path.
        from services.api import db as api_db

        async def _stub_ping() -> tuple[bool, str | None]:
            return (True, None)

        monkeypatch.setattr(api_db, "ping", _stub_ping)
        resp = await client_with_bot_token.get(
            "/api/health",
            headers={"Authorization": _VALID_BEARER},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# TestNoBotConfigured
# ---------------------------------------------------------------------------


class TestNoBotConfigured:
    """When API_DISCORD_BOT_BEARER_TOKEN is unset, the bot path is unserved.

    Requests with a bearer header fall through to downstream middleware
    (NOT rejected with 401 — that would conflict with the watchdog auth
    path which uses its own bearer middleware on a different route).
    """

    async def test_no_token_configured_falls_through(
        self,
        client_without_bot_token: AsyncClient,
    ) -> None:
        # POST with bearer header but no configured bot token → BotAuth
        # noop → CSRF rejects (403, not 401). Confirms the noop semantics.
        resp = await client_without_bot_token.post(
            _INVOKE_PATH,
            headers={"Authorization": "Bearer any-token-at-all"},
            json=_VALID_BODY,
        )
        assert resp.status_code == 403
        body = resp.json()
        assert body["error_code"] == "CSRF_REJECTED"


# ---------------------------------------------------------------------------
# TestProductionFailClose
# ---------------------------------------------------------------------------


class TestProductionFailClose:
    """In live envs, SessionStub fail-closes UNLESS BotAuth injected a session."""

    async def test_no_auth_header_in_production_returns_401(
        self,
        client_in_production: AsyncClient,
    ) -> None:
        # POST without any auth → SessionStub fail-closes with 401.
        # (Note: SessionStub runs INNER of CSRF in middleware order, so
        # in production its 401 short-circuits before CSRF would 403.)
        resp = await client_in_production.post(_INVOKE_PATH, json=_VALID_BODY)
        assert resp.status_code == 401
        body = resp.json()
        assert body["error_code"] == "AUTH_REQUIRED"

    async def test_valid_bot_bearer_in_production_succeeds(
        self,
        client_in_production: AsyncClient,
    ) -> None:
        # With bot bearer, BotAuth injects strong-auth session →
        # SessionStub skips its production fail-close because session is
        # already populated → route runs. Day 25 onward the route returns
        # 409 NO_ACTIVE_ACCOUNT (no account in the stub repo) instead of
        # the prior 501 stub. Either way, the test asserts that the
        # production fail-close was bypassed by BotAuth.
        resp = await client_in_production.post(
            _INVOKE_PATH,
            headers={"Authorization": _VALID_BEARER},
            json=_VALID_BODY,
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["error_code"] == "NO_ACTIVE_ACCOUNT"


# ---------------------------------------------------------------------------
# TestSettingsConfigField
# ---------------------------------------------------------------------------


class TestSettingsConfigField:
    """Smoke that APISettings accepts the new field."""

    def test_settings_accepts_bot_bearer_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "API_DATABASE_URL",
            "postgresql+asyncpg://stub:stub@127.0.0.1:0/stub",
        )
        monkeypatch.setenv("API_DISCORD_BOT_BEARER_TOKEN", "test-token")
        api_config.get_settings.cache_clear()
        settings = api_config.get_settings()
        assert settings.discord_bot_bearer_token is not None
        assert settings.discord_bot_bearer_token.get_secret_value() == "test-token"

    def test_settings_defaults_bot_bearer_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "API_DATABASE_URL",
            "postgresql+asyncpg://stub:stub@127.0.0.1:0/stub",
        )
        monkeypatch.delenv("API_DISCORD_BOT_BEARER_TOKEN", raising=False)
        api_config.get_settings.cache_clear()
        settings = api_config.get_settings()
        assert settings.discord_bot_bearer_token is None
