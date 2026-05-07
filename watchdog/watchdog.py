"""External watchdog for the Trading System backend.

Runs on the Hetzner Nuremberg CX23 VPS (Falkenstein deviation per
`Docs/decisions-log.md` 2026-05-05). Pings the backend's public health
endpoint every 5 minutes via systemd timer; on 3 consecutive failures
(≈ 15 min unreachable), alerts the operator via Resend email and the
Discord `#critical` webhook.

Per `Docs/backend-spec.md` §1.6 + §2.12 the watchdog is **alert-only** —
it has no authority to halt or modify backend state. This is deliberate;
adding halt-authority to a watchdog compounds operational risk.

## Threat model

The watchdog is an *external* observer of the backend. It runs on a
geographically separated DC (EU vs US Ashburn) so a region-wide outage on
the primary will not silence the alarm. Its alerts go through three
independent channels (email via Resend, Discord webhook, systemd journal
on a separate VPS) so a single-vendor outage on any one channel still
surfaces the failure.

## Why stdlib only

Dev-guide §3.5 mandates `structlog` for backend service code. The watchdog
deviates from that:

- It runs on a different VPS with no backend Python environment installed.
  Adding pip dependencies means another upgrade surface, another set of
  CVEs to track, and a venv to maintain on a 1-vCPU host whose only job is
  to run a 5-min cron. Stdlib `urllib.request` + `json` + `logging` covers
  the entire feature set we need.
- Logs go to systemd journal (JSON-formatted via the formatter at the
  bottom of this file) and can be shipped to S3 daily via a separate
  cron — the same pipeline that ships journald from the backend.

## Module surface (importable for tests)

`load_config()`           — read env vars, return `WatchdogConfig`
`load_state(path)`        — read state file, return `WatchdogState`
`save_state(path, state)` — atomic-rename state file write
`perform_check(config)`   — GET health URL, return `CheckResult`
`decide_alerts(...)`      — pure function; returns `AlertDecision`
`send_resend_email(...)`  — POST to Resend API
`send_discord_webhook(...)` — POST to Discord webhook
`run_once(config, state, now, check_fn, email_fn, discord_fn)`
                          — orchestrator; pure-ish (deps injected for tests)
`main()`                  — CLI entrypoint used by systemd
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Tuning knobs — change these only with a corresponding decisions-log entry.
FAILURE_THRESHOLD = 3
ALERT_COOLDOWN_MINUTES = 60
HTTP_TIMEOUT_SECONDS = 10
DEFAULT_STATE_PATH = "/var/lib/trading-watchdog/state.json"


@dataclass(frozen=True)
class WatchdogConfig:
    """Resolved env-var configuration."""

    health_url: str
    operator_email: str
    resend_api_key: str
    resend_from_address: str
    discord_webhook_url: str
    watchdog_id: str
    state_path: Path

    @property
    def email_alerts_enabled(self) -> bool:
        return self.resend_api_key.strip() not in {"", "<TODO_FROM_DAY_3_RESEND_PROVISION>"}

    @property
    def discord_alerts_enabled(self) -> bool:
        return self.discord_webhook_url.startswith("https://")


@dataclass
class WatchdogState:
    """Persistent state across systemd timer invocations."""

    consecutive_failures: int = 0
    last_check_at_utc: str | None = None
    last_status_code: int | None = None
    last_error_message: str | None = None
    last_alert_sent_at_utc: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WatchdogState:
        # Be lenient on unknown keys — older state files should still load.
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass(frozen=True)
class CheckResult:
    """One health-check round-trip outcome."""

    success: bool
    status_code: int | None
    error_message: str | None


@dataclass(frozen=True)
class AlertDecision:
    """Decision tree output for whether and where to alert this tick."""

    send_email: bool
    send_discord: bool
    reason: str
    cooldown_active: bool = False


@dataclass
class RunOutcome:
    """What happened this tick — for the structured log line at the end."""

    check: CheckResult
    decision: AlertDecision
    email_sent: bool = False
    email_error: str | None = None
    discord_sent: bool = False
    discord_error: str | None = None
    state_after: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Config + state IO
# ---------------------------------------------------------------------------
def load_config() -> WatchdogConfig:
    """Read env vars; fail-closed on missing required values.

    Required env vars (set by systemd `EnvironmentFile=/opt/trading-watchdog/watchdog.env`,
    sourced from sops-decrypted `secrets/paper.enc.yaml`):

    - `WATCHDOG_HEALTH_URL`           e.g. `https://paper.spratcapital.com/api/health`
    - `WATCHDOG_OPERATOR_EMAIL`       recipient of the alert email (used by Resend
                                      when email path is enabled; ignored otherwise)
    - `WATCHDOG_DISCORD_WEBHOOK_URL`  Discord `#critical` webhook (sops `discord.webhook_urls.critical`)

    Optional (Phase 0 = Discord-only is fully supported; Resend path activates when
    these are populated; see `Docs/decisions-log.md` 2026-05-06 "Resend deferred"):

    - `WATCHDOG_RESEND_API_KEY`   Resend API key (sops `resend.api_key`)
    - `WATCHDOG_RESEND_FROM`      Resend "from" address (sops `resend.from_address`)
    - `WATCHDOG_ID`               identifier for this watchdog instance; default `hetzner-nuremberg-1`
    - `WATCHDOG_STATE_PATH`       state-file path; default `/var/lib/trading-watchdog/state.json`

    At least one alert channel must be live: if Discord is missing AND Resend is
    not configured, `load_config()` fails closed (a watchdog with no alert
    surface is worse than no watchdog at all — see backend-spec §1.6).
    """
    required = {
        "WATCHDOG_HEALTH_URL": os.environ.get("WATCHDOG_HEALTH_URL", "").strip(),
        "WATCHDOG_OPERATOR_EMAIL": os.environ.get("WATCHDOG_OPERATOR_EMAIL", "").strip(),
        "WATCHDOG_DISCORD_WEBHOOK_URL": os.environ.get("WATCHDOG_DISCORD_WEBHOOK_URL", "").strip(),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise SystemExit(
            f"watchdog: missing required env var(s): {', '.join(missing)}. "
            "Source /opt/trading-watchdog/watchdog.env and retry."
        )
    # Resend is optional for Phase 0; empty values disable the email path
    # gracefully (see WatchdogConfig.email_alerts_enabled).
    resend_api_key = os.environ.get("WATCHDOG_RESEND_API_KEY", "").strip()
    resend_from = os.environ.get("WATCHDOG_RESEND_FROM", "").strip()

    config = WatchdogConfig(
        health_url=required["WATCHDOG_HEALTH_URL"],
        operator_email=required["WATCHDOG_OPERATOR_EMAIL"],
        resend_api_key=resend_api_key,
        resend_from_address=resend_from,
        discord_webhook_url=required["WATCHDOG_DISCORD_WEBHOOK_URL"],
        watchdog_id=os.environ.get("WATCHDOG_ID", "hetzner-nuremberg-1"),
        state_path=Path(os.environ.get("WATCHDOG_STATE_PATH", DEFAULT_STATE_PATH)),
    )
    if not config.email_alerts_enabled and not config.discord_alerts_enabled:
        raise SystemExit(
            "watchdog: no alert channel configured. Set WATCHDOG_DISCORD_WEBHOOK_URL "
            "(recommended for Phase 0) and/or WATCHDOG_RESEND_API_KEY + WATCHDOG_RESEND_FROM."
        )
    return config


def load_state(path: Path) -> WatchdogState:
    """Load persistent state. Missing or corrupt file → fresh state."""
    if not path.exists():
        return WatchdogState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Don't block the check on a bad state file; reset to a fresh state.
        # (We *would* lose the consecutive-failures counter on a corrupt
        # write; the tradeoff is keeping the watchdog itself crash-resistant.)
        return WatchdogState()
    return WatchdogState.from_dict(raw)


def save_state(path: Path, state: WatchdogState) -> None:
    """Atomic write: tmp file in same dir, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# HTTP — health check
