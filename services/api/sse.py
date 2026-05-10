"""services/api/sse.py — SSE multiplexer for the single `/api/sse/events` channel.

Day 16 / Week 5 Tue ships the full backend-spec §4.2 contract:

* **Canonical envelope (§4.2.1, locked):**
  ``{type, sequence_no, server_now, data}`` where ``sequence_no`` is a GLOBAL
  monotonic integer across all event types and ``server_now`` is RFC 3339 UTC
  ms-precision (``2026-05-04T17:30:00.123Z``).
* **Wire frame:** each event is encoded as the SSE byte sequence
  ``id: <sequence_no>\\nevent: <type>\\ndata: <jcs-canonical-json>\\n\\n``.
  The ``data`` line carries the FULL envelope dict (not just ``data.data``)
  so EventSource clients can introspect ``sequence_no`` + ``server_now``
  without depending on the ``id:`` line, which `EventSource` exposes only
  through ``Last-Event-ID`` on reconnect.
* **24h replay buffer (§4.2.3):** in-memory deque of every emitted frame
  pruned by timestamp on each emit. ``Last-Event-ID`` on reconnect replays
  every frame with ``sequence_no > resume_from`` that still sits inside the
  window. Beyond-window resume returns HTTP 426 with a ``version`` event
  carrying ``{must_reload: true, reason: "buffer_expired"}`` (the handler
  for that lives in ``services/api/routes/sse.py``; this module provides
  the helper).
* **Multi-tab eviction (§4.2.2 + frontend-spec §4.6):** N=4 connections per
  user, last-connected wins; the oldest is closed with a ``session_evicted``
  envelope (``reason: tab_limit``) and its queue is drained server-side
  so the connection's generator exits cleanly.
* **Backpressure:** per-subscriber ``asyncio.Queue(maxsize=500)``. A full
  queue means the consumer is wedged — we drop the subscriber rather than
  block the entire fan-out; the client reconnects with Last-Event-ID and
  the replay buffer catches them up. Canonical SSE backpressure handling.
* **30s heartbeat:** background task emits ``event: ping`` (a real SSE
  event, NOT a comment line) every ``_HEARTBEAT_SECONDS``. Pings get a
  ``sequence_no`` and DO land in the replay buffer per §4.2.1's "global
  monotonic across all event types" lock — a client reconnecting with
  Last-Event-ID sees every missed ping in order. The implementation-guide
  §3 Week 5 Tue gate language ("``event: ping`` lines every 30s") is
  literal; the Day-5 ``: keepalive`` comment-line scaffold did NOT satisfy
  the gate text and is replaced today.

Module state ownership
======================

Three module-level globals hold the live multiplexer state:

* :data:`_sequence_counter` (int) — global monotonic counter. Reset on api
  process restart per spec §4.2.1's "GLOBAL monotonic" lock, interpreted as
  global to the single api process in Phase 0. Phase 2 (multi-replica) will
  back this with Redis or Postgres ``LISTEN/NOTIFY``; the module docstring
  for that migration carries the dual-process semantics.
* :data:`_replay_buffer` (deque) — ``(sequence_no, server_now_dt,
  raw_frame_bytes)`` tuples; pruned by ``server_now_dt`` on every emit.
  Hard cap at :data:`_REPLAY_BUFFER_HARD_CAP` entries (safety valve against
  an emit-storm bug; ~500 bytes/entry x 1M = 500MB, well below the api
  container's 1GB RAM allocation).
* :data:`_subscribers_by_user` (dict) — ``{user_id: list[Subscriber]}``,
  iteration order matches connection age (newest at the end). The eviction
  walker pops index 0 when ``len > _MAX_TABS_PER_USER``.

These globals are reset at module import time and on every ``reset_state()``
call (used by tests to isolate runs). They are NOT thread-safe — FastAPI's
single-event-loop model means every read/write happens on the loop thread,
and ``emit_sse`` only schedules work via ``put_nowait`` (no awaits between
state-read and state-write inside a single call). Don't introduce
``await``s into the state-mutation paths without re-deriving the locking
discipline.

Phase-2 deferral
================

The single-process, in-memory design is explicit for Phase 0. When the api
scales to multiple replicas (Phase 2 cutover) the globals migrate to:

* sequence counter → Redis ``INCR`` or Postgres advisory-lock + sequence
* replay buffer → Redis stream with ~24h ``MAXLEN`` policy OR a Postgres
  table with TTL pruning
* subscriber tracking → unchanged (each replica tracks its OWN subscribers;
  the new-connection-evict-oldest logic moves to a cross-replica check via
  the same shared store, e.g. a Redis SETNX on the connection slot)

The :func:`emit_sse` and route-handler surface should NOT change. Tests
written against the in-memory implementation today exercise the same
public contract.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

import structlog

from services.audit.chain import jcs_serialize

log = structlog.get_logger()


# ---------- Locked constants (do not edit without updating spec §4.2) ----------

#: Per-user max simultaneous SSE connections (spec §4.2.2 + dev-guide §1.5
#: "Tab limit: N=4 connections per user"). On connection N+1 the oldest
#: subscriber is evicted with a ``session_evicted`` envelope.
_MAX_TABS_PER_USER: Final[int] = 4

#: Server-side heartbeat cadence — IG §3 Week 5 Tue gate language
#: ("``event: ping`` lines every 30s") is literal.
_HEARTBEAT_SECONDS: Final[float] = 30.0

#: Replay buffer retention window per spec §4.2.3 ("24h backend retention").
_REPLAY_RETENTION: Final[timedelta] = timedelta(hours=24)

#: Hard ceiling on replay-buffer entries to defend against an emit-storm
#: bug. At ~500 bytes/entry x 1_000_000 = ~500MB this stays well below the
#: api container's 1GB RAM allocation (deploy/docker-compose.yml).
_REPLAY_BUFFER_HARD_CAP: Final[int] = 1_000_000

#: Per-subscriber outbound queue size. Choice: large enough to absorb a
#: brief consumer stall (network jitter, slow tab) but bounded so a wedged
#: consumer triggers eviction rather than RAM growth in the multiplexer.
_SUBSCRIBER_QUEUE_MAXSIZE: Final[int] = 500

#: SSE event types the multiplexer accepts. Mirrors backend-spec §4.2.2
#: "Event Types (locked)" table plus ``ping`` (the heartbeat type — present
#: in IG §3 Week 5 Tue gate language but not the spec table because it has
#: no producer beyond the multiplexer itself). Reject any other type at
#: ``emit_sse`` call time so a typo doesn't silently ship a non-spec event.
_CANONICAL_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        # The 7 IG-named types for Week 5 Tue scope:
        "signal",
        "fill",
        "position",
        "pnl",
        "alert",
        "health",
        "risk_state",
        # Spec §4.2.2 additional event types — listed for future emit calls
        # (none land Day 16; the dispatcher Week 4 Wed wires audit + agent;
        # vacation + watchdog + job + version + session_evicted wire at
        # their respective lifecycle points):
        "audit",
        "agent",
        "vacation",
        "watchdog",
        "job",
        "version",
        "session_evicted",
        # Heartbeat (multiplexer-internal):
        "ping",
    }
)

#: Reasons an eviction can fire. Spec §4.2.2 lists 4; today only
#: ``tab_limit`` wires (frontend-spec §4.6 + IG §3 Week 5 Tue task).
SessionEvictionReason = Literal[
    "tab_limit",
    "explicit_logout",
    "breakglass_kill",
    "creds_rotated",
]


# ---------- Module state -----------------------------------------------------


@dataclass(slots=True)
class _ReplayEntry:
    """A single buffered frame for Last-Event-ID resume."""

    sequence_no: int
    server_now: datetime
    raw_frame: bytes


@dataclass(slots=True)
class Subscriber:
    """One connected tab's outbound channel + eviction signal."""

    user_id: str
    queue: asyncio.Queue[bytes] = field(
        default_factory=lambda: asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAXSIZE)
    )
    connected_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    evict_event: asyncio.Event = field(default_factory=asyncio.Event)
    evict_reason: SessionEvictionReason | None = None


