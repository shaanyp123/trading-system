"""Unit tests for `services.api.middleware.RateLimitMiddleware` (Day 17 / Week 5 Wed).

Covers IG §3 Week 5 Wed rate-limiting contract:

  * `/api/**`         — 100 req / 10s per IP (general bucket).
  * `/api/auth/**`    —   5 req / 10s per IP (auth bucket; brute-force-tightened).
  * `/api/health`     — exempt.
  * `/api/sse/events` —  30 req / 10s per IP (sse bucket; added 2026-07-12 —
    each connect costs a session-table lookup under real validation).

Plus middleware-mechanic invariants:

  * 101st request → HTTP 429 with `RATE_LIMITED` envelope + `Retry-After` header.
  * 6th `/api/auth/**` request → HTTP 429.
  * Per-IP isolation — exhausting one IP's bucket does not block another's.
  * Per-bucket isolation — exhausting `/api/auth/**` does not block `/api/**`.
  * Window reset — advancing the clock past the window resets the counter.
  * `RateLimitMiddleware` sits OUTER of CSRF in the request chain (a flood of
    CSRF-failing POSTs still consumes the per-IP budget).
  * Pure Python; no testcontainers (A22); no third-party platform call (A27 N/A).

Time is controlled by monkeypatching `services.api.middleware._monotonic`. No
`asyncio.sleep` anywhere — tests are deterministic + fast.

Path choices:

  * General bucket: probes use ``/api/__rl_test`` (no route handler — FastAPI
    returns 404 — but the middleware ran first and incremented the bucket).
    Avoids tangling rate-limit tests with route-handler DB deps.
  * Auth bucket:    probes use ``/api/auth/__rl_test`` (same idea; under
    ``/api/auth/`` prefix so it routes to the AUTH bucket).
  * Exempt:         /api/health hits a real handler, so `api_db.ping` is
    monkeypatched to a no-op.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from services.api import db as api_db
from services.api import middleware as api_middleware

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def deterministic_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace `_monotonic` with a controllable stub; return a 1-element list.

    Tests advance time by mutating ``store[0]``. List rather than scalar so
    closures share the reference.
    """
    store = [1000.0]

    def _now() -> float:
        return store[0]

    monkeypatch.setattr(api_middleware, "_monotonic", _now)
    return store


@pytest.fixture(autouse=True)
def mock_db_ping(monkeypatch: pytest.MonkeyPatch) -> None:
    """The /api/health route calls `api_db.ping()`; without init_pool that
    raises. These unit tests never hit a real DB; stub the ping to return OK.
    """

    async def _fake_ping(timeout_seconds: float = 0.2) -> tuple[bool, str | None]:
        return True, None

    monkeypatch.setattr(api_db, "ping", _fake_ping)


@pytest.fixture
async def rl_client(
    api_app: FastAPI,
    deterministic_clock: list[float],
) -> AsyncClient:
    """API client whose RateLimitMiddleware sees the deterministic clock.

    The `client=("198.51.100.7", 54321)` tuple gives every test in this
    module a stable client IP so the limiter keys consistently.
    `deterministic_clock` is requested so the fixture chain wires it before
    the app is built; the clock-store reference is unused here directly.
    """
    del deterministic_clock  # presence is what matters; fixture chains via parameter
    transport = ASGITransport(app=api_app, client=("198.51.100.7", 54321))
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


# General-bucket and auth-bucket probe paths. Both 404 from FastAPI's router
# (no handler registered) but the middleware ran FIRST and incremented the
# bucket counter — which is the only thing under test here. The exempt-path
# tests below use real handlers (e.g., /api/health) so they exercise the
# exemption decision through to a real 200.
_GENERAL_PROBE = "/api/__rl_test"
_AUTH_PROBE = "/api/auth/__rl_test"


# ---------------------------------------------------------------------------
# 1. Exempt paths (none of these should ever return 429)
# ---------------------------------------------------------------------------


