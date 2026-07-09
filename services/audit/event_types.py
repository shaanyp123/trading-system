"""Locked audit event type taxonomy — Python enum mirror of backend-spec §3.30.

Every value in :class:`AuditEventType` corresponds 1:1 to an entry in the
spec's locked taxonomy table. New event types REQUIRE:

1. An update to ``Docs/backend-spec.md`` §3.30 (PR through forbidden-paths
   review per dev-guide §11 [A02] — ``services/audit/**``).
2. The same value added to this enum.
3. At least one test that emits the new type and reads it back (anti-pattern
   A04: "DO NOT introduce a new audit event_type without adding it to the
   locked taxonomy enum and writing at least one test that emits and reads
   back").

The string values are **stable wire identifiers** that flow into:

* ``audit_log.event_type`` (TEXT column, indexed for query)
* QC ObjectStore JSONL ``event_type`` field (backend-spec §4.5.1)
* SSE envelopes for client consumption (backend-spec §4.2)
* Discord summaries / decision-diary references

Renaming an existing value is therefore a wire-breaking change; treat it as a
schema migration with the same gating bar as adding a new column. Use the
``StrEnum`` base so equality with raw strings (e.g. from JSONL ingestion)
works without explicit ``.value`` access.

Callers wishing to write an audit event MUST use this enum (or a string that
is a member of this enum's value set) and MUST go through
:func:`services.audit.writer.append_audit_event`. Direct INSERTs into
``audit_log`` are forbidden by anti-pattern A01.

Pre-PR #28 modules (``services/risk/state_machine.py``,
``services/scheduler/vacation.py``, ``services/scheduler/calendar_import.py``)
emit raw ``Literal[str]`` event-type names because this module did not yet
exist when those PRs landed. The writer accepts either form (enum or string)
and validates string inputs against this enum's value set so those modules
do not need to be retrofit.
"""

from __future__ import annotations

from enum import StrEnum


