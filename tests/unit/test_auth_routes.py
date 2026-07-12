"""Unit tests for ``services.api.routes.auth`` — the WebAuthn / TOTP / backup-code endpoints.

Uses the ``api_app`` + ``api_client`` fixtures from ``tests/unit/conftest.py``.
DB-touching deps are overridden with a stub ``AuthRepo`` + a fake DB session;
``CeremonyStore`` is overridden with a fresh instance per test;
``webauthn`` module helpers are monkeypatched at the route layer so tests
don't need real WebAuthn primitives (those are covered separately in
``test_auth_webauthn.py``).

Test classes mirror the endpoint inventory in ``routes/auth.py``.

A22 N/A (no Postgres I/O — TestClient + dependency_overrides only).
A06 enforced — every asserted timestamp is tz-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from services.api.auth import sessions as sessions_mod
from services.api.auth import webauthn as webauthn_mod
from services.api.auth.ceremony import CeremonyStore, get_ceremony_store
from services.api.config import APISettings
from services.api.db import get_session
from services.api.repos.auth import (
    AccountRow,
    BackupCodeRow,
    TotpSecretRow,
    WebAuthnCredentialRow,
)
from services.api.routes.auth import _get_auth_repo


# ---------------------------------------------------------------------------
# Stub repo + db
# ---------------------------------------------------------------------------
@dataclass
class _StubAuthRepo:
    """In-memory ``AuthRepo`` for unit tests."""

    accounts_by_username: dict[str, AccountRow] = field(default_factory=dict)
    webauthn_credentials: list[WebAuthnCredentialRow] = field(default_factory=list)
    totp_secrets: dict[UUID, TotpSecretRow] = field(default_factory=dict)
    backup_codes: list[BackupCodeRow] = field(default_factory=list)
    inserts: list[dict[str, Any]] = field(default_factory=list)

    async def find_account_by_username(self, username: str) -> AccountRow | None:
        return self.accounts_by_username.get(username)

    async def find_account_by_id(self, account_id: UUID) -> AccountRow | None:
        for row in self.accounts_by_username.values():
            if row.id == account_id:
                return row
        return None

    async def find_or_create_bootstrap_account(
        self, username: str, role: Literal["owner", "reader"]
    ) -> AccountRow:
        if username in self.accounts_by_username:
            return self.accounts_by_username[username]
        row = AccountRow(id=uuid4(), external_account_id=username, role=role)
        self.accounts_by_username[username] = row
        return row

    async def list_webauthn_credentials(
        self, account_id: UUID, *, active_only: bool = True
    ) -> list[WebAuthnCredentialRow]:
        return [
            c
            for c in self.webauthn_credentials
            if c.account_id == account_id and (not active_only or c.active)
        ]

    async def get_webauthn_credential_by_id(
        self, credential_id: bytes
    ) -> WebAuthnCredentialRow | None:
        for c in self.webauthn_credentials:
            if c.credential_id == credential_id:
                return c
        return None

    async def insert_webauthn_credential(
        self,
        *,
        account_id: UUID,
        credential_id: bytes,
        public_key: bytes,
        sign_count: int,
        authenticator_attachment: str | None,
        nickname: str | None,
    ) -> UUID:
        new_id = uuid4()
        self.webauthn_credentials.append(
            WebAuthnCredentialRow(
                id=new_id,
                account_id=account_id,
                credential_id=credential_id,
                public_key=public_key,
                sign_count=sign_count,
                nickname=nickname,
                active=True,
            )
        )
        self.inserts.append(
            {
                "type": "webauthn_credential",
                "credential_id": credential_id,
                "account_id": account_id,
            }
        )
        return new_id

    async def update_webauthn_sign_count(
        self, *, credential_id: bytes, new_sign_count: int
    ) -> None:
        for idx, c in enumerate(self.webauthn_credentials):
            if c.credential_id == credential_id:
                self.webauthn_credentials[idx] = WebAuthnCredentialRow(
                    id=c.id,
                    account_id=c.account_id,
                    credential_id=c.credential_id,
                    public_key=c.public_key,
                    sign_count=new_sign_count,
                    nickname=c.nickname,
                    active=c.active,
                )
                break

    async def get_totp_secret(self, account_id: UUID) -> TotpSecretRow | None:
        return self.totp_secrets.get(account_id)

    async def upsert_totp_secret(self, *, account_id: UUID, encrypted_secret: bytes) -> None:
        self.totp_secrets[account_id] = TotpSecretRow(
            account_id=account_id,
            encrypted_secret=encrypted_secret,
            enrolled_at_utc=datetime.now(tz=UTC),
            last_used_at_utc=None,
        )
        self.inserts.append({"type": "totp_secret", "account_id": account_id})

    async def stamp_totp_last_used(self, account_id: UUID) -> None:
        if account_id in self.totp_secrets:
            row = self.totp_secrets[account_id]
            self.totp_secrets[account_id] = TotpSecretRow(
                account_id=row.account_id,
                encrypted_secret=row.encrypted_secret,
                enrolled_at_utc=row.enrolled_at_utc,
                last_used_at_utc=datetime.now(tz=UTC),
            )

    async def list_unused_backup_codes(self, account_id: UUID) -> list[BackupCodeRow]:
        return [
            c for c in self.backup_codes if c.account_id == account_id and c.used_at_utc is None
        ]

    async def insert_backup_codes(
        self,
        *,
        account_id: UUID,
        generation_id: UUID,
        code_hashes: list[str],
    ) -> None:
        for h in code_hashes:
            self.backup_codes.append(
                BackupCodeRow(
                    id=uuid4(),
                    account_id=account_id,
                    code_hash=h,
                    used_at_utc=None,
                    generation_id=generation_id,
                )
            )
        self.inserts.append(
            {
                "type": "backup_codes",
                "account_id": account_id,
                "generation_id": generation_id,
                "count": len(code_hashes),
            }
        )

    async def mark_backup_code_used(self, code_row_id: UUID) -> None:
        for idx, c in enumerate(self.backup_codes):
            if c.id == code_row_id:
                self.backup_codes[idx] = BackupCodeRow(
                    id=c.id,
                    account_id=c.account_id,
                    code_hash=c.code_hash,
                    used_at_utc=datetime.now(tz=UTC),
                    generation_id=c.generation_id,
                )
                break

    async def invalidate_backup_codes_for_account(self, account_id: UUID) -> None:
        for idx, c in enumerate(self.backup_codes):
            if c.account_id == account_id and c.used_at_utc is None:
                self.backup_codes[idx] = BackupCodeRow(
                    id=c.id,
                    account_id=c.account_id,
                    code_hash=c.code_hash,
                    used_at_utc=datetime.now(tz=UTC),
                    generation_id=c.generation_id,
                )


class _FakeDbSession:
    """Stand-in for ``AsyncSession`` -- ``sessions_mod.create_session`` calls
    ``execute`` + ``commit``; we monkeypatch ``create_session`` itself in the
    tests that need a session row, so this stub never has its methods
    invoked in practice.
    """

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def commit(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def stub_repo() -> _StubAuthRepo:
    return _StubAuthRepo()


@pytest.fixture
def fresh_ceremony_store() -> CeremonyStore:
    return CeremonyStore()


@pytest.fixture
def wire_auth_overrides(
    api_app: FastAPI,
    stub_repo: _StubAuthRepo,
    fresh_ceremony_store: CeremonyStore,
) -> None:
    """Override ``_get_auth_repo``, ``get_ceremony_store``, ``get_session``,
    and ``get_settings`` so route handlers run against stubs.
    """
    api_app.dependency_overrides[_get_auth_repo] = lambda: stub_repo
    api_app.dependency_overrides[get_ceremony_store] = lambda: fresh_ceremony_store
    api_app.dependency_overrides[get_session] = lambda: _FakeDbSession()

    # Override settings to include a valid TOTP encryption key + WebAuthn rp_id.
    from pydantic import SecretStr

    from services.api.config import get_settings

    real_settings = get_settings()
    fake_settings = APISettings.model_construct(
        **{
            **real_settings.model_dump(),
            "totp_encryption_key": SecretStr(
                "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="  # 32 zero bytes
            ),
            "webauthn_rp_id": "localhost",
            "webauthn_rp_name": "trading-system-test",
            "webauthn_origin": "http://localhost:3000",
            "operator_username": "operator",
        }
    )
    api_app.dependency_overrides[get_settings] = lambda: fake_settings


@pytest.fixture
def stub_create_session(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace ``sessions_mod.create_session`` so route handlers can issue
    a "session" without needing a real DB.

    Returns the calls list so tests can assert on session-creation params.
    """
    calls: list[dict[str, Any]] = []

    async def _fake(db: Any, **kwargs: Any) -> sessions_mod.SessionRow:
        calls.append(kwargs)
        return sessions_mod.SessionRow(
            session_id=b"\x99" * sessions_mod.SESSION_ID_BYTES,
            account_id=kwargs["account_id"],
            created_at_utc=datetime.now(tz=UTC),
            last_activity_utc=datetime.now(tz=UTC),
            last_uv_at_utc=kwargs.get("last_uv_at_utc"),
            user_agent=kwargs.get("user_agent"),
            ip_address=kwargs.get("ip_address"),
            totp_bootstrap_only=kwargs["totp_bootstrap_only"],
            evicted_at_utc=None,
            evicted_reason=None,
            expires_at_utc=datetime.now(tz=UTC),
        )

    monkeypatch.setattr(sessions_mod, "create_session", _fake)
    # Also monkeypatch via the routes module's reference since it was already
    # imported via ``from services.api.auth import sessions as sessions_mod``.
    from services.api.routes import auth as auth_route_mod

    monkeypatch.setattr(auth_route_mod.sessions_mod, "create_session", _fake)
    return calls


