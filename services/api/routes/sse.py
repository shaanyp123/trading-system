"""services/api/routes/sse.py — ``/api/sse/events`` multiplexer route.

Day 16 / Week 5 Tue ships the full backend-spec §4.2 SSE contract. The
heavy-lifting (envelope, JCS, replay buffer, eviction, heartbeat) lives
in :mod:`services.api.sse`; this module is the thin FastAPI route +
``Last-Event-ID`` parsing layer.

Contract summary (see :mod:`services.api.sse` for full spec citations):

* Long-lived ``text/event-stream`` response with ``Cache-Control: no-cache``
  + ``X-Accel-Buffering: no`` headers. Caddy is configured with
  ``flush_interval -1`` + ``transport.read_timeout 24h`` so frames ship
  immediately and the connection survives a session-day gap (Caddyfile
  per spec §9.2.1).
* On reconnect, ``Last-Event-ID: <sequence_no>`` triggers replay from the
  24h backend buffer; sequences outside the window return HTTP 426 with a
  ``version`` envelope carrying ``{must_reload: true, reason:
  "buffer_expired"}``.
* Per-user N=4 connection limit (frontend-spec §4.6): the multiplexer's
  :func:`~services.api.sse.register_subscriber` evicts the oldest tab and
  ships a ``session_evicted`` envelope to it.
* 30s ``event: ping`` heartbeat from the multiplexer's background task
  (started in the FastAPI lifespan).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from services.api import sse as multiplexer
from services.api.session import SessionContext, get_session_context

log = structlog.get_logger()

router = APIRouter()


def _parse_last_event_id(raw: str | None) -> int | None:
    """Parse the ``Last-Event-ID`` header to an int sequence number.

    Returns None for:

    * absent / empty header (initial connect),
    * non-numeric value (malformed; treat as fresh connect to be lenient
      — the EventSource spec doesn't constrain the format, but our
      server-emitted ids are always decimal integers, so a non-int means
      a buggy/replayed-from-cache header).

    The spec doesn't define what to do for a malformed header; lenient
    treatment matches dev-guide §1.5's "client resumes via last-event-id"
    intent — better to reconnect cleanly than to 4xx.
    """
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        log.warning("sse_last_event_id_malformed", raw=raw)
        return None
    if value <= 0:
        return None
    return value


@router.get("/api/sse/events", tags=["sse"])
async def sse_events(
    request: Request,
    session: SessionContext = Depends(get_session_context),
) -> StreamingResponse:
    """Single multiplexed SSE channel for the entire web client.

    The session middleware injects ``request.state.session`` for
    non-exempt paths; the path is non-exempt as of Day 16 because the
    multiplexer needs ``session.user_id`` for the N=4 eviction logic.
    """
    last_event_id = request.headers.get("last-event-id")
    resume_from = _parse_last_event_id(last_event_id)
    resolution = multiplexer.resolve_replay(resume_from)

    if resolution.buffer_expired:
        log.info(
            "sse_resume_buffer_expired",
            user_id=session.user_id,
            resume_from=resume_from,
        )
        return StreamingResponse(
            _single_frame_stream(multiplexer.build_must_reload_frame()),
            status_code=426,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    subscriber = multiplexer.register_subscriber(session.user_id)
    log.info(
        "sse_connect",
        user_id=session.user_id,
        resume_from=resume_from,
        replay_frame_count=len(resolution.frames),
    )

    async def generator() -> AsyncGenerator[bytes, None]:
        try:
            async for frame in multiplexer.stream_for_subscriber(
                subscriber,
                replay_frames=resolution.frames,
            ):
                # Bail early if the client disconnects mid-stream. FastAPI
                # surfaces the disconnect via ``request.is_disconnected()``
                # which is a coroutine; only check after each yield so the
                # generator doesn't busy-loop on disconnect detection.
                yield frame
                if await request.is_disconnected():
                    break
        finally:
            multiplexer.deregister_subscriber(subscriber)
            log.info(
                "sse_disconnect",
                user_id=session.user_id,
                evict_reason=subscriber.evict_reason,
            )

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _single_frame_stream(frame: bytes) -> AsyncGenerator[bytes, None]:
    """One-shot SSE generator for the 426 must-reload path."""
    yield frame