class TestExemptPaths:
    @pytest.mark.asyncio
    async def test_health_endpoint_exempt(
        self,
        rl_client: AsyncClient,
    ) -> None:
        """200 OK 105 times in a row — `/api/health` MUST NOT be rate-limited.
        The external watchdog (Hetzner Nuremberg) hits this every 5 min; if
        every tick consumed a bucket slot, the bucket would burn down within
        an hour even with no other traffic.
        """
        for _ in range(105):
            response = await rl_client.get("/api/health")
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_non_api_path_exempt(
        self,
        rl_client: AsyncClient,
    ) -> None:
        """Paths outside ``/api/*`` (Next.js frontend, /, etc.) are Caddy's
        concern, not the api's. The api MUST NOT 429 them.
        """
        for _ in range(105):
            response = await rl_client.get("/")
            # 404 from FastAPI (no route), NOT 429 — confirms the limiter
            # short-circuited on the non-api prefix.
            assert response.status_code == 404

    def test_bucket_selection_decision(
        self,
    ) -> None:
        """``/api/sse/events`` gets its own SMALL bucket (2026-07-12): with
        real session validation each SSE connect costs a sessions-table
        lookup, so the old blanket exemption was an unmetered DB-probe seam.
        30/window still clears the N=4-tab reconnect-storm worst case.

        We can't issue a 100-request HTTP loop against SSE because each
        stream is infinite — pytest would hang. Instead we test the bucket
        decision DIRECTLY against the middleware's ``_select_bucket()``
        method. ``/api/internal/watchdog`` lost its exemption the same day:
        the route was never built, and a standing exemption for an unmounted
        path would silently unthrottle whatever mounts there next.
        """
        from services.api.config import APISettings

        settings = APISettings(  # type: ignore[call-arg]
            database_url="postgresql+asyncpg://x:x@127.0.0.1:0/x",  # type: ignore[arg-type]
        )
        # Inert app reference — the middleware only reads it on dispatch.
        mw = api_middleware.RateLimitMiddleware(app=None, settings=settings)  # type: ignore[arg-type]
        assert mw._select_bucket("/api/sse/events") == ("sse", 30)
        assert mw._select_bucket("/api/health") is None
        assert mw._select_bucket("/api/internal/watchdog") == ("general", 100)
        # Sanity: non-exempt paths still resolve to a bucket.
        assert mw._select_bucket("/api/orders") == ("general", 100)
        assert mw._select_bucket("/api/auth/me") == ("auth", 5)
        # Non-/api paths exempt too.
        assert mw._select_bucket("/") is None
        assert mw._select_bucket("/static/foo.css") is None


# ---------------------------------------------------------------------------
# 2. General bucket — /api/** at 100 req / 10s
# ---------------------------------------------------------------------------


class TestGeneralBucket:
    @pytest.mark.asyncio
    async def test_first_100_requests_allowed(
        self,
        rl_client: AsyncClient,
    ) -> None:
        """First 100 requests under the general bucket all pass the limiter
        (router 404s them, but the assertion is that they're not 429).
        """
        for i in range(100):
            response = await rl_client.get(_GENERAL_PROBE)
            assert response.status_code != 429, f"request {i + 1} unexpectedly 429"

    @pytest.mark.asyncio
    async def test_101st_general_request_returns_429(
        self,
        rl_client: AsyncClient,
    ) -> None:
        """Exactly 100 requests succeed; the 101st returns 429 with the
        canonical envelope + Retry-After header + bucket name in details.
        """
        for _ in range(100):
            response = await rl_client.get(_GENERAL_PROBE)
            assert response.status_code != 429
        response = await rl_client.get(_GENERAL_PROBE)
        assert response.status_code == 429
        body = response.json()
        assert body["error_code"] == "RATE_LIMITED"
        assert "100 per 10s" in body["message"]
        assert body["details"]["bucket"] == "general"
        assert body["details"]["limit"] == 100
        # Retry-After is always >= 1 (clients should never poll faster than
        # 1s after a 429).
        assert int(response.headers["Retry-After"]) >= 1

    @pytest.mark.asyncio
    async def test_general_bucket_resets_after_window(
        self,
        rl_client: AsyncClient,
        deterministic_clock: list[float],
    ) -> None:
        """Advance the clock past the 10s window → counter resets → next
        request succeeds.
        """
        for _ in range(100):
            await rl_client.get(_GENERAL_PROBE)
        blocked = await rl_client.get(_GENERAL_PROBE)
        assert blocked.status_code == 429

        # Advance 11 seconds — past the 10s window.
        deterministic_clock[0] += 11.0

        allowed = await rl_client.get(_GENERAL_PROBE)
        assert allowed.status_code != 429


# ---------------------------------------------------------------------------
# 3. Auth bucket — /api/auth/** at 5 req / 10s (brute-force-tightened)
# ---------------------------------------------------------------------------


class TestAuthBucket:
    @pytest.mark.asyncio
    async def test_first_5_auth_requests_allowed(
        self,
        rl_client: AsyncClient,
    ) -> None:
        for i in range(5):
            response = await rl_client.get(_AUTH_PROBE)
            assert response.status_code != 429, f"request {i + 1} unexpectedly 429"

    @pytest.mark.asyncio
    async def test_6th_auth_request_returns_429(
        self,
        rl_client: AsyncClient,
    ) -> None:
        for _ in range(5):
            await rl_client.get(_AUTH_PROBE)
        response = await rl_client.get(_AUTH_PROBE)
        assert response.status_code == 429
        body = response.json()
        assert body["error_code"] == "RATE_LIMITED"
        assert body["details"]["bucket"] == "auth"
        assert body["details"]["limit"] == 5
        assert "5 per 10s" in body["message"]

    @pytest.mark.asyncio
    async def test_auth_bucket_isolated_from_general(
        self,
        rl_client: AsyncClient,
    ) -> None:
        """Exhausting the AUTH bucket must not block GENERAL traffic from the
        same IP — different bucket key (``(host, "auth")`` vs
        ``(host, "general")``).
        """
        for _ in range(5):
            await rl_client.get(_AUTH_PROBE)
        auth_blocked = await rl_client.get(_AUTH_PROBE)
        assert auth_blocked.status_code == 429

        general_ok = await rl_client.get(_GENERAL_PROBE)
        assert general_ok.status_code != 429


