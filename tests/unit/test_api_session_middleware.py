"""Unit tests for the real session-validation middleware (services/api/session.py).

The DB seam (`SessionMiddleware._load_session_context_from_db`) is
monkeypatched per [A22] — no SQL in unit tests. The real SQL path is
covered by the Docker-gated suite in
``tests/integration/test_api_session_middleware_db.py``.

Env matrix under test:
  * ``paper`` (the deployed C1 env) → REAL validation: 401 without a valid
    cookie, 503 on a DB failure, real ``SessionContext`` on success.
  * ``dev`` → Phase-0 stub unchanged (the local-dev + unit-suite seam).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from services.api import config as api_config
from services.api.auth.sessions import encode_cookie_value, mint_session_id
from services.api.session import SessionContext, SessionMiddleware

_SESSION_COOKIE = "__Host-trading_session"
_CSRF_COOKIE = "__Host-csrf_token"


def _paper_env() -> dict[str, str]:
    return {
        "API_DATABASE_URL": "postgresql+asyncpg://stub:stub@127.0.0.1:0/stub",
        "API_ENVIRONMENT": "paper",
        "API_VERSION": "test",
        "API_LOG_LEVEL": "INFO",
    }


def _real_context(account_id: str | None = None) -> SessionContext:
    now = datetime.now(tz=UTC)
    return SessionContext(
        user_id=account_id or str(uuid4()),
        username="operator",
        role="owner",
        auth_strength="strong",
        last_uv_at=now - timedelta(seconds=30),
        session_expires_at=now + timedelta(minutes=30),
        webauthn_enrolled=True,
        totp_enrolled=True,
        is_phase0_stub=False,
    )


def _valid_cookie() -> str:
    return encode_cookie_value(mint_session_id())


@pytest.fixture
def paper_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    for k, v in _paper_env().items():
        monkeypatch.setenv(k, v)
    api_config.get_settings.cache_clear()
    from services.api.main import create_app

    return create_app()


@pytest_asyncio.fixture
async def paper_client(paper_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=paper_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        yield client


def _patch_loader(monkeypatch: pytest.MonkeyPatch, loader: Any) -> None:
    """Swap the DB seam on the class (instances are built lazily by Starlette)."""
    monkeypatch.setattr(SessionMiddleware, "_load_session_context_from_db", loader)


class TestRealValidationRejects:
    @pytest.mark.asyncio
    async def test_no_cookie_is_401_auth_required(
        self, paper_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _never_called(self: Any, session_id: bytes) -> SessionContext | None:
            raise AssertionError("loader must not run without a decodable cookie")

        _patch_loader(monkeypatch, _never_called)
        response = await paper_client.get("/api/auth/me")
        assert response.status_code == 401
        assert response.json()["error_code"] == "AUTH_REQUIRED"

    @pytest.mark.asyncio
    async def test_malformed_cookie_is_401_without_db_lookup(
        self, paper_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _never_called(self: Any, session_id: bytes) -> SessionContext | None:
            raise AssertionError("loader must not run for an undecodable cookie")

        _patch_loader(monkeypatch, _never_called)
        response = await paper_client.get(
            "/api/auth/me", cookies={_SESSION_COOKIE: "!!not-base64url!!"}
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == "AUTH_REQUIRED"

    @pytest.mark.asyncio
    async def test_unknown_or_expired_session_is_401(
        self, paper_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _miss(self: Any, session_id: bytes) -> SessionContext | None:
            return None

        _patch_loader(monkeypatch, _miss)
        response = await paper_client.get(
            "/api/auth/me", cookies={_SESSION_COOKIE: _valid_cookie()}
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == "AUTH_REQUIRED"

    @pytest.mark.asyncio
    async def test_db_failure_is_503_fail_closed(
        self, paper_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _boom(self: Any, session_id: bytes) -> SessionContext | None:
            raise RuntimeError("db unavailable")

        _patch_loader(monkeypatch, _boom)
        response = await paper_client.get(
            "/api/auth/me", cookies={_SESSION_COOKIE: _valid_cookie()}
        )
        assert response.status_code == 503
        assert response.json()["error_code"] == "AUTH_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_rejection_never_injects_stub(
        self, paper_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 2026-07-09 exposure pin: paper must NEVER answer with the
        Phase-0 stub identity."""

        async def _miss(self: Any, session_id: bytes) -> SessionContext | None:
            return None

        _patch_loader(monkeypatch, _miss)
        response = await paper_client.get("/api/auth/me")
        assert response.status_code == 401
        assert "phase0-stub-owner" not in response.text

    @pytest.mark.asyncio
    async def test_risk_loosening_post_is_401_not_reachable(
        self, paper_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kill-switch resume — the concrete risk-loosening endpoint from the
        incident — is refused outright for anonymous callers."""

        async def _miss(self: Any, session_id: bytes) -> SessionContext | None:
            return None

        _patch_loader(monkeypatch, _miss)
        csrf = "token-value"
        response = await paper_client.post(
            "/api/system/kill-switch/resume",
            json={},
            cookies={_CSRF_COOKIE: csrf},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == "AUTH_REQUIRED"


class TestRealValidationAccepts:
    @pytest.mark.asyncio
    async def test_valid_cookie_injects_real_context(
        self, paper_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _real_context()

        async def _hit(self: Any, session_id: bytes) -> SessionContext | None:
            assert isinstance(session_id, bytes) and len(session_id) == 32
            return ctx

        _patch_loader(monkeypatch, _hit)
        response = await paper_client.get(
            "/api/auth/me", cookies={_SESSION_COOKIE: _valid_cookie()}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["user_id"] == ctx.user_id
        assert body["username"] == "operator"
        assert body["auth_strength"] == "strong"
        assert body["webauthn_enrolled"] is True

    @pytest.mark.asyncio
    async def test_weak_session_surfaces_weak_strength(
        self, paper_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(tz=UTC)
        ctx = SessionContext(
            user_id=str(uuid4()),
            username="operator",
            role="owner",
            auth_strength="weak",
            last_uv_at=None,
            session_expires_at=now + timedelta(minutes=30),
            webauthn_enrolled=False,
            totp_enrolled=True,
            is_phase0_stub=False,
        )

        async def _hit(self: Any, session_id: bytes) -> SessionContext | None:
            return ctx

        _patch_loader(monkeypatch, _hit)
        response = await paper_client.get(
            "/api/auth/me", cookies={_SESSION_COOKIE: _valid_cookie()}
        )
        assert response.status_code == 200
        assert response.json()["auth_strength"] == "weak"
        assert response.json()["last_uv_at"] is None


class TestCsrfBootstrapRealMode:
    @pytest.mark.asyncio
    async def test_401_response_mints_csrf_cookie(
        self, paper_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh anonymous browser must be able to bootstrap the CSRF
        cookie off its first (rejected) request, or it could never POST to
        the login endpoints."""

        async def _miss(self: Any, session_id: bytes) -> SessionContext | None:
            return None

        _patch_loader(monkeypatch, _miss)
        response = await paper_client.get("/api/auth/me")
        assert response.status_code == 401
        raw = [h for h in response.headers.get_list("set-cookie") if f"{_CSRF_COOKIE}=" in h]
        assert len(raw) == 1
        assert "Secure" in raw[0]  # paper env: __Host- prefix invariant

    @pytest.mark.asyncio
    async def test_csrf_cookie_not_churned_when_present(
        self, paper_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _real_context()

        async def _hit(self: Any, session_id: bytes) -> SessionContext | None:
            return ctx

        _patch_loader(monkeypatch, _hit)
        response = await paper_client.get(
            "/api/auth/me",
            cookies={_SESSION_COOKIE: _valid_cookie(), _CSRF_COOKIE: "existing"},
        )
        assert response.status_code == 200
        raw = [h for h in response.headers.get_list("set-cookie") if f"{_CSRF_COOKIE}=" in h]
        assert raw == []


class TestExemptPaths:
    @pytest.mark.asyncio
    async def test_health_passes_without_session(
        self, paper_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _never_called(self: Any, session_id: bytes) -> SessionContext | None:
            raise AssertionError("loader must not run on exempt paths")

        _patch_loader(monkeypatch, _never_called)
        response = await paper_client.get("/api/health")
        # Day 5 health route returns 503 if the pool isn't initialized; the
        # point is the session middleware did not 401.
        assert response.status_code in (200, 503)

    @pytest.mark.asyncio
    async def test_logout_passes_middleware_without_session(
        self,
        paper_app: FastAPI,
        paper_client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The auth-ceremony exemptions: a browser with an already-expired
        session must still be able to log out (clear cookies). Logout with
        no session cookie never touches its DB dependency, so it exercises
        the exemption end-to-end in this lifespan-less test app."""

        async def _never_called(self: Any, session_id: bytes) -> SessionContext | None:
            raise AssertionError("loader must not run on exempt paths")

        _patch_loader(monkeypatch, _never_called)

        from services.api.routes.auth import _get_db

        async def _stub_db() -> Any:
            yield object()  # handler skips DB work when no session cookie

        paper_app.dependency_overrides[_get_db] = _stub_db
        csrf = "bootstrap-token"
        response = await paper_client.post(
            "/api/auth/logout",
            json={},
            cookies={_CSRF_COOKIE: csrf},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200
        assert response.json()["success"] is True

    def test_login_and_wizard_paths_are_exempt(self) -> None:
        """Pins the exemption SET itself: every auth-ceremony endpoint a
        browser must reach without a session, plus the machine paths."""
        from services.api.session import _SESSION_EXEMPT_PATHS

        for path in (
            "/api/auth/webauthn/challenge",
            "/api/auth/webauthn/verify",
            "/api/auth/totp/verify",
            "/api/auth/recover",
            "/api/auth/webauthn/register/challenge",
            "/api/auth/webauthn/register/verify",
            "/api/auth/totp/enroll-challenge",
            "/api/auth/totp/setup-verify",
            "/api/auth/backup-codes/generate",
            "/api/auth/logout",
            "/api/health",
            "/api/setup/verify-token",
        ):
            assert path in _SESSION_EXEMPT_PATHS
        # And the ones that must NOT be exempt:
        assert "/api/internal/watchdog" not in _SESSION_EXEMPT_PATHS  # unmounted
        assert "/api/auth/me" not in _SESSION_EXEMPT_PATHS
        assert "/api/auth/backup-codes/regenerate" not in _SESSION_EXEMPT_PATHS
        assert "/api/sse/events" not in _SESSION_EXEMPT_PATHS


class TestBotBearerPath:
    @pytest.mark.asyncio
    async def test_bot_bearer_bypasses_cookie_validation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for k, v in _paper_env().items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("API_DISCORD_BOT_BEARER_TOKEN", "bot-test-token")
        api_config.get_settings.cache_clear()
        from services.api.main import create_app

        async def _never_called(self: Any, session_id: bytes) -> SessionContext | None:
            raise AssertionError("loader must not run for bot-authenticated requests")

        _patch_loader(monkeypatch, _never_called)
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://testserver") as client:
            response = await client.get(
                "/api/auth/me",
                headers={"Authorization": "Bearer bot-test-token"},
            )
        assert response.status_code == 200
        assert response.json()["username"] == "discord-bot"


class TestDevStubSeam:
    @pytest.mark.asyncio
    async def test_dev_env_still_injects_stub(self, api_client: AsyncClient) -> None:
        """Locks the dev-only stub seam the unit suite itself relies on."""
        response = await api_client.get("/api/auth/me")
        assert response.status_code == 200
        assert response.json()["user_id"] == "phase0-stub-owner"
