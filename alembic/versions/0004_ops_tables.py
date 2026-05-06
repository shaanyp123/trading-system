"""Ops tables: reconciliation, data quality, agent actions, vacation, capital, cost, liveness, PDT, dividends, incidents, universe, alerts, macro events.

Implements backend-spec §§3.15-3.28 (minus §3.29 contracts, which lands in 0002
as a FK target for signals/orders/trades).

The `alert_category` enum mirrors the locked taxonomy in §3.27. New categories
require a schema migration AND a corresponding Pydantic Literal update — do
NOT add an alert kind without going through both surfaces.

The spec wording for `universe_state.exclusion_reason` includes `NULL` inside
the IN list, which is invalid SQL semantically (column IN (NULL) evaluates to
NULL, not TRUE/FALSE). Rewritten here as `IS NULL OR IN (...)` to preserve
intent (column may be NULL or one of the named codes).

Revision ID: 0004_ops_tables
Revises: 0003_risk_tables
Create Date: 2026-05-05 11:45:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_ops_tables"
down_revision: str | None = "0003_risk_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # reconciliation_breaks (§3.15) — created early; pdt_day_trade_log FKs to it
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE reconciliation_breaks (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            account_id UUID NOT NULL REFERENCES accounts(id),
            detected_at_utc TIMESTAMPTZ NOT NULL,
            metric TEXT NOT NULL,
            market TEXT,
            contract_id UUID REFERENCES contracts(id),
            expected NUMERIC(30,8),
            actual NUMERIC(30,8),
            delta NUMERIC(30,8),
            tolerance NUMERIC(30,8),
            source TEXT NOT NULL,
            resolved_at_utc TIMESTAMPTZ,
            resolution_path TEXT
                CHECK (resolution_path IN
                    ('grace_period','manual','kill_switch','tolerance_widened_dividend')),
            audit_event_uuid UUID NOT NULL
        )
        """
    )

    # ------------------------------------------------------------------
    # data_quality_events (§3.16)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE data_quality_events (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            market TEXT NOT NULL,
            bar_close_ts TIMESTAMPTZ,
            detected_at_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
            event_type TEXT NOT NULL CHECK (event_type IN ('reject','quarantine')),
            reason TEXT NOT NULL,
            raw_bar JSONB NOT NULL,
            audit_event_uuid UUID NOT NULL
        )
        """
    )

    # ------------------------------------------------------------------
    # agent_actions (§3.17)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE agent_actions (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            account_id UUID NOT NULL REFERENCES accounts(id),
            invoked_at_utc TIMESTAMPTZ NOT NULL,
            action_type TEXT NOT NULL
                CHECK (action_type IN (
                    'tighten_parameter','invoke_defensive_trim','invoke_kill_switch',
                    'draft_pr','deploy_hotfix','generate_briefing','query_audit',
                    'summarize_costs'
                )),
            agent_decision_id TEXT NOT NULL,
            prompt_cache_hit_pct NUMERIC(5,2),
            cost_usd NUMERIC(10,4),
            result TEXT NOT NULL
                CHECK (result IN ('success','partial','failed','reverted')),
            result_detail JSONB,
            audit_event_uuid UUID NOT NULL,
            reverted_at_utc TIMESTAMPTZ,
            revert_reason TEXT
        )
        """
    )
    op.execute("CREATE INDEX agent_actions_invoked ON agent_actions(invoked_at_utc DESC)")

    # ------------------------------------------------------------------
    # vacation_mode (§3.18)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE vacation_mode (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            account_id UUID NOT NULL REFERENCES accounts(id),
            started_at_utc TIMESTAMPTZ NOT NULL,
            scheduled_end_utc TIMESTAMPTZ NOT NULL,
            ended_at_utc TIMESTAMPTZ,
            ended_by TEXT
                CHECK (ended_by IN ('expiry','operator_command','operator_web')),
            audit_event_uuid_start UUID NOT NULL,
            audit_event_uuid_end UUID
        )
        """
    )
    op.execute(
        "CREATE INDEX vacation_active "
        "ON vacation_mode(account_id, ended_at_utc) WHERE ended_at_utc IS NULL"
    )

    # ------------------------------------------------------------------
    # qc_adapter_cursor (§3.19)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE qc_adapter_cursor (
            directory_path TEXT PRIMARY KEY,
            last_consumed_sequence_no BIGINT NOT NULL,
            last_consumed_at_utc TIMESTAMPTZ NOT NULL,
            bytes_consumed BIGINT NOT NULL DEFAULT 0,
            consecutive_failures INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    op.execute(
        """
        INSERT INTO qc_adapter_cursor
            (directory_path, last_consumed_sequence_no, last_consumed_at_utc,
             bytes_consumed, consecutive_failures)
        VALUES
            ('/events/', 0, now(), 0, 0),
            ('/state/portfolio.json', 0, now(), 0, 0),
            ('/instruction_acks/', 0, now(), 0, 0)
        """
    )

    # ------------------------------------------------------------------
    # capital_events (§3.20)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE capital_events (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            account_id UUID NOT NULL REFERENCES accounts(id),
            event_type TEXT NOT NULL CHECK (event_type IN ('deposit','withdrawal')),
            amount_usd NUMERIC(20,4) NOT NULL,
            pre_event_equity NUMERIC(20,4) NOT NULL,
            post_event_equity NUMERIC(20,4) NOT NULL,
            pct_of_pre_equity NUMERIC(10,8) NOT NULL,
            threshold_met BOOLEAN NOT NULL,
            effective_at_utc TIMESTAMPTZ NOT NULL,
            dd_baseline_reset_to NUMERIC(20,4),
            capital_event_mode_session_start INTEGER,
            audit_event_uuid UUID NOT NULL
        )
        """
    )

    # ------------------------------------------------------------------
    # cost_events (§3.21)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE cost_events (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            provider TEXT NOT NULL,
            observed_at_utc TIMESTAMPTZ NOT NULL,
            amount_usd NUMERIC(10,4) NOT NULL,
            rolling_30d_total NUMERIC(10,4),
            threshold_breached TEXT
                CHECK (threshold_breached IN ('soft_200','hard_300')
                       OR threshold_breached IS NULL),
            source TEXT NOT NULL
        )
        """
    )

    # ------------------------------------------------------------------
    # liveness_probes (§3.22)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE liveness_probes (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            account_id UUID NOT NULL REFERENCES accounts(id),
            sent_at_utc TIMESTAMPTZ NOT NULL,
            channel TEXT NOT NULL CHECK (channel IN ('discord','email','web')),
            acknowledged_at_utc TIMESTAMPTZ,
            ack_method TEXT
                CHECK (ack_method IN ('reaction','reply','web_activity','email_reply')),
            audit_event_uuid_sent UUID NOT NULL,
            audit_event_uuid_ack UUID
        )
        """
    )

    # ------------------------------------------------------------------
    # pdt_day_trade_log (§3.23)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE pdt_day_trade_log (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            account_id UUID NOT NULL REFERENCES accounts(id),
            trade_date_nyse DATE NOT NULL,
            market TEXT NOT NULL,
            fills JSONB NOT NULL,
            is_day_trade BOOLEAN NOT NULL,
            source TEXT NOT NULL
                CHECK (source IN ('qc_objectstore','tws_api','flexquery_eod')),
            reconciled_with_flexquery BOOLEAN NOT NULL DEFAULT FALSE,
            reconciliation_break_id UUID REFERENCES reconciliation_breaks(id),
            audit_event_uuid UUID NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX pdt_log_account_date ON pdt_day_trade_log(account_id, trade_date_nyse)"
    )

    # ------------------------------------------------------------------
    # dividend_history (§3.24)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE dividend_history (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            symbol TEXT NOT NULL,
            ex_date DATE NOT NULL,
            pay_date DATE,
            amount_usd_per_share NUMERIC(20,8) NOT NULL,
            source TEXT NOT NULL,
            ingested_at_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
            audit_event_uuid UUID,
            UNIQUE(symbol, ex_date)
        )
        """
    )

    # ------------------------------------------------------------------
    # incident_reviews (§3.25)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE incident_reviews (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            triggering_audit_event_uuid UUID NOT NULL,
            triggering_alert_uuid UUID,
            state_transition_to TEXT NOT NULL,
            authored_at_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
            authored_by TEXT NOT NULL,
            write_up_text TEXT NOT NULL CHECK (length(write_up_text) >= 100),
            resolved BOOLEAN NOT NULL DEFAULT FALSE,
            resolved_at_utc TIMESTAMPTZ,
            resume_audit_event_uuid UUID
        )
        """
    )

    # ------------------------------------------------------------------
    # universe_state (§3.26)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE universe_state (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            account_id UUID NOT NULL REFERENCES accounts(id),
            market TEXT NOT NULL,
            is_active BOOLEAN NOT NULL,
            exclusion_reason TEXT
                CHECK (exclusion_reason IS NULL OR exclusion_reason IN (
                    'single_contract_notional_exceeds_50pct_equity',
                    'data_executability_failed'
                )),
            single_contract_notional NUMERIC(20,4),
            current_equity NUMERIC(20,4),
            evaluated_at_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
            audit_event_uuid UUID NOT NULL,
            UNIQUE(account_id, market, evaluated_at_utc)
        )
        """
    )
    op.execute(
        "CREATE INDEX universe_active "
        "ON universe_state(account_id, is_active, evaluated_at_utc DESC)"
    )

    # ------------------------------------------------------------------
    # alerts + alert_category enum (§3.27)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TYPE alert_category AS ENUM (
            'kill_switch_invoked',
            'kill_switch_resumed',
            'halt_dwell_warning',
            'audit_write_failure',
            'audit_chain_break',
            'qc_objectstore_degraded',
            'broker_disconnect',
            'reconciliation_break',
            'margin_warn',
            'margin_auto_trim',
            'data_quality_reject',
            'data_quality_quarantine',
            'vol_regime_z_high',
            'capacity_warning',
            'slippage_drift',
            'model_decay',
            'capital_event',
            'parameter_change_proposed',
            'parameter_change_reverted',
            'pr_drafted',
            'pr_rejected',
            'liveness_probe_missed',
            'engagement_timeout',
            'cost_alert_soft',
            'cost_alert_hard',
            'external_watchdog_alert',
            'incident_review_required',
            'phase_cutover_started',
            'maintenance_window'
        )
        """
    )
    op.execute(
        """
        CREATE TABLE alerts (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            account_id UUID NOT NULL REFERENCES accounts(id),
            fired_at_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
            severity TEXT NOT NULL CHECK (severity IN ('P0','P1','P2')),
            category alert_category NOT NULL,
            message TEXT NOT NULL,
            detail JSONB,
            delivery_status JSONB,
            acknowledged BOOLEAN NOT NULL DEFAULT FALSE,
            acknowledged_at_utc TIMESTAMPTZ,
            resolved_at_utc TIMESTAMPTZ,
            triggering_audit_event_uuid UUID
        )
        """
    )
    op.execute(
        "CREATE INDEX alerts_severity_unack "
        "ON alerts(severity, acknowledged) WHERE NOT acknowledged"
    )

    # ------------------------------------------------------------------
    # macro_events (§3.28)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE macro_events (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            imported_at_utc TIMESTAMPTZ NOT NULL,
            source TEXT NOT NULL
                CHECK (source IN ('forex_factory','trading_economics','manual')),
            event_name TEXT NOT NULL,
            tier1_classification TEXT,
            scheduled_at_utc TIMESTAMPTZ NOT NULL,
            region TEXT,
            currency CHAR(3),
            importance TEXT CHECK (importance IN ('high','medium','low')),
            ratified_at_utc TIMESTAMPTZ,
            ratified_by TEXT,
            audit_event_uuid_import UUID NOT NULL,
            audit_event_uuid_ratify UUID
        )
        """
    )
    op.execute("CREATE INDEX macro_events_scheduled ON macro_events(scheduled_at_utc)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS macro_events")
    op.execute("DROP TABLE IF EXISTS alerts")
    op.execute("DROP TYPE IF EXISTS alert_category")
    op.execute("DROP TABLE IF EXISTS universe_state")
    op.execute("DROP TABLE IF EXISTS incident_reviews")
    op.execute("DROP TABLE IF EXISTS dividend_history")
    op.execute("DROP TABLE IF EXISTS pdt_day_trade_log")
    op.execute("DROP TABLE IF EXISTS liveness_probes")
    op.execute("DROP TABLE IF EXISTS cost_events")
    op.execute("DROP TABLE IF EXISTS capital_events")
    op.execute("DROP TABLE IF EXISTS qc_adapter_cursor")
    op.execute("DROP TABLE IF EXISTS vacation_mode")
    op.execute("DROP TABLE IF EXISTS agent_actions")
    op.execute("DROP TABLE IF EXISTS data_quality_events")
    op.execute("DROP TABLE IF EXISTS reconciliation_breaks")
