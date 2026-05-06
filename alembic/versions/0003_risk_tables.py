"""Risk + parameter + strategy + diary tables; close deferred FKs from 0002.

Implements backend-spec §§3.10-3.14 (strategy_versions, parameters,
parameter_sets, slippage_calibration_versions, decision_diary, risk_state)
and adds the FKs that 0002 deferred:
    - signals.slippage_calibration_version_id -> slippage_calibration_versions
    - signals.decision_diary_entry_id          -> decision_diary
    - trades.slippage_calibration_version_id  -> slippage_calibration_versions

Revision ID: 0003_risk_tables
Revises: 0002_core_tables
Create Date: 2026-05-05 11:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_risk_tables"
down_revision: str | None = "0002_core_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # strategy_versions (backend-spec §3.10)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE strategy_versions (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            strategy_hash CHAR(40) NOT NULL UNIQUE,
            short_hash CHAR(7) NOT NULL UNIQUE,
            strategy_name TEXT NOT NULL,
            branch TEXT NOT NULL,
            deployed_at_utc TIMESTAMPTZ NOT NULL,
            deployed_by TEXT NOT NULL CHECK (deployed_by IN ('operator', 'agent')),
            deploy_method TEXT NOT NULL
                CHECK (deploy_method IN ('pr_merge', 'agent_hot_fix')),
            parent_version_short_hash CHAR(7) REFERENCES strategy_versions(short_hash),
            backtest_baseline_id UUID,
            parameter_set_hash TEXT NOT NULL,
            slippage_calibration_version TEXT NOT NULL,
            paper_days_required INTEGER NOT NULL DEFAULT 30,
            paper_days_completed INTEGER NOT NULL DEFAULT 0,
            decommissioned BOOLEAN NOT NULL DEFAULT false,
            decommissioned_at_utc TIMESTAMPTZ,
            decommissioned_reason TEXT
        )
        """
    )
    op.execute("CREATE INDEX strategy_versions_short_hash ON strategy_versions(short_hash)")

    # ------------------------------------------------------------------
    # parameters + parameter_sets (backend-spec §3.11)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE parameters (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            parameter_name TEXT NOT NULL,
            parameter_value JSONB NOT NULL,
            valid_from TIMESTAMPTZ NOT NULL,
            valid_to TIMESTAMPTZ,
            changed_by TEXT NOT NULL
                CHECK (changed_by IN ('agent','operator','init','revert')),
            change_reason TEXT,
            parameter_set_hash CHAR(64) NOT NULL,
            prev_parameter_set_hash CHAR(64),
            audit_event_uuid UUID NOT NULL,
            pr_url TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX parameters_valid_at ON parameters(parameter_name, valid_from DESC)")

    op.execute(
        """
        CREATE TABLE parameter_sets (
            parameter_set_hash CHAR(64) PRIMARY KEY,
            parameters JSONB NOT NULL,
            first_active_at TIMESTAMPTZ NOT NULL,
            last_active_at TIMESTAMPTZ
        )
        """
    )

    # ------------------------------------------------------------------
    # slippage_calibration_versions (backend-spec §3.12)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE slippage_calibration_versions (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            version_no INTEGER NOT NULL UNIQUE,
            is_head BOOLEAN NOT NULL DEFAULT FALSE,
            calibrated_at_utc TIMESTAMPTZ NOT NULL,
            trigger TEXT NOT NULL CHECK (trigger IN
                ('bootstrap','scheduled_monthly','scheduled_quarterly','manual')),
            per_market_coefficients JSONB NOT NULL,
            data_window_start TIMESTAMPTZ,
            data_window_end TIMESTAMPTZ,
            audit_event_uuid UUID NOT NULL,
            notes TEXT
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX slippage_head "
        "ON slippage_calibration_versions(is_head) WHERE is_head = TRUE"
    )

    # ------------------------------------------------------------------
    # decision_diary (backend-spec §3.13)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE decision_diary (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            account_id UUID NOT NULL REFERENCES accounts(id),
            entry_class TEXT NOT NULL
                CHECK (entry_class IN ('signal_response','forward_looking','general')),
            linked_signal_id UUID REFERENCES signals(id),
            linked_market_id TEXT,
            tag TEXT NOT NULL
                CHECK (tag IN ('data_concern','regime_concern','size_concern',
                              'manual_judgment','other')),
            ts_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
            monotonic_ns BIGINT,
            author TEXT NOT NULL CHECK (author IN ('operator','agent')),
            reasoning_text TEXT NOT NULL,
            CONSTRAINT operator_min_10 CHECK (
                author <> 'operator' OR length(reasoning_text) >= 10
            ),
            CONSTRAINT signal_response_requires_link CHECK (
                entry_class <> 'signal_response' OR linked_signal_id IS NOT NULL
            ),
            CONSTRAINT non_signal_response_no_link CHECK (
                entry_class = 'signal_response' OR linked_signal_id IS NULL
            )
        )
        """
    )
    op.execute("CREATE INDEX decision_diary_signal ON decision_diary(linked_signal_id)")
    op.execute("CREATE INDEX decision_diary_ts ON decision_diary(ts_utc DESC)")

    # ------------------------------------------------------------------
    # risk_state (backend-spec §3.14) — single-current + history
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE risk_state (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            account_id UUID NOT NULL REFERENCES accounts(id),
            state TEXT NOT NULL
                CHECK (state IN ('NORMAL','HALT_NEW','CONVALESCENT')),
            severity TEXT
                CHECK (severity IN ('routine','defensive_envelope','incident_review')
                       OR state <> 'HALT_NEW'),
            reason TEXT,
            entered_at_utc TIMESTAMPTZ NOT NULL,
            convalescent_session_count INTEGER NOT NULL DEFAULT 0,
            capital_event_active_until_session_no INTEGER,
            capital_event_vol_normalized_at_session_no INTEGER,
            monthly_dd_breached_for_calendar_month CHAR(7),
            vacation_active BOOLEAN NOT NULL DEFAULT FALSE,
            vacation_until_utc TIMESTAMPTZ,
            audit_event_uuid UUID NOT NULL,
            is_current BOOLEAN NOT NULL DEFAULT FALSE
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX risk_state_current "
        "ON risk_state(account_id, is_current) WHERE is_current = TRUE"
    )

    # ------------------------------------------------------------------
    # Close deferred FKs from 0002
    # ------------------------------------------------------------------
    op.execute(
        "ALTER TABLE signals "
        "ADD CONSTRAINT signals_slippage_calibration_fk "
        "FOREIGN KEY (slippage_calibration_version_id) "
        "REFERENCES slippage_calibration_versions(id)"
    )
    op.execute(
        "ALTER TABLE signals "
        "ADD CONSTRAINT signals_decision_diary_fk "
        "FOREIGN KEY (decision_diary_entry_id) REFERENCES decision_diary(id)"
    )
    op.execute(
        "ALTER TABLE trades "
        "ADD CONSTRAINT trades_slippage_calibration_fk "
        "FOREIGN KEY (slippage_calibration_version_id) "
        "REFERENCES slippage_calibration_versions(id)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_slippage_calibration_fk")
    op.execute("ALTER TABLE signals DROP CONSTRAINT IF EXISTS signals_decision_diary_fk")
    op.execute("ALTER TABLE signals DROP CONSTRAINT IF EXISTS signals_slippage_calibration_fk")
    op.execute("DROP TABLE IF EXISTS risk_state")
    op.execute("DROP TABLE IF EXISTS decision_diary")
    op.execute("DROP TABLE IF EXISTS slippage_calibration_versions")
    op.execute("DROP TABLE IF EXISTS parameter_sets")
    op.execute("DROP TABLE IF EXISTS parameters")
    op.execute("DROP TABLE IF EXISTS strategy_versions")