# ---------------------------------------------------------------------------
def perform_check(config: WatchdogConfig) -> CheckResult:
    """One GET to `health_url`. Anything other than 2xx is a failure."""
    request = urllib.request.Request(  # noqa: S310 — URL is from sops-validated config
        config.health_url,
        method="GET",
        headers={"User-Agent": f"trading-watchdog/{config.watchdog_id}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310
            status = resp.getcode()
            if 200 <= status < 300:
                return CheckResult(success=True, status_code=status, error_message=None)
            return CheckResult(
                success=False,
                status_code=status,
                error_message=f"non-2xx status {status}",
            )
    except urllib.error.HTTPError as exc:
        return CheckResult(
            success=False,
            status_code=exc.code,
            error_message=f"HTTPError {exc.code} {exc.reason}",
        )
    except urllib.error.URLError as exc:
        return CheckResult(
            success=False,
            status_code=None,
            error_message=f"URLError {exc.reason}",
        )
    except TimeoutError:
        return CheckResult(
            success=False,
            status_code=None,
            error_message=f"timeout after {HTTP_TIMEOUT_SECONDS}s",
        )
    except OSError as exc:
        return CheckResult(
            success=False,
            status_code=None,
            error_message=f"OSError {exc}",
        )


# ---------------------------------------------------------------------------
# Alert decision (pure function — easiest to test)
# ---------------------------------------------------------------------------
def decide_alerts(
    *,
    state: WatchdogState,
    config: WatchdogConfig,
    check: CheckResult,
    now: datetime,
) -> AlertDecision:
    """Decide whether to fire an alert given the current state + check result.

    Rules (matching backend-spec §1.6):
    1. If the check succeeded → no alert. (Caller still resets the counter.)
    2. If consecutive failures < threshold → no alert yet; just bump counter.
    3. If counter just hit threshold (this is the 3rd failure or later) AND
       no alert was sent in the last `ALERT_COOLDOWN_MINUTES` → alert.
    4. If we're in cooldown → suppress alert; mark `cooldown_active`.
    """
    if check.success:
        return AlertDecision(send_email=False, send_discord=False, reason="check ok")

    pending_failures = state.consecutive_failures + 1
    if pending_failures < FAILURE_THRESHOLD:
        return AlertDecision(
            send_email=False,
            send_discord=False,
            reason=f"failure {pending_failures}/{FAILURE_THRESHOLD} (under threshold)",
        )

    if state.last_alert_sent_at_utc is not None:
        last_alert = datetime.fromisoformat(state.last_alert_sent_at_utc)
        if last_alert.tzinfo is None:
            last_alert = last_alert.replace(tzinfo=UTC)
        if now - last_alert < timedelta(minutes=ALERT_COOLDOWN_MINUTES):
            return AlertDecision(
                send_email=False,
                send_discord=False,
                reason=(
                    f"failure {pending_failures} but in cooldown "
                    f"(last_alert={state.last_alert_sent_at_utc})"
                ),
                cooldown_active=True,
            )

    return AlertDecision(
        send_email=config.email_alerts_enabled,
        send_discord=config.discord_alerts_enabled,
        reason=f"threshold reached: {pending_failures} consecutive failures",
    )


# ---------------------------------------------------------------------------
# HTTP — outbound alerts
# ---------------------------------------------------------------------------
def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, str]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 — URLs are sops-validated
        url, data=body, method="POST", headers={"Content-Type": "application/json", **headers}
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310
        return resp.getcode(), resp.read().decode("utf-8", errors="replace")


def send_resend_email(
    *,
    config: WatchdogConfig,
    subject: str,
    body_text: str,
    poster: Callable[[str, dict[str, Any], dict[str, str]], tuple[int, str]] = _post_json,
) -> tuple[bool, str | None]:
    """Resend HTTP API: POST https://api.resend.com/emails (Bearer auth).

    Returns (success, error_message_or_none).
    """
    if not config.email_alerts_enabled:
        return False, "Resend API key is placeholder; email alerts disabled"
    payload = {
        "from": config.resend_from_address,
        "to": [config.operator_email],
        "subject": subject,
        "text": body_text,
    }
    try:
        status, response_body = poster(
            "https://api.resend.com/emails",
            payload,
            {"Authorization": f"Bearer {config.resend_api_key}"},
        )
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return False, f"resend send failed: {exc}"
    if 200 <= status < 300:
        return True, None
    return False, f"resend returned {status}: {response_body[:200]}"


def send_discord_webhook(
    *,
    config: WatchdogConfig,
    content: str,
    poster: Callable[[str, dict[str, Any], dict[str, str]], tuple[int, str]] = _post_json,
) -> tuple[bool, str | None]:
    """Discord webhook — `content` is the message body. Discord webhook URLs
    embed their auth token in the path; no extra header needed.
    """
    if not config.discord_alerts_enabled:
        return False, "discord webhook URL is placeholder; discord alerts disabled"
    try:
        status, response_body = poster(
            config.discord_webhook_url,
            {"content": content},
            {},
        )
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return False, f"discord send failed: {exc}"
    # Discord returns 204 No Content on success.
    if 200 <= status < 300:
        return True, None
    return False, f"discord returned {status}: {response_body[:200]}"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def _format_alert_subject(config: WatchdogConfig, state: WatchdogState) -> str:
    return (
        f"[CRITICAL] Trading System unreachable — "
        f"{state.consecutive_failures + 1} consecutive failures from {config.watchdog_id}"
    )


def _format_alert_body(config: WatchdogConfig, state: WatchdogState, check: CheckResult) -> str:
    return (
        f"The external watchdog `{config.watchdog_id}` could not reach the "
        f"Trading System health endpoint at {config.health_url} on "
        f"{state.consecutive_failures + 1} consecutive 5-minute checks "
        f"(≈ {(state.consecutive_failures + 1) * 5} minutes unreachable).\n\n"
        f"Last check: status_code={check.status_code} error={check.error_message}\n\n"
        f"Investigate now: SSH into the Ashburn primary, check `systemctl status trading.service`, "
        f"and review `docker compose logs caddy api` for startup errors. "
        f"If the primary VPS is itself unreachable, check Hetzner status + network reachability "
        f"from a third location.\n\n"
        f"This alert will repeat at most every {ALERT_COOLDOWN_MINUTES} minutes while the "
        f"system is unreachable."
    )


def run_once(
    *,
    config: WatchdogConfig,
    state: WatchdogState,
    now: datetime,
    check_fn: Callable[[WatchdogConfig], CheckResult] = perform_check,
    email_fn: Callable[..., tuple[bool, str | None]] = send_resend_email,
    discord_fn: Callable[..., tuple[bool, str | None]] = send_discord_webhook,
) -> RunOutcome:
    """One full tick: check → decide → alert → update state → return outcome.

    Pure-ish: external IO is delegated to injectable callables for testability.
    """
    check = check_fn(config)
    decision = decide_alerts(state=state, config=config, check=check, now=now)
    outcome = RunOutcome(check=check, decision=decision)

    if decision.send_email:
        ok, err = email_fn(
            config=config,
            subject=_format_alert_subject(config, state),
            body_text=_format_alert_body(config, state, check),
        )
        outcome.email_sent, outcome.email_error = ok, err

    if decision.send_discord:
        ok, err = discord_fn(
            config=config,
            content=_format_alert_subject(config, state)
            + "\n"
            + _format_alert_body(config, state, check),
        )
        outcome.discord_sent, outcome.discord_error = ok, err

    # Update state. consecutive_failures bumps on every failure regardless of
    # whether we alerted (cooldown-suppressed failures still count).
    state.last_check_at_utc = now.isoformat()
    state.last_status_code = check.status_code
    state.last_error_message = check.error_message
    if check.success:
        state.consecutive_failures = 0
    else:
        state.consecutive_failures += 1

    if decision.send_email or decision.send_discord:
        state.last_alert_sent_at_utc = now.isoformat()

    outcome.state_after = state.to_dict()
    return outcome


# ---------------------------------------------------------------------------
# CLI entrypoint + logging setup
# ---------------------------------------------------------------------------
class _JsonFormatter(logging.Formatter):
    """Minimal JSON-line formatter matching the backend's structured-log shape."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp_utc": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "service_name": "watchdog",
            "event": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def _configure_logging() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return logging.getLogger("watchdog")


def main() -> int:
    """Systemd `ExecStart` entrypoint. Exit 0 on a successful tick (whether
    or not the backend was healthy); exit 1 only on a watchdog-side fault
    (config missing, state-file unwriteable, etc.) so systemd-journald can
    distinguish operator-actionable issues from routine checks.
    """
    log = _configure_logging()
    try:
        config = load_config()
    except SystemExit as exc:
        log.error("watchdog_config_error", extra={"extra_fields": {"error": str(exc)}})
        return 1
    state = load_state(config.state_path)
    now = datetime.now(tz=UTC)
    outcome = run_once(config=config, state=state, now=now)
    save_state(config.state_path, state)
    log.info(
        "watchdog_tick_completed",
        extra={
            "extra_fields": {
                "watchdog_id": config.watchdog_id,
                "health_url": config.health_url,
                "check_success": outcome.check.success,
                "check_status_code": outcome.check.status_code,
                "check_error": outcome.check.error_message,
                "consecutive_failures": state.consecutive_failures,
                "decision_reason": outcome.decision.reason,
                "email_sent": outcome.email_sent,
                "email_error": outcome.email_error,
                "discord_sent": outcome.discord_sent,
                "discord_error": outcome.discord_error,
            }
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
