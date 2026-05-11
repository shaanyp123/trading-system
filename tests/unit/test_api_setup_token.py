"""Unit tests for `services/api/routes/setup.py`.

The route is exercised against an in-memory `SetupTokenRepo` stub. The
production repo is unit-tested against a real Postgres in
`tests/integration/test_setup_token_repo.py` (testcontainers).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient

from services.api.auth.ceremony import CeremonyStore, get_ceremony_store
from services.api.repos.setup_tokens import SetupTokenRow
from services.api.routes import setup as setup_route


class _StubRepo:
    """Minimal in-memory repo for unit tests."""

    def __init__(
        self,
        *,
        valid_token: str | None = None,
        intended_role: str = "owner",
    ) -> None:
        self._valid_token = valid_token
        self._intended_role = intended_role
        self.consume_calls: list[str] = []

    async def consume_if_valid(self, raw_token: str) -> SetupTokenRow | None:
        self.consume_calls.append(raw_token)
        if self._valid_token is not None and raw_token == self._valid_token:
            now = datetime.now(tz=UTC)
            return SetupTokenRow(
                token_uuid=uuid4(),
                intended_role=self._intended_role,  # type: ignore[arg-type]
                created_at=now - timedelta(minutes=1),
                expires_at=now + timedelta(hours=24),
                consumed_at=now,
            )
        return None

    async def has_unconsumed_owner_token(self) -> bool:
        return self._valid_token is not None

    async def insert_owner_token(self, raw_token: str) -> None:
        self._valid_token = raw_token


@pytest.mark.asyncio
async def test_verify_token_returns_200_with_session_id_when_valid(
    api_client: AsyncClient,
    override_dep,  # type: ignore[no-untyped-def]
) -> None:
    repo = _StubRepo(valid_token="valid-raw-token-1234")
    fresh_store = CeremonyStore()
    override_dep(setup_route._get_repo, lambda: repo)
    override_dep(get_ceremony_store, lambda: fresh_store)

    response = await api_client.post(
        "/api/setup/verify-token",
        json={"token": "valid-raw-token-1234"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["intended_role"] == "owner"
    assert isinstance(body["ceremony_session_id"], str)
    # CeremonyStore.mint_key returns secrets.token_urlsafe(16) ≈ 22 chars.
    # Lower-bound matches the WebAuthnRegisterChallengeRequest schema's
    # min_length=8 constraint.
    assert len(body["ceremony_session_id"]) >= 16
    assert repo.consume_calls == ["valid-raw-token-1234"]


@pytest.mark.asyncio
async def test_verify_token_persists_ceremony_in_store(
    api_client: AsyncClient,
    override_dep,  # type: ignore[no-untyped-def]
) -> None:
    """Day 24 regression: verify_token must call create_setup_ceremony so
    subsequent wizard endpoints (webauthn/register/challenge etc.) can
    look up the ceremony state by id.

    Day-5 bug: the route minted a free-floating UUID7 and returned it but
    never stored anything → wizard endpoints rejected with 401
    INVALID_CEREMONY_SESSION. This test guards against the regression.
    """
    repo = _StubRepo(valid_token="ceremony-persist-test")
    fresh_store = CeremonyStore()
    override_dep(setup_route._get_repo, lambda: repo)
    override_dep(get_ceremony_store, lambda: fresh_store)

    response = await api_client.post(
        "/api/setup/verify-token",
        json={"token": "ceremony-persist-test"},
    )

    assert response.status_code == 200
    ceremony_session_id = response.json()["ceremony_session_id"]

    # The ceremony MUST be findable in the store with kind="setup".
    state = await fresh_store.get(ceremony_session_id)
    assert state is not None, (
        "verify_token returned a ceremony_session_id but did not actually "
        "create the ceremony in the store — subsequent wizard endpoints "
        "would 401 with INVALID_CEREMONY_SESSION"
    )
    assert state.kind == "setup"
    assert state.intended_role == "owner"
    assert state.setup_token_uuid is not None


@pytest.mark.asyncio
async def test_verify_token_returns_401_envelope_when_invalid(
    api_client: AsyncClient,
    override_dep,  # type: ignore[no-untyped-def]
) -> None:
    repo = _StubRepo(valid_token="real-token")
    override_dep(setup_route._get_repo, lambda: repo)

    response = await api_client.post(
        "/api/setup/verify-token",
        json={"token": "wrong-token"},
    )

    assert response.status_code == 401
    body = response.json()
    assert body["error_code"] == "INVALID_SETUP_TOKEN"
    assert "invalid" in body["message"].lower()


@pytest.mark.asyncio
async def test_verify_token_rejects_short_token_with_validation_error(
    api_client: AsyncClient,
    override_dep,  # type: ignore[no-untyped-def]
) -> None:
    repo = _StubRepo()
    override_dep(setup_route._get_repo, lambda: repo)

    response = await api_client.post(
        "/api/setup/verify-token",
        json={"token": "short"},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error_code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_verify_token_is_csrf_exempt(
    api_client: AsyncClient,
    override_dep,  # type: ignore[no-untyped-def]
) -> None:
    """Bootstrap endpoint must not require a session cookie + CSRF header.

    Per the CSRF middleware allowlist in services/api/middleware.py: setup
    is exempt because no session exists yet at first-run.
    """
    repo = _StubRepo(valid_token="tok-tok-tok-tok")
    override_dep(setup_route._get_repo, lambda: repo)

    # No __Host-csrf_token cookie nor X-CSRF-Token header set:
    response = await api_client.post(
        "/api/setup/verify-token",
        json={"token": "tok-tok-tok-tok"},
    )

    assert response.status_code == 200
