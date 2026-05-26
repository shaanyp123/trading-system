"""scripts/operator_tools/recovery_agent.py — async_task_died poller + replay decisioning.

Polls the ``alerts`` table for unhandled ``worker_failure`` events
(INSERTed by the new task-death hook in
``services/api/async_task_monitor.py``) and, for each event, decides
whether to invoke the existing fill-recovery tool
``scripts/operator_tools/replay_executions.py``. Closes the manual
operator step from drill 5 (2026-05-18) where the operator hand-ran a
``/tmp/drill5_recovery.py`` script to recover backend-blind fills + the
same step repeated in drill 7 (2026-05-18). Pairs with the autoheal
sidecar (PR #240; handles gateway stuck-state restart) and the
verify-chain cron (PRs #242-#245; verifies audit chain integrity 24/7)
as the third pillar of the operator's 24/7 safety net.

Architecture
============

This is a one-shot agent invoked every 60s by a systemd timer
(``deploy/audit/systemd/recovery-agent-poll.{service,timer}``). It is
NOT a long-running daemon — each tick fires, processes the inbox, and
exits. Reasons:

* Independent of api uptime — the agent connects directly to Postgres
  + Discord, not via the api's webhook_pusher. If the api is down,
  the audit + alerts row landed via the in-process monitor hook BEFORE
  api died, and this agent can still process them.
* Matches the verify-chain + autoheal pattern — VPS cron / systemd as
  the 24/7 floor per ``Docs/agentic-patterns.md`` Pattern 6.
* Simpler reasoning: no long-lived state, no event loop, no
  cancellation contract. Per-tick correctness is the only invariant.

Per the operator brief: v1 is alert-only for hard crashes (no
auto-restart). Auto-restart for transient failures is deferred to a
follow-up after the baseline recovery agent is proven.

Audit-first ordering
====================

Per backend-spec §2.10.1 + memory ``feedback_audit_first_ordering``:
any state-mutating action goes through audit-first. The agent's
flow per alert:

  1. **Classify** the failure from ``detail.exception_type`` +
     ``detail.exception_repr`` (pure function; no I/O).
  2. **Audit-first**: ``append_audit_event(RECOVERY_ACTION_TAKEN, ...)``
     committed BEFORE any other state change. Payload carries
     ``triggering_alert_uuid``, ``triggering_audit_event_uuid`` (the
     upstream ``ASYNC_TASK_DIED`` audit row), ``decision``,
     ``task_name``, ``classification``, ``plan``.
  3. **If decision is invoke_replay**: spawn subprocess
     ``python -m scripts.operator_tools.replay_executions`` with the
     orphan client_order_ids inside the dead window. The subprocess
     handles its own audit chain (ORDER_FILLED → POSITION_OPENED → ...)
     via ``services.risk.fill_processor.process_fill_event``.
  4. **UPDATE alerts** SET ``acknowledged=TRUE``,
     ``resolved_at_utc=now()``, append ``detail.recovery_outcome`` +
     ``detail.audit_event_uuid`` + ``detail.replay_exit_code`` +
     ``detail.replay_summary`` so the operator sees the full outcome.
  5. **POST recovery summary** to Discord ``#critical`` via the
     dedicated webhook at ``/etc/trading/critical-webhook.url``
     (independent of api/webhook_pusher per the operator's locked
     preference for verify-chain-pattern Discord posting).

Idempotency is enforced by ``alerts.acknowledged = FALSE AND
resolved_at_utc IS NULL`` filter + ``FOR UPDATE SKIP LOCKED`` row-lock.
A crash between step 2 and step 4 leaves the alert un-acknowledged but
the audit row durable — the next 60s tick re-decides, and the
``RECOVERY_ACTION_TAKEN`` audit row is the operator's evidence that the
prior decision happened (so they can investigate the crash manually).

Classification (transient vs hard crash vs ambiguous)
=====================================================

* **Transient** → ``invoke_replay``: ``TimeoutError``, ``ConnectionError``,
  ``ConnectionRefusedError``, ``ConnectionResetError``, ``BrokenPipeError``,
  ``CancelledError``, OR any exception whose repr contains an IBKR
  connectivity error code (1100/1101/1102). Drill 5/7's recovery path
  is the canonical exemplar.
* **Hard crash** → ``alert_only``: ``AssertionError``, ``TypeError``,
  ``AttributeError``, ``KeyError``, ``IndexError``, ``NameError``,
  ``ZeroDivisionError``, ``NotImplementedError``, ``SyntaxError``,
  ``RuntimeError`` (without IBKR markers). These need operator
  investigation + a code fix; auto-replay would just rerun against a
  still-broken worker.
* **Ambiguous** → ``alert_only`` (fail-closed): exit_reason ==
  ``done_without_exception`` (worker exited cleanly without stop
  signal) OR unknown exception type. Operator triages manually.

clientId allocation
===================

The recovery agent itself does NOT connect to IBKR. The subprocess
``replay_executions.py`` uses ``clientId=99`` (locked per
``services/api/async_task_monitor.py`` neighbors + dev-guide §1.5 +
memory ``project_clientid_allocation``).

A-gates
=======

* **A01 enforced** — every state-mutating write goes through
  ``services.audit.writer.append_audit_event`` OR is structurally
  downstream of it. ``alerts`` table is mutable per alembic 0005
  immutability scope (only audit_log + attribution are trigger-
  locked).
* **A04 satisfied** — ``RECOVERY_ACTION_TAKEN`` + ``ASYNC_TASK_DIED``
  added to ``services/audit/event_types.py`` in this same PR; tests
  in ``tests/unit/test_recovery_agent.py::TestAuditEventTypes`` lock
  the emit + read-back contract.
* **A05 satisfied** — no float in money paths; the recovery agent
  doesn't touch money values. Replay subprocess handles its own
  A05 contract.
* **A06 enforced** — every datetime tz-aware UTC at the boundary;
  the audit writer's JCS canonicaliser re-validates.
* **A22 N/A** — unit tests stub the subprocess invocation + httpx
  client; integration tests use testcontainers per dev-guide §6.3.
  No live Discord / IBKR / api in CI.
* **A27 satisfied** — operator runbook in
  ``deploy/audit/README.md`` (the "Recovery agent — install
  ceremony" section landed alongside this script).

Exit codes
==========

Always **0** when invoked under systemd (cron-friendly; per-alert
outcomes flow through Discord + structured logs, not the process
exit). The semantic exit codes below are surfaced via the per-tick
``recovery_agent_completed`` structured log + matter for test
assertion:

* ``0`` — success: all unhandled alerts processed (audit + alert
  update + Discord post all landed) OR no unhandled alerts in the
  window.
* ``5`` — DB init failure (DATABASE_URL missing / wrong / Postgres
  unreachable). Tick logs + exits without processing any alerts.
* ``6`` — Invalid CLI args (caught early before any I/O).
* ``99`` — Unexpected exception (logged with traceback). Per-alert
  failures DO NOT bubble up to exit 99 — they're caught + the alert
  is left un-acknowledged for the next tick.

Usage
=====

CLI invocation (typically via the bash wrapper):

::

    python -m scripts.operator_tools.recovery_agent --env paper

Required env vars (staged by the bash wrapper):

* ``DATABASE_URL`` — ``postgresql+asyncpg://app_service:...@postgres:5432/trading``
* ``CRITICAL_WEBHOOK_URL`` — Discord webhook URL for ``#critical`` channel

Sample bash wrapper: ``scripts/operator_tools/recovery_agent_tick.sh``.

Forbidden-paths check
=====================

``scripts/operator_tools/**`` is NOT on the dev-guide §11 anti-pattern
[A02] forbidden-modification whitelist. The audit event_types added
(``services/audit/event_types.py`` + alembic ``worker_failure``) ARE on
the forbidden list and gate the PR via ``risk-review-approved`` label.
This file is pure tooling; it CALLS but does not modify
``services/audit/**`` / ``services/risk/**`` / etc.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal
from uuid import UUID

import httpx
import structlog

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Exit code contract (load-bearing for the bash wrapper + tests)
# ---------------------------------------------------------------------------

EXIT_OK: Final[int] = 0
EXIT_DB_INIT_FAILED: Final[int] = 5
EXIT_BAD_ARGS: Final[int] = 6
EXIT_UNEXPECTED: Final[int] = 99


# ---------------------------------------------------------------------------
# CLI args
# ---------------------------------------------------------------------------

_ALLOWED_ENVS: Final[tuple[str, ...]] = ("paper", "live-small", "live-scale")
EnvName = Literal["paper", "live-small", "live-scale"]

DATABASE_URL_ENV: Final[str] = "DATABASE_URL"
CRITICAL_WEBHOOK_URL_ENV: Final[str] = "CRITICAL_WEBHOOK_URL"

DEFAULT_POLL_WINDOW_MINUTES: Final[int] = 5
DEFAULT_DEAD_WINDOW_MINUTES: Final[int] = 30
DEFAULT_REPLAY_TIMEOUT_SECONDS: Final[float] = 120.0
DEFAULT_DISCORD_TIMEOUT_SECONDS: Final[float] = 15.0
DEFAULT_MAX_ALERTS_PER_TICK: Final[int] = 10


@dataclass(frozen=True, slots=True)
class ParsedArgs:
    """Normalized CLI args. Tests use this surface directly so they
    don't fight stderr capture on argparse SystemExit paths.
    """

    env: EnvName
    poll_window_minutes: int
    dead_window_minutes: int
    replay_timeout_seconds: float
    discord_timeout_seconds: float
    max_alerts_per_tick: int
    allow_non_paper: bool
    dry_run: bool


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts.operator_tools.recovery_agent",
        description=(
            "Poll alerts table for unhandled worker_failure events + "
            "invoke replay_executions.py for transient failures + post "
            "recovery summary to Discord #critical. Designed for systemd-"
            "timer invocation every 60s. See deploy/audit/README.md for "
            "the operator runbook."
        ),
    )
    parser.add_argument(
        "--env",
        required=True,
        choices=_ALLOWED_ENVS,
        help=(
            "Audit env tag for RECOVERY_ACTION_TAKEN writes — mirrors "
            "audit_log.env CHECK. Mutating live-* envs requires the "
            "--allow-non-paper safety gate."
        ),
    )
    parser.add_argument(
        "--poll-window-minutes",
        type=int,
        default=DEFAULT_POLL_WINDOW_MINUTES,
        help=(
            f"How far back (minutes) to scan alerts.fired_at_utc for "
            f"unhandled rows. Default: {DEFAULT_POLL_WINDOW_MINUTES}. "
            "Larger windows catch alerts whose first processing tick "
            "crashed mid-flight; smaller windows tighten the upper "
            "bound on re-processing latency."
        ),
    )
    parser.add_argument(
        "--dead-window-minutes",
        type=int,
        default=DEFAULT_DEAD_WINDOW_MINUTES,
        help=(
            f"How far back (minutes) before alert.fired_at_utc to "
            f"consider orders 'orphan candidates' for replay. "
            f"Default: {DEFAULT_DEAD_WINDOW_MINUTES}. Drill 5's gateway-"
            "hang window was ~4h, but the worker's death (if it died) "
            "would be much later than the placement, so 30min covers "
            "the typical signal→placement→fill latency."
        ),
    )
    parser.add_argument(
        "--replay-timeout-seconds",
        type=float,
        default=DEFAULT_REPLAY_TIMEOUT_SECONDS,
        help=(
            f"Max wall-clock seconds for the replay_executions.py "
            f"subprocess. Default: {DEFAULT_REPLAY_TIMEOUT_SECONDS}s "
            "(replay itself has a 30s IBKR connect timeout; full "
            "happy path is ~10s; 120s is comfortable headroom)."
        ),
    )
    parser.add_argument(
        "--discord-timeout-seconds",
        type=float,
        default=DEFAULT_DISCORD_TIMEOUT_SECONDS,
        help=(
            f"Max wall-clock seconds for the Discord webhook POST. "
            f"Default: {DEFAULT_DISCORD_TIMEOUT_SECONDS}s."
        ),
    )
    parser.add_argument(
        "--max-alerts-per-tick",
        type=int,
        default=DEFAULT_MAX_ALERTS_PER_TICK,
        help=(
            f"Per-tick cap on alerts processed. Default: "
            f"{DEFAULT_MAX_ALERTS_PER_TICK}. Prevents a flood of "
            "synthetic alerts from blowing past the 120s systemd "
            "TimeoutStartSec budget."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "If set, classify + log decisions but DO NOT emit audit "
            "events, INSERT/UPDATE alerts, invoke replay, or POST to "
            "Discord. Useful for operator-side sanity check before "
            "enabling the systemd timer."
        ),
    )
    parser.add_argument(
        "--allow-non-paper",
        action="store_true",
        default=False,
        help=(
            "Required when --env is live-small or live-scale. The "
            "agent fails closed without this flag for tooling that "
            "mutates audit chain rows + invokes replay in production."
        ),
    )
    return parser


def _parse_args(argv: Sequence[str] | None) -> ParsedArgs:
    parser = _build_parser()
    args = parser.parse_args(argv)

    env: EnvName = args.env
    if env in ("live-small", "live-scale") and not args.allow_non_paper:
        parser.error(
            f"--env={env!r} requires --allow-non-paper (fail-closed default "
            "for tooling that mutates audit chain + invokes replay in "
            "production)"
        )

    if args.poll_window_minutes <= 0:
        parser.error("--poll-window-minutes must be > 0")
    if args.dead_window_minutes <= 0:
        parser.error("--dead-window-minutes must be > 0")
    if args.replay_timeout_seconds <= 0:
        parser.error("--replay-timeout-seconds must be > 0")
    if args.discord_timeout_seconds <= 0:
        parser.error("--discord-timeout-seconds must be > 0")
    if args.max_alerts_per_tick <= 0:
        parser.error("--max-alerts-per-tick must be > 0")

    return ParsedArgs(
        env=env,
        poll_window_minutes=int(args.poll_window_minutes),
        dead_window_minutes=int(args.dead_window_minutes),
        replay_timeout_seconds=float(args.replay_timeout_seconds),
        discord_timeout_seconds=float(args.discord_timeout_seconds),
        max_alerts_per_tick=int(args.max_alerts_per_tick),
        allow_non_paper=bool(args.allow_non_paper),
        dry_run=bool(args.dry_run),
    )


# ---------------------------------------------------------------------------
# Classification (pure function — tested via TestClassification)
# ---------------------------------------------------------------------------

#: Exception type names that indicate transient connectivity failure —
#: invoke replay to recover any orphan fills that may have landed at
#: IBKR while the worker was unresponsive.
TRANSIENT_EXCEPTION_TYPES: Final[frozenset[str]] = frozenset(
    {
        "TimeoutError",
        "asyncio.TimeoutError",
        "ConnectionError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "BrokenPipeError",
        "CancelledError",
        "asyncio.CancelledError",
    }
)

#: Markers (substrings) that, when present in the exception's repr,
#: classify the failure as a transient broker-connectivity issue.
#: IBKR error codes 1100/1101/1102 are the canonical
#: connectivity-loss/restoration set per
#: ``services/api/async_task_monitor.py::DEFAULT_IBKR_CONNECTIVITY_CODES``.
TRANSIENT_IBKR_ERROR_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "1100",
        "1101",
        "1102",
    }
)

#: Exception type names that indicate a deterministic programming bug
#: or operator-actionable failure — alert-only; do NOT auto-replay
#: (replay would just rerun against the still-broken worker). Operator
#: investigation + code fix required.
HARD_CRASH_EXCEPTION_TYPES: Final[frozenset[str]] = frozenset(
    {
        "AssertionError",
        "TypeError",
        "AttributeError",
        "KeyError",
        "IndexError",
        "NameError",
        "ZeroDivisionError",
        "NotImplementedError",
        "SyntaxError",
        "ValueError",
        # RuntimeError is generic; without an IBKR marker we
        # conservatively treat it as hard-crash (fail-closed).
        "RuntimeError",
    }
)


ClassificationKind = Literal["transient", "hard_crash", "ambiguous"]
Decision = Literal["invoke_replay", "alert_only"]


@dataclass(frozen=True, slots=True)
class Classification:
    """Result of classifying a dead-task failure descriptor.

    ``reason`` is a human-readable explanation surfaced in the audit
    payload + Discord post so the operator understands WHY the agent
    made the call it made.
    """

    kind: ClassificationKind
    decision: Decision
    reason: str


def classify_failure(
    *,
    exit_reason: str,
    exception_type: str | None,
    exception_repr: str | None,
) -> Classification:
    """Pure-function classification of an ``async_task_died`` descriptor.

    Precedence (highest to lowest):

      1. IBKR connectivity marker in ``exception_repr`` → transient
         (wins over the exception_type match because an IBKR-derived
         error can have any type — usually it's a TimeoutError raised
         from inside ib-async, but it could be an OSError or
         RuntimeError depending on the layer).
      2. ``exception_type`` ∈ TRANSIENT_EXCEPTION_TYPES → transient.
      3. ``exception_type`` ∈ HARD_CRASH_EXCEPTION_TYPES → hard_crash.
      4. ``exit_reason == 'done_without_exception'`` → ambiguous
         (alert-only; likely SystemExit/KeyboardInterrupt or logic
         bug).
      5. Otherwise (unknown exception_type, exception_type=None with
         exit_reason='exception') → ambiguous (fail-closed alert-only).

    Defaults to ``alert_only`` whenever the failure mode isn't clearly
    transient — fail-closed against the false-positive case of
    invoking replay on a deterministic bug.
    """
    repr_str = exception_repr or ""

    # 1. IBKR connectivity marker takes precedence
    for marker in TRANSIENT_IBKR_ERROR_MARKERS:
        if marker in repr_str:
            return Classification(
                kind="transient",
                decision="invoke_replay",
                reason=(
                    f"exception_repr contains IBKR connectivity error "
                    f"code {marker!r} — replay recovers any orphan fills "
                    "that landed at IBKR during the connectivity gap"
                ),
            )

    # 2. exception_type ∈ TRANSIENT
    if exception_type is not None and exception_type in TRANSIENT_EXCEPTION_TYPES:
        return Classification(
            kind="transient",
            decision="invoke_replay",
            reason=(
                f"exception_type={exception_type!r} matches "
                "TRANSIENT_EXCEPTION_TYPES — likely a transient I/O "
                "or timeout failure recoverable via replay"
            ),
        )

    # 3. exception_type ∈ HARD_CRASH
    if exception_type is not None and exception_type in HARD_CRASH_EXCEPTION_TYPES:
        return Classification(
            kind="hard_crash",
            decision="alert_only",
            reason=(
                f"exception_type={exception_type!r} matches "
                "HARD_CRASH_EXCEPTION_TYPES — deterministic programming "
                "bug or operator-actionable failure; auto-replay would "
                "rerun against the still-broken worker. Operator "
                "investigation + code fix required."
            ),
        )

    # 4. done_without_exception
    if exit_reason == "done_without_exception":
        return Classification(
            kind="ambiguous",
            decision="alert_only",
            reason=(
                "task exited cleanly via 'done_without_exception' — "
                "likely SystemExit/KeyboardInterrupt or a logic bug "
                "that flipped the inner while loop condition. "
                "Operator-gated investigation."
            ),
        )

    # 5. ambiguous / fail-closed
    if exception_type is None:
        return Classification(
            kind="ambiguous",
            decision="alert_only",
            reason=(
                "exception_type missing in death descriptor (monitor "
                "didn't record it); fail-closed to alert_only"
            ),
        )
    return Classification(
        kind="ambiguous",
        decision="alert_only",
        reason=(
            f"exception_type={exception_type!r} not in TRANSIENT or "
            "HARD_CRASH known sets; fail-closed to alert_only"
        ),
    )


# ---------------------------------------------------------------------------
# DB queries (alerts inbox + orphan CIDs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AlertRow:
    """The subset of ``alerts`` columns the recovery agent needs.

    Caller (``_amain``) reads from the alerts table and constructs
    this. Columns not relevant to recovery (``message``,
    ``delivery_status``) are deliberately omitted to keep the
    in-process struct minimal.
    """

    id: UUID
    account_id: UUID
    fired_at_utc: datetime
    triggering_audit_event_uuid: UUID | None
    detail: dict[str, Any]


async def fetch_unhandled_alerts(
    session: Any,
    *,
    window_minutes: int,
    max_rows: int,
) -> list[AlertRow]:
    """SELECT unhandled worker_failure alerts within the poll window.

    Uses ``FOR UPDATE SKIP LOCKED`` so two concurrent recovery-agent
    ticks (a planned 60s cadence shouldn't collide, but a delayed
    previous tick OR a manual invocation could) don't double-process
    the same row.

    Note: SQLAlchemy doesn't propagate the row-lock past the read
    here — we don't UPDATE in the same transaction. The lock is
    advisory; the canonical idempotency mechanism is
    ``acknowledged = FALSE`` (UPDATEd in step 4 of the per-alert
    flow). The SKIP LOCKED is just defense-in-depth.
    """
    from sqlalchemy import text

    result = await session.execute(
        text(
            "SELECT id, account_id, fired_at_utc, "
            "       triggering_audit_event_uuid, detail "
            "FROM alerts "
            "WHERE category = 'worker_failure' "
            "  AND acknowledged = FALSE "
            "  AND resolved_at_utc IS NULL "
            "  AND fired_at_utc > NOW() - (:window || ' minutes')::interval "
            "ORDER BY fired_at_utc ASC "
            "LIMIT :limit "
            "FOR UPDATE SKIP LOCKED"
        ),
        {"window": str(window_minutes), "limit": max_rows},
    )
    rows: list[AlertRow] = []
    for row in result.fetchall():
        detail_raw = row.detail
        if detail_raw is None:
            detail = {}
        elif isinstance(detail_raw, dict):
            detail = dict(detail_raw)
        else:
            # asyncpg returns JSONB as already-parsed dict typically;
            # defensive fallback if a driver swap returns str.
            try:
                detail = json.loads(detail_raw)
            except (TypeError, ValueError):
                detail = {}
        triggering_uuid_raw = row.triggering_audit_event_uuid
        triggering_uuid = (
            UUID(str(triggering_uuid_raw)) if triggering_uuid_raw is not None else None
        )
        rows.append(
            AlertRow(
                id=UUID(str(row.id)),
                account_id=UUID(str(row.account_id)),
                fired_at_utc=row.fired_at_utc,
                triggering_audit_event_uuid=triggering_uuid,
                detail=detail,
            )
        )
    return rows


async def fetch_orphan_client_order_ids(
    session: Any,
    *,
    account_id: UUID,
    alert_fired_at_utc: datetime,
    dead_window_minutes: int,
) -> tuple[str, ...]:
    """SELECT orders.client_order_id for likely-orphan rows.

    "Orphan" = placed within the dead window, account_id matches the
    dead worker's account, and still in a non-terminal status
    ('pending', 'working', 'partially_filled'). These are the
    candidates for replay — if IBKR shows fills for them, the backend
    missed the orderStatus events and the fill_processor needs to be
    invoked.

    Returns a sorted tuple of CIDs (sorted by placed_at_utc ASC so
    replay processes in placement order). Empty tuple if no candidates.
    """
    from sqlalchemy import text

    dead_window_start = alert_fired_at_utc - timedelta(minutes=dead_window_minutes)
    result = await session.execute(
        text(
            "SELECT client_order_id "
            "FROM orders "
            "WHERE account_id = :acct "
            "  AND status IN ('pending', 'working', 'partially_filled') "
            "  AND placed_at_utc >= :dead_start "
            "  AND placed_at_utc <= :alert_fired "
            "ORDER BY placed_at_utc ASC"
        ),
        {
            "acct": account_id,
            "dead_start": dead_window_start,
            "alert_fired": alert_fired_at_utc,
        },
    )
    return tuple(row.client_order_id for row in result.fetchall())


# ---------------------------------------------------------------------------
# Replay subprocess
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReplaySubprocessResult:
    """Outcome of invoking ``replay_executions.py`` via subprocess.

    ``invoked`` is False when the recovery agent decided to skip
    invocation (alert_only path OR no orphan CIDs). In that case
    ``exit_code`` is None and the stdout/stderr are empty.
    """

    invoked: bool
    exit_code: int | None
    stdout_tail: str
    stderr_tail: str
    cids: tuple[str, ...]
    timed_out: bool = False


#: Number of trailing lines from subprocess output to capture into the
#: alerts.detail JSONB + Discord post. Enough for operator triage; not
#: so much that it blows the Discord 6000-char embed limit.
SUBPROCESS_OUTPUT_TAIL_LINES: Final[int] = 20


def _tail_lines(text_blob: str, n_lines: int) -> str:
    """Return the last n_lines of text_blob, joined with \\n."""
    if not text_blob:
        return ""
    lines = text_blob.splitlines()
    return "\n".join(lines[-n_lines:])


#: Type hint for the subprocess-invocation hook (tests inject a fake).
SubprocessRunFn = Callable[..., "subprocess.CompletedProcess[str]"]


def invoke_replay_executions(
    *,
    env: EnvName,
    client_order_ids: tuple[str, ...],
    timeout_seconds: float,
    python_executable: str = sys.executable,
    subprocess_run: SubprocessRunFn = subprocess.run,
) -> ReplaySubprocessResult:
    """Spawn ``python -m scripts.operator_tools.replay_executions``.

    Synchronous (subprocess.run with timeout); the recovery agent's
    main loop awaits this via ``asyncio.to_thread`` so the event loop
    isn't blocked. Captures stdout + stderr; truncates to the last
    ``SUBPROCESS_OUTPUT_TAIL_LINES`` lines for the alerts.detail update.

    If ``client_order_ids`` is empty, returns invoked=False
    immediately — replay's --client-order-ids is required, so this is
    a fail-fast invariant.

    On timeout, returns ``timed_out=True`` and exit_code=None. Caller
    surfaces this in Discord + alert detail so the operator can
    investigate (gateway wedged? lock contention?).
    """
    if not client_order_ids:
        return ReplaySubprocessResult(
            invoked=False,
            exit_code=None,
            stdout_tail="",
            stderr_tail="",
            cids=(),
        )

    cmd = [
        python_executable,
        "-m",
        "scripts.operator_tools.replay_executions",
        "--client-order-ids",
        ",".join(client_order_ids),
        "--env",
        env,
    ]
    if env in ("live-small", "live-scale"):
        cmd.append("--allow-non-paper")

    log.info(
        "recovery_agent_invoking_replay",
        cmd=cmd,
        client_order_id_count=len(client_order_ids),
        timeout_seconds=timeout_seconds,
    )

    try:
        completed = subprocess_run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # text=True above guarantees stdout/stderr are str|None at runtime,
        # but TimeoutExpired's type hints are bytes|str|None — defensive
        # decode for the bytes path so mypy is happy + the bash wrapper
        # never sees a Python repr leak through into the journal.
        raw_stdout = exc.stdout
        raw_stderr = exc.stderr
        stdout_str = (
            raw_stdout.decode("utf-8", errors="replace")
            if isinstance(raw_stdout, bytes)
            else (raw_stdout or "")
        )
        stderr_str = (
            raw_stderr.decode("utf-8", errors="replace")
            if isinstance(raw_stderr, bytes)
            else (raw_stderr or "")
        )
        stdout_tail = _tail_lines(stdout_str, SUBPROCESS_OUTPUT_TAIL_LINES)
        stderr_tail = _tail_lines(stderr_str, SUBPROCESS_OUTPUT_TAIL_LINES)
        log.warning(
            "recovery_agent_replay_timed_out",
            timeout_seconds=timeout_seconds,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
        )
        return ReplaySubprocessResult(
            invoked=True,
            exit_code=None,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            cids=client_order_ids,
            timed_out=True,
        )

    stdout_tail = _tail_lines(completed.stdout, SUBPROCESS_OUTPUT_TAIL_LINES)
    stderr_tail = _tail_lines(completed.stderr, SUBPROCESS_OUTPUT_TAIL_LINES)
    log.info(
        "recovery_agent_replay_completed",
        exit_code=completed.returncode,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
    )
    return ReplaySubprocessResult(
        invoked=True,
        exit_code=int(completed.returncode),
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        cids=client_order_ids,
    )


# ---------------------------------------------------------------------------
# Audit emit (RECOVERY_ACTION_TAKEN)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecoveryAuditPayload:
    """Strongly-typed view of the RECOVERY_ACTION_TAKEN payload.

    Built by ``_build_recovery_audit_payload``; serialized into a dict
    when passed to ``append_audit_event``.
    """

    triggering_alert_uuid: UUID
    triggering_audit_event_uuid: UUID | None
    task_name: str | None
    classification_kind: ClassificationKind
    decision: Decision
    classification_reason: str
    plan_orphan_cid_count: int
    env: EnvName


def _build_recovery_audit_payload(
    *,
    alert: AlertRow,
    classification: Classification,
    orphan_cids: tuple[str, ...],
    env: EnvName,
) -> dict[str, Any]:
    """Compose the audit_log payload for RECOVERY_ACTION_TAKEN.

    Pure function so tests can pin the exact JCS-canonical shape
    without touching the DB. Keys sorted; only JCS-safe types (str,
    int, bool, None, list, dict).
    """
    task_name = alert.detail.get("task_name")
    return {
        "triggering_alert_uuid": str(alert.id),
        "triggering_audit_event_uuid": (
            str(alert.triggering_audit_event_uuid)
            if alert.triggering_audit_event_uuid is not None
            else None
        ),
        "task_name": task_name if isinstance(task_name, str) else None,
        "classification": classification.kind,
        "decision": classification.decision,
        "classification_reason": classification.reason,
        "plan_orphan_cid_count": len(orphan_cids),
        "env": env,
    }


async def emit_recovery_action_audit(
    session_factory: Any,
    *,
    alert: AlertRow,
    classification: Classification,
    orphan_cids: tuple[str, ...],
    env: EnvName,
) -> Any:
    """append_audit_event(RECOVERY_ACTION_TAKEN, ...) — audit-first.

    Returns the inserted ``AuditLogRecord``. Caller threads
    ``record.event_uuid`` into the alerts UPDATE so a future reader
    can trace from the alert back to the recovery decision row.

    Lazy-imports the audit module so module-load doesn't require it
    (matches the replay_executions.py pattern).
    """
    from services.audit.event_types import AuditEventType
    from services.audit.writer import append_audit_event

    payload = _build_recovery_audit_payload(
        alert=alert,
        classification=classification,
        orphan_cids=orphan_cids,
        env=env,
    )
    async with session_factory() as audit_session:
        record = await append_audit_event(
            audit_session,
            AuditEventType.RECOVERY_ACTION_TAKEN,
            payload,
            account_id=alert.account_id,
            env=env,
            phase_at_emit=1,
        )
    log.info(
        "recovery_action_audit_emitted",
        audit_event_uuid=str(record.event_uuid),
        sequence_no=record.sequence_no,
        triggering_alert_uuid=str(alert.id),
        decision=classification.decision,
        classification=classification.kind,
        env=env,
    )
    return record


# ---------------------------------------------------------------------------
# Alert UPDATE
# ---------------------------------------------------------------------------


async def update_alert_handled(
    session: Any,
    *,
    alert: AlertRow,
    classification: Classification,
    orphan_cids: tuple[str, ...],
    replay_result: ReplaySubprocessResult,
    audit_event_uuid: UUID,
) -> None:
    """UPDATE alerts SET acknowledged=true, resolved_at_utc=now(), detail += outcome.

    The detail JSONB is extended (not overwritten) via ``||`` so the
    monitor-side fields (task_name, exception_type, etc.) survive
    alongside the recovery outcome.
    """
    from sqlalchemy import text

    recovery_outcome: dict[str, Any] = {
        "classification": classification.kind,
        "decision": classification.decision,
        "classification_reason": classification.reason,
        "audit_event_uuid": str(audit_event_uuid),
        "orphan_cids_count": len(orphan_cids),
        "replay_invoked": replay_result.invoked,
        "replay_exit_code": replay_result.exit_code,
        "replay_timed_out": replay_result.timed_out,
        "replay_stdout_tail": replay_result.stdout_tail,
        "replay_stderr_tail": replay_result.stderr_tail,
        "recovery_agent_handled_at_utc": datetime.now(tz=UTC).isoformat(),
    }
    await session.execute(
        text(
            "UPDATE alerts "
            "SET acknowledged = TRUE, "
            "    acknowledged_at_utc = NOW(), "
            "    resolved_at_utc = NOW(), "
            "    detail = COALESCE(detail, '{}'::jsonb) || CAST(:outcome AS JSONB) "
            "WHERE id = :alert_id"
        ),
        {
            "outcome": json.dumps({"recovery_outcome": recovery_outcome}),
            "alert_id": alert.id,
        },
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Discord post (independent webhook — no api/webhook_pusher dependency)
# ---------------------------------------------------------------------------


def _build_discord_payload(
    *,
    alert: AlertRow,
    classification: Classification,
    orphan_cids: tuple[str, ...],
    replay_result: ReplaySubprocessResult,
    audit_event_uuid: UUID,
    env: EnvName,
) -> dict[str, Any]:
    """Compose the Discord webhook JSON payload for the recovery summary.

    Single embed; uses orange color (P1 visual) since by the time
    this fires, the in-process P0 alert has already gone out (or would
    have if api/webhook_pusher had been wired). This post is the
    recovery agent's "I took care of it" follow-up.

    Pure function so tests can assert exact embed shape.
    """
    task_name = alert.detail.get("task_name", "<unknown>")
    exception_type = alert.detail.get("exception_type") or "<none>"
    title = f"Recovery agent: {classification.decision} for {task_name}"
    color = 0x2ECC71 if classification.decision == "invoke_replay" else 0xFFA500  # green / orange

    fields: list[dict[str, Any]] = [
        {"name": "task_name", "value": str(task_name), "inline": True},
        {"name": "exception_type", "value": str(exception_type), "inline": True},
        {"name": "classification", "value": classification.kind, "inline": True},
        {"name": "decision", "value": classification.decision, "inline": True},
        {"name": "env", "value": env, "inline": True},
        {"name": "audit_event_uuid", "value": str(audit_event_uuid), "inline": False},
        {"name": "classification_reason", "value": classification.reason, "inline": False},
    ]
    if classification.decision == "invoke_replay":
        replay_summary = (
            f"invoked={replay_result.invoked} "
            f"exit_code={replay_result.exit_code} "
            f"timed_out={replay_result.timed_out} "
            f"orphan_cid_count={len(orphan_cids)}"
        )
        fields.append({"name": "replay_summary", "value": replay_summary, "inline": False})
        if replay_result.stderr_tail:
            # Discord field value cap is 1024; truncate defensively
            tail = replay_result.stderr_tail
            if len(tail) > 1000:
                tail = tail[-1000:] + "…"
            fields.append({"name": "replay_stderr_tail", "value": tail, "inline": False})
        if classification.decision == "invoke_replay" and (
            replay_result.exit_code not in (None, 0) or replay_result.timed_out
        ):
            fields.append(
                {
                    "name": "next_operator_action",
                    "value": (
                        "Replay subprocess exited non-zero or timed out. "
                        "SSH to VPS + inspect journalctl -u "
                        "recovery-agent-poll.service for the full output. "
                        "If exit_code=4, ib_gateway may be wedged — check "
                        "docker compose ps ib_gateway + autoheal logs."
                    ),
                    "inline": False,
                }
            )
    else:
        fields.append(
            {
                "name": "next_operator_action",
                "value": (
                    "Recovery agent did NOT invoke replay — failure was "
                    "classified as alert_only (hard crash or ambiguous). "
                    "Investigate the worker death via journalctl + api "
                    "logs. The alerts row is marked resolved but the "
                    "underlying bug needs a code fix before the worker "
                    "can be safely restarted."
                ),
                "inline": False,
            }
        )

    return {
        "username": "recovery-agent",
        "embeds": [
            {
                "title": title,
                "description": (
                    f"Triggering alert {alert.id} fired at "
                    f"{alert.fired_at_utc.isoformat()}; recovery agent processed it."
                ),
                "color": color,
                "fields": fields,
                "footer": {
                    "text": (f"recovery-agent · audit {audit_event_uuid} · alert {alert.id}")
                },
            }
        ],
    }


async def post_recovery_summary_to_discord(
    *,
    webhook_url: str,
    alert: AlertRow,
    classification: Classification,
    orphan_cids: tuple[str, ...],
    replay_result: ReplaySubprocessResult,
    audit_event_uuid: UUID,
    env: EnvName,
    http_client: httpx.AsyncClient,
) -> None:
    """POST the recovery summary to the operator's Discord #critical webhook.

    Best-effort: a Discord 5xx or network failure is logged at WARNING
    + propagated so the caller can decide whether to mark the alert
    resolved (today we do — the audit row + alerts UPDATE are the
    load-bearing safety-net writes; Discord push is enhancement).
    """
    payload = _build_discord_payload(
        alert=alert,
        classification=classification,
        orphan_cids=orphan_cids,
        replay_result=replay_result,
        audit_event_uuid=audit_event_uuid,
        env=env,
    )
    try:
        response = await http_client.post(webhook_url, json=payload)
        response.raise_for_status()
        log.info(
            "recovery_summary_discord_posted",
            alert_id=str(alert.id),
            audit_event_uuid=str(audit_event_uuid),
            status_code=response.status_code,
        )
    except httpx.HTTPError as exc:
        log.warning(
            "recovery_summary_discord_failed",
            alert_id=str(alert.id),
            audit_event_uuid=str(audit_event_uuid),
            error=str(exc),
            error_class=type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# Per-alert orchestration
# ---------------------------------------------------------------------------


ProcessOutcomeKind = Literal["processed", "skipped_dry_run", "failed"]


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    """Per-alert outcome surfaced to the tick-level summary."""

    alert_id: UUID
    kind: ProcessOutcomeKind
    classification: Classification | None
    audit_event_uuid: UUID | None
    replay_invoked: bool
    error: str | None = None


async def process_one_alert(
    *,
    alert: AlertRow,
    env: EnvName,
    session_factory: Any,
    webhook_url: str | None,
    dead_window_minutes: int,
    replay_timeout_seconds: float,
    http_client: httpx.AsyncClient,
    dry_run: bool,
    subprocess_run: SubprocessRunFn = subprocess.run,
) -> ProcessOutcome:
    """End-to-end recovery flow for one alerts row.

    Order matters (audit-first per backend-spec §2.10.1):

      1. Classify (pure function; no I/O)
      2. Fetch orphan CIDs (read-only)
      3. Audit-first: emit RECOVERY_ACTION_TAKEN
      4. Decision branch: invoke replay (if transient) OR skip
      5. UPDATE alerts row (acknowledged + resolved + outcome detail)
      6. POST Discord summary

    Steps 5 + 6 happen after step 3 so audit-first is preserved even
    if the subprocess crashes (step 3 lands, step 4 fails, steps 5-6
    skip — but the audit row is there as the operator's evidence).

    For ``dry_run=True``: classify + log but skip steps 3-6. Useful
    for operator-side sanity before enabling the timer.
    """
    # ---- 1. Classify ------------------------------------------------
    classification = classify_failure(
        exit_reason=alert.detail.get("exit_reason", "<unknown>"),
        exception_type=alert.detail.get("exception_type"),
        exception_repr=alert.detail.get("exception_repr"),
    )
    task_name = alert.detail.get("task_name", "<unknown>")
    log.info(
        "recovery_agent_classified_alert",
        alert_id=str(alert.id),
        task_name=task_name,
        classification=classification.kind,
        decision=classification.decision,
        classification_reason=classification.reason,
    )

    if dry_run:
        log.info(
            "recovery_agent_dry_run_skip",
            alert_id=str(alert.id),
            classification=classification.kind,
            decision=classification.decision,
        )
        return ProcessOutcome(
            alert_id=alert.id,
            kind="skipped_dry_run",
            classification=classification,
            audit_event_uuid=None,
            replay_invoked=False,
        )

    # ---- 2. Fetch orphan CIDs --------------------------------------
    orphan_cids: tuple[str, ...] = ()
    if classification.decision == "invoke_replay":
        async with session_factory() as orphan_session:
            orphan_cids = await fetch_orphan_client_order_ids(
                orphan_session,
                account_id=alert.account_id,
                alert_fired_at_utc=alert.fired_at_utc,
                dead_window_minutes=dead_window_minutes,
            )
        log.info(
            "recovery_agent_orphan_cids_fetched",
            alert_id=str(alert.id),
            orphan_cid_count=len(orphan_cids),
        )

    # ---- 3. Audit-first: emit RECOVERY_ACTION_TAKEN ----------------
    try:
        audit_record = await emit_recovery_action_audit(
            session_factory,
            alert=alert,
            classification=classification,
            orphan_cids=orphan_cids,
            env=env,
        )
    except Exception as exc:
        log.error(
            "recovery_agent_audit_emit_failed",
            alert_id=str(alert.id),
            error=str(exc),
            error_class=type(exc).__name__,
        )
        return ProcessOutcome(
            alert_id=alert.id,
            kind="failed",
            classification=classification,
            audit_event_uuid=None,
            replay_invoked=False,
            error=f"audit emit failed: {type(exc).__name__}: {exc}",
        )

    # ---- 4. Decision branch: invoke replay or skip -----------------
    if classification.decision == "invoke_replay":
        replay_result = await asyncio.to_thread(
            invoke_replay_executions,
            env=env,
            client_order_ids=orphan_cids,
            timeout_seconds=replay_timeout_seconds,
            subprocess_run=subprocess_run,
        )
    else:
        replay_result = ReplaySubprocessResult(
            invoked=False,
            exit_code=None,
            stdout_tail="",
            stderr_tail="",
            cids=(),
        )

    # ---- 5. UPDATE alerts row --------------------------------------
    try:
        async with session_factory() as update_session:
            await update_alert_handled(
                update_session,
                alert=alert,
                classification=classification,
                orphan_cids=orphan_cids,
                replay_result=replay_result,
                audit_event_uuid=UUID(str(audit_record.event_uuid)),
            )
    except Exception as exc:
        log.error(
            "recovery_agent_alert_update_failed",
            alert_id=str(alert.id),
            audit_event_uuid=str(audit_record.event_uuid),
            error=str(exc),
            error_class=type(exc).__name__,
        )
        # The audit row is durable; the alert stays un-acknowledged
        # for the next tick to re-decide. Surface the failure in the
        # tick-level outcome.
        return ProcessOutcome(
            alert_id=alert.id,
            kind="failed",
            classification=classification,
            audit_event_uuid=UUID(str(audit_record.event_uuid)),
            replay_invoked=replay_result.invoked,
            error=f"alert UPDATE failed: {type(exc).__name__}: {exc}",
        )

    # ---- 6. POST Discord summary -----------------------------------
    if webhook_url:
        await post_recovery_summary_to_discord(
            webhook_url=webhook_url,
            alert=alert,
            classification=classification,
            orphan_cids=orphan_cids,
            replay_result=replay_result,
            audit_event_uuid=UUID(str(audit_record.event_uuid)),
            env=env,
            http_client=http_client,
        )
    else:
        log.warning(
            "recovery_agent_no_critical_webhook_url",
            alert_id=str(alert.id),
            note=(
                f"{CRITICAL_WEBHOOK_URL_ENV} env var is unset; Discord "
                "#critical post skipped. Audit + alert UPDATE still landed."
            ),
        )

    return ProcessOutcome(
        alert_id=alert.id,
        kind="processed",
        classification=classification,
        audit_event_uuid=UUID(str(audit_record.event_uuid)),
        replay_invoked=replay_result.invoked,
    )


# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------


async def _build_session_factory(database_url: str) -> tuple[Any, Any]:
    """Build a one-shot SQLAlchemy async engine + sessionmaker.

    Mirrors the one-shot pattern from replay_executions.py + verify_chain.
    We do NOT reuse services.api.db's pool because the agent runs
    independently of the api lifespan.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(database_url, echo=False, pool_pre_ping=True)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    return engine, sessionmaker


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def _configure_logging() -> None:
    """Console renderer — systemd journal captures stdout; operator
    reads via journalctl + the recovery agent's own Discord post.
    """
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp_utc"),
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def _amain(
    args: ParsedArgs,
    *,
    session_factory_override: Any = None,
    subprocess_run: SubprocessRunFn = subprocess.run,
    http_client_override: httpx.AsyncClient | None = None,
) -> int:
    """Async entry point. Returns process exit code.

    Test hooks via the three overrides:

    * ``session_factory_override`` — skip the engine creation step
    * ``subprocess_run`` — fake the replay subprocess invocation
    * ``http_client_override`` — fake the httpx client for Discord POSTs
    """
    log.info(
        "recovery_agent_started",
        env=args.env,
        poll_window_minutes=args.poll_window_minutes,
        dead_window_minutes=args.dead_window_minutes,
        replay_timeout_seconds=args.replay_timeout_seconds,
        max_alerts_per_tick=args.max_alerts_per_tick,
        dry_run=args.dry_run,
    )

    webhook_url = os.environ.get(CRITICAL_WEBHOOK_URL_ENV)
    if not webhook_url and not args.dry_run:
        log.warning(
            "recovery_agent_no_critical_webhook_url",
            note=(
                f"{CRITICAL_WEBHOOK_URL_ENV} env var is unset. Audit + "
                "alert UPDATEs will still land but no Discord #critical "
                "post will fire. Wire /etc/trading/critical-webhook.url "
                "+ restart the systemd timer."
            ),
        )

    # ---- Build session factory --------------------------------------
    engine: Any = None
    session_factory: Any = session_factory_override
    if session_factory is None:
        database_url = os.environ.get(DATABASE_URL_ENV)
        if not database_url:
            log.error(
                "recovery_agent_db_init_failed",
                reason=f"{DATABASE_URL_ENV} is not set",
            )
            return EXIT_DB_INIT_FAILED
        try:
            engine, session_factory = await _build_session_factory(database_url)
        except Exception as exc:
            log.error(
                "recovery_agent_db_init_failed",
                error=str(exc),
                error_class=type(exc).__name__,
            )
            return EXIT_DB_INIT_FAILED

    # ---- Fetch unhandled alerts -------------------------------------
    try:
        async with session_factory() as fetch_session:
            alerts = await fetch_unhandled_alerts(
                fetch_session,
                window_minutes=args.poll_window_minutes,
                max_rows=args.max_alerts_per_tick,
            )
    except Exception as exc:
        log.error(
            "recovery_agent_fetch_failed",
            error=str(exc),
            error_class=type(exc).__name__,
        )
        if engine is not None:
            try:
                await engine.dispose()
            except Exception as dispose_exc:
                log.warning("recovery_agent_db_dispose_error", error=str(dispose_exc))
        return EXIT_UNEXPECTED

    log.info("recovery_agent_alerts_fetched", count=len(alerts))

    if not alerts:
        log.info("recovery_agent_no_alerts", env=args.env)
        if engine is not None:
            try:
                await engine.dispose()
            except Exception as exc:
                log.warning("recovery_agent_db_dispose_error", error=str(exc))
        return EXIT_OK

    # ---- Process each alert -----------------------------------------
    outcomes: list[ProcessOutcome] = []
    http_client = (
        http_client_override
        if http_client_override is not None
        else httpx.AsyncClient(timeout=httpx.Timeout(args.discord_timeout_seconds))
    )
    try:
        for alert in alerts:
            try:
                outcome = await process_one_alert(
                    alert=alert,
                    env=args.env,
                    session_factory=session_factory,
                    webhook_url=webhook_url,
                    dead_window_minutes=args.dead_window_minutes,
                    replay_timeout_seconds=args.replay_timeout_seconds,
                    http_client=http_client,
                    dry_run=args.dry_run,
                    subprocess_run=subprocess_run,
                )
            except Exception as exc:
                log.error(
                    "recovery_agent_alert_processing_failed",
                    alert_id=str(alert.id),
                    error=str(exc),
                    error_class=type(exc).__name__,
                )
                outcomes.append(
                    ProcessOutcome(
                        alert_id=alert.id,
                        kind="failed",
                        classification=None,
                        audit_event_uuid=None,
                        replay_invoked=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue
            outcomes.append(outcome)
    finally:
        if http_client_override is None:
            try:
                await http_client.aclose()
            except Exception as exc:
                log.warning("recovery_agent_http_client_close_error", error=str(exc))
        if engine is not None:
            try:
                await engine.dispose()
            except Exception as exc:
                log.warning("recovery_agent_db_dispose_error", error=str(exc))

    log.info(
        "recovery_agent_completed",
        alerts_processed=len(outcomes),
        outcomes=[
            {
                "alert_id": str(o.alert_id),
                "kind": o.kind,
                "classification": o.classification.kind if o.classification else None,
                "decision": o.classification.decision if o.classification else None,
                "audit_event_uuid": (str(o.audit_event_uuid) if o.audit_event_uuid else None),
                "replay_invoked": o.replay_invoked,
                "error": o.error,
            }
            for o in outcomes
        ],
    )
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Sync entry point. Returns exit code; never sys.exits."""
    _configure_logging()
    try:
        try:
            args = _parse_args(argv)
        except SystemExit as exc:
            # argparse exits with 2 on usage error; map to our EXIT_BAD_ARGS.
            return EXIT_BAD_ARGS if exc.code != 0 else EXIT_OK
        return asyncio.run(_amain(args))
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CRITICAL_WEBHOOK_URL_ENV",
    "DATABASE_URL_ENV",
    "DEFAULT_DEAD_WINDOW_MINUTES",
    "DEFAULT_DISCORD_TIMEOUT_SECONDS",
    "DEFAULT_MAX_ALERTS_PER_TICK",
    "DEFAULT_POLL_WINDOW_MINUTES",
    "DEFAULT_REPLAY_TIMEOUT_SECONDS",
    "EXIT_BAD_ARGS",
    "EXIT_DB_INIT_FAILED",
    "EXIT_OK",
    "EXIT_UNEXPECTED",
    "HARD_CRASH_EXCEPTION_TYPES",
    "SUBPROCESS_OUTPUT_TAIL_LINES",
    "TRANSIENT_EXCEPTION_TYPES",
    "TRANSIENT_IBKR_ERROR_MARKERS",
    "AlertRow",
    "Classification",
    "ClassificationKind",
    "Decision",
    "ParsedArgs",
    "ProcessOutcome",
    "ProcessOutcomeKind",
    "RecoveryAuditPayload",
    "ReplaySubprocessResult",
    "SubprocessRunFn",
    "_amain",
    "_build_discord_payload",
    "_build_parser",
    "_build_recovery_audit_payload",
    "_parse_args",
    "classify_failure",
    "emit_recovery_action_audit",
    "fetch_orphan_client_order_ids",
    "fetch_unhandled_alerts",
    "invoke_replay_executions",
    "main",
    "post_recovery_summary_to_discord",
    "process_one_alert",
    "update_alert_handled",
]