# ---------------------------------------------------------------------------
# /api/auth/me
# ---------------------------------------------------------------------------
class TestAuthMe:
    @pytest.mark.asyncio
    async def test_returns_stub_session_envelope(
        self, api_client: AsyncClient, wire_auth_overrides: None
    ) -> None:
        response = await api_client.get("/api/auth/me")
        assert response.status_code == 200
        payload = response.json()
        assert payload["username"] == "operator"
        assert payload["role"] == "owner"
        # Day 25 (Week 7 Mon): the Phase-0 stub session was realigned to
        # ``auth_strength="strong"`` to match its docstring intent so the
        # 5-min re-auth gate on risk-loosening endpoints
        # (kill-switch resume, backup-codes regenerate) is reachable in
        # paper/dev environments. Production envs fail closed before this
        # stub injects, so the change carries no security weight there.
        assert payload["auth_strength"] == "strong"
        assert payload["webauthn_enrolled"] is False
        assert payload["totp_enrolled"] is False


# ---------------------------------------------------------------------------
# WebAuthn registration
# ---------------------------------------------------------------------------
def _csrf_kwargs(token: str = "test-csrf") -> dict[str, Any]:
    """Helper: cookie + header for double-submit CSRF on POSTs."""
    return {
        "cookies": {"__Host-csrf_token": token},
        "headers": {"X-CSRF-Token": token},
    }


