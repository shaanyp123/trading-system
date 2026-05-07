"""Unit tests for `watchdog/watchdog.py`.

Coverage targets per backend-spec §1.6 + §2.12:

- consecutive failures counter advances 0→1→2→3 then triggers alerts
- success at any point resets the counter to 0
- alert cooldown suppresses repeat alerts for ALERT_COOLDOWN_MINUTES
- state file persists atomically across runs
- Resend payload formatting (auth header, JSON body)
- Discord payload formatting (no auth, content body)
- env-var fail-closed on missing required vars
- placeholder Resend API key disables email path (graceful)
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# The watchdog module lives at <repo_root>/watchdog/watchdog.py, which is not on
# sys.path by default (it's not part of the `services/` or `infrastructure/`
# packages). Tests load it explicitly.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_WATCHDOG_DIR = str(_REPO_ROOT / "watchdog")
if _WATCHDOG_DIR not in sys.path:
    sys.path.insert(0, _WATCHDOG_DIR)

import watchdog as wd  # noqa: E402  (deliberate post-sys.path-edit import)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _config(tmp_path: Path, **overrides: Any) -> wd.WatchdogConfig:
    defaults = {
        "health_url": "https://paper.example.test/api/health",
        "operator_email": "operator@example.test",
        "resend_api_key": "re_test_xxx",
        "resend_from_address": "noreply@example.test",
        "discord_webhook_url": "https://discord.com/api/webhooks/123/abc",
        "watchdog_id": "test-watchdog-1",
        "state_path": tmp_path / "state.json",
    }
    defaults.update(overrides)
    return wd.WatchdogConfig(**defaults)


def _fail_check(_: wd.WatchdogConfig) -> wd.CheckResult:
    return wd.CheckResult(success=False, status_code=503, error_message="non-2xx status 503")


def _ok_check(_: wd.WatchdogConfig) -> wd.CheckResult:
    return wd.CheckResult(success=True, status_code=200, error_message=None)


def _silent_email(**_: Any) -> tuple[bool, str | None]:
    return True, None


def _silent_discord(**_: Any) -> tuple[bool, str | None]:
    return True, None


# ---------------------------------------------------------------------------
# decide_alerts
# ---------------------------------------------------------------------------
class TestDecideAlerts:
    def test_check_ok_no_alert(self, tmp_path: Path) -> None:
        """A successful check is never alert-worthy."""
        cfg = _config(tmp_path)
        decision = wd.decide_alerts(
            state=wd.WatchdogState(consecutive_failures=2),
            config=cfg,
            check=wd.CheckResult(success=True, status_code=200, error_message=None),
            now=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
        )
        assert decision.send_email is False
        assert decision.send_discord is False
        assert "ok" in decision.reason

    def test_under_threshold_no_alert(self, tmp_path: Path) -> None:
        """Failures 1 and 2 (out of 3) should not alert."""
        cfg = _config(tmp_path)
        for prior in (0, 1):  # pending=1 then pending=2
            decision = wd.decide_alerts(
                state=wd.WatchdogState(consecutive_failures=prior),
                config=cfg,
                check=wd.CheckResult(success=False, status_code=503, error_message="x"),
                now=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
            )
            assert decision.send_email is False
            assert decision.send_discord is False
            assert "under threshold" in decision.reason

    def test_threshold_reached_alerts_both(self, tmp_path: Path) -> None:
        """3rd consecutive failure triggers both Resend + Discord."""
        cfg = _config(tmp_path)
        decision = wd.decide_alerts(
            state=wd.WatchdogState(consecutive_failures=2),  # pending = 3
            config=cfg,
            check=wd.CheckResult(success=False, status_code=503, error_message="x"),
            now=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
        )
        assert decision.send_email is True
        assert decision.send_discord is True
        assert "threshold reached" in decision.reason

    def test_cooldown_suppresses_repeat_alert(self, tmp_path: Path) -> None:
        """Alert sent 30 min ago + still failing → cooldown active, no alert."""
        cfg = _config(tmp_path)
        last_alert = datetime(2026, 5, 6, 11, 30, tzinfo=UTC).isoformat()
        decision = wd.decide_alerts(
            state=wd.WatchdogState(
                consecutive_failures=10,  # well past threshold
                last_alert_sent_at_utc=last_alert,
            ),
            config=cfg,
            check=wd.CheckResult(success=False, status_code=503, error_message="x"),
            now=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
        )
        assert decision.send_email is False
        assert decision.send_discord is False
        assert decision.cooldown_active is True
        assert "cooldown" in decision.reason

    def test_cooldown_expired_alerts_again(self, tmp_path: Path) -> None:
        """Alert sent 65 min ago (past 60-min cooldown) → alert fires again."""
        cfg = _config(tmp_path)
        last_alert = datetime(2026, 5, 6, 10, 55, tzinfo=UTC).isoformat()
        decision = wd.decide_alerts(
            state=wd.WatchdogState(
                consecutive_failures=10,
                last_alert_sent_at_utc=last_alert,
            ),
            config=cfg,
            check=wd.CheckResult(success=False, status_code=503, error_message="x"),
            now=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
        )
        assert decision.send_email is True
        assert decision.send_discord is True
        assert decision.cooldown_active is False

    def test_placeholder_resend_disables_email_only(self, tmp_path: Path) -> None:
        """Resend key is a placeholder → email suppressed, Discord still fires."""
        cfg = _config(tmp_path, resend_api_key="<TODO_FROM_DAY_3_RESEND_PROVISION>")
        decision = wd.decide_alerts(
            state=wd.WatchdogState(consecutive_failures=2),
            config=cfg,
            check=wd.CheckResult(success=False, status_code=503, error_message="x"),
            now=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
        )
        assert decision.send_email is False
        assert decision.send_discord is True


# ---------------------------------------------------------------------------
# run_once — full state-machine over a sequence of ticks
# ---------------------------------------------------------------------------
class TestRunOnceStateMachine:
    def test_three_failures_then_alert_then_recovery(self, tmp_path: Path) -> None:
        """
        Simulate: fail, fail, fail (alert!), recover (counter resets).
        Verifies the canonical 3-failure threshold flow end-to-end.
        """
        cfg = _config(tmp_path)
        state = wd.WatchdogState()
        email_calls: list[dict[str, Any]] = []
        discord_calls: list[dict[str, Any]] = []

        def email_fn(**kwargs: Any) -> tuple[bool, str | None]:
            email_calls.append(kwargs)
            return True, None

        def discord_fn(**kwargs: Any) -> tuple[bool, str | None]:
            discord_calls.append(kwargs)
            return True, None

        # Tick 1: failure 1/3 → no alert
        wd.run_once(
            config=cfg,
            state=state,
            now=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
            check_fn=_fail_check,
            email_fn=email_fn,
            discord_fn=discord_fn,
        )
        assert state.consecutive_failures == 1
        assert email_calls == []

        # Tick 2: failure 2/3 → no alert
        wd.run_once(
            config=cfg,
            state=state,
            now=datetime(2026, 5, 6, 12, 5, tzinfo=UTC),
            check_fn=_fail_check,
            email_fn=email_fn,
            discord_fn=discord_fn,
        )
        assert state.consecutive_failures == 2
        assert email_calls == []

        # Tick 3: failure 3/3 → alerts fire
        wd.run_once(
            config=cfg,
            state=state,
            now=datetime(2026, 5, 6, 12, 10, tzinfo=UTC),
            check_fn=_fail_check,
            email_fn=email_fn,
            discord_fn=discord_fn,
        )
        assert state.consecutive_failures == 3
        assert len(email_calls) == 1
        assert len(discord_calls) == 1
        assert state.last_alert_sent_at_utc is not None

        # Tick 4: recovery → counter resets, no new alert
        wd.run_once(
            config=cfg,
            state=state,
            now=datetime(2026, 5, 6, 12, 15, tzinfo=UTC),
            check_fn=_ok_check,
            email_fn=email_fn,
            discord_fn=discord_fn,
        )
        assert state.consecutive_failures == 0
        assert len(email_calls) == 1  # unchanged

    def test_repeated_failures_in_cooldown_no_repeat_alert(self, tmp_path: Path) -> None:
        """While cooldown is active, alerts don't re-fire even on more failures."""
        cfg = _config(tmp_path)
        state = wd.WatchdogState(
            consecutive_failures=5,
            last_alert_sent_at_utc=datetime(2026, 5, 6, 11, 30, tzinfo=UTC).isoformat(),
        )
        email_calls: list[dict[str, Any]] = []

        def email_fn(**kwargs: Any) -> tuple[bool, str | None]:
            email_calls.append(kwargs)
            return True, None

        wd.run_once(
            config=cfg,
            state=state,
            now=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),  # 30 min after last alert
            check_fn=_fail_check,
            email_fn=email_fn,
            discord_fn=_silent_discord,
        )
        assert state.consecutive_failures == 6
        assert email_calls == []  # cooldown suppresses

    def test_alert_re_fires_after_cooldown_expires(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        state = wd.WatchdogState(
            consecutive_failures=5,
            last_alert_sent_at_utc=datetime(2026, 5, 6, 10, 50, tzinfo=UTC).isoformat(),
        )
        email_calls: list[dict[str, Any]] = []

        def email_fn(**kwargs: Any) -> tuple[bool, str | None]:
            email_calls.append(kwargs)
            return True, None

        # 12:00 - 10:50 = 70 min, past the 60-min cooldown
        wd.run_once(
            config=cfg,
            state=state,
            now=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
            check_fn=_fail_check,
            email_fn=email_fn,
            discord_fn=_silent_discord,
        )
        assert len(email_calls) == 1


# ---------------------------------------------------------------------------
# State file persistence
# ---------------------------------------------------------------------------
class TestStateFile:
    def test_save_then_load_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        original = wd.WatchdogState(
            consecutive_failures=2,
            last_check_at_utc="2026-05-06T12:00:00+00:00",
            last_status_code=503,
            last_error_message="non-2xx status 503",
            last_alert_sent_at_utc="2026-05-06T11:30:00+00:00",
        )
        wd.save_state(path, original)
        reloaded = wd.load_state(path)
        assert reloaded.consecutive_failures == 2
        assert reloaded.last_status_code == 503
        assert reloaded.last_alert_sent_at_utc == "2026-05-06T11:30:00+00:00"

    def test_missing_file_returns_fresh_state(self, tmp_path: Path) -> None:
        state = wd.load_state(tmp_path / "does-not-exist.json")
        assert state.consecutive_failures == 0
        assert state.last_check_at_utc is None

    def test_corrupt_file_returns_fresh_state(self, tmp_path: Path) -> None:
        """A corrupt state file shouldn't crash the watchdog. We'd rather lose
        the counter than block the next check."""
        path = tmp_path / "state.json"
        path.write_text("this is not json {{{", encoding="utf-8")
        state = wd.load_state(path)
        assert state.consecutive_failures == 0

    def test_atomic_write_creates_parent_dir(self, tmp_path: Path) -> None:
        """save_state should create the parent dir if missing (mirrors what
        the systemd StateDirectory= setting also does)."""
        path = tmp_path / "nested" / "state.json"
        wd.save_state(path, wd.WatchdogState(consecutive_failures=1))
        assert path.exists()

    def test_unknown_keys_in_state_file_are_ignored(self, tmp_path: Path) -> None:
        """Forward-compat: an older watchdog version writing a key we don't
        know about should still load."""
        path = tmp_path / "state.json"
        path.write_text(
            json.dumps({"consecutive_failures": 1, "future_field": "abc"}),
            encoding="utf-8",
        )
        state = wd.load_state(path)
        assert state.consecutive_failures == 1


# ---------------------------------------------------------------------------
# HTTP payload shape
# ---------------------------------------------------------------------------
class TestResendPayload:
    def test_auth_header_and_payload_shape(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)
        captured: dict[str, Any] = {}

        def fake_post(
            url: str, payload: dict[str, Any], headers: dict[str, str]
        ) -> tuple[int, str]:
            captured["url"] = url
            captured["payload"] = payload
            captured["headers"] = headers
            return 200, "{}"

        ok, err = wd.send_resend_email(
            config=cfg, subject="subj", body_text="body", poster=fake_post
        )
        assert ok is True
        assert err is None
        assert captured["url"] == "https://api.resend.com/emails"
        assert captured["headers"] == {"Authorization": "Bearer re_test_xxx"}
        assert captured["payload"] == {
            "from": "noreply@example.test",
            "to": ["operator@example.test"],
            "subject": "subj",
            "text": "body",
        }

    def test_resend_500_returns_error(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path)

        def fake_post(*_: Any, **__: Any) -> tuple[int, str]:
            return 500, "internal error"

        ok, err = wd.send_resend_email(config=cfg, subject="s", body_text="b", poster=fake_post)
        assert ok is False
        assert err is not None
        assert "500" in err

    def test_placeholder_api_key_returns_error_without_calling(self, tmp_path: Path) -> None:
        cfg = _config(tmp_path, resend_api_key="<TODO_FROM_DAY_3_RESEND_PROVISION>")
        sentinel = MagicMock()
        ok, err = wd.send_resend_email(config=cfg, subject="s", body_text="b", poster=sentinel)
        assert ok is False
        assert err is not None
        assert "placeholder" in err
        sentinel.assert_not_called()


class TestDiscordPayload:
    def test_no_auth_header_content_body(self, tmp_path: Path) -> None:
        """send_discord_webhook adds NO custom headers above the _post_json layer
        (the User-Agent + Content-Type are added inside _post_json, see
        TestPostJsonHttpHeaders below)."""
        cfg = _config(tmp_path)
        captured: dict[str, Any] = {}

        def fake_post(
            url: str, payload: dict[str, Any], headers: dict[str, str]
        ) -> tuple[int, str]:
            captured["url"] = url
            captured["payload"] = payload
            captured["headers"] = headers
            return 204, ""

        ok, err = wd.send_discord_webhook(config=cfg, content="alert body", poster=fake_post)
        assert ok is True
        assert err is None
        assert captured["url"] == "https://discord.com/api/webhooks/123/abc"
        assert captured["headers"] == {}
        assert captured["payload"] == {"content": "alert body"}


class TestPostJsonHttpHeaders:
    """Verify _post_json sets the headers Discord/Resend require.

    Pins the regression-fix invariant for the 2026-05-07 Discord 403 bug:
    requests without an explicit User-Agent default to `Python-urllib/3.x`,
    which Discord rejects with 403 Forbidden. _post_json must always send
    a non-default User-Agent.
    """

    def _capture_post(self, status_code: int = 204):
        """Patch urllib.request.urlopen and capture the Request object passed to it."""
        captured: dict[str, Any] = {}

        class _FakeResponse:
            def __init__(self) -> None:
                self.status = status_code

            def __enter__(self) -> _FakeResponse:
                return self

            def __exit__(self, *_: Any) -> None:
                pass

            def getcode(self) -> int:
                return status_code

            def read(self) -> bytes:
                return b""

        def fake_urlopen(req: Any, timeout: int = 10) -> _FakeResponse:
            captured["request"] = req
            return _FakeResponse()

        return captured, fake_urlopen

    def test_post_json_sets_user_agent(self, tmp_path: Path) -> None:
        """_post_json includes a non-default User-Agent on every call."""
        captured, fake_urlopen = self._capture_post()
        with patch("watchdog.urllib.request.urlopen", side_effect=fake_urlopen):
            status, _ = wd._post_json(
                "https://example.test/webhook",
                {"content": "x"},
                {},
            )
        assert status == 204
        req = captured["request"]
        ua = req.get_header("User-agent")
        assert ua is not None
        assert ua == wd.WATCHDOG_USER_AGENT
        assert not ua.lower().startswith("python-urllib")

    def test_post_json_sets_content_type(self, tmp_path: Path) -> None:
        captured, fake_urlopen = self._capture_post()
        with patch("watchdog.urllib.request.urlopen", side_effect=fake_urlopen):
            wd._post_json("https://example.test/webhook", {"content": "x"}, {})
        req = captured["request"]
        assert req.get_header("Content-type") == "application/json"

    def test_post_json_caller_can_override_user_agent(self, tmp_path: Path) -> None:
        """If a future endpoint requires a custom User-Agent, callers can override."""
        captured, fake_urlopen = self._capture_post()
        with patch("watchdog.urllib.request.urlopen", side_effect=fake_urlopen):
            wd._post_json(
                "https://example.test/webhook",
                {"content": "x"},
                {"User-Agent": "custom-agent/1.0"},
            )
        req = captured["request"]
        assert req.get_header("User-agent") == "custom-agent/1.0"

    def test_user_agent_constant_format_meets_discord_expectation(self) -> None:
        """Discord's docs recommend User-Agent identifying the bot + URL.
        Pin the format so a future cleanup doesn't reduce it back to something
        that looks like the stdlib default."""
        ua = wd.WATCHDOG_USER_AGENT
        assert "trading-watchdog" in ua
        assert "/" in ua  # version separator
        assert not ua.lower().startswith("python")
        assert not ua.lower().startswith("urllib")


# ---------------------------------------------------------------------------
# Config / env vars
# ---------------------------------------------------------------------------
class TestLoadConfig:
    REQUIRED = (
        "WATCHDOG_HEALTH_URL",
        "WATCHDOG_OPERATOR_EMAIL",
        "WATCHDOG_DISCORD_WEBHOOK_URL",
    )
    OPTIONAL = (
        "WATCHDOG_RESEND_API_KEY",
        "WATCHDOG_RESEND_FROM",
    )

    def _clear_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k in (*self.REQUIRED, *self.OPTIONAL, "WATCHDOG_ID", "WATCHDOG_STATE_PATH"):
            monkeypatch.delenv(k, raising=False)

    def test_missing_required_env_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_all(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            wd.load_config()
        msg = str(exc.value)
        assert "missing required env var" in msg

    def test_full_env_loads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_all(monkeypatch)
        env = {
            "WATCHDOG_HEALTH_URL": "https://paper.example/api/health",
            "WATCHDOG_OPERATOR_EMAIL": "ops@example.test",
            "WATCHDOG_RESEND_API_KEY": "re_xxx",
            "WATCHDOG_RESEND_FROM": "noreply@example.test",
            "WATCHDOG_DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/2",
            "WATCHDOG_ID": "nbg-1",
        }
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        cfg = wd.load_config()
        assert cfg.watchdog_id == "nbg-1"
        assert cfg.health_url.endswith("/api/health")
        assert cfg.email_alerts_enabled is True
        assert cfg.discord_alerts_enabled is True

    def test_default_watchdog_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clear_all(monkeypatch)
        for k, v in {
            "WATCHDOG_HEALTH_URL": "https://x/api/health",
            "WATCHDOG_OPERATOR_EMAIL": "o@e.test",
            "WATCHDOG_RESEND_API_KEY": "r",
            "WATCHDOG_RESEND_FROM": "f@e.test",
            "WATCHDOG_DISCORD_WEBHOOK_URL": "https://d/wh",
        }.items():
            monkeypatch.setenv(k, v)
        cfg = wd.load_config()
        assert cfg.watchdog_id == "hetzner-nuremberg-1"

    def test_discord_only_phase_0_loads_without_resend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Phase 0 deliberate decision: Discord-only is fully supported; Resend
        is deferred until Phase 1. See decisions-log 2026-05-06 'Resend deferred'."""
        self._clear_all(monkeypatch)
        for k, v in {
            "WATCHDOG_HEALTH_URL": "https://paper.example/api/health",
            "WATCHDOG_OPERATOR_EMAIL": "ops@example.test",
            "WATCHDOG_DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/2",
        }.items():
            monkeypatch.setenv(k, v)
        cfg = wd.load_config()
        assert cfg.discord_alerts_enabled is True
        assert cfg.email_alerts_enabled is False
        # Resend fields default to empty strings, not None — match the
        # WatchdogConfig dataclass contract.
        assert cfg.resend_api_key == ""
        assert cfg.resend_from_address == ""

    def test_no_alert_channel_configured_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A watchdog with no alert surface is worse than no watchdog. Fail closed."""
        self._clear_all(monkeypatch)
        for k, v in {
            "WATCHDOG_HEALTH_URL": "https://paper.example/api/health",
            "WATCHDOG_OPERATOR_EMAIL": "ops@example.test",
            # Discord URL provided but isn't a valid https:// URL → discord_alerts_enabled=False
            "WATCHDOG_DISCORD_WEBHOOK_URL": "not-a-valid-url",
        }.items():
            monkeypatch.setenv(k, v)
        with pytest.raises(SystemExit) as exc:
            wd.load_config()
        assert "no alert channel" in str(exc.value)


# ---------------------------------------------------------------------------
# Outcome bookkeeping (regression guard for the structured log line)
# ---------------------------------------------------------------------------
def test_run_once_returns_outcome_with_state_after(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    state = wd.WatchdogState()
    outcome = wd.run_once(
        config=cfg,
        state=state,
        now=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
        check_fn=_fail_check,
        email_fn=_silent_email,
        discord_fn=_silent_discord,
    )
    assert outcome.check.success is False
    assert outcome.state_after["consecutive_failures"] == 1
    assert outcome.state_after["last_status_code"] == 503


def test_failure_threshold_is_three(tmp_path: Path) -> None:
    """Regression guard: changing FAILURE_THRESHOLD requires a decisions-log
    entry per the module's tuning-knobs comment."""
    assert wd.FAILURE_THRESHOLD == 3


def test_alert_cooldown_is_one_hour(tmp_path: Path) -> None:
    """Same — ALERT_COOLDOWN_MINUTES change needs an explicit decision."""
    assert wd.ALERT_COOLDOWN_MINUTES == 60


def test_cooldown_handles_naive_datetime_in_state(tmp_path: Path) -> None:
    """Old state files might have naive ISO strings (no tz suffix). The
    decide_alerts logic shouldn't crash; it should treat them as UTC."""
    cfg = _config(tmp_path)
    naive_ts = datetime(2026, 5, 6, 11, 30).isoformat()  # no tz
    decision = wd.decide_alerts(
        state=wd.WatchdogState(
            consecutive_failures=10,
            last_alert_sent_at_utc=naive_ts,
        ),
        config=cfg,
        check=wd.CheckResult(success=False, status_code=503, error_message="x"),
        now=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
    )
    assert decision.cooldown_active is True


def test_save_state_atomic_no_partial_file_on_concurrent_load(
    tmp_path: Path,
) -> None:
    """save_state writes to .json.tmp then renames; load_state should never
    observe a half-written .json. We can't easily trigger a race in unit tests,
    but we can verify the .tmp file is gone after save."""
    path = tmp_path / "state.json"
    wd.save_state(path, wd.WatchdogState(consecutive_failures=1))
    assert path.exists()
    assert not path.with_suffix(".json.tmp").exists()


def test_failure_with_timedelta_just_under_cooldown(tmp_path: Path) -> None:
    """Exactly 59:59 since last alert → still in cooldown."""
    cfg = _config(tmp_path)
    last_alert = datetime(2026, 5, 6, 11, 0, 1, tzinfo=UTC).isoformat()
    decision = wd.decide_alerts(
        state=wd.WatchdogState(
            consecutive_failures=10,
            last_alert_sent_at_utc=last_alert,
        ),
        config=cfg,
        check=wd.CheckResult(success=False, status_code=503, error_message="x"),
        now=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
    )
    assert decision.cooldown_active is True


def test_check_result_records_status_code_on_failure(tmp_path: Path) -> None:
    """state.last_status_code reflects the *last* check's status, even on failure."""
    cfg = _config(tmp_path)
    state = wd.WatchdogState()

    def fail_with_code(_: wd.WatchdogConfig, code: int = 503) -> wd.CheckResult:
        return wd.CheckResult(success=False, status_code=code, error_message=f"http {code}")

    wd.run_once(
        config=cfg,
        state=state,
        now=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
        check_fn=lambda c: fail_with_code(c, 502),
        email_fn=_silent_email,
        discord_fn=_silent_discord,
    )
    assert state.last_status_code == 502


def test_min_holding_no_alert_on_first_failure(tmp_path: Path) -> None:
    """One failure should never alert, full stop."""
    cfg = _config(tmp_path)
    state = wd.WatchdogState()
    email_calls = []

    def email_fn(**kwargs: Any) -> tuple[bool, str | None]:
        email_calls.append(kwargs)
        return True, None

    wd.run_once(
        config=cfg,
        state=state,
        now=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
        check_fn=_fail_check,
        email_fn=email_fn,
        discord_fn=_silent_discord,
    )
    assert email_calls == []
    assert state.consecutive_failures == 1


def test_run_once_records_email_error(tmp_path: Path) -> None:
    """If Resend send fails, the outcome should record the error verbatim."""
    cfg = _config(tmp_path)
    state = wd.WatchdogState(consecutive_failures=2)  # next tick = 3rd failure

    def failing_email(**_: Any) -> tuple[bool, str | None]:
        return False, "resend returned 500: internal error"

    outcome = wd.run_once(
        config=cfg,
        state=state,
        now=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
        check_fn=_fail_check,
        email_fn=failing_email,
        discord_fn=_silent_discord,
    )
    assert outcome.email_sent is False
    assert outcome.email_error is not None
    assert "500" in outcome.email_error
    # Discord still attempted independently
    assert outcome.discord_sent is True


def test_email_unhandled_exception_does_not_block_discord(tmp_path: Path) -> None:
    """Channel-isolation invariant (backend-spec §1.6): an unhandled exception
    in the email path MUST NOT prevent the Discord path from firing.

    This guards Phase 0 (Discord-only — the email path doesn't run, so this is
    moot) AND Phase 1 (both channels live — a bug in send_resend_email or its
    dependencies cannot regress Discord delivery).
    """
    cfg = _config(tmp_path)
    state = wd.WatchdogState(consecutive_failures=2)
    discord_calls: list[dict[str, Any]] = []

    def exploding_email(**_: Any) -> tuple[bool, str | None]:
        # Simulates a future bug: TypeError, AttributeError, an unhandled
        # ssl error, etc. — anything not in URLError/OSError/TimeoutError.
        raise RuntimeError("simulated unhandled exception in resend path")

    def discord_fn(**kwargs: Any) -> tuple[bool, str | None]:
        discord_calls.append(kwargs)
        return True, None

    outcome = wd.run_once(
        config=cfg,
        state=state,
        now=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
        check_fn=_fail_check,
        email_fn=exploding_email,
        discord_fn=discord_fn,
    )
    # Email failed, error captured
    assert outcome.email_sent is False
    assert outcome.email_error is not None
    assert "RuntimeError" in outcome.email_error
    assert "simulated unhandled exception" in outcome.email_error
    # Discord fired regardless — channel isolation upheld
    assert outcome.discord_sent is True
    assert len(discord_calls) == 1


def test_discord_unhandled_exception_does_not_block_email(tmp_path: Path) -> None:
    """Mirror of the above — Discord failure must not block email delivery
    (relevant only in Phase 1 when both channels are live, but worth pinning
    so the symmetry can't regress)."""
    cfg = _config(tmp_path)
    state = wd.WatchdogState(consecutive_failures=2)
    email_calls: list[dict[str, Any]] = []

    def email_fn(**kwargs: Any) -> tuple[bool, str | None]:
        email_calls.append(kwargs)
        return True, None

    def exploding_discord(**_: Any) -> tuple[bool, str | None]:
        raise ValueError("simulated unhandled exception in discord path")

    outcome = wd.run_once(
        config=cfg,
        state=state,
        now=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
        check_fn=_fail_check,
        email_fn=email_fn,
        discord_fn=exploding_discord,
    )
    # Email fired (channel isolation works in both directions)
    assert outcome.email_sent is True
    assert len(email_calls) == 1
    # Discord captured the error
    assert outcome.discord_sent is False
    assert outcome.discord_error is not None
    assert "ValueError" in outcome.discord_error


def test_first_check_after_recovery_does_not_carry_old_error(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    state = wd.WatchdogState(
        consecutive_failures=5,
        last_error_message="non-2xx status 503",
    )
    wd.run_once(
        config=cfg,
        state=state,
        now=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
        check_fn=_ok_check,
        email_fn=_silent_email,
        discord_fn=_silent_discord,
    )
    assert state.consecutive_failures == 0
    assert state.last_error_message is None
    assert state.last_status_code == 200
