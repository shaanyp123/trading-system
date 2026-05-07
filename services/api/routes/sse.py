"""services/api/routes/sse.py — `/api/sse/events` heartbeat scaffold.

Day 5 ships a heartbeat-only SSE endpoint. The full event-emitter machinery
(replay buffer, JCS canonicalization, fan-out queue) lands when real events
flow - Week 4-5 once signals/fills/positions exist. The Caddy-side wiring
(`flush_interval -1`, `read_timeout 24h`) is already in `deploy/Caddyfile`
per backend-spec §9.2.1 so this endpoint can grow into the full §4.2 contract
without a Caddy redeploy.

Per backend-spec §4.2.1 the canonical envelope is:

    {
      "type": "<event_type>",
      "sequence_no": <global monotonic int>,
      "server_now": "<RFC 3339 UTC ms-precision>",
      "data": { ... }
    }

The Day 5 implementation emits SSE comment lines (`": keepalive\\n\\n"`) every
30 seconds. Comments don't appear in the EventSource event stream on the
client side, but they keep the connection warm through any intermediate
proxy idle timers (Caddy `idle 24h` already covers our path; the keepalive
defends against future proxies + ISP-level NAT timeouts).

`Last-Event-ID` replay returns 426 for any non-zero header value because
the replay buffer doesn't exist yet — explicit per-spec "buffer expired"
fallback (`§4.2.3`).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

log = structlog.get_logger()

router = APIRouter()

_KEEPALIVE_SECONDS = 30.0


@router.get("/api/sse/events", tags=["sse"])
async def sse_events(request: Request) -> StreamingResponse:
    last_event_id = request.headers.get("last-event-id")
    if last_event_id:
        # No replay buffer exists yet; per §4.2.3 the spec calls for 426 +
        # `must_reload` when the gap exceeds the 24h window. Day 5 has no
        # buffer at all so every resume request gets the same fallback.
        log.info("sse_replay_unsupported_day5", last_event_id=last_event_id)
        return StreamingResponse(
            _replay_unsupported_payload(),
            status_code=426,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return StreamingResponse(
        _heartbeat_generator(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _heartbeat_generator(request: Request) -> AsyncGenerator[bytes, None]:
    """Emit SSE comment-line keepalives until the client disconnects."""
    log.info("sse_connect")
    try:
        # Initial comment line so EventSource clients see an open stream
        # immediately rather than waiting `_KEEPALIVE_SECONDS` for the first
        # keepalive.
        yield b": connected\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                await asyncio.sleep(_KEEPALIVE_SECONDS)
            except asyncio.CancelledError:
                break
            yield b": keepalive\n\n"
    finally:
        log.info("sse_disconnect")


async def _replay_unsupported_payload() -> AsyncGenerator[bytes, None]:
    """Single-shot SSE message instructing the client to full-refetch."""
    yield (b'event: version\ndata: {"must_reload": true, "reason": "buffer_expired"}\n\n')