class TestWebAuthnRegisterChallenge:
    @pytest.mark.asyncio
    async def test_invalid_ceremony_returns_401(
        self,
        api_client: AsyncClient,
        wire_auth_overrides: None,
    ) -> None:
        response = await api_client.post(
            "/api/auth/webauthn/register/challenge",
            json={"ceremony_session_id": "definitely-not-real"},
            **_csrf_kwargs(),
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == "INVALID_CEREMONY_SESSION"

    @pytest.mark.asyncio
    async def test_happy_path_issues_options_and_persists_state(
        self,
        api_client: AsyncClient,
        wire_auth_overrides: None,
        fresh_ceremony_store: CeremonyStore,
        stub_repo: _StubAuthRepo,
    ) -> None:
        # Mint a setup ceremony first.
        ceremony_id = await fresh_ceremony_store.create_setup_ceremony(
            setup_token_uuid=uuid4(),
            intended_role="owner",
        )
        response = await api_client.post(
            "/api/auth/webauthn/register/challenge",
            json={"ceremony_session_id": ceremony_id, "nickname": "YubiKey 5C"},
            **_csrf_kwargs(),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        # py_webauthn options_to_json shape
        for key in ("rp", "user", "challenge", "pubKeyCredParams"):
            assert key in body["options"], f"missing options.{key}"
        # The state should be persisted with the challenge bytes + account.
        state = await fresh_ceremony_store.get(ceremony_id)
        assert state is not None
        assert state.webauthn_register_challenge is not None
        assert state.webauthn_user_id is not None
        assert state.webauthn_nickname == "YubiKey 5C"
        assert state.account_id is not None
        # The bootstrap account should now exist in the stub.
        assert "operator" in stub_repo.accounts_by_username


class TestWebAuthnRegisterVerify:
    @pytest.mark.asyncio
    async def test_out_of_order_returns_400(
        self,
        api_client: AsyncClient,
        wire_auth_overrides: None,
        fresh_ceremony_store: CeremonyStore,
    ) -> None:
        # Mint a setup ceremony but don't issue the register challenge first.
        ceremony_id = await fresh_ceremony_store.create_setup_ceremony(
            setup_token_uuid=uuid4(),
            intended_role="owner",
        )
        response = await api_client.post(
            "/api/auth/webauthn/register/verify",
            json={"ceremony_session_id": ceremony_id, "credential": {"id": "x"}},
            **_csrf_kwargs(),
        )
        assert response.status_code == 400
        assert response.json()["error_code"] == "CEREMONY_OUT_OF_ORDER"

    @pytest.mark.asyncio
    async def test_invalid_credential_returns_401(
        self,
        api_client: AsyncClient,
        wire_auth_overrides: None,
        fresh_ceremony_store: CeremonyStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Mint setup ceremony + simulate a prior /challenge call.
        ceremony_id = await fresh_ceremony_store.create_setup_ceremony(
            setup_token_uuid=uuid4(),
            intended_role="owner",
        )
        state = await fresh_ceremony_store.get(ceremony_id)
        assert state is not None
        state.webauthn_register_challenge = b"\x42" * 32
        state.webauthn_user_id = b"\x01" * 64
        state.account_id = uuid4()
        await fresh_ceremony_store.update(state)

        response = await api_client.post(
            "/api/auth/webauthn/register/verify",
            json={
                "ceremony_session_id": ceremony_id,
                "credential": {"id": "x", "rawId": "x", "type": "public-key", "response": {}},
            },
            **_csrf_kwargs(),
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == "WEBAUTHN_VERIFY_FAILED"

    @pytest.mark.asyncio
    async def test_happy_path_inserts_credential_and_issues_session(
        self,
        api_client: AsyncClient,
        wire_auth_overrides: None,
        fresh_ceremony_store: CeremonyStore,
        stub_repo: _StubAuthRepo,
        stub_create_session: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from services.api.routes import auth as auth_route_mod

        # Mint setup ceremony + fake a prior /challenge call.
        ceremony_id = await fresh_ceremony_store.create_setup_ceremony(
            setup_token_uuid=uuid4(),
            intended_role="owner",
        )
        state = await fresh_ceremony_store.get(ceremony_id)
        assert state is not None
        state.webauthn_register_challenge = b"\x42" * 32
        state.webauthn_user_id = b"\x01" * 64
        state.account_id = uuid4()
        state.webauthn_nickname = "Touch ID"
        await fresh_ceremony_store.update(state)

        # Stub the verify call to return a successful registration.
        fake_verified = webauthn_mod.VerifiedRegistration(
            credential_id=b"\xab" * 32,
            credential_public_key=b"\xcd" * 65,
            sign_count=0,
            authenticator_attachment="cross-platform",
        )

        def _fake_verify(**kwargs: Any) -> webauthn_mod.VerifiedRegistration:
            return fake_verified

        monkeypatch.setattr(auth_route_mod.webauthn_mod, "verify_registration", _fake_verify)

        response = await api_client.post(
            "/api/auth/webauthn/register/verify",
            json={
                "ceremony_session_id": ceremony_id,
                "credential": {"id": "stubbed", "type": "public-key"},
            },
            **_csrf_kwargs(),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["success"] is True
        assert "credential_id_b64" in body
        # Credential was inserted.
        assert len(stub_repo.webauthn_credentials) == 1
        inserted = stub_repo.webauthn_credentials[0]
        assert inserted.credential_id == b"\xab" * 32
        assert inserted.nickname == "Touch ID"
        # Session was minted with totp_bootstrap_only=False (strong) + UV stamp.
        assert len(stub_create_session) == 1
        call = stub_create_session[0]
        assert call["totp_bootstrap_only"] is False
        assert call["last_uv_at_utc"] is not None
        # Session cookie + CSRF cookie set on the response.
        cookie_header = response.headers.get("set-cookie", "")
        assert "__Host-trading_session=" in cookie_header
        assert "__Host-csrf_token=" in cookie_header


# ---------------------------------------------------------------------------
# WebAuthn login
# ---------------------------------------------------------------------------
class TestWebAuthnLoginChallenge:
    @pytest.mark.asyncio
    async def test_404_when_no_account(
        self,
        api_client: AsyncClient,
        wire_auth_overrides: None,
    ) -> None:
        response = await api_client.post(
            "/api/auth/webauthn/challenge",
            json={},
            **_csrf_kwargs(),
        )
        assert response.status_code == 404
        assert response.json()["error_code"] == "ACCOUNT_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_404_when_no_credentials(
        self,
        api_client: AsyncClient,
        wire_auth_overrides: None,
        stub_repo: _StubAuthRepo,
    ) -> None:
        # Seed account but no webauthn credentials.
        stub_repo.accounts_by_username["operator"] = AccountRow(
            id=uuid4(), external_account_id="operator", role="owner"
        )
        response = await api_client.post(
            "/api/auth/webauthn/challenge",
            json={},
            **_csrf_kwargs(),
        )
        assert response.status_code == 404
        assert response.json()["error_code"] == "NO_WEBAUTHN_CREDENTIALS"

    @pytest.mark.asyncio
    async def test_happy_path_returns_ceremony_id_and_options(
        self,
        api_client: AsyncClient,
        wire_auth_overrides: None,
        stub_repo: _StubAuthRepo,
        fresh_ceremony_store: CeremonyStore,
    ) -> None:
        account_id = uuid4()
        stub_repo.accounts_by_username["operator"] = AccountRow(
            id=account_id, external_account_id="operator", role="owner"
        )
        stub_repo.webauthn_credentials.append(
            WebAuthnCredentialRow(
                id=uuid4(),
                account_id=account_id,
                credential_id=b"\xab" * 32,
                public_key=b"\xcd" * 65,
                sign_count=0,
                nickname=None,
                active=True,
            )
        )
        response = await api_client.post(
            "/api/auth/webauthn/challenge",
            json={"target_url": "/today"},
            **_csrf_kwargs(),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert "ceremony_id" in body
        assert "options" in body
        assert "challenge" in body["options"]
        state = await fresh_ceremony_store.get(body["ceremony_id"])
        assert state is not None
        assert state.kind == "login"
        assert state.target_url == "/today"


class TestWebAuthnLoginVerify:
    @pytest.mark.asyncio
    async def test_invalid_ceremony_returns_401(
        self,
        api_client: AsyncClient,
        wire_auth_overrides: None,
    ) -> None:
        response = await api_client.post(
            "/api/auth/webauthn/verify",
            json={"ceremony_id": "not-real", "credential": {"id": "x"}},
            **_csrf_kwargs(),
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == "INVALID_CEREMONY"

    @pytest.mark.asyncio
    async def test_unknown_credential_returns_401(
        self,
        api_client: AsyncClient,
        wire_auth_overrides: None,
        fresh_ceremony_store: CeremonyStore,
    ) -> None:
        ceremony_id = await fresh_ceremony_store.create_login_ceremony(
            allowed_credential_ids=[b"\xab" * 32],
            target_url=None,
        )
        state = await fresh_ceremony_store.get(ceremony_id)
        assert state is not None
        state.webauthn_login_challenge = b"\x42" * 32
        await fresh_ceremony_store.update(state)

        # rawId base64url of bytes that don't match any stored credential.
        import base64

        unknown_b64 = base64.urlsafe_b64encode(b"\x99" * 32).rstrip(b"=").decode("ascii")
        response = await api_client.post(
            "/api/auth/webauthn/verify",
            json={
                "ceremony_id": ceremony_id,
                "credential": {"id": unknown_b64, "rawId": unknown_b64},
            },
            **_csrf_kwargs(),
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == "WEBAUTHN_VERIFY_FAILED"

    @pytest.mark.asyncio
    async def test_happy_path_issues_session_and_advances_sign_count(
        self,
        api_client: AsyncClient,
        wire_auth_overrides: None,
        fresh_ceremony_store: CeremonyStore,
        stub_repo: _StubAuthRepo,
        stub_create_session: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import base64

        from services.api.routes import auth as auth_route_mod

        account_id = uuid4()
        stub_repo.accounts_by_username["operator"] = AccountRow(
            id=account_id, external_account_id="operator", role="owner"
        )
        stub_repo.webauthn_credentials.append(
            WebAuthnCredentialRow(
                id=uuid4(),
                account_id=account_id,
                credential_id=b"\xab" * 32,
                public_key=b"\xcd" * 65,
                sign_count=42,
                nickname=None,
                active=True,
            )
        )
        ceremony_id = await fresh_ceremony_store.create_login_ceremony(
            allowed_credential_ids=[b"\xab" * 32],
            target_url="/today",
        )
        state = await fresh_ceremony_store.get(ceremony_id)
        assert state is not None
        state.webauthn_login_challenge = b"\x42" * 32
        await fresh_ceremony_store.update(state)

        def _fake_verify(**kwargs: Any) -> webauthn_mod.VerifiedAuthentication:
            return webauthn_mod.VerifiedAuthentication(
                credential_id=b"\xab" * 32,
                new_sign_count=43,
            )

        monkeypatch.setattr(auth_route_mod.webauthn_mod, "verify_authentication", _fake_verify)

        cred_b64 = base64.urlsafe_b64encode(b"\xab" * 32).rstrip(b"=").decode("ascii")
        response = await api_client.post(
            "/api/auth/webauthn/verify",
            json={
                "ceremony_id": ceremony_id,
                "credential": {"id": cred_b64, "rawId": cred_b64},
            },
            **_csrf_kwargs(),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["success"] is True
        assert body["target_url"] == "/today"
        # sign_count advanced.
        assert stub_repo.webauthn_credentials[0].sign_count == 43
        # Session was strong (totp_bootstrap_only=False, last_uv_at set).
        assert len(stub_create_session) == 1
        assert stub_create_session[0]["totp_bootstrap_only"] is False
        assert stub_create_session[0]["last_uv_at_utc"] is not None


# ---------------------------------------------------------------------------
# TOTP enrollment + verify
# ---------------------------------------------------------------------------
class TestTotpEnrollChallenge:
    @pytest.mark.asyncio
    async def test_happy_path_returns_secret_and_uri(
        self,
        api_client: AsyncClient,
        wire_auth_overrides: None,
        fresh_ceremony_store: CeremonyStore,
    ) -> None:
        ceremony_id = await fresh_ceremony_store.create_setup_ceremony(
            setup_token_uuid=uuid4(),
            intended_role="owner",
        )
        response = await api_client.post(
            "/api/auth/totp/enroll-challenge",
            json={"ceremony_session_id": ceremony_id},
            **_csrf_kwargs(),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["secret_base32"]) == 32
        assert body["provisioning_uri"].startswith("otpauth://totp/")
        # State should remember the secret.
        state = await fresh_ceremony_store.get(ceremony_id)
        assert state is not None
        assert state.totp_secret_base32 == body["secret_base32"]


class TestTotpSetupVerify:
    @pytest.mark.asyncio
    async def test_out_of_order_returns_400(
        self,
        api_client: AsyncClient,
        wire_auth_overrides: None,
        fresh_ceremony_store: CeremonyStore,
    ) -> None:
        ceremony_id = await fresh_ceremony_store.create_setup_ceremony(
            setup_token_uuid=uuid4(),
            intended_role="owner",
        )
        # No enroll-challenge yet; totp_secret_base32 is None.
        response = await api_client.post(
            "/api/auth/totp/setup-verify",
            json={"ceremony_session_id": ceremony_id, "totp_code": "000000"},
            **_csrf_kwargs(),
        )
        assert response.status_code == 400
        assert response.json()["error_code"] == "CEREMONY_OUT_OF_ORDER"

    @pytest.mark.asyncio
    async def test_invalid_code_returns_401(
        self,
        api_client: AsyncClient,
        wire_auth_overrides: None,
        fresh_ceremony_store: CeremonyStore,
    ) -> None:
        ceremony_id = await fresh_ceremony_store.create_setup_ceremony(
            setup_token_uuid=uuid4(),
            intended_role="owner",
        )
        state = await fresh_ceremony_store.get(ceremony_id)
        assert state is not None
        state.totp_secret_base32 = "JBSWY3DPEHPK3PXP"
        state.account_id = uuid4()
        await fresh_ceremony_store.update(state)
        response = await api_client.post(
            "/api/auth/totp/setup-verify",
            json={"ceremony_session_id": ceremony_id, "totp_code": "000000"},
            **_csrf_kwargs(),
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == "TOTP_CODE_INVALID"

    @pytest.mark.asyncio
    async def test_happy_path_encrypts_and_persists_secret(
        self,
        api_client: AsyncClient,
        wire_auth_overrides: None,
        fresh_ceremony_store: CeremonyStore,
        stub_repo: _StubAuthRepo,
    ) -> None:
        import pyotp

        ceremony_id = await fresh_ceremony_store.create_setup_ceremony(
            setup_token_uuid=uuid4(),
            intended_role="owner",
        )
        secret = pyotp.random_base32()
        account_id = uuid4()
        state = await fresh_ceremony_store.get(ceremony_id)
        assert state is not None
        state.totp_secret_base32 = secret
        state.account_id = account_id
        await fresh_ceremony_store.update(state)

        current_code = pyotp.TOTP(secret).now()
        response = await api_client.post(
            "/api/auth/totp/setup-verify",
            json={"ceremony_session_id": ceremony_id, "totp_code": current_code},
            **_csrf_kwargs(),
        )
        assert response.status_code == 200, response.text
        # Secret persisted, encrypted.
        assert account_id in stub_repo.totp_secrets
        stored = stub_repo.totp_secrets[account_id]
        assert stored.encrypted_secret != secret.encode()
        assert len(stored.encrypted_secret) >= 12 + 16  # nonce + GCM tag
        # State zeroed.
        post_state = await fresh_ceremony_store.get(ceremony_id)
        assert post_state is not None
        assert post_state.totp_secret_base32 is None
        assert post_state.totp_secret_verified is True


class TestTotpLoginVerify:
    @pytest.mark.asyncio
    async def test_account_not_found_returns_401(
        self,
        api_client: AsyncClient,
        wire_auth_overrides: None,
    ) -> None:
        response = await api_client.post(
            "/api/auth/totp/verify",
            json={"username": "ghost", "totp_code": "123456"},
            **_csrf_kwargs(),
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == "TOTP_LOGIN_FAILED"

    @pytest.mark.asyncio
    async def test_no_secret_returns_401(
        self,
        api_client: AsyncClient,
        wire_auth_overrides: None,
        stub_repo: _StubAuthRepo,
    ) -> None:
        stub_repo.accounts_by_username["operator"] = AccountRow(
            id=uuid4(), external_account_id="operator", role="owner"
        )
        response = await api_client.post(
            "/api/auth/totp/verify",
            json={"username": "operator", "totp_code": "123456"},
            **_csrf_kwargs(),
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == "TOTP_LOGIN_FAILED"

    @pytest.mark.asyncio
    async def test_happy_path_issues_weak_session(
        self,
        api_client: AsyncClient,
        wire_auth_overrides: None,
        stub_repo: _StubAuthRepo,
        stub_create_session: list[dict[str, Any]],
    ) -> None:
        import pyotp

        from services.api.auth import totp as totp_mod

        account_id = uuid4()
        stub_repo.accounts_by_username["operator"] = AccountRow(
            id=account_id, external_account_id="operator", role="owner"
        )
        secret = pyotp.random_base32()
        # Use the same key the wire_auth_overrides fixture stubs in
        # (32 zero bytes → 32 base64 'A' chars + '=').
        key = b"\x00" * 32
        ciphertext = totp_mod.encrypt_secret(secret_base32=secret, key=key)
        stub_repo.totp_secrets[account_id] = TotpSecretRow(
            account_id=account_id,
            encrypted_secret=ciphertext,
            enrolled_at_utc=datetime.now(tz=UTC),
            last_used_at_utc=None,
        )
        current_code = pyotp.TOTP(secret).now()
        response = await api_client.post(
            "/api/auth/totp/verify",
            json={"username": "operator", "totp_code": current_code},
            **_csrf_kwargs(),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["success"] is True
        assert body["auth_strength"] == "weak"
        # Session was weak (totp_bootstrap_only=True, last_uv_at None).
        assert len(stub_create_session) == 1
        assert stub_create_session[0]["totp_bootstrap_only"] is True
        assert stub_create_session[0]["last_uv_at_utc"] is None


# ---------------------------------------------------------------------------
# Backup codes
# ---------------------------------------------------------------------------
class TestBackupCodesGenerate:
    @pytest.mark.asyncio
    async def test_out_of_order_returns_400(
        self,
        api_client: AsyncClient,
        wire_auth_overrides: None,
        fresh_ceremony_store: CeremonyStore,
    ) -> None:
        # Ceremony without account_id set.
        ceremony_id = await fresh_ceremony_store.create_setup_ceremony(
            setup_token_uuid=uuid4(),
            intended_role="owner",
        )
        response = await api_client.post(
            "/api/auth/backup-codes/generate",
            json={"ceremony_session_id": ceremony_id},
            **_csrf_kwargs(),
        )
        assert response.status_code == 400
        assert response.json()["error_code"] == "CEREMONY_OUT_OF_ORDER"

    @pytest.mark.asyncio
    async def test_happy_path_returns_eight_codes_and_drops_ceremony(
        self,
        api_client: AsyncClient,
        wire_auth_overrides: None,
        fresh_ceremony_store: CeremonyStore,
        stub_repo: _StubAuthRepo,
    ) -> None:
        account_id = uuid4()
        ceremony_id = await fresh_ceremony_store.create_setup_ceremony(
            setup_token_uuid=uuid4(),
            intended_role="owner",
        )
        state = await fresh_ceremony_store.get(ceremony_id)
        assert state is not None
        state.account_id = account_id
        await fresh_ceremony_store.update(state)
        response = await api_client.post(
            "/api/auth/backup-codes/generate",
            json={"ceremony_session_id": ceremony_id},
            **_csrf_kwargs(),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["codes"]) == 8
        for c in body["codes"]:
            assert len(c) == 11 and c[5] == "-"
        assert "generation_id" in body
        # Ceremony dropped post-success.
        assert await fresh_ceremony_store.get(ceremony_id) is None
        # 8 hashes inserted.
        codes = [c for c in stub_repo.backup_codes if c.account_id == account_id]
        assert len(codes) == 8


class TestRecover:
    @pytest.mark.asyncio
    async def test_unknown_account_returns_401(
        self,
        api_client: AsyncClient,
        wire_auth_overrides: None,
    ) -> None:
        response = await api_client.post(
            "/api/auth/recover",
            json={"username": "ghost", "backup_code": "ABCDE-FGHIJ"},
            **_csrf_kwargs(),
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == "RECOVER_FAILED"

    @pytest.mark.asyncio
    async def test_wrong_code_returns_401(
        self,
        api_client: AsyncClient,
        wire_auth_overrides: None,
        stub_repo: _StubAuthRepo,
    ) -> None:
        from services.api.auth import backup_codes as bc_mod

        account_id = uuid4()
        stub_repo.accounts_by_username["operator"] = AccountRow(
            id=account_id, external_account_id="operator", role="owner"
        )
        # Seed 8 random hashed codes.
        codes = bc_mod.generate_codes()
        for c in codes:
            stub_repo.backup_codes.append(
                BackupCodeRow(
                    id=uuid4(),
                    account_id=account_id,
                    code_hash=c.hash,
                    used_at_utc=None,
                    generation_id=uuid4(),
                )
            )
        response = await api_client.post(
            "/api/auth/recover",
            json={"username": "operator", "backup_code": "ZZZZZ-ZZZZZ"},
            **_csrf_kwargs(),
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == "RECOVER_FAILED"

    @pytest.mark.asyncio
    async def test_happy_path_marks_code_used_and_issues_weak_session(
        self,
        api_client: AsyncClient,
        wire_auth_overrides: None,
        stub_repo: _StubAuthRepo,
        stub_create_session: list[dict[str, Any]],
    ) -> None:
        from services.api.auth import backup_codes as bc_mod

        account_id = uuid4()
        stub_repo.accounts_by_username["operator"] = AccountRow(
            id=account_id, external_account_id="operator", role="owner"
        )
        codes = bc_mod.generate_codes()
        for c in codes:
            stub_repo.backup_codes.append(
                BackupCodeRow(
                    id=uuid4(),
                    account_id=account_id,
                    code_hash=c.hash,
                    used_at_utc=None,
                    generation_id=uuid4(),
                )
            )
        first_display = codes[0].display
        response = await api_client.post(
            "/api/auth/recover",
            json={"username": "operator", "backup_code": first_display},
            **_csrf_kwargs(),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["auth_strength"] == "weak"
        # The matched row is now used; the other 7 are not.
        used_count = sum(1 for c in stub_repo.backup_codes if c.used_at_utc is not None)
        assert used_count == 1
        # Session was weak.
        assert len(stub_create_session) == 1
        assert stub_create_session[0]["totp_bootstrap_only"] is True


# ---------------------------------------------------------------------------
# Backup-code regenerate (re-auth-gated; account resolution via session)
# ---------------------------------------------------------------------------
def _strong_session_context(user_id: str, username: str) -> Any:
    """A strong-auth SessionContext with a fresh UV (passes the 5-min gate)."""
    from services.api.session import SessionContext

    now = datetime.now(tz=UTC)
    return SessionContext(
        user_id=user_id,
        username=username,
        role="owner",
        auth_strength="strong",
        last_uv_at=now,
        session_expires_at=now,
        webauthn_enrolled=True,
        totp_enrolled=True,
        is_phase0_stub=False,
    )


class TestBackupCodesRegenerate:
    @pytest.mark.asyncio
    async def test_real_session_resolves_by_account_uuid_not_username(
        self,
        api_app: FastAPI,
        api_client: AsyncClient,
        wire_auth_overrides: None,
        stub_repo: _StubAuthRepo,
    ) -> None:
        """#380 review N-a: a real session carries the account UUID in
        ``user_id`` — the codes must be minted for exactly that account,
        even when another account shares the session's username surface."""
        from services.api.session import get_session_context

        real_account = AccountRow(id=uuid4(), external_account_id="real-owner", role="owner")
        decoy_account = AccountRow(id=uuid4(), external_account_id="operator", role="owner")
        stub_repo.accounts_by_username["real-owner"] = real_account
        # Decoy: same username the session presents, DIFFERENT account id.
        stub_repo.accounts_by_username["operator"] = decoy_account
        api_app.dependency_overrides[get_session_context] = lambda: _strong_session_context(
            user_id=str(real_account.id), username="operator"
        )

        response = await api_client.post(
            "/api/auth/backup-codes/regenerate", json={}, **_csrf_kwargs()
        )

        assert response.status_code == 200, response.text
        assert len(response.json()["codes"]) == 8
        insert = next(i for i in stub_repo.inserts if i["type"] == "backup_codes")
        assert insert["account_id"] == real_account.id  # by-id, not by-username

    @pytest.mark.asyncio
    async def test_real_session_unknown_account_uuid_404s(
        self,
        api_app: FastAPI,
        api_client: AsyncClient,
        wire_auth_overrides: None,
        stub_repo: _StubAuthRepo,
    ) -> None:
        from services.api.session import get_session_context

        api_app.dependency_overrides[get_session_context] = lambda: _strong_session_context(
            user_id=str(uuid4()), username="operator"
        )
        response = await api_client.post(
            "/api/auth/backup-codes/regenerate", json={}, **_csrf_kwargs()
        )
        assert response.status_code == 404
        assert response.json()["error_code"] == "ACCOUNT_NOT_FOUND"
        assert not any(i["type"] == "backup_codes" for i in stub_repo.inserts)

    @pytest.mark.asyncio
    async def test_stub_session_falls_back_to_username_and_invalidates_old(
        self,
        api_app: FastAPI,
        api_client: AsyncClient,
        wire_auth_overrides: None,
        stub_repo: _StubAuthRepo,
    ) -> None:
        """Dev-only Phase-0 stub (``user_id`` is not a UUID): resolution
        falls back to the username lookup; the old batch is invalidated."""
        from services.api.auth import backup_codes as bc_mod
        from services.api.session import get_session_context

        account = AccountRow(id=uuid4(), external_account_id="operator", role="owner")
        stub_repo.accounts_by_username["operator"] = account
        old_codes = bc_mod.generate_codes()
        for c in old_codes:
            stub_repo.backup_codes.append(
                BackupCodeRow(
                    id=uuid4(),
                    account_id=account.id,
                    code_hash=c.hash,
                    used_at_utc=None,
                    generation_id=uuid4(),
                )
            )
        api_app.dependency_overrides[get_session_context] = lambda: _strong_session_context(
            user_id="phase0-stub-owner", username="operator"
        )

        response = await api_client.post(
            "/api/auth/backup-codes/regenerate", json={}, **_csrf_kwargs()
        )

        assert response.status_code == 200, response.text
        assert len(response.json()["codes"]) == 8
        # The 8 pre-existing codes are used-up; 8 fresh unused rows exist.
        unused = [c for c in stub_repo.backup_codes if c.used_at_utc is None]
        assert len(unused) == 8
        assert len(stub_repo.backup_codes) == 16


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------
class TestLogout:
    @pytest.mark.asyncio
    async def test_clears_cookies(
        self,
        api_client: AsyncClient,
        wire_auth_overrides: None,
    ) -> None:
        response = await api_client.post(
            "/api/auth/logout",
            json={},
            **_csrf_kwargs(),
        )
        assert response.status_code == 200
        cookie_header = response.headers.get("set-cookie", "")
        # delete_cookie sets Max-Age=0 / Expires=Thu, 01 Jan 1970...
        assert "__Host-trading_session=" in cookie_header
        assert "__Host-csrf_token=" in cookie_header


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------
class TestModuleContract:
    def test_router_exports(self) -> None:
        from services.api.routes.auth import router

        # 12 endpoints expected (11 POST + 1 GET). FastAPI stores
        # ``methods`` as a set on each route, so we flatten to (path).
        flat = {r.path for r in router.routes}  # type: ignore[attr-defined]
        for expected in (
            "/api/auth/me",
            "/api/auth/webauthn/challenge",
            "/api/auth/webauthn/verify",
            "/api/auth/webauthn/register/challenge",
            "/api/auth/webauthn/register/verify",
            "/api/auth/totp/enroll-challenge",
            "/api/auth/totp/setup-verify",
            "/api/auth/totp/verify",
            "/api/auth/backup-codes/generate",
            "/api/auth/backup-codes/regenerate",
            "/api/auth/recover",
            "/api/auth/logout",
        ):
            assert expected in flat, f"missing route: {expected}"
