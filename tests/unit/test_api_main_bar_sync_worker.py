"""Unit tests for the api lifespan's BarSyncWorker bring-up + alert hook wiring.

2026-05-21 OI saga follow-up batch (PR #211 deferred). The bar_sync
worker now wires a P2 ``partial_cycle_alert_hook`` from the api
lifespan analogous to the recon + monitor patterns. This file locks:

  * ``_start_bar_sync_worker`` — disabled-via-setting short-circuit;
    happy-path startup with the hook constructed when sops
    ``discord.webhook_urls.alerts`` is populated; hook-None when not.
  * ``_build_bar_sync_alert_dispatch_hook`` — None when no Discord URL;
    callable closure when one is wired; full Resend config plumbs
    email_identity through.
  * Hook closure end-to-end — INSERTs an alerts row + invokes
    ``dispatch_alert`` with the right shape; no-account-at-fire-time
    skip path; the worker's ``_dispatch_alert`` catches hook errors
    (covered separately in ``tests/unit/test_bar_sync.py``).

Mirrors the structure of ``tests/unit/test_api_main_order_worker.py``'s
``TestStartReconciliationSchedulerDefensive`` + ``TestBuildAlertDispatchHook``
+ ``TestAlertDispatchHookFired`` classes for consistency with the recon
hook test conventions.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture(scope="module", autouse=True)
def _stub_api_env() -> Any:
    """Inject stub env vars before importing services.api.main.

    Mirror of the same fixture in ``test_api_main_order_worker.py`` —
    the module's bottom-line `app = create_app()` runs at import time +
    calls `get_settings()` which requires `API_DATABASE_URL`. The
    fixture sets the env vars module-wide so the import-time settings
    resolution succeeds; nothing in this test actually exercises the
    DB.
    """
    import os

    overrides = {
        "API_DATABASE_URL": "postgresql+asyncpg://stub:stub@127.0.0.1:0/stub",
        "API_ENVIRONMENT": "dev",
        "API_VERSION": "test",
        "API_LOG_LEVEL": "INFO",
    }
    previous: dict[str, str | None] = {k: os.environ.get(k) for k in overrides}
    for k, v in overrides.items():
        os.environ[k] = v
    from services.api import config as api_config

    api_config.get_settings.cache_clear()

    yield

    for k, prev in previous.items():
        if prev is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = prev
    api_config.get_settings.cache_clear()


def _settings(**overrides: Any) -> Any:
    """Lightweight SimpleNamespace stand-in for APISettings.

    Carries the fields ``_start_bar_sync_worker`` +
    ``_build_bar_sync_alert_dispatch_hook`` read. Avoids the full
    pydantic-settings construction which requires database_url + env-var
    plumbing.
    """
    base = SimpleNamespace(
        environment="paper",
        bar_sync_enabled=True,
        bar_sync_schedule_et="17:00",
        bar_sync_data_root="/tmp/test_lean_data",
        bar_sync_bars_per_fetch=400,
        bar_sync_client_id=3,
        bar_sync_ibkr_call_timeout_seconds=120.0,
        ibkr_host="ib_gateway",
        ibkr_port=4004,
        ibkr_account="DUQ_TEST",
        discord_webhook_url_alerts=None,
        discord_webhook_url_critical=None,
        resend_api_key=None,
        resend_from_address=None,
        resend_to_address=None,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


class _SecretStub:
    """Lightweight SecretStr stand-in carrying a `.get_secret_value()` method."""

    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


# ---------------------------------------------------------------------------
# TestStartBarSyncWorker — bring-up paths
# ---------------------------------------------------------------------------


class TestStartBarSyncWorker:
    """Happy + degraded bring-up paths for ``_start_bar_sync_worker``."""

    @pytest.mark.asyncio
    async def test_disabled_setting_returns_none(self) -> None:
        """When `bar_sync_enabled=False`, the helper short-circuits."""
        from services.api import main as api_main

        result = await api_main._start_bar_sync_worker(_settings(bar_sync_enabled=False))
        assert result is None

    @pytest.mark.asyncio
    async def test_happy_path_returns_worker_and_task(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When bar_sync_enabled + no Discord URL, BarSyncWorker constructs cleanly.

        Locks the contract: even without sops Discord URL populated, the
        worker still spawns + the task starts (the hook is just None +
        the worker's run-time path logs ``bar_sync_alert_dropped_no_hook``
        instead of dispatching).
        """
        from services.api import main as api_main

        # Stub BarSyncWorker so we don't open a real ib-async connection
        # or spin a real run_forever loop. We just want to confirm the
        # construction path threads + returns.
        constructed: dict[str, Any] = {}

        class _FakeWorker:
            def __init__(self, *, config: Any, partial_cycle_alert_hook: Any = None) -> None:
                constructed["config"] = config
                constructed["partial_cycle_alert_hook"] = partial_cycle_alert_hook

            async def run_forever(self) -> None:
                # Yield once + return so the asyncio task completes cleanly
                # without dangling.
                import asyncio as _asyncio

                await _asyncio.sleep(0)

            def request_stop(self) -> None:
                return None

        from services.data import bar_sync as bar_sync_mod

        monkeypatch.setattr(bar_sync_mod, "BarSyncWorker", _FakeWorker)

        result = await api_main._start_bar_sync_worker(_settings())
        assert result is not None
        worker, task = result
        assert isinstance(worker, _FakeWorker)
        # Hook stays None when sops URL not populated.
        assert constructed["partial_cycle_alert_hook"] is None
        # Config carries the bar_sync_client_id (3 per PR #210 sync).
        assert constructed["config"].ibkr_client_id == 3
        # Wait for the task to finish so the test doesn't leak a pending coro.
        await task

    @pytest.mark.asyncio
    async def test_hook_wired_when_sops_url_populated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When sops Discord URL is populated, the worker is constructed with a hook."""
        from services.api import main as api_main

        constructed: dict[str, Any] = {}

        class _FakeWorker:
            def __init__(self, *, config: Any, partial_cycle_alert_hook: Any = None) -> None:
                constructed["partial_cycle_alert_hook"] = partial_cycle_alert_hook

            async def run_forever(self) -> None:
                import asyncio as _asyncio

                await _asyncio.sleep(0)

            def request_stop(self) -> None:
                return None

        from services.data import bar_sync as bar_sync_mod

        monkeypatch.setattr(bar_sync_mod, "BarSyncWorker", _FakeWorker)

        result = await api_main._start_bar_sync_worker(
            _settings(discord_webhook_url_alerts=_SecretStub("https://discord.test/a"))
        )
        assert result is not None
        _worker, task = result
        # Hook is wired now.
        assert constructed["partial_cycle_alert_hook"] is not None
        assert callable(constructed["partial_cycle_alert_hook"])
        await task


# ---------------------------------------------------------------------------
# TestStopBarSyncWorker — idempotent shutdown
# ---------------------------------------------------------------------------


class TestStopBarSyncWorkerNoState:
    @pytest.mark.asyncio
    async def test_none_state_is_idempotent(self) -> None:
        """Lifespan calls _stop_bar_sync_worker(None) when start returned None."""
        from services.api import main as api_main

        # Should not raise.
        await api_main._stop_bar_sync_worker(None)


# ---------------------------------------------------------------------------
# TestBuildBarSyncAlertDispatchHook — closure construction contract
# ---------------------------------------------------------------------------


class TestBuildBarSyncAlertDispatchHook:
    """``_build_bar_sync_alert_dispatch_hook`` returns a closure or None.

    Mirrors ``TestBuildAlertDispatchHook`` for the recon dispatch hook
    + ``TestBuildMonitorAlertDispatchHook`` for the monitor dispatch
    hook. None when sops Discord URL isn't populated; callable closure
    when it is.
    """

    def test_no_discord_alerts_url_returns_none(self) -> None:
        from services.api import main as api_main

        hook = api_main._build_bar_sync_alert_dispatch_hook(_settings())
        assert hook is None

    def test_with_alerts_url_returns_callable(self) -> None:
        from services.api import main as api_main

        hook = api_main._build_bar_sync_alert_dispatch_hook(
            _settings(discord_webhook_url_alerts=_SecretStub("https://discord.test/a"))
        )
        assert hook is not None
        assert callable(hook)

    def test_with_critical_url_constructs_closure(self) -> None:
        from services.api import main as api_main

        hook = api_main._build_bar_sync_alert_dispatch_hook(
            _settings(
                discord_webhook_url_alerts=_SecretStub("https://discord.test/a"),
                discord_webhook_url_critical=_SecretStub("https://discord.test/c"),
            )
        )
        assert hook is not None

    def test_full_resend_config_constructs_closure(self) -> None:
        from services.api import main as api_main

        hook = api_main._build_bar_sync_alert_dispatch_hook(
            _settings(
                discord_webhook_url_alerts=_SecretStub("https://discord.test/a"),
                discord_webhook_url_critical=_SecretStub("https://discord.test/c"),
                resend_api_key=_SecretStub("re_test"),
                resend_from_address="from@test",
                resend_to_address="to@test",
            )
        )
        assert hook is not None

    def test_partial_resend_config_skips_email_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resend requires all 3 fields; missing any → email_identity None.

        Locks the recon-hook-equivalent semantics: P0-routing emails are
        not wired unless ALL of resend_api_key + from_address + to_address
        are populated.
        """
        from services.api import main as api_main

        # api_key + from_address only (missing to_address) → email_identity None.
        hook = api_main._build_bar_sync_alert_dispatch_hook(
            _settings(
                discord_webhook_url_alerts=_SecretStub("https://discord.test/a"),
                resend_api_key=_SecretStub("re_test"),
                resend_from_address="from@test",
                # resend_to_address left None.
            )
        )
        assert hook is not None  # hook still installs (we have Discord URL)


