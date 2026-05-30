"""Unit tests for the nightly-kill-switch fail-open escalation in the LEAN
heartbeat handler (``services/api/routes/internal/lean.py``).

PR-C (#302, parameter-sets-bootstrap-design §12.4) made the nightly LEAN cycle
fail OPEN when it can't read ``STRATEGY_DECOMMISSIONED`` from
``GET /api/internal/lean/parameters``; it tags the ``lean_cycle_heartbeat`` it
POSTs with ``error="parameters_fetch_failed"``. This follow-up escalates that
tag to a **P1 ``#alerts``** row + Discord push — the *compensating control* that
makes a silent api outage during an intended-decommission window noticeable
(safe only while every signal is operator-gated; see design §12.4 forward-guard).

These tests lock:

  * heartbeat carrying the tag → INSERT a ``P1`` / ``incident_review_required``
    alert row + ``dispatch_alert`` invoked with the ``#alerts`` channel.
  * heartbeat WITHOUT the tag (None or a different ``error``) → no INSERT, no
    dispatch (the gate holds).
  * **best-effort**: a ``dispatch_alert`` failure does NOT propagate (the
    heartbeat must never 5xx) — and the alert row still landed (durable).
  * no active account provisioned → skip (no INSERT, no dispatch).
  * no ``discord.webhook_urls.alerts`` in sops → skip (no INSERT, no dispatch).
  * **wiring + scoping**: ``post_lean_signal`` invokes the helper on a
    ``lean_cycle_heartbeat`` but NOT on a ``lean_strategy_initialized`` event.

The helper is exercised directly (the same way ``test_api_main_bar_sync_worker``
exercises the bar_sync hook closure) with the session factory + account lookup +
``dispatch_alert`` monkeypatched — no live DB, no live Discord. The consumer side
(``lean/v1_strategy.py``'s fetch + fail-open ``error`` tag) is covered by the
LEAN↔api smoke checklist (A27) + the risk-review pass, not unit-testable here.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from services.api.routes.internal import lean as lean_route
from services.api.schemas.lean import LeanEventRequest

_RECEIVED_AT = datetime(2026, 5, 30, 21, 30, 5, tzinfo=UTC)


def _heartbeat(*, error: str | None) -> LeanEventRequest:
    return LeanEventRequest(
        event_type="lean_cycle_heartbeat",
        ts_utc=datetime(2026, 5, 30, 21, 30, tzinfo=UTC),
        algorithm_id="v1_trend_following",
        session_date_et="2026-05-30",
        equity_usd=None,
        live_mode=True,
        signals_emitted_count=0,
        rejections_count=0,
        error=error,
    )


class _SecretStub:
    """SecretStr stand-in carrying ``.get_secret_value()``."""

    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


def _settings(**overrides: Any) -> Any:
    base = SimpleNamespace(
        discord_webhook_url_alerts=_SecretStub("https://discord.test/alerts"),
        discord_webhook_url_critical=None,
        resend_api_key=None,
        resend_from_address=None,
        resend_to_address=None,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _install_stub_session_factory(
    monkeypatch: pytest.MonkeyPatch, *, captured_inserts: list[dict[str, Any]]
) -> Any:
    """Monkeypatch ``lean_route.api_db.get_session_factory`` → MagicMock sessions.

    The INSERT's ``execute()`` records its bind params (those carrying ``sev``)
    and returns a fake RETURNING-id row. Returns the minted alert id so callers
    can assert it round-trips to ``dispatch_alert``.
    """
    new_id = uuid4()

    def factory() -> Any:
        session = MagicMock()

        async def execute(stmt: Any, params: Any = None) -> Any:
            if params is not None and "sev" in params:
                captured_inserts.append(params)
            result = MagicMock()
            result.fetchone = MagicMock(return_value=SimpleNamespace(id=new_id))
            return result

        async def commit() -> None:
            return None

        session.execute = execute  # type: ignore[method-assign]
        session.commit = commit  # type: ignore[method-assign]

        @asynccontextmanager
        async def cm() -> Any:
            yield session

        return cm()

    monkeypatch.setattr(lean_route.api_db, "get_session_factory", lambda: factory)
    return new_id


def _stub_account(monkeypatch: pytest.MonkeyPatch, *, account_id: Any) -> None:
    async def fake_fetch(self: Any) -> Any:
        return account_id

    monkeypatch.setattr(lean_route.PostgresPhase1QueryRepo, "fetch_active_account_id", fake_fetch)


def _patch_dispatch(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    from services.webhook_pusher import dispatcher as wpd

    monkeypatch.setattr(wpd, "dispatch_alert", fake)


# ---------------------------------------------------------------------------
# Happy path — INSERT + dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_param_fetch_error_inserts_p1_alert_and_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lean_route, "get_settings", lambda: _settings())
    captured: list[dict[str, Any]] = []
    new_id = _install_stub_session_factory(monkeypatch, captured_inserts=captured)
    _stub_account(monkeypatch, account_id=uuid4())

    dispatched: dict[str, Any] = {}

    async def fake_dispatch(
        *,
        session: Any,
        alert_id: Any,
        http_client: Any,
        webhook_urls: Any,
        email_identity: Any,
    ) -> Any:
        dispatched["alert_id"] = alert_id
        dispatched["webhook_urls"] = dict(webhook_urls)
        dispatched["email_identity"] = email_identity
        return SimpleNamespace(short_circuited=False, delivery_status={"discord_alerts": "ok"})

    _patch_dispatch(monkeypatch, fake_dispatch)

    await lean_route._maybe_alert_parameters_fetch_failed(
        _heartbeat(error="parameters_fetch_failed"),
        received_at=_RECEIVED_AT,
        log_kwargs={"event_type": "lean_cycle_heartbeat"},
    )

    # Exactly one alerts INSERT, with the locked severity + category.
    assert len(captured) == 1
    assert captured[0]["sev"] == "P1"
    assert captured[0]["cat"] == "incident_review_required"
    assert "fail-opened" in captured[0]["msg"]
    import json as _json

    detail = _json.loads(captured[0]["detail"])
    assert detail["alert_kind"] == "lean_parameters_fetch_failed"
    assert detail["error_tag"] == "parameters_fetch_failed"

    # Dispatched to #alerts with the INSERT's id; P1 → no #critical, no email.
    assert dispatched["alert_id"] == new_id
    assert "discord_alerts" in dispatched["webhook_urls"]
    assert "discord_critical" not in dispatched["webhook_urls"]
    assert dispatched["email_identity"] is None


# ---------------------------------------------------------------------------
# Gate — no alert for missing / other error tags
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_error_tag_does_not_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lean_route, "get_settings", lambda: _settings())
    captured: list[dict[str, Any]] = []
    _install_stub_session_factory(monkeypatch, captured_inserts=captured)
    _stub_account(monkeypatch, account_id=uuid4())

    calls: list[Any] = []

    async def fake_dispatch(**kwargs: Any) -> Any:
        calls.append(kwargs)

    _patch_dispatch(monkeypatch, fake_dispatch)

    # Clean heartbeat (no error) AND a different error tag must both no-op.
    await lean_route._maybe_alert_parameters_fetch_failed(
        _heartbeat(error=None), received_at=_RECEIVED_AT, log_kwargs={}
    )
    await lean_route._maybe_alert_parameters_fetch_failed(
        _heartbeat(error="generate_signals_failed"),
        received_at=_RECEIVED_AT,
        log_kwargs={},
    )

    assert captured == []
    assert calls == []


# ---------------------------------------------------------------------------
# Best-effort — a dispatch failure must not propagate (no 5xx)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_best_effort_swallows_dispatch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lean_route, "get_settings", lambda: _settings())
    captured: list[dict[str, Any]] = []
    _install_stub_session_factory(monkeypatch, captured_inserts=captured)
    _stub_account(monkeypatch, account_id=uuid4())

    async def boom(**kwargs: Any) -> Any:
        raise RuntimeError("discord 5xx")

    _patch_dispatch(monkeypatch, boom)

    # Must NOT raise — best-effort contract.
    await lean_route._maybe_alert_parameters_fetch_failed(
        _heartbeat(error="parameters_fetch_failed"),
        received_at=_RECEIVED_AT,
        log_kwargs={},
    )

    # The alert row still landed (durable) even though the Discord push raised.
    assert len(captured) == 1


# ---------------------------------------------------------------------------
# Degraded-config skips (still no raise)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skips_when_no_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lean_route, "get_settings", lambda: _settings())
    captured: list[dict[str, Any]] = []
    _install_stub_session_factory(monkeypatch, captured_inserts=captured)
    _stub_account(monkeypatch, account_id=None)

    calls: list[Any] = []

    async def fake_dispatch(**kwargs: Any) -> Any:
        calls.append(kwargs)

    _patch_dispatch(monkeypatch, fake_dispatch)

    await lean_route._maybe_alert_parameters_fetch_failed(
        _heartbeat(error="parameters_fetch_failed"),
        received_at=_RECEIVED_AT,
        log_kwargs={},
    )

    # No account → no INSERT (would violate the NOT NULL FK) + no dispatch.
    assert captured == []
    assert calls == []


@pytest.mark.asyncio
async def test_skips_when_no_webhook_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        lean_route, "get_settings", lambda: _settings(discord_webhook_url_alerts=None)
    )
    captured: list[dict[str, Any]] = []
    _install_stub_session_factory(monkeypatch, captured_inserts=captured)

    calls: list[Any] = []

    async def fake_dispatch(**kwargs: Any) -> Any:
        calls.append(kwargs)

    _patch_dispatch(monkeypatch, fake_dispatch)

    await lean_route._maybe_alert_parameters_fetch_failed(
        _heartbeat(error="parameters_fetch_failed"),
        received_at=_RECEIVED_AT,
        log_kwargs={},
    )

    # No #alerts webhook in sops → skip before any INSERT/dispatch.
    assert captured == []
    assert calls == []


# ---------------------------------------------------------------------------
# Wiring + scoping — the route handler invokes the helper on heartbeats only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_invokes_helper_for_heartbeat_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``post_lean_signal`` calls the helper on a ``lean_cycle_heartbeat`` but
    NOT on a ``lean_strategy_initialized`` event (call is scoped to the
    heartbeat-only branch). Exercised by calling the handler directly with a
    stub Request — heartbeats with no ``market_evaluations`` touch no DB."""
    invoked: list[str] = []

    async def spy(body: LeanEventRequest, *, received_at: Any, log_kwargs: Any) -> None:
        invoked.append(body.event_type)

    monkeypatch.setattr(lean_route, "_maybe_alert_parameters_fetch_failed", spy)

    heartbeat = _heartbeat(error="parameters_fetch_failed")
    resp = await lean_route.post_lean_signal(heartbeat, MagicMock(), None)
    assert resp.event_type == "lean_cycle_heartbeat"
    assert invoked == ["lean_cycle_heartbeat"]

    init_event = LeanEventRequest(
        event_type="lean_strategy_initialized",
        ts_utc=datetime(2026, 5, 30, 21, 10, tzinfo=UTC),
        algorithm_id="v1_trend_following",
    )
    await lean_route.post_lean_signal(init_event, MagicMock(), None)
    # Unchanged — the init event did NOT invoke the helper.
    assert invoked == ["lean_cycle_heartbeat"]
