"""Unit tests for `services/api/routes/sse.py` (heartbeat scaffold).

We test the generator + the route handler directly rather than streaming
through `httpx.AsyncClient`. ASGITransport's request-disconnect signal
doesn't reliably propagate to a long-lived `StreamingResponse` generator
in-process, which would make real-stream tests flaky.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from services.api.routes import sse as sse_route


class _FakeRequest:
    """Minimal Request-shaped object to drive `_heartbeat_generator`."""

    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}
        self._disconnect_after: int | None = None
        self._reads = 0

    def disconnect_after(self, n: int) -> None:
        self._disconnect_after = n

    async def is_disconnected(self) -> bool:
        self._reads += 1
        return self._disconnect_after is not None and self._reads > self._disconnect_after


@pytest.mark.asyncio
async def test_heartbeat_generator_emits_initial_connected_then_keepalive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sse_route, "_KEEPALIVE_SECONDS", 0.01)
    request = _FakeRequest()
    request.disconnect_after(1)  # exit after the first keepalive

    chunks: list[bytes] = []
    async for chunk in sse_route._heartbeat_generator(request):  # type: ignore[arg-type]
        chunks.append(chunk)
        if len(chunks) >= 2:
            request.disconnect_after(0)
        if len(chunks) >= 3:  # safety stop
            break

    assert chunks[0] == b": connected\n\n"
    assert b": keepalive" in chunks[1]


@pytest.mark.asyncio
async def test_heartbeat_generator_exits_on_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sse_route, "_KEEPALIVE_SECONDS", 0.01)
    request = _FakeRequest()
    request.disconnect_after(0)  # disconnect before first keepalive

    chunks: list[bytes] = []
    async for chunk in sse_route._heartbeat_generator(request):  # type: ignore[arg-type]
        chunks.append(chunk)

    # Got the connect comment, then the loop saw is_disconnected() == True.
    assert chunks == [b": connected\n\n"]


@pytest.mark.asyncio
async def test_replay_unsupported_payload_renders_must_reload_event() -> None:
    chunks: list[bytes] = []
    async for chunk in sse_route._replay_unsupported_payload():
        chunks.append(chunk)
    body = b"".join(chunks).decode()
    assert "event: version" in body
    assert '"must_reload": true' in body
    assert '"reason": "buffer_expired"' in body


@pytest.mark.asyncio
async def test_sse_route_returns_426_when_last_event_id_present(
    api_client: AsyncClient,
) -> None:
    response = await api_client.get(
        "/api/sse/events",
        headers={"last-event-id": "12345"},
    )
    assert response.status_code == 426
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    assert "event: version" in body
    assert "must_reload" in body
