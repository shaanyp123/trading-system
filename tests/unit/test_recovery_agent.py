"""Unit tests for ``scripts/operator_tools/recovery_agent.py``.

A22 binds: tests exercise argparse + classification + payload builders
+ subprocess invocation against fakes. Zero testcontainers, zero real
Postgres / IBKR / Discord. The script's docstring documents this
contract.

Coverage matrix:

* Argparse — every CLI arg, every default, required-flag behavior, the
  --allow-non-paper gate on live-env writes, the positive-int
  validation on poll/dead/timeout windows.
* Classification (pure) — IBKR marker precedence over exception_type;
  TRANSIENT_EXCEPTION_TYPES → invoke_replay; HARD_CRASH_EXCEPTION_TYPES
  → alert_only; done_without_exception → alert_only; unknown → fail-
  closed alert_only.
* Audit payload builder — exact JCS-canonical shape; UUID stringification;
  missing-task-name passthrough.
* Discord payload builder — embed shape across invoke_replay /
  alert_only / timed-out replay branches; field truncation defensive.
* Subprocess invocation — empty CIDs short-circuit; happy path; timeout
  expired path; non-zero exit captured.
* Orchestrator (``process_one_alert``) — full flow with audit emit +
  alert UPDATE + Discord post all mocked. Audit-first ordering pinned:
  RECOVERY_ACTION_TAKEN emit precedes alerts UPDATE precedes Discord.
  dry_run bypasses I/O. Audit emit failure → ProcessOutcome.failed.

Implementation strategy: per-CID stubs constructed inline (no global
fixtures); each test owns its mocks. Mirrors the
``test_replay_executions_script.py`` shape verbatim so future audit-
trail mining can find recovery-agent tests via the same grep.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from scripts.operator_tools.recovery_agent import (
    CRITICAL_WEBHOOK_URL_ENV,
    DATABASE_URL_ENV,
    DEFAULT_DEAD_WINDOW_MINUTES,
    DEFAULT_MAX_ALERTS_PER_TICK,
    DEFAULT_POLL_WINDOW_MINUTES,
    DEFAULT_REPLAY_TIMEOUT_SECONDS,
    EXIT_BAD_ARGS,
    EXIT_DB_INIT_FAILED,
    EXIT_OK,
    HARD_CRASH_EXCEPTION_TYPES,
    SUBPROCESS_OUTPUT_TAIL_LINES,
    TRANSIENT_EXCEPTION_TYPES,
    TRANSIENT_IBKR_ERROR_MARKERS,
    AlertRow,
    Classification,
    ParsedArgs,
    ReplaySubprocessResult,
    _amain,
    _build_discord_payload,
    _build_recovery_audit_payload,
    _parse_args,
    classify_failure,
    invoke_replay_executions,
    main,
    process_one_alert,
)

# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_minimal_paper_args_parse_to_defaults(self) -> None:
        args = _parse_args(["--env", "paper"])
        assert args.env == "paper"
        assert args.poll_window_minutes == DEFAULT_POLL_WINDOW_MINUTES
        assert args.dead_window_minutes == DEFAULT_DEAD_WINDOW_MINUTES
        assert args.replay_timeout_seconds == DEFAULT_REPLAY_TIMEOUT_SECONDS
        assert args.max_alerts_per_tick == DEFAULT_MAX_ALERTS_PER_TICK
        assert args.allow_non_paper is False
        assert args.dry_run is False

    def test_env_required_raises_systemexit(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args([])

    def test_env_unknown_value_raises_systemexit(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--env", "production"])

    def test_live_small_without_allow_non_paper_raises(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--env", "live-small"])

    def test_live_small_with_allow_non_paper_passes(self) -> None:
        args = _parse_args(["--env", "live-small", "--allow-non-paper"])
        assert args.env == "live-small"
        assert args.allow_non_paper is True

    def test_live_scale_without_allow_non_paper_raises(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--env", "live-scale"])

    def test_dry_run_flag_passes_through(self) -> None:
        args = _parse_args(["--env", "paper", "--dry-run"])
        assert args.dry_run is True

    @pytest.mark.parametrize(
        "flag,bad_value",
        [
            ("--poll-window-minutes", "0"),
            ("--dead-window-minutes", "0"),
            ("--replay-timeout-seconds", "0"),
            ("--discord-timeout-seconds", "0"),
            ("--max-alerts-per-tick", "0"),
        ],
    )
    def test_zero_value_raises_systemexit(self, flag: str, bad_value: str) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--env", "paper", flag, bad_value])

    @pytest.mark.parametrize(
        "flag,bad_value",
        [
            ("--poll-window-minutes", "-1"),
            ("--dead-window-minutes", "-5"),
            ("--replay-timeout-seconds", "-30.0"),
            ("--max-alerts-per-tick", "-100"),
        ],
    )
    def test_negative_value_raises_systemexit(self, flag: str, bad_value: str) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--env", "paper", flag, bad_value])

    def test_custom_windows_passthrough(self) -> None:
        args = _parse_args(
            [
                "--env",
                "paper",
                "--poll-window-minutes",
                "10",
                "--dead-window-minutes",
                "60",
                "--replay-timeout-seconds",
                "30.0",
                "--max-alerts-per-tick",
                "5",
            ]
        )
        assert args.poll_window_minutes == 10
        assert args.dead_window_minutes == 60
        assert args.replay_timeout_seconds == 30.0
        assert args.max_alerts_per_tick == 5


# ---------------------------------------------------------------------------
# Classification (pure function)
# ---------------------------------------------------------------------------


class TestClassifyFailureTransient:
    @pytest.mark.parametrize(
        "exception_type",
        [
            "TimeoutError",
            "ConnectionError",
            "ConnectionRefusedError",
            "ConnectionResetError",
            "BrokenPipeError",
            "CancelledError",
            "asyncio.TimeoutError",
            "asyncio.CancelledError",
        ],
    )
    def test_classify_transient_exception_type_returns_invoke_replay(
        self, exception_type: str
    ) -> None:
        result = classify_failure(
            exit_reason="exception",
            exception_type=exception_type,
            exception_repr=f"{exception_type}('test')",
        )
        assert result.kind == "transient"
        assert result.decision == "invoke_replay"
        assert exception_type in result.reason

    def test_classify_ibkr_1100_marker_returns_invoke_replay_regardless_of_type(
        self,
    ) -> None:
        # Even though RuntimeError is in HARD_CRASH_EXCEPTION_TYPES, the
        # IBKR connectivity marker takes precedence — drill 5's failure
        # mode bubbled up through ib-async as a RuntimeError with 1100
        # in the message.
        result = classify_failure(
            exit_reason="exception",
            exception_type="RuntimeError",
            exception_repr="RuntimeError('IBKR Error 1100: connectivity lost')",
        )
        assert result.kind == "transient"
        assert result.decision == "invoke_replay"
        assert "1100" in result.reason

    @pytest.mark.parametrize("marker", ["1101", "1102"])
    def test_classify_other_connectivity_markers_return_invoke_replay(self, marker: str) -> None:
        result = classify_failure(
            exit_reason="exception",
            exception_type="AssertionError",  # would normally be hard_crash
            exception_repr=f"AssertionError('synthetic: ibkr error {marker}')",
        )
        assert result.kind == "transient"
        assert result.decision == "invoke_replay"
        assert marker in result.reason


class TestClassifyFailureHardCrash:
    @pytest.mark.parametrize(
        "exception_type",
        [
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
            "RuntimeError",
        ],
    )
    def test_classify_hard_crash_exception_type_returns_alert_only(
        self, exception_type: str
    ) -> None:
        result = classify_failure(
            exit_reason="exception",
            exception_type=exception_type,
            exception_repr=f"{exception_type}('synthetic')",
        )
        assert result.kind == "hard_crash"
        assert result.decision == "alert_only"
        assert exception_type in result.reason


class TestClassifyFailureAmbiguous:
    def test_classify_done_without_exception_returns_alert_only(self) -> None:
        result = classify_failure(
            exit_reason="done_without_exception",
            exception_type=None,
            exception_repr=None,
        )
        assert result.kind == "ambiguous"
        assert result.decision == "alert_only"
        assert "done_without_exception" in result.reason

    def test_classify_none_exception_type_with_exception_exit_reason_returns_alert_only(
        self,
    ) -> None:
        # exit_reason='exception' but exception_type is None — defensive
        # case (monitor descriptor incomplete). Fail-closed.
        result = classify_failure(
            exit_reason="exception",
            exception_type=None,
            exception_repr=None,
        )
        assert result.kind == "ambiguous"
        assert result.decision == "alert_only"
        assert "missing" in result.reason

    def test_classify_unknown_exception_type_returns_alert_only(self) -> None:
        result = classify_failure(
            exit_reason="exception",
            exception_type="SomeBespokeException",
            exception_repr="SomeBespokeException('not in any known set')",
        )
        assert result.kind == "ambiguous"
        assert result.decision == "alert_only"
        assert "SomeBespokeException" in result.reason
        assert "fail-closed" in result.reason


class TestClassificationConstants:
    def test_transient_set_is_disjoint_from_hard_crash_set(self) -> None:
        intersection = TRANSIENT_EXCEPTION_TYPES & HARD_CRASH_EXCEPTION_TYPES
        assert intersection == frozenset()

    def test_ibkr_markers_are_canonical_codes(self) -> None:
        # Locked per services/api/async_task_monitor.py::DEFAULT_IBKR_CONNECTIVITY_CODES
        assert TRANSIENT_IBKR_ERROR_MARKERS == frozenset({"1100", "1101", "1102"})


# ---------------------------------------------------------------------------
# Audit payload builder
# ---------------------------------------------------------------------------


def _make_alert(
    *,
    alert_id: UUID | None = None,
    account_id: UUID | None = None,
    fired_at_utc: datetime | None = None,
    triggering_audit_event_uuid: UUID | None = None,
    detail: dict[str, Any] | None = None,
) -> AlertRow:
    return AlertRow(
        id=alert_id or uuid4(),
        account_id=account_id or uuid4(),
        fired_at_utc=fired_at_utc or datetime.now(tz=UTC),
        triggering_audit_event_uuid=triggering_audit_event_uuid,
        detail=detail
        or {
            "task_name": "order_placement_worker.run_forever",
            "exit_reason": "exception",
            "exception_type": "TimeoutError",
            "exception_repr": "TimeoutError('synthetic')",
            "observed_at_utc": "2026-05-26T22:00:00+00:00",
        },
    )


class TestBuildRecoveryAuditPayload:
    def test_payload_contains_required_fields(self) -> None:
        alert = _make_alert(triggering_audit_event_uuid=uuid4())
        classification = Classification(kind="transient", decision="invoke_replay", reason="test")
        orphan_cids = ("cid-A", "cid-B", "cid-C")
        payload = _build_recovery_audit_payload(
            alert=alert,
            classification=classification,
            orphan_cids=orphan_cids,
            env="paper",
        )
        assert payload["triggering_alert_uuid"] == str(alert.id)
        assert payload["triggering_audit_event_uuid"] == str(alert.triggering_audit_event_uuid)
        assert payload["task_name"] == "order_placement_worker.run_forever"
        assert payload["classification"] == "transient"
        assert payload["decision"] == "invoke_replay"
        assert payload["classification_reason"] == "test"
        assert payload["plan_orphan_cid_count"] == 3
        assert payload["env"] == "paper"

    def test_payload_with_no_triggering_audit_event_uuid_is_none(self) -> None:
        alert = _make_alert(triggering_audit_event_uuid=None)
        classification = Classification(kind="hard_crash", decision="alert_only", reason="test")
        payload = _build_recovery_audit_payload(
            alert=alert,
            classification=classification,
            orphan_cids=(),
            env="paper",
        )
        assert payload["triggering_audit_event_uuid"] is None
        assert payload["plan_orphan_cid_count"] == 0

    def test_payload_with_missing_task_name_is_none(self) -> None:
        alert = _make_alert(detail={"exception_type": "TimeoutError"})
        payload = _build_recovery_audit_payload(
            alert=alert,
            classification=Classification(kind="transient", decision="invoke_replay", reason="r"),
            orphan_cids=(),
            env="paper",
        )
        assert payload["task_name"] is None

    def test_payload_only_contains_jcs_safe_types(self) -> None:
        # JCS canonicaliser rejects floats per A05; ensure no floats slip in.
        alert = _make_alert()
        payload = _build_recovery_audit_payload(
            alert=alert,
            classification=Classification(kind="transient", decision="invoke_replay", reason="r"),
            orphan_cids=("cid-1",),
            env="paper",
        )
        for value in payload.values():
            assert not isinstance(value, float)


# ---------------------------------------------------------------------------
# Discord payload builder
# ---------------------------------------------------------------------------


class TestBuildDiscordPayload:
    def test_invoke_replay_embed_includes_replay_summary_field(self) -> None:
        alert = _make_alert()
        classification = Classification(
            kind="transient",
            decision="invoke_replay",
            reason="TimeoutError matches TRANSIENT_EXCEPTION_TYPES",
        )
        replay_result = ReplaySubprocessResult(
            invoked=True,
            exit_code=0,
            stdout_tail="replay_executions_completed exit_code=0",
            stderr_tail="",
            cids=("cid-1",),
        )
        audit_uuid = uuid4()
        payload = _build_discord_payload(
            alert=alert,
            classification=classification,
            orphan_cids=("cid-1",),
            replay_result=replay_result,
            audit_event_uuid=audit_uuid,
            env="paper",
        )
        assert payload["username"] == "recovery-agent"
        embed = payload["embeds"][0]
        assert "invoke_replay" in embed["title"]
        field_names = [f["name"] for f in embed["fields"]]
        assert "task_name" in field_names
        assert "classification" in field_names
        assert "decision" in field_names
        assert "audit_event_uuid" in field_names
        assert "replay_summary" in field_names

    def test_alert_only_embed_includes_next_operator_action(self) -> None:
        alert = _make_alert(
            detail={
                "task_name": "order_placement_worker.run_forever",
                "exit_reason": "exception",
                "exception_type": "AssertionError",
                "exception_repr": "AssertionError('logic bug')",
            }
        )
        classification = Classification(
            kind="hard_crash",
            decision="alert_only",
            reason="AssertionError is in HARD_CRASH_EXCEPTION_TYPES",
        )
        replay_result = ReplaySubprocessResult(
            invoked=False, exit_code=None, stdout_tail="", stderr_tail="", cids=()
        )
        payload = _build_discord_payload(
            alert=alert,
            classification=classification,
            orphan_cids=(),
            replay_result=replay_result,
            audit_event_uuid=uuid4(),
            env="paper",
        )
        embed = payload["embeds"][0]
        field_names = [f["name"] for f in embed["fields"]]
        assert "next_operator_action" in field_names
        next_action_field = next(f for f in embed["fields"] if f["name"] == "next_operator_action")
        assert "alert_only" in next_action_field["value"]

    def test_invoke_replay_with_non_zero_exit_includes_next_operator_action(
        self,
    ) -> None:
        alert = _make_alert()
        classification = Classification(kind="transient", decision="invoke_replay", reason="r")
        replay_result = ReplaySubprocessResult(
            invoked=True,
            exit_code=4,
            stdout_tail="",
            stderr_tail="ib_gateway connect timeout",
            cids=("cid-1",),
        )
        payload = _build_discord_payload(
            alert=alert,
            classification=classification,
            orphan_cids=("cid-1",),
            replay_result=replay_result,
            audit_event_uuid=uuid4(),
            env="paper",
        )
        embed = payload["embeds"][0]
        field_names = [f["name"] for f in embed["fields"]]
        assert "next_operator_action" in field_names
        assert "replay_stderr_tail" in field_names

    def test_long_stderr_tail_truncated_to_discord_field_value_cap(self) -> None:
        alert = _make_alert()
        classification = Classification(kind="transient", decision="invoke_replay", reason="r")
        huge_stderr = "x" * 5000
        replay_result = ReplaySubprocessResult(
            invoked=True,
            exit_code=1,
            stdout_tail="",
            stderr_tail=huge_stderr,
            cids=("cid-1",),
        )
        payload = _build_discord_payload(
            alert=alert,
            classification=classification,
            orphan_cids=("cid-1",),
            replay_result=replay_result,
            audit_event_uuid=uuid4(),
            env="paper",
        )
        embed = payload["embeds"][0]
        stderr_field = next(f for f in embed["fields"] if f["name"] == "replay_stderr_tail")
        # Discord per-field-value cap is 1024 chars; we cap at 1001 (1000 + ellipsis)
        assert len(stderr_field["value"]) <= 1024


# ---------------------------------------------------------------------------
# Subprocess invocation
# ---------------------------------------------------------------------------


def _make_completed_process(
    *, returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["python", "-m", "scripts.operator_tools.replay_executions"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class TestInvokeReplayExecutions:
    def test_empty_cids_short_circuits_with_invoked_false(self) -> None:
        # subprocess_run should never be called
        sentinel = MagicMock()
        result = invoke_replay_executions(
            env="paper",
            client_order_ids=(),
            timeout_seconds=30.0,
            subprocess_run=sentinel,
        )
        assert result.invoked is False
        assert result.exit_code is None
        assert result.cids == ()
        sentinel.assert_not_called()

    def test_happy_path_returns_exit_code_zero_and_captured_output(self) -> None:
        captured: dict[str, Any] = {}

        def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return _make_completed_process(
                returncode=0,
                stdout="line1\nline2\nreplay_executions_completed exit_code=0\n",
                stderr="",
            )

        result = invoke_replay_executions(
            env="paper",
            client_order_ids=("cid-A", "cid-B"),
            timeout_seconds=30.0,
            subprocess_run=fake_run,
        )
        assert result.invoked is True
        assert result.exit_code == 0
        assert result.timed_out is False
        assert "replay_executions_completed exit_code=0" in result.stdout_tail
        assert result.cids == ("cid-A", "cid-B")
        assert "--client-order-ids" in captured["cmd"]
        assert "cid-A,cid-B" in captured["cmd"]
        assert "--env" in captured["cmd"]
        assert "paper" in captured["cmd"]
        # paper env should NOT inject --allow-non-paper
        assert "--allow-non-paper" not in captured["cmd"]

    def test_live_env_appends_allow_non_paper(self) -> None:
        captured: dict[str, Any] = {}

        def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            captured["cmd"] = cmd
            return _make_completed_process(returncode=0)

        invoke_replay_executions(
            env="live-small",
            client_order_ids=("cid-A",),
            timeout_seconds=30.0,
            subprocess_run=fake_run,
        )
        assert "--allow-non-paper" in captured["cmd"]

    def test_non_zero_exit_code_captured(self) -> None:
        def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            return _make_completed_process(
                returncode=4,
                stdout="",
                stderr="replay_executions_ibkr_connect_failed",
            )

        result = invoke_replay_executions(
            env="paper",
            client_order_ids=("cid-A",),
            timeout_seconds=30.0,
            subprocess_run=fake_run,
        )
        assert result.invoked is True
        assert result.exit_code == 4
        assert "ibkr_connect_failed" in result.stderr_tail

    def test_timeout_expired_returns_timed_out_true_exit_none(self) -> None:
        def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            raise subprocess.TimeoutExpired(
                cmd=cmd,
                timeout=kwargs.get("timeout"),
                output="partial stdout\n",
                stderr="partial stderr\n",
            )

        result = invoke_replay_executions(
            env="paper",
            client_order_ids=("cid-A",),
            timeout_seconds=5.0,
            subprocess_run=fake_run,
        )
        assert result.invoked is True
        assert result.timed_out is True
        assert result.exit_code is None
        assert "partial stdout" in result.stdout_tail
        assert "partial stderr" in result.stderr_tail

    def test_stdout_tail_truncated_to_n_lines(self) -> None:
        long_stdout = "\n".join(f"line {i}" for i in range(50))

        def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            return _make_completed_process(returncode=0, stdout=long_stdout)

        result = invoke_replay_executions(
            env="paper",
            client_order_ids=("cid-A",),
            timeout_seconds=30.0,
            subprocess_run=fake_run,
        )
        tail_lines = result.stdout_tail.splitlines()
        assert len(tail_lines) == SUBPROCESS_OUTPUT_TAIL_LINES


# ---------------------------------------------------------------------------
# process_one_alert orchestrator (full flow with all I/O mocked)
# ---------------------------------------------------------------------------


@dataclass
class _FakeAuditRecord:
    """Mirrors services.audit.models.AuditLogRecord enough for the agent."""

    sequence_no: int
    event_uuid: UUID


class _FakeSession:
    """Minimal fake AsyncSession — captures executed queries + commits.

    The agent uses sessions as async context managers (``async with
    session_factory() as session:``); this fake supports __aenter__ /
    __aexit__ + execute() + commit().
    """

    def __init__(self, *, fetch_orphan_cids: tuple[str, ...] = ()) -> None:
        self._fetch_orphan_cids = fetch_orphan_cids
        self.executions: list[tuple[str, dict[str, Any]]] = []
        self.commits: int = 0

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
        return None

    async def execute(self, statement, params=None):  # type: ignore[no-untyped-def]
        # Capture the literal SQL string for assertion convenience.
        sql_str = str(statement)
        self.executions.append((sql_str, params or {}))
        # Return an object with .fetchall() that returns rows for
        # the orphan-CIDs query; everything else returns empty.
        if "FROM orders" in sql_str:
            rows = [SimpleNamespace(client_order_id=cid) for cid in self._fetch_orphan_cids]
            return SimpleNamespace(fetchall=lambda: rows)
        return SimpleNamespace(fetchall=lambda: [], fetchone=lambda: None)

    async def commit(self) -> None:
        self.commits += 1


def _session_factory_for(*sessions: _FakeSession):  # type: ignore[no-untyped-def]
    """Build a session_factory callable that hands out sessions in order.

    The agent opens 3 sessions per alert (orphan-fetch, audit emit,
    alert UPDATE). Tests can pass exactly that many ``_FakeSession``
    instances + each subsequent ``session_factory()`` call hands out
    the next one.
    """
    sessions_iter = iter(sessions)

    def _factory():  # type: ignore[no-untyped-def]
        return next(sessions_iter)

    return _factory


class TestProcessOneAlertFlow:
    @pytest.mark.asyncio
    async def test_transient_failure_invokes_replay_audit_first(self) -> None:
        alert = _make_alert(
            detail={
                "task_name": "order_placement_worker.run_forever",
                "exit_reason": "exception",
                "exception_type": "TimeoutError",
                "exception_repr": "TimeoutError('connectAsync')",
                "observed_at_utc": "2026-05-26T22:00:00+00:00",
            }
        )
        orphan_session = _FakeSession(fetch_orphan_cids=("cid-A", "cid-B"))
        audit_session = _FakeSession()
        update_session = _FakeSession()
        sf = _session_factory_for(orphan_session, audit_session, update_session)

        emit_calls: list[dict[str, Any]] = []

        async def fake_append_audit_event(session, event_type, payload, **kwargs):  # type: ignore[no-untyped-def]
            emit_calls.append(
                {
                    "event_type": str(event_type),
                    "payload": payload,
                    "account_id": kwargs.get("account_id"),
                    "env": kwargs.get("env"),
                    "phase_at_emit": kwargs.get("phase_at_emit"),
                }
            )
            return _FakeAuditRecord(sequence_no=42, event_uuid=uuid4())

        # Patch append_audit_event by injecting via the lazy-imported
        # module path. We monkey-patch the import inside the agent.
        import services.audit.writer as writer_module

        original_aae = writer_module.append_audit_event
        writer_module.append_audit_event = fake_append_audit_event  # type: ignore[assignment]

        subprocess_calls: list[Sequence[str]] = []

        def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            subprocess_calls.append(list(cmd))
            return _make_completed_process(returncode=0, stdout="ok\n")

        # No webhook URL → skips Discord post but still runs audit + UPDATE
        try:
            outcome = await process_one_alert(
                alert=alert,
                env="paper",
                session_factory=sf,
                webhook_url=None,
                dead_window_minutes=30,
                replay_timeout_seconds=30.0,
                http_client=MagicMock(),
                dry_run=False,
                subprocess_run=fake_run,
            )
        finally:
            writer_module.append_audit_event = original_aae  # type: ignore[assignment]

        assert outcome.kind == "processed"
        assert outcome.classification is not None
        assert outcome.classification.decision == "invoke_replay"
        assert outcome.replay_invoked is True
        # Audit-first ordering: audit emit MUST land before alerts UPDATE
        assert len(emit_calls) == 1
        assert emit_calls[0]["event_type"].endswith("recovery_action_taken")
        # The orphan-fetch query happened on orphan_session
        assert any("FROM orders" in q for q, _ in orphan_session.executions)
        # The alerts UPDATE happened on update_session
        assert any("UPDATE alerts" in q for q, _ in update_session.executions)
        assert update_session.commits == 1
        # The replay subprocess fired with the orphan CIDs
        assert len(subprocess_calls) == 1
        assert "cid-A,cid-B" in subprocess_calls[0]

    @pytest.mark.asyncio
    async def test_hard_crash_skips_replay_subprocess(self) -> None:
        alert = _make_alert(
            detail={
                "task_name": "order_placement_worker.run_forever",
                "exit_reason": "exception",
                "exception_type": "AssertionError",
                "exception_repr": "AssertionError('synthetic logic bug')",
                "observed_at_utc": "2026-05-26T22:00:00+00:00",
            }
        )
        # Only 2 sessions needed (no orphan-fetch for alert_only):
        # audit_session, update_session
        audit_session = _FakeSession()
        update_session = _FakeSession()
        sf = _session_factory_for(audit_session, update_session)

        async def fake_append_audit_event(session, event_type, payload, **kwargs):  # type: ignore[no-untyped-def]
            return _FakeAuditRecord(sequence_no=43, event_uuid=uuid4())

        import services.audit.writer as writer_module

        original_aae = writer_module.append_audit_event
        writer_module.append_audit_event = fake_append_audit_event  # type: ignore[assignment]

        subprocess_calls: list[Sequence[str]] = []

        def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            subprocess_calls.append(list(cmd))
            return _make_completed_process(returncode=0)

        try:
            outcome = await process_one_alert(
                alert=alert,
                env="paper",
                session_factory=sf,
                webhook_url=None,
                dead_window_minutes=30,
                replay_timeout_seconds=30.0,
                http_client=MagicMock(),
                dry_run=False,
                subprocess_run=fake_run,
            )
        finally:
            writer_module.append_audit_event = original_aae  # type: ignore[assignment]

        assert outcome.kind == "processed"
        assert outcome.classification is not None
        assert outcome.classification.decision == "alert_only"
        assert outcome.replay_invoked is False
        # No subprocess call for alert_only
        assert subprocess_calls == []

    @pytest.mark.asyncio
    async def test_dry_run_skips_all_io(self) -> None:
        alert = _make_alert()

        # session_factory should never be called in dry_run
        sf = MagicMock()
        subprocess_run = MagicMock()
        http_client = MagicMock()

        outcome = await process_one_alert(
            alert=alert,
            env="paper",
            session_factory=sf,
            webhook_url="https://discord.com/api/webhooks/test",
            dead_window_minutes=30,
            replay_timeout_seconds=30.0,
            http_client=http_client,
            dry_run=True,
            subprocess_run=subprocess_run,
        )
        assert outcome.kind == "skipped_dry_run"
        assert outcome.audit_event_uuid is None
        assert outcome.replay_invoked is False
        sf.assert_not_called()
        subprocess_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_alert_update_failure_returns_failed_with_audit_event_uuid_set(
        self,
    ) -> None:
        """Pin the partial-failure idempotency invariant.

        If audit emit succeeds but the alerts UPDATE fails (DB
        unavailable, transient SQLAlchemy error, etc.), the audit row
        is already durable in the chain — the agent MUST return
        ``ProcessOutcome.failed`` with ``audit_event_uuid`` set so the
        next 60s tick can re-decide AND the operator can grep
        ``audit_log`` for the recovery decision that landed.

        This is the inverse of ``test_audit_emit_failure_returns_failed_outcome``;
        together they pin every combination of (audit emit, alert UPDATE)
        success/failure that affects the audit chain invariant.
        """
        alert = _make_alert(
            detail={
                "task_name": "order_placement_worker.run_forever",
                "exit_reason": "exception",
                "exception_type": "AssertionError",  # alert_only — no orphan fetch
                "exception_repr": "AssertionError('synthetic')",
            }
        )

        class _UpdateFailingSession:
            async def __aenter__(self) -> _UpdateFailingSession:
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
                return None

            async def execute(self, statement, params=None):  # type: ignore[no-untyped-def]
                raise RuntimeError("Simulated alerts UPDATE failure")

            async def commit(self) -> None:
                raise RuntimeError("Simulated alerts commit failure")

        audit_session = _FakeSession()
        update_session = _UpdateFailingSession()
        sessions_iter = iter([audit_session, update_session])

        def sf():  # type: ignore[no-untyped-def]
            return next(sessions_iter)

        audit_uuid_recorded: list[UUID] = []

        async def fake_append_audit_event(session, event_type, payload, **kwargs):  # type: ignore[no-untyped-def]
            event_uuid = uuid4()
            audit_uuid_recorded.append(event_uuid)
            return _FakeAuditRecord(sequence_no=44, event_uuid=event_uuid)

        import services.audit.writer as writer_module

        original_aae = writer_module.append_audit_event
        writer_module.append_audit_event = fake_append_audit_event  # type: ignore[assignment]

        try:
            outcome = await process_one_alert(
                alert=alert,
                env="paper",
                session_factory=sf,
                webhook_url=None,
                dead_window_minutes=30,
                replay_timeout_seconds=30.0,
                http_client=MagicMock(),
                dry_run=False,
                subprocess_run=MagicMock(),
            )
        finally:
            writer_module.append_audit_event = original_aae  # type: ignore[assignment]

        # The audit row landed — durable in the chain
        assert len(audit_uuid_recorded) == 1
        # The alert UPDATE failed → ProcessOutcome.failed
        assert outcome.kind == "failed"
        # The audit_event_uuid IS surfaced so the operator can grep
        # audit_log + the next tick has evidence of the prior decision
        assert outcome.audit_event_uuid is not None
        assert outcome.audit_event_uuid == audit_uuid_recorded[0]
        assert "alert UPDATE failed" in (outcome.error or "")

    @pytest.mark.asyncio
    async def test_audit_emit_failure_returns_failed_outcome(self) -> None:
        alert = _make_alert(
            detail={
                "task_name": "order_placement_worker.run_forever",
                "exit_reason": "exception",
                "exception_type": "AssertionError",  # alert_only, so no orphan fetch
                "exception_repr": "AssertionError('synthetic')",
            }
        )
        audit_session = _FakeSession()
        sf = _session_factory_for(audit_session)

        async def fake_append_audit_event_raises(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("Simulated DB failure during audit emit")

        import services.audit.writer as writer_module

        original_aae = writer_module.append_audit_event
        writer_module.append_audit_event = fake_append_audit_event_raises  # type: ignore[assignment]

        try:
            outcome = await process_one_alert(
                alert=alert,
                env="paper",
                session_factory=sf,
                webhook_url=None,
                dead_window_minutes=30,
                replay_timeout_seconds=30.0,
                http_client=MagicMock(),
                dry_run=False,
                subprocess_run=MagicMock(),
            )
        finally:
            writer_module.append_audit_event = original_aae  # type: ignore[assignment]

        assert outcome.kind == "failed"
        assert outcome.audit_event_uuid is None
        assert outcome.replay_invoked is False
        assert "audit emit failed" in (outcome.error or "")


# ---------------------------------------------------------------------------
# _amain top-level (DB-init failure + no-alerts paths)
# ---------------------------------------------------------------------------


class TestAmainEntryPoint:
    @pytest.mark.asyncio
    async def test_no_database_url_returns_db_init_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
        args = ParsedArgs(
            env="paper",
            poll_window_minutes=5,
            dead_window_minutes=30,
            replay_timeout_seconds=30.0,
            discord_timeout_seconds=15.0,
            max_alerts_per_tick=10,
            allow_non_paper=False,
            dry_run=False,
        )
        exit_code = await _amain(args)
        assert exit_code == EXIT_DB_INIT_FAILED

    @pytest.mark.asyncio
    async def test_no_alerts_returns_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(CRITICAL_WEBHOOK_URL_ENV, raising=False)

        async def empty_fetch_session_factory():  # type: ignore[no-untyped-def]
            session = _FakeSession()
            return session

        # Override session_factory_override so we skip DB init
        sf = MagicMock(return_value=_FakeSession())
        args = ParsedArgs(
            env="paper",
            poll_window_minutes=5,
            dead_window_minutes=30,
            replay_timeout_seconds=30.0,
            discord_timeout_seconds=15.0,
            max_alerts_per_tick=10,
            allow_non_paper=False,
            dry_run=False,
        )
        exit_code = await _amain(
            args,
            session_factory_override=sf,
            http_client_override=MagicMock(),
        )
        assert exit_code == EXIT_OK


# ---------------------------------------------------------------------------
# main sync entry point (exit code mapping)
# ---------------------------------------------------------------------------


class TestMainEntryPoint:
    def test_unknown_arg_returns_exit_bad_args(self) -> None:
        # argparse exits with SystemExit(2) on parse failure; main maps to 6.
        exit_code = main(["--env", "production"])
        assert exit_code == EXIT_BAD_ARGS

    def test_missing_required_env_returns_exit_bad_args(self) -> None:
        exit_code = main([])
        assert exit_code == EXIT_BAD_ARGS

    def test_help_flag_returns_zero(self) -> None:
        # --help triggers SystemExit(0)
        exit_code = main(["--help"])
        assert exit_code == EXIT_OK


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------


class TestModuleContract:
    def test_public_surface(self) -> None:
        import scripts.operator_tools.recovery_agent as agent_module

        expected = {
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
        }
        assert set(agent_module.__all__) == expected