class AuditEventType(StrEnum):
    """Canonical taxonomy of audit_log event_type values (backend-spec §3.30)."""

    # ---------- Lifecycle / state ----------
    SYSTEM_STARTED = "system_started"
    SYSTEM_STOPPED = "system_stopped"
    MIGRATION_APPLIED = "migration_applied"
    PHASE_CUTOVER_STARTED = "phase_cutover_started"
    PHASE_CUTOVER_COMPLETED = "phase_cutover_completed"

    # ---------- Strategy / signals ----------
    SIGNAL_EMITTED = "signal_emitted"
    SIGNAL_APPROVED = "signal_approved"
    SIGNAL_REJECTED = "signal_rejected"
    SIGNAL_DEFERRED = "signal_deferred"
    SIGNAL_EXPIRED = "signal_expired"
    BULK_APPROVE_INVOKED = "bulk_approve_invoked"
    TRADE_REALIZED = "trade_realized"
    SIGNAL_ANOMALY_FLAGGED = "signal_anomaly_flagged"
    MARKET_DROP_SETTLEMENT_UNAVAILABLE = "market_drop_settlement_unavailable"
    MACRO_WINDOW_DROP = "macro_window_drop"

    # ---------- Orders / execution ----------
    ORDER_PLACED = "order_placed"
    ORDER_FILLED = "order_filled"
    ORDER_PARTIALLY_FILLED = "order_partially_filled"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_REJECTED = "order_rejected"
    ORDER_RETRY_ATTEMPTED = "order_retry_attempted"
    MANUAL_CLOSE_INVOKED = "manual_close_invoked"
    ROLL_INITIATED = "roll_initiated"
    ROLL_COMPLETED = "roll_completed"
    # ---------- Position / balance / trade lifecycle ----------
    # Emitted by the fill processor (services/risk/fill_processor.py)
    # downstream of an IBKR Filled status. Each fill produces one
    # POSITION_* event + BALANCE_SNAPSHOT_RECORDED + TRADE_*; the
    # ORDER_FILLED row above remains the broker-side confirmation. See
    # backend-spec §3.30 for the locked taxonomy.
    # PR-I also emits BALANCE_SNAPSHOT_RECORDED + POSITION_MARK_TO_MARKET
    # for the per-recon broker-snapshot refresh path BEFORE the recon
    # planner runs.
    POSITION_OPENED = "position_opened"
    POSITION_UPDATED = "position_updated"
    POSITION_CLOSED = "position_closed"
    POSITION_MARK_TO_MARKET = "position_mark_to_market"
    BALANCE_SNAPSHOT_RECORDED = "balance_snapshot_recorded"
    TRADE_OPENED = "trade_opened"
    TRADE_CLOSED = "trade_closed"
    ATTRIBUTION_ROLLUP_RECORDED = "attribution_rollup_recorded"

    # ---------- Risk / state machine ----------
    STATE_TRANSITION_NORMAL_TO_HALT = "state_transition_normal_to_halt"
    STATE_TRANSITION_HALT_TO_CONVALESCENT = "state_transition_halt_to_convalescent"
    STATE_TRANSITION_CONVALESCENT_TO_NORMAL = "state_transition_convalescent_to_normal"
    KILL_SWITCH_TRIGGERED = "kill_switch_triggered"
    DEFENSIVE_TRIM_INVOKED = "defensive_trim_invoked"
    MARGIN_AUTO_TRIM_INVOKED = "margin_auto_trim_invoked"
    CONVALESCENT_COUNTER_RESET = "convalescent_counter_reset"
    DECOMMISSION_FLOOR_TRIGGERED = "decommission_floor_triggered"

    # ---------- Capital / equity ----------
    CAPITAL_EVENT_DEPOSIT = "capital_event_deposit"
    CAPITAL_EVENT_WITHDRAWAL = "capital_event_withdrawal"
    CAPITAL_EVENT_MODE_STARTED = "capital_event_mode_started"
    CAPITAL_EVENT_MODE_ENDED = "capital_event_mode_ended"
    DD_BASELINE_RESET = "dd_baseline_reset"
    PEAK_MTM_UPDATED = "peak_mtm_updated"

    # ---------- Universe / parameters ----------
    UNIVERSE_EXCLUSION = "universe_exclusion"
    UNIVERSE_INCLUSION = "universe_inclusion"
    PARAMETER_CHANGE_PROPOSED = "parameter_change_proposed"
    PARAMETER_CHANGE_APPLIED = "parameter_change_applied"
    PARAMETER_CHANGE_REVERTED = "parameter_change_reverted"
    PR_DRAFTED = "pr_drafted"
    PR_APPROVED = "pr_approved"
    PR_REJECTED = "pr_rejected"
    PR_MERGED = "pr_merged"
    STRATEGY_VERSION_DEPLOYED = "strategy_version_deployed"
    STRATEGY_VERSION_DECOMMISSIONED = "strategy_version_decommissioned"
    SLIPPAGE_CALIBRATION_RECALIBRATED = "slippage_calibration_recalibrated"

    # ---------- Reconciliation / data quality ----------
    RECONCILIATION_CHECK_PASSED = "reconciliation_check_passed"
    RECONCILIATION_BREAK_DETECTED = "reconciliation_break_detected"
    RECONCILIATION_BREAK_RESOLVED = "reconciliation_break_resolved"
    # Option C recon-fix follow-up (2026-05-29). Historically emitted by
    # the IBKR-era EOD cycle when its per-cycle reqPositions fetch
    # failed terminally and the cycle silently fell back to the
    # FlexQuery position list (durable breadcrumb + paired ``alerts``
    # row of category ``reconciliation_data_source_degraded``, P1 →
    # Discord #alerts). That producer died with the IBKR stack
    # (crypto-pivot C0-B2b) and the FlexQuery path was deleted in C0
    # §3.5 — the value is DORMANT but stays per the locked audit
    # taxonomy ([A04]); the Phase C1 intraday-probe fallback is its
    # natural future producer.
    RECONCILIATION_DATA_SOURCE_DEGRADED = "reconciliation_data_source_degraded"
    DATA_QUALITY_REJECT = "data_quality_reject"
    DATA_QUALITY_QUARANTINE = "data_quality_quarantine"
    PSD_REPAIR_APPLIED = "psd_repair_applied"

    # ---------- Communications / engagement ----------
    LIVENESS_PROBE_SENT = "liveness_probe_sent"
    LIVENESS_PROBE_ACKNOWLEDGED = "liveness_probe_acknowledged"
    ENGAGEMENT_TIMEOUT_TRIGGERED = "engagement_timeout_triggered"
    DISCORD_DELIVERY_FAILED = "discord_delivery_failed"
    EMAIL_BACKUP_SENT = "email_backup_sent"

    # ---------- Vacation / calendar ----------
    VACATION_STARTED = "vacation_started"
    VACATION_ENDED = "vacation_ended"
    CALENDAR_IMPORTED = "calendar_imported"
    CALENDAR_RATIFIED = "calendar_ratified"
    CALENDAR_UNRATIFIED = "calendar_unratified"
    CALENDAR_SERVICE_OUTAGE = "calendar_service_outage"

    # ---------- Auth / security ----------
    WEBAUTHN_REGISTERED = "webauthn_registered"
    WEBAUTHN_LOGIN = "webauthn_login"
    TOTP_LOGIN = "totp_login"
    BACKUP_CODE_USED = "backup_code_used"
    SESSION_EVICTED = "session_evicted"
    RE_AUTH_REQUIRED = "re_auth_required"
    RE_AUTH_PASSED = "re_auth_passed"
    BREAKGLASS_INVOKED = "breakglass_invoked"
    SECRETS_ROTATED = "secrets_rotated"

    # ---------- Agent ----------
    AGENT_DECISION_MADE = "agent_decision_made"
    AGENT_HOT_FIX_DEPLOYED = "agent_hot_fix_deployed"
    AGENT_HOT_FIX_ROLLED_BACK = "agent_hot_fix_rolled_back"
    AGENT_PR_DRAFTED = "agent_pr_drafted"
    AGENT_ACTION_FAILED = "agent_action_failed"

    # ---------- System health ----------
    SERVICE_DEGRADED = "service_degraded"
    SERVICE_RECOVERED = "service_recovered"
    COST_ALERT_SOFT_CEILING = "cost_alert_soft_ceiling"
    COST_ALERT_HARD_CEILING = "cost_alert_hard_ceiling"
    EXTERNAL_WATCHDOG_ALERT = "external_watchdog_alert"
    INCIDENT_REVIEW_LOGGED = "incident_review_logged"
    AUDIT_CHAIN_INTEGRITY_VERIFIED = "audit_chain_integrity_verified"
    AUDIT_REPAIR_APPLIED = "audit_repair_applied"
    # PR #154 follow-up (2026-05-16): emitted by the heartbeat staleness
    # probe (services/api/heartbeat_probe.py) when a tracked cron-like
    # service hasn't recorded a heartbeat within its locked threshold
    # (HEARTBEAT_STALE_THRESHOLDS_S in services/api/heartbeats.py).
    # Single durable breadcrumb per stale-detection cycle; dedup +
    # cooldown live on the probe side.
    HEARTBEAT_STALE_DETECTED = "heartbeat_stale_detected"
    # Recovery-agent follow-up to drill 5 (2026-05-18) + drill 6 (2026-05-25).
    # Emitted by ``services/api/async_task_monitor.py`` when a tracked
    # lifespan task (today: order_placement_worker.run_forever) transitions
    # to .done() with an exception OR a clean exit no caller was awaiting.
    # Pairs with the ``alerts`` row (category='worker_failure') INSERTed by
    # the same monitor hook; the audit row is the durable record + the
    # alerts row is the recovery-agent inbox. Drill 5's manual
    # ``/tmp/drill5_recovery.py`` step lives behind this event in v1.
    ASYNC_TASK_DIED = "async_task_died"
    # Emitted by ``scripts/operator_tools/recovery_agent.py`` BEFORE it
    # mutates state (alerts UPDATE + subprocess replay_executions.py).
    # Payload carries ``triggering_alert_uuid``, ``triggering_audit_event_uuid``
    # (the ASYNC_TASK_DIED row this responds to), ``decision``
    # ('invoke_replay' | 'alert_only'), ``task_name``, ``classification``
    # ('transient' | 'hard_crash' | 'ambiguous'), ``plan``. Preserves
    # audit-first ordering (backend-spec §2.10.1) on the recovery agent's
    # actions; the replay subprocess itself emits its own audit chain via
    # ``process_fill_event``.
    RECOVERY_ACTION_TAKEN = "recovery_action_taken"
    # Exit-pipeline PR-C (2026-05-27). Emitted by the order_placement_worker
    # exit branch when the bracket-stop CANCEL succeeds but the subsequent
    # close-order PLACE fails. The position is NAKED (no protective stop)
    # until the operator intervenes — the worst failure mode in the exit
    # pipeline (R3 in ``Docs/exit-pipeline-design.md`` §11). Payload carries
    # ``signal_id`` (the exit signal), ``account_id``, ``market``,
    # ``prior_position_direction`` ('long'/'short' — derived from the FRESH
    # positions_current read), ``prior_position_quantity`` (signed magnitude
    # from positions_current at fail-time), ``cancelled_stop_client_order_id``,
    # ``cancelled_stop_broker_order_id``, ``last_known_stop_price`` (the
    # bracket stop's price right before cancel), ``close_client_order_id``,
    # ``close_failure_reason``, ``linked_cancel_audit_event_uuid``, and
    # ``observed_at_utc``. Note: ``exit_reason`` is DELIBERATELY omitted
    # because PR-B persists it only in the SIGNAL_EMITTED audit payload
    # (audit_log.payload_jcs is BYTEA, not JSONB; signals has no
    # audit_event_uuid back-pointer) — operator deep-links from the
    # Discord embed's signal_id to /signals for full context.
    # Pairs with an ``alerts`` row of category ``position_unprotected``
    # severity ``P0`` → Discord #alerts + #critical + Resend email.
    # Operator-runbook: ``scripts/operator_tools/replace_protective_stop.py``
    # (deferred PR per design §Q5) places a fresh bracket-stop at the same
    # ATR-derived level until the next cycle re-emits the exit. Until that
    # tool lands, the operator manually flattens or places a stop via TWS
    # + writes a manual audit row through the agent-actions surface.
    POSITION_UNPROTECTED = "position_unprotected"


__all__ = ["AuditEventType"]
