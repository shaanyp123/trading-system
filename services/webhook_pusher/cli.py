"""Operator smoke CLI — `python -m services.webhook_pusher.cli`.

Exercises the full webhook_pusher delivery path end-to-end against the
real Discord guild + Resend account configured in
``secrets/paper.enc.yaml``. This is the A27 smoke-test runbook anchor for
``deploy/webhook_pusher/README.md`` — Step 4 closure of the Week 3
verification gate ("Discord: send a test message via webhook URL →
message appears in #alerts").

Two smoke modes:

1. **Bare smoke (no DB)**: planner + sender only. Useful for quick
   verification when Postgres isn't running or the operator just wants
   to confirm a webhook URL works.

       python -m services.webhook_pusher.cli --severity P2 \\
           --message "smoke test from $(hostname)"

2. **Full DB roundtrip** (``--with-db``): INSERTs an ``alerts`` row,
   calls :func:`dispatch_alert`, prints the resulting ``delivery_status``
   JSONB. Closes Step 7 of the runbook (the ``alerts`` row UPDATE).

       python -m services.webhook_pusher.cli --severity P0 \\
           --message "smoke test P0 from $(hostname)" --with-db

Required env vars (sourced from sops via the api container's entrypoint
on the VPS, or set manually for local dev):

- ``WEBHOOK_PUSHER_DISCORD_ALERTS_URL`` — sops ``discord.webhook_urls.alerts``
- ``WEBHOOK_PUSHER_DISCORD_CRITICAL_URL`` — sops ``discord.webhook_urls.critical``
- ``WEBHOOK_PUSHER_RESEND_API_KEY`` — sops ``resend.api_key``
- ``WEBHOOK_PUSHER_RESEND_FROM`` — sops ``resend.from_address``
- ``WEBHOOK_PUSHER_OPERATOR_EMAIL`` — operator email (Phase 0 = same as `from`)
- ``WEBHOOK_PUSHER_DATABASE_URL`` (only with ``--with-db``) —
  postgresql+asyncpg URL for the api container's ``app_service`` role

Exit codes:

- ``0`` — all configured channels delivered OK (or channel filtered out
  by severity, e.g. P2 doesn't route to email).
- ``1`` — at least one channel returned a non-OK status. The operator
  reads the printed ``delivery_status`` to triage.
- ``2`` — config error (missing env var, malformed CLI arg).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import structlog

from services.webhook_pusher.dispatcher import dispatch_alert
from services.webhook_pusher.payloads import (
    AlertCategory,
    AlertRow,
    AlertSeverity,
    ChannelName,
    EmailIdentity,
    plan_alert_dispatch,
)
from services.webhook_pusher.sender import (
    DeliveryStatus,
    post_outbound_message,
)

# CLI default category — keeps smoke tests off the operationally meaningful
# enum values. ``external_watchdog_alert`` is a closer fit semantically, but
# would conflate operator-driven smokes with real watchdog escalations.
DEFAULT_SMOKE_CATEGORY: AlertCategory = AlertCategory.EXTERNAL_WATCHDOG_ALERT

log = structlog.get_logger()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m services.webhook_pusher.cli",
        description=(
            "Send a smoke test alert through the webhook_pusher pipeline. "
            "See deploy/webhook_pusher/README.md for the runbook."
        ),
    )
    parser.add_argument(
        "--severity",
        type=str,
        choices=[s.value for s in AlertSeverity],
        required=True,
        help="P0 fans out to #alerts + #critical + email; P1/P2 → #alerts only",
    )
    parser.add_argument(
        "--message",
        type=str,
        required=True,
        help="Human-readable message body (becomes the Discord embed description)",
    )
    parser.add_argument(
        "--category",
        type=str,
        choices=[c.value for c in AlertCategory],
        default=DEFAULT_SMOKE_CATEGORY.value,
        help=f"alert_category enum value (default: {DEFAULT_SMOKE_CATEGORY.value})",
    )
    parser.add_argument(
        "--with-db",
        action="store_true",
        help=(
            "INSERT an alerts row and run dispatch_alert (full path). "
            "Without this flag, the CLI calls the planner + sender only "
            "and skips the alerts table altogether."
        ),
    )
    parser.add_argument(
        "--account-id",
        type=str,
        default=None,
        help=(
            "Required with --with-db: UUID of the accounts row to attach the "
            "alert to. Operator queries `SELECT id FROM accounts LIMIT 1` "
            "post-bootstrap and supplies the value."
        ),
    )
    return parser.parse_args(argv)


def _env_required(name: str) -> str:
    """Read an env var or fail fast with a clear message."""
    value = os.environ.get(name, "").strip()
    if not value:
        print(
            f"ERROR: required env var {name} not set or empty. "
            "See deploy/webhook_pusher/README.md for the sops mapping.",
            file=sys.stderr,
        )
        sys.exit(2)
    return value


def _load_webhook_urls(severity: AlertSeverity) -> dict[ChannelName, str]:
    """Read Discord webhook URLs from env (sops-decrypted at deploy time).

    Resend's URL is the constant ``RESEND_API_URL`` — only needed when
    severity routes to email (P0). The mapping is constructed minimally
    (only the channels the alert will route to) so an operator running
    only --severity P2 doesn't need P0 secrets in env.
    """
    from services.webhook_pusher.payloads import (
        RESEND_API_URL,
        SEVERITY_TO_CHANNELS,
    )

    routed = SEVERITY_TO_CHANNELS[severity]
    urls: dict[ChannelName, str] = {}
    if ChannelName.DISCORD_ALERTS in routed:
        urls[ChannelName.DISCORD_ALERTS] = _env_required("WEBHOOK_PUSHER_DISCORD_ALERTS_URL")
    if ChannelName.DISCORD_CRITICAL in routed:
        urls[ChannelName.DISCORD_CRITICAL] = _env_required("WEBHOOK_PUSHER_DISCORD_CRITICAL_URL")
    if ChannelName.EMAIL in routed:
        urls[ChannelName.EMAIL] = RESEND_API_URL
    return urls


def _load_email_identity(severity: AlertSeverity) -> EmailIdentity | None:
    """Build :class:`EmailIdentity` from env when the severity routes to email."""
    from services.webhook_pusher.payloads import SEVERITY_TO_CHANNELS

    if ChannelName.EMAIL not in SEVERITY_TO_CHANNELS[severity]:
        return None
    return EmailIdentity(
        from_address=_env_required("WEBHOOK_PUSHER_RESEND_FROM"),
        to_address=_env_required("WEBHOOK_PUSHER_OPERATOR_EMAIL"),
        resend_api_key=_env_required("WEBHOOK_PUSHER_RESEND_API_KEY"),
    )


async def _run_bare_smoke(args: argparse.Namespace) -> int:
    """Planner + sender only — no DB, no alerts row. Closes runbook Step 4."""
    severity = AlertSeverity(args.severity)
    category = AlertCategory(args.category)

    alert = AlertRow(
        id=uuid4(),
        severity=severity,
        category=category,
        message=args.message,
        detail={"smoke_test": True, "hostname": os.uname().nodename},
        fired_at_utc=datetime.now(UTC),
    )

    plan = plan_alert_dispatch(
        alert=alert,
        webhook_urls=_load_webhook_urls(severity),
        email_identity=_load_email_identity(severity),
    )

    print(f"Plan: severity={severity.value}, channels={[c.value for c in plan.channels]}")
    print(f"Alert id: {alert.id}")

    any_failed = False
    async with httpx.AsyncClient() as client:
        for msg in plan.outbound_messages:
            print(
                f"  POST {msg.channel.value} -> {msg.url[:60]}{'...' if len(msg.url) > 60 else ''}"
            )
            result = await post_outbound_message(client, msg)
            print(
                f"    status={result.status.value} http={result.http_status} "
                f"retried={result.retried}"
                + (f" error={result.error[:120]}" if result.error else "")
            )
            if result.status is not DeliveryStatus.OK:
                any_failed = True

    return 1 if any_failed else 0


async def _run_with_db(args: argparse.Namespace) -> int:
    """Full path: INSERT alerts row, dispatch_alert, print delivery_status JSONB.

    Closes runbook Steps 6 + 7 (the actual end-to-end trip including the
    alerts table UPDATE).
    """
    if args.account_id is None:
        print(
            "ERROR: --with-db requires --account-id <uuid>. "
            "Query: SELECT id FROM accounts LIMIT 1;",
            file=sys.stderr,
        )
        return 2

    try:
        account_id = UUID(args.account_id)
    except ValueError:
        print(f"ERROR: --account-id {args.account_id!r} is not a valid UUID", file=sys.stderr)
        return 2

    severity = AlertSeverity(args.severity)
    category = AlertCategory(args.category)

    database_url = _env_required("WEBHOOK_PUSHER_DATABASE_URL")

    # Lazy import to keep the bare-smoke path free of SQLAlchemy startup cost.
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(database_url, pool_pre_ping=True, pool_size=2)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    alert_id: UUID
    detail = {"smoke_test": True, "hostname": os.uname().nodename}
    try:
        async with session_factory() as session:
            insert_stmt = text(
                """
                INSERT INTO alerts (account_id, severity, category, message, detail)
                VALUES (:account_id, :severity, :category, :message, CAST(:detail AS JSONB))
                RETURNING id, fired_at_utc
                """
            )
            result = await session.execute(
                insert_stmt,
                {
                    "account_id": account_id,
                    "severity": severity.value,
                    "category": category.value,
                    "message": args.message,
                    "detail": json.dumps(detail, sort_keys=True),
                },
            )
            row = result.one()
            alert_id = row.id
            await session.commit()
            print(f"Inserted alerts row id={alert_id}")

        async with httpx.AsyncClient() as http_client, session_factory() as session:
            report = await dispatch_alert(
                session=session,
                alert_id=alert_id,
                http_client=http_client,
                webhook_urls=_load_webhook_urls(severity),
                email_identity=_load_email_identity(severity),
            )

        print(f"short_circuited: {report.short_circuited}")
        print(f"delivery_status: {json.dumps(dict(report.delivery_status), sort_keys=True)}")
        for r in report.results:
            print(
                f"  {r.channel.value}: status={r.status.value} http={r.http_status} "
                f"retried={r.retried}" + (f" error={r.error[:120]}" if r.error else "")
            )

        any_failed = any(v != DeliveryStatus.OK.value for v in report.delivery_status.values())
        return 1 if any_failed else 0

    finally:
        await engine.dispose()


def _configure_logging() -> None:
    """Console renderer for CLI use — operator reads stdout directly."""
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp_utc"),
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )


async def _amain(argv: list[str]) -> int:
    args = _parse_args(argv)
    _configure_logging()
    if args.with_db:
        return await _run_with_db(args)
    return await _run_bare_smoke(args)


def main() -> int:
    return asyncio.run(_amain(sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