# ---------------------------------------------------------------------------
# 4. Per-IP isolation
# ---------------------------------------------------------------------------


class TestPerIPIsolation:
    @pytest.mark.asyncio
    async def test_different_ips_independent_buckets(
        self,
        api_app: FastAPI,
        deterministic_clock: list[float],
    ) -> None:
        """IP A exhausts its bucket; IP B from the same window starts fresh.

        Uses two separate ASGI clients with different `client=` tuples so
        ``request.client.host`` resolves to different IPs.
        """
        del deterministic_clock  # clock is pinned by the fixture; not advanced
        transport_a = ASGITransport(app=api_app, client=("203.0.113.1", 11111))
        transport_b = ASGITransport(app=api_app, client=("203.0.113.2", 22222))

        async with AsyncClient(transport=transport_a, base_url="http://testserver") as client_a:
            for _ in range(100):
                await client_a.get(_GENERAL_PROBE)
            blocked_a = await client_a.get(_GENERAL_PROBE)
            assert blocked_a.status_code == 429

        async with AsyncClient(transport=transport_b, base_url="http://testserver") as client_b:
            allowed_b = await client_b.get(_GENERAL_PROBE)
            assert allowed_b.status_code != 429


# ---------------------------------------------------------------------------
# 5. Unknown client (no request.client)
# ---------------------------------------------------------------------------


class TestUnknownClient:
    @pytest.mark.asyncio
    async def test_unknown_client_falls_back_to_string_key(
        self,
        api_app: FastAPI,
        deterministic_clock: list[float],
    ) -> None:
        """ASGITransport without ``client=`` produces ``request.client is None``.
        The middleware falls back to the literal "unknown" string key — all
        such requests share a single bucket. Safer than per-request keys
        (an unidentified flood is still rate-limited as a single source).
        """
        del deterministic_clock
        transport = ASGITransport(app=api_app)  # no client= tuple
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            for _ in range(100):
                await client.get(_GENERAL_PROBE)
            blocked = await client.get(_GENERAL_PROBE)
            assert blocked.status_code == 429


# ---------------------------------------------------------------------------
# 6. Middleware composition — RateLimit outer of CSRF
# ---------------------------------------------------------------------------


class TestMiddlewareOrdering:
    @pytest.mark.asyncio
    async def test_rate_limit_consumes_slot_even_on_csrf_failure(
        self,
        rl_client: AsyncClient,
    ) -> None:
        """A POST without a CSRF token would normally 403 at CSRFMiddleware.
        Because RateLimit is OUTER of CSRF (Day 17 ordering — see
        ``register_middleware`` docstring), each such request still consumes
        a bucket slot. Prevents an attacker from flooding bad-CSRF POSTs at
        unbounded rate.

        Sends 100 POSTs without a CSRF cookie+header pair; each returns 403
        from CSRFMiddleware (rate-limit allowed it through). The 101st MUST
        return 429 — proving RateLimit incremented on every attempt.
        """
        # Use a path that requires CSRF (state-changing) AND is in the
        # general bucket (so the auth-bucket isn't burned first).
        post_path = "/api/signals/00000000-0000-0000-0000-000000000000/defer"
        body = {"diary_text": "x" * 20}
        for i in range(100):
            response = await rl_client.post(post_path, json=body)
            assert response.status_code == 403, (
                f"request {i + 1}: expected 403 from CSRFMiddleware, got {response.status_code}"
            )
        response = await rl_client.post(post_path, json=body)
        assert response.status_code == 429
        assert response.json()["error_code"] == "RATE_LIMITED"


# ---------------------------------------------------------------------------
# 7. Retry-After header math
# ---------------------------------------------------------------------------


class TestRetryAfter:
    @pytest.mark.asyncio
    async def test_retry_after_decreases_within_window(
        self,
        rl_client: AsyncClient,
        deterministic_clock: list[float],
    ) -> None:
        """Within the same window, Retry-After should shrink as time passes
        (fewer seconds left until the window resets). Always >= 1.
        """
        for _ in range(100):
            await rl_client.get(_GENERAL_PROBE)
        blocked_early = await rl_client.get(_GENERAL_PROBE)
        assert blocked_early.status_code == 429
        retry_early = int(blocked_early.headers["Retry-After"])

        # Advance 7s within the 10s window.
        deterministic_clock[0] += 7.0

        blocked_late = await rl_client.get(_GENERAL_PROBE)
        assert blocked_late.status_code == 429
        retry_late = int(blocked_late.headers["Retry-After"])

        assert retry_late < retry_early
        assert retry_late >= 1
