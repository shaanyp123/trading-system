"""Unit tests for the webhook_pusher SSE runner entrypoint (`__main__.py`).

Wire-up follow-up to PR #106. Tests the env-var → SSESubscriberConfig
mapping + the fail-close behavior when required vars are missing.
The long-lived ``_run`` coroutine is tested separately in
``test_webhook_pusher_sse_subscriber.py``.
"""

from __future__ import annotations

import pytest

from services.webhook_pusher import __main__ as runner


class TestBuildConfig:
    def test_all_env_vars_set_builds_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WEBHOOK_PUSHER_API_BEARER_TOKEN", "my-bearer")
        monkeypatch.setenv(
            "WEBHOOK_PUSHER_SIGNALS_WEBHOOK_URL", "https://discord.example.com/signals"
        )
        monkeypatch.setenv("WEBHOOK_PUSHER_FILLS_WEBHOOK_URL", "https://discord.example.com/fills")
        monkeypatch.setenv("WEBHOOK_PUSHER_API_BASE_URL", "http://api-test:8000")

        cfg = runner._build_config()
        assert cfg.api_bearer_token == "my-bearer"
        assert cfg.signals_webhook_url == "https://discord.example.com/signals"
        assert cfg.fills_webhook_url == "https://discord.example.com/fills"
        assert cfg.api_base_url == "http://api-test:8000"

    def test_api_base_url_defaults_to_internal_dns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WEBHOOK_PUSHER_API_BEARER_TOKEN", "t")
        monkeypatch.setenv("WEBHOOK_PUSHER_SIGNALS_WEBHOOK_URL", "https://s")
        monkeypatch.setenv("WEBHOOK_PUSHER_FILLS_WEBHOOK_URL", "https://f")
        monkeypatch.delenv("WEBHOOK_PUSHER_API_BASE_URL", raising=False)
        cfg = runner._build_config()
        assert cfg.api_base_url == "http://api:8000"

    def test_missing_bearer_exits_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WEBHOOK_PUSHER_API_BEARER_TOKEN", raising=False)
        monkeypatch.setenv("WEBHOOK_PUSHER_SIGNALS_WEBHOOK_URL", "https://s")
        monkeypatch.setenv("WEBHOOK_PUSHER_FILLS_WEBHOOK_URL", "https://f")
        with pytest.raises(SystemExit) as exc:
            runner._build_config()
        assert exc.value.code == 2

    def test_missing_signals_url_exits_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WEBHOOK_PUSHER_API_BEARER_TOKEN", "t")
        monkeypatch.delenv("WEBHOOK_PUSHER_SIGNALS_WEBHOOK_URL", raising=False)
        monkeypatch.setenv("WEBHOOK_PUSHER_FILLS_WEBHOOK_URL", "https://f")
        with pytest.raises(SystemExit) as exc:
            runner._build_config()
        assert exc.value.code == 2

    def test_missing_fills_url_exits_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WEBHOOK_PUSHER_API_BEARER_TOKEN", "t")
        monkeypatch.setenv("WEBHOOK_PUSHER_SIGNALS_WEBHOOK_URL", "https://s")
        monkeypatch.delenv("WEBHOOK_PUSHER_FILLS_WEBHOOK_URL", raising=False)
        with pytest.raises(SystemExit) as exc:
            runner._build_config()
        assert exc.value.code == 2

    def test_empty_bearer_is_treated_as_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WEBHOOK_PUSHER_API_BEARER_TOKEN", "")
        monkeypatch.setenv("WEBHOOK_PUSHER_SIGNALS_WEBHOOK_URL", "https://s")
        monkeypatch.setenv("WEBHOOK_PUSHER_FILLS_WEBHOOK_URL", "https://f")
        with pytest.raises(SystemExit) as exc:
            runner._build_config()
        assert exc.value.code == 2

    def test_whitespace_only_treated_as_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WEBHOOK_PUSHER_API_BEARER_TOKEN", "   ")
        monkeypatch.setenv("WEBHOOK_PUSHER_SIGNALS_WEBHOOK_URL", "https://s")
        monkeypatch.setenv("WEBHOOK_PUSHER_FILLS_WEBHOOK_URL", "https://f")
        with pytest.raises(SystemExit) as exc:
            runner._build_config()
        assert exc.value.code == 2
