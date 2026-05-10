"""services/api/schemas/alerts.py — `/api/alerts` schema.

Backend-spec §4.1.5b ``Alert`` + ``AlertsResponse`` + the locked
``AlertCategory`` literal (mirrors Postgres enum from alembic 0004 §3.27).

Underlying table is ``alerts`` (alembic 0004 §3.27). The webhook_pusher
service (PR #44) writes alerts via planner→sender→dispatcher; status derives
from the ``acknowledged`` boolean + ``resolved_at_utc`` nullable timestamp:

  * ``status = "open"``         when ``NOT acknowledged AND resolved_at_utc IS NULL``
  * ``status = "acknowledged"`` when ``acknowledged AND resolved_at_utc IS NULL``
  * ``status = "resolved"``     when ``resolved_at_utc IS NOT NULL``

Phase 0 the table is empty — no kill-switch transitions, no recon breaks,
no audit-write failures have surfaced in production yet. The list endpoint
returns ``alerts=[]``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

# 29 values; mirrors the locked Postgres enum from alembic 0004 lines 311-342.
# Order MUST match the enum file order so OpenAPI emits a deterministic schema.
AlertCategory = Literal[
    "kill_switch_invoked",
    "kill_switch_resumed",
    "halt_dwell_warning",
    "audit_write_failure",
    "audit_chain_break",
    "qc_objectstore_degraded",
    "broker_disconnect",
    "reconciliation_break",
    "margin_warn",
    "margin_auto_trim",
    "data_quality_reject",
    "data_quality_quarantine",
    "vol_regime_z_high",
    "capacity_warning",
    "slippage_drift",
    "model_decay",
    "capital_event",
    "parameter_change_proposed",
    "parameter_change_reverted",
    "pr_drafted",
    "pr_rejected",
    "liveness_probe_missed",
    "engagement_timeout",
    "cost_alert_soft",
    "cost_alert_hard",
    "external_watchdog_alert",
    "incident_review_required",
    "phase_cutover_started",
    "maintenance_window",
]

AlertSeverity = Literal["P0", "P1", "P2"]
AlertStatus = Literal["open", "acknowledged", "resolved"]


class Alert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alert_uuid: str
    severity: AlertSeverity
    category: AlertCategory
    title: str
    body_md: str
    status: AlertStatus
    fired_at: datetime
    acknowledged_at: datetime | None
    resolved_at: datetime | None
    audit_event_uuid: str | None


class AlertsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alerts: list[Alert]
    next_cursor: str | None
    has_more: bool


__all__ = [
    "Alert",
    "AlertCategory",
    "AlertSeverity",
    "AlertStatus",
    "AlertsResponse",
]