_sequence_counter: int = 0
_replay_buffer: deque[_ReplayEntry] = deque()
_subscribers_by_user: dict[str, list[Subscriber]] = {}
_heartbeat_task: asyncio.Task[None] | None = None


# ---------- Helpers ----------------------------------------------------------


def _next_sequence_no() -> int:
    """Allocate the next global monotonic sequence number."""
    global _sequence_counter
    _sequence_counter += 1
    return _sequence_counter


def _format_server_now(dt: datetime) -> str:
    """RFC 3339 UTC ms-precision per spec §4.2.1.

    Output shape: ``2026-05-04T17:30:00.123Z``. The ``Z`` suffix is the
    UTC offset; Python's ``isoformat(timespec='milliseconds')`` would
    emit ``+00:00`` instead, which is RFC-3339-equivalent but the spec
    locks the ``Z`` form explicitly.
    """
    if dt.tzinfo is None:
        raise ValueError("server_now must be timezone-aware UTC (anti-pattern A06)")
    # Force UTC: an aware non-UTC datetime would canonicalize to a different
    # wall-clock; ``astimezone(UTC)`` normalizes the offset to zero so the
    # ms-precision below is unambiguous.
    dt_utc = dt.astimezone(UTC)
    base = dt_utc.strftime("%Y-%m-%dT%H:%M:%S")
    ms = f"{dt_utc.microsecond // 1000:03d}"
    return f"{base}.{ms}Z"


