"""Unit tests for the Day 16 SSE multiplexer + route.

Coverage targets (backend-spec §4.2 + dev-guide §5.2 + frontend-spec §4.6):

* Canonical envelope shape per §4.2.1 (type / sequence_no / server_now /
  data).
* RFC 3339 UTC ms-precision ``server_now`` (``Z`` suffix, not ``+00:00``).
* JCS-canonical wire frame (sorted keys, no whitespace).
* Global monotonic sequence_no across all event types (including
  ``ping`` heartbeats).
* 24h replay buffer pruning by timestamp; hard-cap safety valve.
* ``Last-Event-ID`` resume returns frames > resume_from.
* 426 + ``must_reload`` envelope when the gap exceeds the window.
* N=4 multi-tab eviction (frontend-spec §4.6) ships a ``session_evicted``
  envelope with ``reason="tab_limit"`` to the evicted tab.
* Backpressure: full subscriber queue → drop subscriber, no envelope
  (since the queue is full anyway).
* Canonical event-type taxonomy gate at emit time.
* Heartbeat task emits ``event: ping`` at the locked cadence.
* Route surface: ``/api/sse/events`` returns 200 + ``text/event-stream``;
  malformed ``Last-Event-ID`` treated leniently; 426 path produces the
  must-reload envelope.

Tests use direct module-call patterns (``emit_sse`` / ``register_subscriber``
/ ``stream_for_subscriber``) where possible, falling back to the FastAPI
TestClient only for the route-level header parsing + status assertions.
Streaming-disconnect behavior is exercised via the multiplexer's
``evict_event`` rather than `httpx`'s in-process ASGI transport (which
doesn't reliably propagate request.is_disconnected for in-memory streams).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from services.api import sse as multiplexer
from services.api.routes import sse as sse_route


@pytest.fixture(autouse=True)
def _reset_multiplexer() -> None:
    multiplexer.reset_state()
    yield
    multiplexer.reset_state()


class TestEnvelopeShape:
    @pytest.mark.asyncio
    async def test_emit_returns_sequence_no_starting_at_one(self) -> None:
        seq = await multiplexer.emit_sse("signal", {"signal_id": "abc"})
        assert seq == 1

    @pytest.mark.asyncio
    async def test_sequence_no_monotonically_increases_across_types(self) -> None:
        s1 = await multiplexer.emit_sse("signal", {})
        s2 = await multiplexer.emit_sse("fill", {})
        s3 = await multiplexer.emit_sse("alert", {})
        assert (s1, s2, s3) == (1, 2, 3)

    def test_format_server_now_rfc3339_ms_precision_with_z_suffix(self) -> None:
        dt = datetime(2026, 5, 10, 17, 30, 45, 123_456, tzinfo=UTC)
        assert multiplexer._format_server_now(dt) == "2026-05-10T17:30:45.123Z"

    def test_format_server_now_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValueError, match="anti-pattern A06"):
            multiplexer._format_server_now(datetime(2026, 5, 10, 17, 30, 0))

    def test_format_server_now_normalizes_non_utc_offset(self) -> None:
        from datetime import timezone

        et_minus_4 = timezone(timedelta(hours=-4))
        dt = datetime(2026, 5, 10, 13, 30, 0, 500_000, tzinfo=et_minus_4)
        # 13:30-04:00 == 17:30Z
        assert multiplexer._format_server_now(dt) == "2026-05-10T17:30:00.500Z"


class TestWireFrame:
    @pytest.mark.asyncio
    async def test_frame_carries_full_envelope_in_data_line(self) -> None:
        sub = multiplexer.register_subscriber("u1")
        seq = await multiplexer.emit_sse("signal", {"market": "MES"})
        frame = sub.queue.get_nowait()

        text = frame.decode("utf-8")
        assert text.startswith(f"id: {seq}\n")
        assert "event: signal\n" in text
        assert text.endswith("\n\n")

        data_line = next(line for line in text.splitlines() if line.startswith("data: "))
        envelope = json.loads(data_line.removeprefix("data: "))
        assert envelope["type"] == "signal"
        assert envelope["sequence_no"] == seq
        assert envelope["data"] == {"market": "MES"}
        assert envelope["server_now"].endswith("Z")

    @pytest.mark.asyncio
    async def test_frame_is_jcs_canonical_sorted_keys_no_whitespace(self) -> None:
        sub = multiplexer.register_subscriber("u1")
        await multiplexer.emit_sse("position", {"zeta": 1, "alpha": 2})
        frame = sub.queue.get_nowait()

        data_line = next(
            line for line in frame.decode("utf-8").splitlines() if line.startswith("data: ")
        )
        payload = data_line.removeprefix("data: ")
        # JCS: alphabetical key order at every level + no whitespace between
        # tokens. The envelope-level keys are data/sequence_no/server_now/type.
        assert payload.startswith('{"data":')
        # Inner data dict also sorts: alpha before zeta.
        assert '"data":{"alpha":2,"zeta":1}' in payload
        # No whitespace between tokens (JCS forbids it).
        assert ": " not in payload
        assert ", " not in payload


class TestCanonicalEventTypeGate:
    @pytest.mark.asyncio
    async def test_non_canonical_event_type_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="non-canonical SSE event_type"):
            await multiplexer.emit_sse("not_a_real_type", {})

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "event_type",
        ["signal", "fill", "position", "pnl", "alert", "health", "risk_state"],
    )
    async def test_all_seven_ig_event_types_accepted(self, event_type: str) -> None:
        await multiplexer.emit_sse(event_type, {})
        assert multiplexer._test_current_sequence_no() == 1

    @pytest.mark.asyncio
    async def test_ping_event_type_accepted_for_heartbeat(self) -> None:
        await multiplexer.emit_sse("ping", {"server_now": "..."})
        assert multiplexer._test_current_sequence_no() == 1


class TestReplayBuffer:
    @pytest.mark.asyncio
    async def test_each_emit_appends_one_entry(self) -> None:
        await multiplexer.emit_sse("signal", {})
        await multiplexer.emit_sse("fill", {})
        assert multiplexer._test_replay_buffer_size() == 2

    @pytest.mark.asyncio
    async def test_prune_drops_entries_older_than_24h(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Set retention to 1s so the test is fast + deterministic.
        monkeypatch.setattr(multiplexer, "_REPLAY_RETENTION", timedelta(milliseconds=10))
        await multiplexer.emit_sse("signal", {})
        await asyncio.sleep(0.02)
        await multiplexer.emit_sse("fill", {})
        # The first emit is older than 10ms; the second emit's prune pass
        # should have dropped it.
        snapshot = multiplexer._test_replay_buffer_snapshot()
        assert len(snapshot) == 1
        assert snapshot[0][0] == 2

    def test_prune_enforces_hard_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Lower the hard cap + push directly into the deque so we don't
        # depend on emit-storm timing. ``_prune_replay_buffer`` is the
        # function under test.
        monkeypatch.setattr(multiplexer, "_REPLAY_BUFFER_HARD_CAP", 3)
        now = datetime.now(tz=UTC)
        for i in range(5):
            multiplexer._replay_buffer.append(
                multiplexer._ReplayEntry(
                    sequence_no=i + 1,
                    server_now=now,
                    raw_frame=b"x",
                )
            )
        multiplexer._prune_replay_buffer(now)
        assert multiplexer._test_replay_buffer_size() == 3
        # FIFO eviction from the LEFT: keep the newest 3.
        seqs = [e[0] for e in multiplexer._test_replay_buffer_snapshot()]
        assert seqs == [3, 4, 5]


class TestResolveReplay:
    @pytest.mark.asyncio
    async def test_no_header_returns_empty_no_expired(self) -> None:
        await multiplexer.emit_sse("signal", {})
        await multiplexer.emit_sse("fill", {})
        resolution = multiplexer.resolve_replay(None)
        assert resolution.frames == []
        assert resolution.buffer_expired is False

    @pytest.mark.asyncio
    async def test_zero_resume_treated_as_fresh_connect(self) -> None:
        await multiplexer.emit_sse("signal", {})
        resolution = multiplexer.resolve_replay(0)
        assert resolution.frames == []
        assert resolution.buffer_expired is False

    @pytest.mark.asyncio
    async def test_resume_after_one_returns_subsequent_frames(self) -> None:
        await multiplexer.emit_sse("signal", {})
        await multiplexer.emit_sse("fill", {})
        await multiplexer.emit_sse("position", {})
        resolution = multiplexer.resolve_replay(1)
        assert resolution.buffer_expired is False
        assert len(resolution.frames) == 2
        # Frame ids 2 and 3 are present.
        assert b"id: 2\n" in resolution.frames[0]
        assert b"id: 3\n" in resolution.frames[1]

    @pytest.mark.asyncio
    async def test_resume_at_current_seq_returns_empty(self) -> None:
        await multiplexer.emit_sse("signal", {})
        resolution = multiplexer.resolve_replay(1)
        assert resolution.frames == []
        assert resolution.buffer_expired is False

    @pytest.mark.asyncio
    async def test_resume_ahead_of_current_seq_treated_as_current(self) -> None:
        # Models the post-process-restart case: client's last-seen seq is
        # higher than the new process counter; treat as current.
        await multiplexer.emit_sse("signal", {})
        resolution = multiplexer.resolve_replay(999)
        assert resolution.frames == []
        assert resolution.buffer_expired is False

    @pytest.mark.asyncio
    async def test_resume_before_oldest_buffered_returns_expired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Tight retention so each emit prunes the prior; afterward the
        # buffer holds only the latest. The client's resume header points
        # at a sequence that's no longer in the window → expired.
        monkeypatch.setattr(multiplexer, "_REPLAY_RETENTION", timedelta(milliseconds=5))
        await multiplexer.emit_sse("signal", {})  # seq=1
        await asyncio.sleep(0.02)
        await multiplexer.emit_sse("fill", {})  # seq=2, prunes seq=1
        await asyncio.sleep(0.02)
        await multiplexer.emit_sse("position", {})  # seq=3, prunes seq=2
        # Buffer now holds only seq=3 (oldest_seq=3). resume_from=1 means
        # client last saw seq=1; the gap (1 → 3) exceeds the window.
        # Per the resolver: resume_from=1 < oldest_seq - 1 = 2 → expired.
        assert multiplexer._replay_buffer[0].sequence_no == 3
        resolution = multiplexer.resolve_replay(1)
        assert resolution.buffer_expired is True
        assert resolution.frames == []

    def test_resume_with_empty_buffer_but_prior_events_returns_expired(self) -> None:
        # No emits but counter is forced > 0 (models a buffer wipe with
        # counter still set). Use the internal counter directly.
        multiplexer._sequence_counter = 42
        resolution = multiplexer.resolve_replay(10)
        assert resolution.buffer_expired is True


class TestMustReloadFrame:
    def test_must_reload_frame_shape(self) -> None:
        frame = multiplexer.build_must_reload_frame()
        text = frame.decode("utf-8")
        assert "event: version\n" in text
        data_line = next(line for line in text.splitlines() if line.startswith("data: "))
        envelope = json.loads(data_line.removeprefix("data: "))
        assert envelope["type"] == "version"
        assert envelope["data"] == {
            "must_reload": True,
            "reason": "buffer_expired",
        }
        assert envelope["sequence_no"] >= 1

    def test_must_reload_frame_increments_sequence(self) -> None:
        multiplexer.build_must_reload_frame()
        multiplexer.build_must_reload_frame()
        assert multiplexer._test_current_sequence_no() == 2


class TestSubscriberRegistration:
    def test_registers_subscriber_under_user_id(self) -> None:
        sub = multiplexer.register_subscriber("u1")
        assert sub.user_id == "u1"
        assert multiplexer._test_subscriber_count("u1") == 1

    def test_multiple_users_tracked_separately(self) -> None:
        multiplexer.register_subscriber("u1")
        multiplexer.register_subscriber("u2")
        multiplexer.register_subscriber("u2")
        assert multiplexer._test_subscriber_count("u1") == 1
        assert multiplexer._test_subscriber_count("u2") == 2

    def test_deregister_idempotent_when_already_removed(self) -> None:
        sub = multiplexer.register_subscriber("u1")
        multiplexer.deregister_subscriber(sub)
        multiplexer.deregister_subscriber(sub)  # second call no-op
        assert multiplexer._test_subscriber_count("u1") == 0


class TestTabLimitEviction:
    def test_fifth_connection_evicts_oldest(self) -> None:
        subs = [multiplexer.register_subscriber("u1") for _ in range(4)]
        assert multiplexer._test_subscriber_count("u1") == 4
        new_sub = multiplexer.register_subscriber("u1")
        # Oldest evicted, new sub added.
        assert multiplexer._test_subscriber_count("u1") == 4
        assert subs[0].evict_event.is_set()
        assert subs[0].evict_reason == "tab_limit"
        # New subscriber not evicted.
        assert not new_sub.evict_event.is_set()

    def test_evicted_subscriber_receives_session_evicted_envelope(self) -> None:
        oldest = multiplexer.register_subscriber("u1")
        for _ in range(3):
            multiplexer.register_subscriber("u1")
        # Trigger eviction.
        multiplexer.register_subscriber("u1")

        # Oldest's queue carries the eviction envelope.
        frame = oldest.queue.get_nowait()
        text = frame.decode("utf-8")
        assert "event: session_evicted\n" in text
        data_line = next(line for line in text.splitlines() if line.startswith("data: "))
        envelope = json.loads(data_line.removeprefix("data: "))
        assert envelope["type"] == "session_evicted"
        assert envelope["data"] == {"reason": "tab_limit"}

    def test_eviction_only_affects_target_users_bucket(self) -> None:
        u1_subs = [multiplexer.register_subscriber("u1") for _ in range(4)]
        u2_sub = multiplexer.register_subscriber("u2")
        # u1 fills, triggers eviction on next register.
        multiplexer.register_subscriber("u1")
        assert u1_subs[0].evict_event.is_set()
        # u2's subscriber untouched.
        assert not u2_sub.evict_event.is_set()
        assert multiplexer._test_subscriber_count("u2") == 1

    @pytest.mark.asyncio
    async def test_evict_event_in_replay_buffer(self) -> None:
        for _ in range(5):
            multiplexer.register_subscriber("u1")
        # One eviction happened; the session_evicted frame is in the
        # buffer too (so reconnect-with-Last-Event-ID skips it).
        snapshot = multiplexer._test_replay_buffer_snapshot()
        assert len(snapshot) == 1
        # And the global counter advanced.
        assert multiplexer._test_current_sequence_no() == 1


class TestBroadcastFanOut:
    @pytest.mark.asyncio
    async def test_emit_fans_out_to_all_subscribers(self) -> None:
        a = multiplexer.register_subscriber("ua")
        b = multiplexer.register_subscriber("ub")
        await multiplexer.emit_sse("signal", {"signal_id": "x"})
        assert a.queue.qsize() == 1
        assert b.queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_emit_fans_out_to_multiple_tabs_of_same_user(self) -> None:
        s1 = multiplexer.register_subscriber("u1")
        s2 = multiplexer.register_subscriber("u1")
        await multiplexer.emit_sse("position", {})
        assert s1.queue.qsize() == 1
        assert s2.queue.qsize() == 1


class TestBackpressure:
    @pytest.mark.asyncio
    async def test_full_queue_drops_subscriber(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Fill a subscriber's queue manually, then emit — the emit should
        # detect QueueFull and evict the subscriber.
        sub = multiplexer.register_subscriber("u1")
        # Force the queue full by replacing it with a maxsize=1 queue that
        # already has one item.
        sub.queue = asyncio.Queue(maxsize=1)
        sub.queue.put_nowait(b"prefill")
        await multiplexer.emit_sse("signal", {})
        assert sub.evict_event.is_set()
        # Subscriber removed from tracking.
        assert multiplexer._test_subscriber_count("u1") == 0


class TestStreamGenerator:
    @pytest.mark.asyncio
    async def test_yields_replay_frames_then_live(self) -> None:
        sub = multiplexer.register_subscriber("u1")
        replay = [b"frame1\n\n", b"frame2\n\n"]
        gen = multiplexer.stream_for_subscriber(sub, replay_frames=replay)
        # First two yields are the replay frames.
        f1 = await gen.__anext__()
        f2 = await gen.__anext__()
        assert f1 == b"frame1\n\n"
        assert f2 == b"frame2\n\n"
        # Now emit a live event and confirm it shows up.
        await multiplexer.emit_sse("signal", {})
        live = await gen.__anext__()
        assert b"event: signal\n" in live
        # Trigger eviction so the generator can exit cleanly.
        sub.evict_event.set()
        await gen.aclose()

    @pytest.mark.asyncio
    async def test_evict_event_closes_generator(self) -> None:
        sub = multiplexer.register_subscriber("u1")
        sub.evict_event.set()
        gen = multiplexer.stream_for_subscriber(sub)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(gen.__anext__(), timeout=0.5)


class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_heartbeat_emits_ping_at_locked_cadence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(multiplexer, "_HEARTBEAT_SECONDS", 0.02)
        task = multiplexer.start_heartbeat()
        # Idempotent: calling start again returns the same task.
        assert multiplexer.start_heartbeat() is task
        sub = multiplexer.register_subscriber("u1")
        # Wait long enough for at least 2 pings.
        await asyncio.sleep(0.07)
        await multiplexer.stop_heartbeat()
        # At least 2 ping frames in the queue.
        frames: list[bytes] = []
        while not sub.queue.empty():
            frames.append(sub.queue.get_nowait())
        ping_frames = [f for f in frames if b"event: ping\n" in f]
        assert len(ping_frames) >= 2
        first = ping_frames[0].decode("utf-8")
        data_line = next(line for line in first.splitlines() if line.startswith("data: "))
        envelope = json.loads(data_line.removeprefix("data: "))
        assert envelope["type"] == "ping"
        assert envelope["data"]["server_now"].endswith("Z")

    @pytest.mark.asyncio
    async def test_stop_heartbeat_when_never_started_is_noop(self) -> None:
        # No start; stop should be safe.
        await multiplexer.stop_heartbeat()


class TestRoute:
    @pytest.mark.asyncio
    async def test_initial_connect_registers_subscriber_and_returns_streaming_response(
        self, api_app
    ) -> None:
        # Call the route handler directly with a Request stub rather than
        # streaming through ASGITransport, which doesn't release in-memory
        # streams until the body is fully consumed. The multiplexer's
        # heartbeat task isn't started by this test, so the stream would
        # block forever in a real httpx call.
        from fastapi.responses import StreamingResponse

        from services.api.routes.sse import sse_events
        from services.api.session import SessionContext

        class _StubRequest:
            def __init__(self) -> None:
                self.headers: dict[str, str] = {}

            async def is_disconnected(self) -> bool:
                return False

        session = SessionContext(
            user_id="u-route",
            username="op",
            role="owner",
            auth_strength="weak",
            last_uv_at=datetime.now(tz=UTC),
            session_expires_at=datetime.now(tz=UTC) + timedelta(minutes=30),
            webauthn_enrolled=False,
            totp_enrolled=False,
            is_phase0_stub=True,
        )
        response = await sse_events(request=_StubRequest(), session=session)
        assert isinstance(response, StreamingResponse)
        assert response.status_code == 200
        assert response.media_type == "text/event-stream"
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        # Subscriber registered under session.user_id.
        assert multiplexer._test_subscriber_count("u-route") == 1
        # Close the body generator so it deregisters; otherwise the
        # autouse reset_state still nukes the globals.
        await response.body_iterator.aclose()  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_buffer_expired_resume_returns_426_with_must_reload(
        self, api_client: AsyncClient
    ) -> None:
        # Bump the counter so the resume header looks "expired".
        multiplexer._sequence_counter = 100
        response = await api_client.get(
            "/api/sse/events",
            headers={"last-event-id": "5"},
        )
        assert response.status_code == 426
        assert response.headers["content-type"].startswith("text/event-stream")
        text = response.text
        assert "event: version\n" in text
        data_line = next(line for line in text.splitlines() if line.startswith("data: "))
        envelope = json.loads(data_line.removeprefix("data: "))
        assert envelope["type"] == "version"
        assert envelope["data"]["must_reload"] is True
        assert envelope["data"]["reason"] == "buffer_expired"

    def test_parse_last_event_id_malformed_returns_none(self) -> None:
        assert sse_route._parse_last_event_id("not-an-int") is None

    def test_parse_last_event_id_negative_returns_none(self) -> None:
        assert sse_route._parse_last_event_id("-5") is None

    def test_parse_last_event_id_zero_returns_none(self) -> None:
        assert sse_route._parse_last_event_id("0") is None

    def test_parse_last_event_id_empty_returns_none(self) -> None:
        assert sse_route._parse_last_event_id("") is None
        assert sse_route._parse_last_event_id(None) is None

    def test_parse_last_event_id_valid_returns_int(self) -> None:
        assert sse_route._parse_last_event_id("42") == 42


class TestSessionExemptPathsUpdated:
    def test_sse_events_not_in_exempt_paths(self) -> None:
        from services.api.session import _SESSION_EXEMPT_PATHS

        assert "/api/sse/events" not in _SESSION_EXEMPT_PATHS
        # But the other Day-15 exemptions remain.
        assert "/api/health" in _SESSION_EXEMPT_PATHS
        assert "/api/setup/verify-token" in _SESSION_EXEMPT_PATHS
        assert "/api/internal/watchdog" in _SESSION_EXEMPT_PATHS
