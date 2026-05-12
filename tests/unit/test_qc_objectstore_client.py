"""Unit tests for ``services/qc_adapter/qc_objectstore_client.py``.

Test inventory:

- TestAuthHeader — HMAC password math, base64 envelope, Timestamp header
- TestListKeys — happy path returns sorted key list; success=false body
  raises; auth rejection (401) raises QCObjectStoreAuthError
- TestGetObject — happy path: 2-step presigned URL → S3 download; 404
  raises QCObjectStoreNotFoundError; inline-body fallback when QC
  returns ``data`` instead of ``url``
- TestSetObject — happy path POSTs body to QC; 401/403 → AuthError
- TestRetryLogic — 5xx retries up to budget; transport error retries;
  budget exhaustion raises QCObjectStoreError
- TestQueryShape — Authorization, Timestamp, and User-Agent headers
  attached on every request

A22 N/A — pure-Python tests; no DB, no audit writes.
A27 N/A — respx-mocked HTTP exercises the wire shape; the operator
runbook (deploy/qc_adapter/README.md) handles the live-QC contract.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

import httpx
import pytest
import respx

from services.qc_adapter.qc_objectstore_client import (
    USER_AGENT,
    QCObjectStoreAuthError,
    QCObjectStoreClient,
    QCObjectStoreError,
    QCObjectStoreNotFoundError,
    _build_auth_headers,
)

_BASE = "https://www.quantconnect.com/api/v2"


def _make_client(client: httpx.AsyncClient | None = None) -> QCObjectStoreClient:
    return QCObjectStoreClient(
        user_id="12345",
        api_token="secret-token",
        base_url=_BASE,
        timeout_seconds=5.0,
        client=client,
    )


class TestAuthHeader:
    def test_password_is_sha256_of_token_colon_timestamp(self) -> None:
        h = _build_auth_headers("12345", "secret-token", now_epoch=1_700_000_000)
        # Expected password = sha256("secret-token:1700000000")
        expected_pwd = hashlib.sha256(b"secret-token:1700000000").hexdigest()
        expected_basic = base64.b64encode(f"12345:{expected_pwd}".encode()).decode()
        assert h.authorization == f"Basic {expected_basic}"
        assert h.timestamp == "1700000000"
        assert h.user_agent == USER_AGENT

    def test_timestamp_defaults_to_wall_clock_when_not_passed(self) -> None:
        h = _build_auth_headers("12345", "secret-token")
        assert h.timestamp.isdigit()
        assert int(h.timestamp) > 1_000_000_000  # sanity: epoch seconds


class TestListKeys:
    @pytest.mark.asyncio
    @respx.mock
    async def test_list_keys_happy_path_returns_sorted_keys(self) -> None:
        respx.post(f"{_BASE}/object-store/list").mock(
            return_value=httpx.Response(
                200,
                json={
                    "objects": [
                        {"key": "events/2.jsonl"},
                        {"key": "events/1.jsonl"},
                        {"key": "events/3.jsonl"},
                    ],
                    "success": True,
                },
            )
        )
        client = _make_client()
        try:
            keys = await client.list_keys("events/")
        finally:
            await client.aclose()
        assert keys == ["events/1.jsonl", "events/2.jsonl", "events/3.jsonl"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_list_keys_empty_objects_array(self) -> None:
        respx.post(f"{_BASE}/object-store/list").mock(
            return_value=httpx.Response(200, json={"objects": [], "success": True})
        )
        client = _make_client()
        try:
            keys = await client.list_keys("events/")
        finally:
            await client.aclose()
        assert keys == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_list_keys_401_raises_auth_error(self) -> None:
        respx.post(f"{_BASE}/object-store/list").mock(
            return_value=httpx.Response(401, json={"errors": ["bad auth"]})
        )
        client = _make_client()
        with pytest.raises(QCObjectStoreAuthError):
            try:
                await client.list_keys("events/")
            finally:
                await client.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_list_keys_success_false_raises_terminal(self) -> None:
        respx.post(f"{_BASE}/object-store/list").mock(
            return_value=httpx.Response(200, json={"success": False, "errors": ["invalid path"]})
        )
        client = _make_client()
        with pytest.raises(QCObjectStoreError):
            try:
                await client.list_keys("events/")
            finally:
                await client.aclose()


class TestGetObject:
    @pytest.mark.asyncio
    @respx.mock
    async def test_get_object_two_step_returns_body(self) -> None:
        # Step 1: POST /object-store/get returns presigned URL
        respx.post(f"{_BASE}/object-store/get").mock(
            return_value=httpx.Response(
                200, json={"url": "https://qc-presigned.example.com/k1", "success": True}
            )
        )
        # Step 2: GET the presigned URL returns the actual bytes
        respx.get("https://qc-presigned.example.com/k1").mock(
            return_value=httpx.Response(200, content=b"line1\nline2\n")
        )
        client = _make_client()
        try:
            body = await client.get_object("events/1.jsonl")
        finally:
            await client.aclose()
        assert body == b"line1\nline2\n"

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_object_inline_body_fallback(self) -> None:
        # QC sometimes returns the data inline for small objects.
        respx.post(f"{_BASE}/object-store/get").mock(
            return_value=httpx.Response(200, json={"data": "inline-content", "success": True})
        )
        client = _make_client()
        try:
            body = await client.get_object("events/1.jsonl")
        finally:
            await client.aclose()
        assert body == b"inline-content"

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_object_404_raises_not_found(self) -> None:
        respx.post(f"{_BASE}/object-store/get").mock(
            return_value=httpx.Response(404, json={"errors": ["not found"]})
        )
        client = _make_client()
        with pytest.raises(QCObjectStoreNotFoundError):
            try:
                await client.get_object("events/missing.jsonl")
            finally:
                await client.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_get_object_missing_url_and_data_raises(self) -> None:
        respx.post(f"{_BASE}/object-store/get").mock(
            return_value=httpx.Response(200, json={"success": True})
        )
        client = _make_client()
        with pytest.raises(QCObjectStoreError):
            try:
                await client.get_object("events/1.jsonl")
            finally:
                await client.aclose()


class TestSetObject:
    @pytest.mark.asyncio
    @respx.mock
    async def test_set_object_happy_path(self) -> None:
        respx.post(f"{_BASE}/object-store/set").mock(
            return_value=httpx.Response(200, json={"success": True})
        )
        client = _make_client()
        try:
            # No exception on happy path; method returns None
            await client.set_object("instructions/1.json", b"payload")
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_set_object_403_raises_auth_error(self) -> None:
        respx.post(f"{_BASE}/object-store/set").mock(
            return_value=httpx.Response(403, json={"errors": ["forbidden"]})
        )
        client = _make_client()
        with pytest.raises(QCObjectStoreAuthError):
            try:
                await client.set_object("instructions/1.json", b"payload")
            finally:
                await client.aclose()


class TestRetryLogic:
    @pytest.mark.asyncio
    @respx.mock
    async def test_5xx_retries_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Patch out the backoff sleep for fast tests.
        async def _no_sleep(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr("services.qc_adapter.qc_objectstore_client.asyncio.sleep", _no_sleep)

        route = respx.post(f"{_BASE}/object-store/list").mock(
            side_effect=[
                httpx.Response(503, text="degraded"),
                httpx.Response(503, text="degraded"),
                httpx.Response(200, json={"objects": [{"key": "events/1.jsonl"}], "success": True}),
            ]
        )
        client = _make_client()
        try:
            keys = await client.list_keys("events/")
        finally:
            await client.aclose()
        assert keys == ["events/1.jsonl"]
        assert route.call_count == 3

    @pytest.mark.asyncio
    @respx.mock
    async def test_5xx_budget_exhausted_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _no_sleep(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr("services.qc_adapter.qc_objectstore_client.asyncio.sleep", _no_sleep)

        respx.post(f"{_BASE}/object-store/list").mock(
            return_value=httpx.Response(503, text="degraded")
        )
        client = _make_client()
        with pytest.raises(QCObjectStoreError):
            try:
                await client.list_keys("events/")
            finally:
                await client.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_transport_error_retries_then_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _no_sleep(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr("services.qc_adapter.qc_objectstore_client.asyncio.sleep", _no_sleep)

        respx.post(f"{_BASE}/object-store/list").mock(side_effect=httpx.ConnectError("boom"))
        client = _make_client()
        with pytest.raises(QCObjectStoreError):
            try:
                await client.list_keys("events/")
            finally:
                await client.aclose()


class TestHeadersAttached:
    @pytest.mark.asyncio
    @respx.mock
    async def test_auth_timestamp_user_agent_on_every_post(self) -> None:
        route = respx.post(f"{_BASE}/object-store/list").mock(
            return_value=httpx.Response(200, json={"objects": [], "success": True})
        )
        client = _make_client()
        try:
            await client.list_keys("events/")
        finally:
            await client.aclose()
        request = route.calls.last.request
        assert request.headers["Authorization"].startswith("Basic ")
        assert request.headers["Timestamp"].isdigit()
        assert request.headers["User-Agent"] == USER_AGENT


class TestConstructorValidation:
    def test_empty_user_id_raises(self) -> None:
        with pytest.raises(ValueError):
            QCObjectStoreClient(
                user_id="",
                api_token="t",
                base_url=_BASE,
                timeout_seconds=5.0,
            )

    def test_empty_api_token_raises(self) -> None:
        with pytest.raises(ValueError):
            QCObjectStoreClient(
                user_id="1",
                api_token="",
                base_url=_BASE,
                timeout_seconds=5.0,
            )