def _build_envelope(
    event_type: str,
    sequence_no: int,
    server_now: datetime,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Compose the canonical §4.2.1 envelope dict.

    The wire frame is the JCS-canonical serialization of this dict.
    """
    return {
        "type": event_type,
        "sequence_no": sequence_no,
        "server_now": _format_server_now(server_now),
        "data": data,
    }


def _encode_sse_frame(envelope: dict[str, Any]) -> bytes:
    """Encode a canonical envelope as an SSE wire frame.

    Output bytes::

        id: <sequence_no>\\nevent: <type>\\ndata: <jcs-canonical-json>\\n\\n

    The ``data`` line carries the FULL envelope (not just the inner
    ``data`` field) so clients can introspect ``sequence_no`` +
    ``server_now`` without relying on the ``id:`` line — `EventSource`
    surfaces ``id:`` only via the subsequent reconnect's ``Last-Event-ID``
    header, not on the in-progress message.
    """
    canonical = jcs_serialize(envelope)
    sequence_no: int = envelope["sequence_no"]
    event_type: str = envelope["type"]
    frame: bytes = (
        b"id: "
        + str(sequence_no).encode("ascii")
        + b"\nevent: "
        + event_type.encode("ascii")
        + b"\ndata: "
        + canonical
        + b"\n\n"
    )
    return frame


def _prune_replay_buffer(now: datetime) -> None:
    """Drop entries older than :data:`_REPLAY_RETENTION` from the LEFT.

    Also enforces the hard cap (:data:`_REPLAY_BUFFER_HARD_CAP`) by
    popping from the LEFT until the deque is at or below the cap. The
    cap is a safety valve — under normal Phase-0 emit volume the time-
    based prune drops entries well before the count matters.
    """
    cutoff = now - _REPLAY_RETENTION
    while _replay_buffer and _replay_buffer[0].server_now < cutoff:
        _replay_buffer.popleft()
    while len(_replay_buffer) > _REPLAY_BUFFER_HARD_CAP:
        _replay_buffer.popleft()


def _enqueue_or_drop(subscriber: Subscriber, frame: bytes) -> bool:
    """Try to enqueue ``frame``; return False if the subscriber's queue is full.

    Per the module's backpressure contract, a full queue means we drop
    the subscriber rather than block the fan-out. The caller is expected
    to evict the subscriber when this returns False.
    """
    try:
        subscriber.queue.put_nowait(frame)
    except asyncio.QueueFull:
        return False
    return True


def _evict_subscriber(subscriber: Subscriber, reason: SessionEvictionReason) -> None:
    """Signal the subscriber's generator to close + remove from tracking.

    Order matters here:

    1. Mark the reason + set the evict_event so the route generator's
       wait loop picks it up on its next iteration.
    2. Remove the subscriber from ``_subscribers_by_user`` so future
       broadcasts don't try to enqueue (or count it toward N=4).

    The actual close-of-stream happens in the route's generator when it
    sees ``evict_event.is_set()`` — we don't close the underlying queue
    here because the generator may still be draining buffered frames it
    wants to ship before exiting.
    """
    subscriber.evict_reason = reason
    subscriber.evict_event.set()

    bucket = _subscribers_by_user.get(subscriber.user_id)
    if bucket is None:
        return
    try:
        bucket.remove(subscriber)
    except ValueError:
        return
    if not bucket:
        del _subscribers_by_user[subscriber.user_id]


def _emit_session_evicted_to(subscriber: Subscriber, reason: SessionEvictionReason) -> int:
    """Push a ``session_evicted`` envelope directly to a single subscriber.

    Unlike :func:`emit_sse` this is NOT broadcast — only the evicted
    subscriber receives the frame, then their generator closes.

    Synchronous: no awaits inside. Called from :func:`register_subscriber`
    (also sync) so the ordering is: eviction frame queued → evict_event
    set, ensuring the evicted client's generator sees the frame before
    closing.
    """
    seq = _next_sequence_no()
    now = datetime.now(tz=UTC)
    envelope = _build_envelope(
        "session_evicted",
        seq,
        now,
        {"reason": reason},
    )
    frame = _encode_sse_frame(envelope)
    # Even the evicted subscriber's frame goes into the replay buffer —
    # if they reconnect immediately, Last-Event-ID resume should NOT
    # re-deliver their own eviction notice (they'll only see frames with
    # sequence_no > the eviction frame's). This preserves the §4.2.1
    # "global monotonic across all event types" lock.
    _replay_buffer.append(_ReplayEntry(sequence_no=seq, server_now=now, raw_frame=frame))
    _prune_replay_buffer(now)
    # Best-effort enqueue. If the queue is full we still mark the eviction
    # signal; the route's generator handles the close regardless of whether
    # the final frame made it through.
    _enqueue_or_drop(subscriber, frame)
    return seq


# ---------- Public emit API --------------------------------------------------


async def emit_sse(event_type: str, data: dict[str, Any]) -> int:
    """Broadcast an SSE event to every connected subscriber.

    Allocates the next sequence number, composes the canonical §4.2.1
    envelope, JCS-canonicalizes it, appends to the replay buffer, prunes
    expired entries, and enqueues the frame on every subscriber's queue.
    Subscribers whose queues are full at enqueue time are dropped: the
    connection's evict_event is set + the bucket entry removed, but NO
    ``session_evicted`` envelope ships (the queue is full — the frame
    wouldn't fit anyway). The client gets an EOF, reconnects with
    Last-Event-ID, and the replay buffer catches them up per spec §4.2.3.

    Returns the allocated sequence number so callers can correlate the
    SSE emit with their audit-log row (audit_log INSERT first, then SSE
    emit per dev-guide §5.2's ordering — audit is authoritative).

    Raises
    ------
    ValueError
        If ``event_type`` is not in the canonical taxonomy. Catches typos
        + accidental non-spec event types at the call site rather than
        silently broadcasting a non-spec frame.
    """
    if event_type not in _CANONICAL_EVENT_TYPES:
        raise ValueError(
            f"non-canonical SSE event_type {event_type!r}; "
            f"see services.api.sse._CANONICAL_EVENT_TYPES for the locked set"
        )

    seq = _next_sequence_no()
    now = datetime.now(tz=UTC)
    envelope = _build_envelope(event_type, seq, now, data)
    frame = _encode_sse_frame(envelope)

    _replay_buffer.append(_ReplayEntry(sequence_no=seq, server_now=now, raw_frame=frame))
    _prune_replay_buffer(now)

    dropped: list[Subscriber] = []
    # Snapshot the iteration target so eviction-driven mutations of the
    # subscriber dict during fan-out don't trip ``RuntimeError: dictionary
    # changed size during iteration``.
    snapshot = [sub for bucket in _subscribers_by_user.values() for sub in bucket]
    for sub in snapshot:
        if not _enqueue_or_drop(sub, frame):
            dropped.append(sub)

    for sub in dropped:
        log.warning(
            "sse_subscriber_dropped_full_queue",
            user_id=sub.user_id,
            sequence_no=seq,
            event_type=event_type,
        )
        # No session_evicted envelope for backpressure drops — the queue
        # is already full so the frame wouldn't ship. The route's
        # generator picks up the evict_event signal on its next iteration
        # and closes the stream; the client reconnects + replays.
        _evict_subscriber(sub, "tab_limit")

    log.debug(
        "sse_event_emitted",
        event_type=event_type,
        sequence_no=seq,
        subscriber_count=len(snapshot),
        dropped_subscribers=len(dropped),
    )
    return seq


# ---------- Subscriber registration + eviction-by-tab-limit ------------------


def register_subscriber(user_id: str) -> Subscriber:
    """Create a Subscriber, evict the oldest tab if the user is at N=4.

    Eviction order: walk the user's bucket from index 0 (oldest by
    ``connected_at``) and evict subscribers until length is back at
    ``_MAX_TABS_PER_USER - 1`` so the new subscriber fits. The bucket is
    maintained in connection-order (append on register, no resort) so
    index 0 is reliably the oldest.

    Phase-0 + Phase-1: this fires per spec §4.2.2 N=4 lock. Phase-2 the
    cross-replica check moves to shared storage; the public signature
    doesn't change.
    """
    bucket = _subscribers_by_user.setdefault(user_id, [])
    while len(bucket) >= _MAX_TABS_PER_USER:
        oldest = bucket[0]
        # Order matters: queue the eviction envelope FIRST so the evicted
        # client's stream generator wakes up, drains the queue, sees the
        # session_evicted frame, then picks up evict_event on the next
        # iteration. Reversing the order would mean the generator might
        # exit before the frame ships.
        _emit_session_evicted_to(oldest, "tab_limit")
        _evict_subscriber(oldest, "tab_limit")
        log.info(
            "sse_subscriber_evicted_tab_limit",
            user_id=user_id,
            evicted_connected_at=oldest.connected_at.isoformat(),
        )

    subscriber = Subscriber(user_id=user_id)
    bucket.append(subscriber)
    log.info(
        "sse_subscriber_registered",
        user_id=user_id,
        active_tabs=len(bucket),
    )
    return subscriber


def deregister_subscriber(subscriber: Subscriber) -> None:
    """Remove a subscriber on stream close.

    Idempotent — called from the route generator's ``finally`` clause.
    Tab-limit eviction has already removed the subscriber via
    :func:`_evict_subscriber`; this is a no-op in that path.
    """
    bucket = _subscribers_by_user.get(subscriber.user_id)
    if bucket is None:
        return
    try:
        bucket.remove(subscriber)
    except ValueError:
        return
    if not bucket:
        del _subscribers_by_user[subscriber.user_id]
    log.info(
        "sse_subscriber_deregistered",
        user_id=subscriber.user_id,
        remaining_tabs=len(_subscribers_by_user.get(subscriber.user_id, [])),
    )


# ---------- Replay (Last-Event-ID) -------------------------------------------


@dataclass(frozen=True, slots=True)
class ReplayResolution:
    """Outcome of resolving a Last-Event-ID header against the buffer.

    * ``frames`` non-empty + ``buffer_expired=False``: client gets caught
      up. The route streams ``frames`` first, then continues live.
    * ``frames`` empty + ``buffer_expired=False``: client is already
      current (or hasn't seen any prior events). The route streams live
      only.
    * ``buffer_expired=True``: gap exceeds the 24h window. The route
      returns HTTP 426 with a ``version`` envelope carrying
      ``{must_reload: true, reason: "buffer_expired"}``.
    """

    frames: list[bytes]
    buffer_expired: bool


def resolve_replay(resume_from: int | None) -> ReplayResolution:
    """Return the frames a reconnecting client should see before going live.

    Semantics (spec §4.2.3):

    * ``resume_from is None`` or ``<= 0`` → fresh connection; no replay.
    * ``resume_from >= _sequence_counter`` → client is current; no replay.
    * ``resume_from < oldest_buffered_seq`` AND the buffer is non-empty
      AND ``_sequence_counter > resume_from`` → gap exceeds the window;
      ``buffer_expired=True``.
    * Otherwise → frames with ``sequence_no > resume_from`` from the
      buffer.

    Edge case: an empty replay buffer with ``resume_from < _sequence_counter``
    is treated as buffer-expired (we have evidence of prior events but
    no replay data — the client must full-refetch).
    """
    if resume_from is None or resume_from <= 0:
        return ReplayResolution(frames=[], buffer_expired=False)

    if resume_from >= _sequence_counter:
        # Client is current or ahead of the server — no replay needed.
        # The "ahead" case can happen across api process restarts where
        # the counter resets to zero; the spec treats this as "client is
        # current" because no in-flight events could have been missed
        # between restart and now.
        return ReplayResolution(frames=[], buffer_expired=False)

    if not _replay_buffer:
        # We have prior events (counter > resume_from) but nothing in the
        # buffer. Either the buffer expired or a process restart wiped
        # the deque. Either way, signal must-reload.
        return ReplayResolution(frames=[], buffer_expired=True)

    oldest_seq = _replay_buffer[0].sequence_no
    if resume_from < oldest_seq - 1:
        # ``- 1`` because resume_from = oldest_seq - 1 means the client's
        # last-seen IS the buffer's predecessor — the next emit is the
        # buffer's first frame; that's a clean resume, not expired.
        return ReplayResolution(frames=[], buffer_expired=True)

    frames = [entry.raw_frame for entry in _replay_buffer if entry.sequence_no > resume_from]
    return ReplayResolution(frames=frames, buffer_expired=False)


def build_must_reload_frame() -> bytes:
    """Build the canonical ``version`` envelope for the 426 path.

    Spec §4.2.3:

        send {type: "version", data: {must_reload: true, reason: "buffer_expired"}}

    The frame goes through the same envelope + JCS pipeline as a live
    event so wire-format is identical end-to-end. Note this DOES allocate
    a sequence_no, even though the route closes immediately after sending
    it — the §4.2.1 "global monotonic" lock requires every envelope on the
    wire to carry a unique seq.
    """
    seq = _next_sequence_no()
    now = datetime.now(tz=UTC)
    envelope = _build_envelope(
        "version",
        seq,
        now,
        {"must_reload": True, "reason": "buffer_expired"},
    )
    return _encode_sse_frame(envelope)


# ---------- Heartbeat task ---------------------------------------------------


async def _heartbeat_loop() -> None:
    """Emit one ``event: ping`` per :data:`_HEARTBEAT_SECONDS`.

    The ping data is the server's current RFC 3339 UTC ms-precision
    timestamp (same as the envelope's ``server_now``) so a client can
    detect clock skew with one event. The ping carries a full sequence_no
    + lands in the replay buffer per spec §4.2.1.

    Cancellation: the lifespan shutdown handler cancels this task; the
    ``CancelledError`` propagates out of the ``asyncio.sleep`` and exits
    the loop cleanly.
    """
    log.info("sse_heartbeat_loop_started", interval_seconds=_HEARTBEAT_SECONDS)
    try:
        while True:
            await asyncio.sleep(_HEARTBEAT_SECONDS)
            now_iso = _format_server_now(datetime.now(tz=UTC))
            try:
                await emit_sse("ping", {"server_now": now_iso})
            except Exception:  # pragma: no cover — defensive
                # Don't let one bad emit kill the heartbeat loop. The
                # structlog .exception() captures the traceback for ops
                # diagnosis.
                log.exception("sse_heartbeat_emit_failed")
    except asyncio.CancelledError:
        log.info("sse_heartbeat_loop_cancelled")
        raise


def start_heartbeat() -> asyncio.Task[None]:
    """Spawn the background heartbeat task; idempotent on re-entry.

    Called from the FastAPI lifespan startup phase
    (``services/api/main.py``). Returns the task handle so the lifespan
    can ``.cancel()`` it on shutdown.
    """
    global _heartbeat_task
    if _heartbeat_task is not None and not _heartbeat_task.done():
        return _heartbeat_task
    _heartbeat_task = asyncio.create_task(_heartbeat_loop())
    return _heartbeat_task


async def stop_heartbeat() -> None:
    """Cancel the background heartbeat task; idempotent on re-entry."""
    global _heartbeat_task
    if _heartbeat_task is None:
        return
    if not _heartbeat_task.done():
        _heartbeat_task.cancel()
        try:
            await _heartbeat_task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("sse_heartbeat_stop_unexpected_error")
    _heartbeat_task = None


# ---------- Per-subscriber stream generator ----------------------------------


async def stream_for_subscriber(
    subscriber: Subscriber,
    *,
    replay_frames: list[bytes] | None = None,
) -> AsyncGenerator[bytes, None]:
    """Yield SSE bytes for one connection until evicted or disconnected.

    Order of yields:

    1. Optional ``replay_frames`` (from :func:`resolve_replay`) — drained
       in order before live events.
    2. Frames pulled from the subscriber's queue until the
       :attr:`Subscriber.evict_event` fires.

    The route handler wraps this generator in a ``StreamingResponse`` and
    handles the deregistration in its ``finally``.
    """
    if replay_frames:
        for frame in replay_frames:
            yield frame

    queue = subscriber.queue
    evict = subscriber.evict_event

    while not evict.is_set():
        get_task = asyncio.create_task(queue.get())
        evict_task = asyncio.create_task(evict.wait())
        done, pending = await asyncio.wait(
            {get_task, evict_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except asyncio.CancelledError:
                # Expected: we cancelled `pending` above on every iteration.
                continue
            except Exception:  # pragma: no cover — defensive
                log.exception("sse_stream_pending_task_unexpected_error")

        if get_task in done:
            frame = get_task.result()
            yield frame
            # Drain any remaining queued frames before re-arming the wait
            # so eviction signals are picked up promptly on the next loop
            # iteration but the subscriber doesn't lose in-flight events.
            while True:
                try:
                    yield queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        if evict_task in done:
            break


# ---------- Test-only state reset --------------------------------------------


def reset_state() -> None:
    """Reset multiplexer globals to their initial state.

    Used by tests to isolate runs — pytest fixtures invoke this between
    tests so the module-level globals don't leak across cases. NOT for
    production use; the api process never resets state mid-flight.
    """
    global _sequence_counter, _heartbeat_task
    _sequence_counter = 0
    _replay_buffer.clear()
    _subscribers_by_user.clear()
    _heartbeat_task = None


# ---------- Test-visible internals (read-only) -------------------------------
#
# These getters expose internal state for assertions in unit tests. Route
# code MUST NOT call them — read paths go through ``emit_sse`` / ``resolve_replay``
# / ``register_subscriber``. Exposed here (rather than via direct module-
# attribute access) so the public surface stays explicit and the names
# document the test-only contract.


def _test_current_sequence_no() -> int:
    return _sequence_counter


def _test_replay_buffer_size() -> int:
    return len(_replay_buffer)


def _test_replay_buffer_snapshot() -> list[tuple[int, datetime]]:
    return [(e.sequence_no, e.server_now) for e in _replay_buffer]


def _test_subscriber_count(user_id: str | None = None) -> int:
    if user_id is None:
        return sum(len(b) for b in _subscribers_by_user.values())
    return len(_subscribers_by_user.get(user_id, []))


__all__ = [
    "ReplayResolution",
    "SessionEvictionReason",
    "Subscriber",
    "build_must_reload_frame",
    "deregister_subscriber",
    "emit_sse",
    "register_subscriber",
    "reset_state",
    "resolve_replay",
    "start_heartbeat",
    "stop_heartbeat",
    "stream_for_subscriber",
]