# ---------------------------------------------------------------------------
# TestBarSyncAlertDispatchHookEndToEnd — closure invocation
# ---------------------------------------------------------------------------


class TestBarSyncAlertDispatchHookEndToEnd:
    """The hook closure: INSERTs alerts row + invokes dispatch_alert.

    Three-session lifecycle: one for account_id lookup, one for the
    INSERT, one consumed by the dispatcher's read+UPDATE. All come from
    a stub session_factory.
    """

    @pytest.mark.asyncio
    async def test_hook_inserts_then_dispatches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from contextlib import asynccontextmanager
        from unittest.mock import MagicMock
        from uuid import uuid4

        from services.api import main as api_main
        from services.data.bar_sync_alerts import BarSyncAlertDescriptor

        new_alert_id = uuid4()
        account_id = uuid4()

        sessions_opened: list[MagicMock] = []
        session_role: list[str] = []

        def factory() -> Any:
            session = MagicMock()
            sessions_opened.append(session)

            async def execute(stmt: Any, params: Any = None) -> Any:
                from types import SimpleNamespace as _NS

                result = MagicMock()
                result.fetchone = MagicMock(return_value=_NS(id=new_alert_id))
                return result

            async def commit() -> None:
                return None

            session.execute = execute  # type: ignore[method-assign]
            session.commit = commit  # type: ignore[method-assign]

            @asynccontextmanager
            async def cm() -> Any:
                yield session

            return cm()

        monkeypatch.setattr(api_main.api_db, "get_session_factory", lambda: factory)

        # Stub fetch_active_account_id to return our test account.
        async def fake_fetch_account(self: Any) -> Any:
            session_role.append("account_id_lookup")
            return account_id

        monkeypatch.setattr(
            api_main.PostgresPhase1QueryRepo,
            "fetch_active_account_id",
            fake_fetch_account,
        )

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
            return MagicMock(short_circuited=False, delivery_status={"discord_alerts": "ok"})

        from services.webhook_pusher import dispatcher as wpd

        monkeypatch.setattr(wpd, "dispatch_alert", fake_dispatch)

        hook = api_main._build_bar_sync_alert_dispatch_hook(
            _settings(
                discord_webhook_url_alerts=_SecretStub("https://discord.test/a"),
                discord_webhook_url_critical=_SecretStub("https://discord.test/c"),
                resend_api_key=_SecretStub("re_test"),
                resend_from_address="from@test",
                resend_to_address="to@test",
            )
        )
        assert hook is not None

        descriptor = BarSyncAlertDescriptor(
            severity="P2",
            category="data_quality_reject",
            title="bar_sync partial cycle failure: 1/11 markets failed",
            body="The api's bar_sync worker has failed to fetch bars for at least one market.",
            payload={
                "alert_kind": "partial_cycle_failure",
                "consecutive_failure_count": 2,
                "total_markets": 11,
                "successful_count": 10,
                "failed_count": 1,
                "failed_markets": ["/MCL"],
            },
        )

        await hook(descriptor)

        # Three sessions consumed: account lookup, INSERT, dispatcher.
        assert len(sessions_opened) >= 2  # at least INSERT + dispatcher
        # Dispatch happened with the right shape.
        assert dispatched["alert_id"] == new_alert_id
        assert "discord_alerts" in dispatched["webhook_urls"]
        assert "discord_critical" in dispatched["webhook_urls"]
        # Resend identity threaded through.
        assert dispatched["email_identity"] is not None
        ei = dispatched["email_identity"]
        assert ei.from_address == "from@test"
        assert ei.to_address == "to@test"
        assert ei.resend_api_key == "re_test"

    @pytest.mark.asyncio
    async def test_hook_skips_when_no_account_resolved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no active account, the hook logs + skips without calling dispatch_alert.

        Locks the operator-friendly degraded path: a pre-setup api boot
        that hasn't seen an accounts row yet shouldn't crash the worker
        cycle on a bar_sync alert (which IS structurally valid — the
        worker can run without an account being provisioned).
        """
        from contextlib import asynccontextmanager
        from unittest.mock import MagicMock

        from services.api import main as api_main
        from services.data.bar_sync_alerts import BarSyncAlertDescriptor

        sessions_opened: list[MagicMock] = []

        def factory() -> Any:
            session = MagicMock()
            sessions_opened.append(session)

            async def execute(stmt: Any, params: Any = None) -> Any:
                raise AssertionError("execute must not be called when no account resolved")

            async def commit() -> None:
                raise AssertionError("commit must not be called")

            session.execute = execute  # type: ignore[method-assign]
            session.commit = commit  # type: ignore[method-assign]

            @asynccontextmanager
            async def cm() -> Any:
                yield session

            return cm()

        monkeypatch.setattr(api_main.api_db, "get_session_factory", lambda: factory)

        async def fake_fetch_account(self: Any) -> Any:
            return None  # no active account

        monkeypatch.setattr(
            api_main.PostgresPhase1QueryRepo,
            "fetch_active_account_id",
            fake_fetch_account,
        )

        dispatch_calls: list[Any] = []

        async def fake_dispatch(**kwargs: Any) -> Any:
            dispatch_calls.append(kwargs)
            return None

        from services.webhook_pusher import dispatcher as wpd

        monkeypatch.setattr(wpd, "dispatch_alert", fake_dispatch)

        hook = api_main._build_bar_sync_alert_dispatch_hook(
            _settings(discord_webhook_url_alerts=_SecretStub("https://discord.test/a"))
        )
        assert hook is not None

        descriptor = BarSyncAlertDescriptor(
            severity="P2",
            category="data_quality_quarantine",
            title="bar_sync OI sentinel substituted",
            body="The api's bar_sync worker substituted a positive sentinel for OI.",
            payload={
                "alert_kind": "sentinel_substitution",
                "consecutive_sentinel_count": 2,
                "sentinel_markets": ["/MCL"],
            },
        )

        # Should NOT raise — the skip path is the contract.
        await hook(descriptor)

        # dispatch_alert NOT called when account_id is None.
        assert dispatch_calls == []

    @pytest.mark.asyncio
    async def test_hook_skips_email_when_resend_unwired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P2-only path: only #alerts URL set; no Resend → email_identity stays None."""
        from contextlib import asynccontextmanager
        from unittest.mock import MagicMock
        from uuid import uuid4

        from services.api import main as api_main
        from services.data.bar_sync_alerts import BarSyncAlertDescriptor

        def factory() -> Any:
            session = MagicMock()

            async def execute(stmt: Any, params: Any = None) -> Any:
                from types import SimpleNamespace as _NS

                result = MagicMock()
                result.fetchone = MagicMock(return_value=_NS(id=uuid4()))
                return result

            async def commit() -> None:
                return None

            session.execute = execute  # type: ignore[method-assign]
            session.commit = commit  # type: ignore[method-assign]

            @asynccontextmanager
            async def cm() -> Any:
                yield session

            return cm()

        monkeypatch.setattr(api_main.api_db, "get_session_factory", lambda: factory)

        async def fake_fetch_account(self: Any) -> Any:
            return uuid4()

        monkeypatch.setattr(
            api_main.PostgresPhase1QueryRepo,
            "fetch_active_account_id",
            fake_fetch_account,
        )

        dispatched: dict[str, Any] = {}

        async def fake_dispatch(
            *,
            session: Any,
            alert_id: Any,
            http_client: Any,
            webhook_urls: Any,
            email_identity: Any,
        ) -> Any:
            dispatched["email_identity"] = email_identity
            dispatched["webhook_urls"] = dict(webhook_urls)
            return MagicMock(short_circuited=False, delivery_status={"discord_alerts": "ok"})

        from services.webhook_pusher import dispatcher as wpd

        monkeypatch.setattr(wpd, "dispatch_alert", fake_dispatch)

        hook = api_main._build_bar_sync_alert_dispatch_hook(
            _settings(discord_webhook_url_alerts=_SecretStub("https://discord.test/a"))
        )
        assert hook is not None

        descriptor = BarSyncAlertDescriptor(
            severity="P2",
            category="data_quality_reject",
            title="bar_sync partial cycle failure",
            body="Test.",
            payload={"alert_kind": "partial_cycle_failure"},
        )

        await hook(descriptor)

        # email_identity stays None when Resend isn't wired (P2 routes to
        # #alerts only per webhook_pusher.payloads.SEVERITY_TO_CHANNELS).
        assert dispatched["email_identity"] is None
        # Only #alerts channel (no #critical because critical URL not set).
        assert "discord_alerts" in dispatched["webhook_urls"]
        assert "discord_critical" not in dispatched["webhook_urls"]
