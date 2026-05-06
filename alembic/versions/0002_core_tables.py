"""Core trading-flow tables: accounts, contracts, signals, orders, fills, trades, attribution, positions, balances.

Implements backend-spec §§3.1, 3.1.1, 3.3-3.9, 3.29.

Forward FKs deferred to 0003:
    - signals.slippage_calibration_version_id -> slippage_calibration_versions
    - signals.decision_diary_entry_id          -> decision_diary
    - trades.slippage_calibration_version_id  -> slippage_calibration_versions
The columns exist here; the FK constraints are added in 0003 once the target
tables are created. signals/orders/fills/trades all carry contract_id even
where the spec marks the FK "futures only" — Postgres NULLs satisfy the FK.

Raw SQL is used throughout to keep these migrations 1:1 with backend-spec §3.
The spec is the source of truth; comparing SQL blocks side-by-side is the
review path.

Revision ID: 0002_core_tables
Revises: 0001_audit_log
Create Date: 2026-05-05 11:15:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_core_tables"
down_revision: str | None = "0001_audit_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PARTITION_YEARS: tuple[int, ...] = (2026, 2027, 2028, 2029, 2030, 2031)


def _create_yearly_partitions(parent_table: str, partition_column: str) -> None:
    for year in _PARTITION_YEARS:
        op.execute(
            f"""
            CREATE TABLE {parent_table}_y{year} PARTITION OF {parent_table}
            FOR VALUES FROM ('{year}-01-01') TO ('{year + 1}-01-01')
            """
        )
    # `partition_column` is referenced for documentation / future-author clarity:
    _ = partition_column


def upgrade() -> None:
    # ------------------------------------------------------------------
    # accounts (backend-spec §3.1)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE accounts (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            external_account_id TEXT NOT NULL UNIQUE,
            account_type TEXT NOT NULL CHECK (account_type IN ('individual','llc','prop')),
            base_currency CHAR(3) NOT NULL DEFAULT 'USD',
            reg_t_eligible BOOLEAN NOT NULL DEFAULT TRUE,
            span_eligible BOOLEAN NOT NULL DEFAULT TRUE,
            pdt_eligible BOOLEAN NOT NULL DEFAULT TRUE,
            role TEXT NOT NULL DEFAULT 'owner' CHECK (role IN ('owner', 'reader')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            active_from TIMESTAMPTZ NOT NULL,
            active_to TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX accounts_active_unique "
        "ON accounts(external_account_id) WHERE active_to IS NULL"
    )

    # Now that accounts exists, add the FK from audit_log.
    op.execute(
        "ALTER TABLE audit_log "
        "ADD CONSTRAINT audit_log_account_id_fkey "
        "FOREIGN KEY (account_id) REFERENCES accounts(id)"
    )

    # ------------------------------------------------------------------
    # setup_tokens (backend-spec §3.1.1)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE setup_tokens (
            token_uuid UUID PRIMARY KEY,
            token_hash TEXT NOT NULL,
            intended_role TEXT NOT NULL CHECK (intended_role IN ('owner', 'reader')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            consumed_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_setup_tokens_unconsumed "
        "ON setup_tokens(consumed_at) WHERE consumed_at IS NULL"
    )

    # ------------------------------------------------------------------
    # contracts (backend-spec §3.29) — futures contract metadata, FK target
    # for signals/orders/trades.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE contracts (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            market_root TEXT NOT NULL,
            ibkr_local_symbol TEXT NOT NULL,
            ibkr_con_id BIGINT,
            expiry_date DATE NOT NULL,
            point_value NUMERIC(20,8) NOT NULL,
            multiplier NUMERIC(20,8) NOT NULL DEFAULT 1,
            exchange TEXT NOT NULL,
            exchange_calendar_code TEXT NOT NULL,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            UNIQUE(market_root, expiry_date)
        )
        """
    )

    # ------------------------------------------------------------------
    # signals (backend-spec §3.3)
    # FKs to slippage_calibration_versions and decision_diary deferred to 0003.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE signals (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            account_id UUID NOT NULL REFERENCES accounts(id),
            env TEXT NOT NULL,
            market TEXT NOT NULL,
            contract_id UUID REFERENCES contracts(id),
            emitted_at_utc TIMESTAMPTZ NOT NULL,
            session_date DATE NOT NULL,
            direction TEXT NOT NULL CHECK (direction IN ('long','short','flat')),
            signal_type TEXT NOT NULL,
            strategy_hash CHAR(40) NOT NULL,
            parameter_set_hash CHAR(64) NOT NULL,
            slippage_calibration_version_id UUID NOT NULL,
            decision_price NUMERIC(20,8) NOT NULL,
            expected_slippage_bps NUMERIC(10,4),
            expected_fill_price NUMERIC(20,8),
            target_contracts INTEGER NOT NULL,
            sizing_trace JSONB NOT NULL,
            unsettled BOOLEAN NOT NULL DEFAULT FALSE,
            anomaly_reasons TEXT[] NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN (
                    'pending','approved','rejected','deferred','expired',
                    'working','partially_filled','filled','cancelled',
                    'closed','stopped_out','sub_minimum_size','macro_window_drop',
                    'market_drop_settlement_unavailable'
                )),
            approved_at_utc TIMESTAMPTZ,
            rejected_at_utc TIMESTAMPTZ,
            expires_at_utc TIMESTAMPTZ,
            decision_diary_entry_id UUID,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX signals_emitted_at ON signals(emitted_at_utc DESC)")
    op.execute("CREATE INDEX signals_status ON signals(status)")
    op.execute("CREATE INDEX signals_session_date_market ON signals(session_date, market)")

    # ------------------------------------------------------------------
    # orders (backend-spec §3.4) — partitioned by created_at
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE orders (
            id UUID NOT NULL DEFAULT uuid_generate_v7(),
            account_id UUID NOT NULL REFERENCES accounts(id),
            env TEXT NOT NULL,
            signal_id UUID NOT NULL REFERENCES signals(id),
            client_order_id TEXT NOT NULL,
            broker_order_id TEXT,
            market TEXT NOT NULL,
            contract_id UUID REFERENCES contracts(id),
            direction TEXT NOT NULL CHECK (direction IN ('buy','sell')),
            order_type TEXT NOT NULL
                CHECK (order_type IN (
                    'limit_marketable','stop_market','limit',
                    'calendar_spread','market_emergency'
                )),
            quantity INTEGER NOT NULL,
            limit_price NUMERIC(20,8),
            stop_price NUMERIC(20,8),
            placed_at_utc TIMESTAMPTZ NOT NULL,
            acknowledged_at_utc TIMESTAMPTZ,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN (
                    'pending','working','partially_filled','filled',
                    'cancelled','rejected','expired'
                )),
            rejection_reason TEXT,
            retry_n SMALLINT NOT NULL DEFAULT 0,
            parent_order_id UUID,
            strategy_hash CHAR(40) NOT NULL,
            parameter_set_hash CHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (id, created_at),
            UNIQUE (client_order_id, created_at)
        ) PARTITION BY RANGE (created_at)
        """
    )
    _create_yearly_partitions("orders", "created_at")
    op.execute("CREATE INDEX orders_signal_id ON orders(signal_id)")
    op.execute("CREATE INDEX orders_status ON orders(status)")
    op.execute("CREATE INDEX orders_client_order_id ON orders(client_order_id)")

    # ------------------------------------------------------------------
    # fills (backend-spec §3.5) — partitioned by created_at
    # FK to orders is composite (id, created_at) since orders is partitioned;
    # we enforce only order_id NOT NULL here. Application code resolves the
    # parent partition via signal->order joins. Reconciliation reads the FK
    # via the unique (id) lookup index.
    # ------------------------------------------------------------------
    op.execute("CREATE INDEX orders_id_idx ON orders(id)")
    op.execute(
        """
        CREATE TABLE fills (
            id UUID NOT NULL DEFAULT uuid_generate_v7(),
            account_id UUID NOT NULL REFERENCES accounts(id),
            env TEXT NOT NULL,
            order_id UUID NOT NULL,
            broker_fill_id TEXT NOT NULL,
            filled_at_utc TIMESTAMPTZ NOT NULL,
            fill_price NUMERIC(20,8) NOT NULL,
            fill_quantity INTEGER NOT NULL,
            commission NUMERIC(20,8) NOT NULL DEFAULT 0,
            exchange_fee NUMERIC(20,8) NOT NULL DEFAULT 0,
            realized_slippage_bps NUMERIC(10,4),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (id, created_at),
            UNIQUE (broker_fill_id, created_at)
        ) PARTITION BY RANGE (created_at)
        """
    )
    _create_yearly_partitions("fills", "created_at")
    op.execute("CREATE INDEX fills_order_id ON fills(order_id)")
    op.execute("CREATE INDEX fills_filled_at ON fills(filled_at_utc)")

    # ------------------------------------------------------------------
    # trades (backend-spec §3.6) — partitioned by created_at
    # FK to slippage_calibration_versions deferred to 0003.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE trades (
            id UUID NOT NULL DEFAULT uuid_generate_v7(),
            account_id UUID NOT NULL REFERENCES accounts(id),
            env TEXT NOT NULL,
            market TEXT NOT NULL,
            contract_id UUID REFERENCES contracts(id),
            entry_signal_id UUID NOT NULL REFERENCES signals(id),
            entry_order_id UUID NOT NULL,
            exit_order_id UUID,
            direction TEXT NOT NULL CHECK (direction IN ('long','short')),
            opened_at_utc TIMESTAMPTZ NOT NULL,
            closed_at_utc TIMESTAMPTZ,
            total_quantity INTEGER NOT NULL,
            avg_entry_price NUMERIC(20,8) NOT NULL,
            avg_exit_price NUMERIC(20,8),
            realized_pnl_usd NUMERIC(20,4),
            realized_commission_usd NUMERIC(20,4) NOT NULL DEFAULT 0,
            dividend_pnl_usd NUMERIC(20,4) NOT NULL DEFAULT 0,
            state TEXT NOT NULL CHECK (state IN
                ('open_position','closed','stopped_out','capacity_constrained')),
            managed_by_version CHAR(40) NOT NULL,
            strategy_hash CHAR(40) NOT NULL,
            parameter_set_hash CHAR(64) NOT NULL,
            slippage_calibration_version_id UUID NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at)
        """
    )
    _create_yearly_partitions("trades", "created_at")
    op.execute("CREATE INDEX trades_state ON trades(state)")
    op.execute("CREATE INDEX trades_opened_at ON trades(opened_at_utc)")
    op.execute("CREATE INDEX trades_market ON trades(market)")
    op.execute("CREATE INDEX trades_id_idx ON trades(id)")

    # ------------------------------------------------------------------
    # attribution (backend-spec §3.7) — partitioned by created_at
    # Trigger blocking expected_* mutation lands in 0005.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE attribution (
            id UUID NOT NULL DEFAULT uuid_generate_v7(),
            trade_id UUID NOT NULL,
            account_id UUID NOT NULL REFERENCES accounts(id),
            env TEXT NOT NULL,
            expected_entry_price NUMERIC(20,8) NOT NULL,
            expected_exit_price NUMERIC(20,8),
            expected_pnl_usd NUMERIC(20,4) NOT NULL,
            expected_slippage_bps NUMERIC(10,4) NOT NULL,
            expected_holding_days INTEGER NOT NULL,
            expected_at_utc TIMESTAMPTZ NOT NULL,
            realized_entry_price NUMERIC(20,8),
            realized_exit_price NUMERIC(20,8),
            realized_pnl_usd NUMERIC(20,4),
            realized_slippage_bps NUMERIC(10,4),
            realized_holding_days INTEGER,
            realized_at_utc TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at)
        """
    )
    _create_yearly_partitions("attribution", "created_at")
    op.execute("CREATE INDEX attribution_trade_id ON attribution(trade_id)")

    # ------------------------------------------------------------------
    # positions_current + positions_history (backend-spec §3.8)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE positions_current (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            account_id UUID NOT NULL REFERENCES accounts(id),
            market TEXT NOT NULL,
            contract_id UUID REFERENCES contracts(id),
            quantity INTEGER NOT NULL,
            avg_cost NUMERIC(20,8) NOT NULL,
            margin_held NUMERIC(20,4) NOT NULL DEFAULT 0,
            unrealized_pnl NUMERIC(20,4),
            last_mark_ts TIMESTAMPTZ NOT NULL,
            managed_by_version CHAR(40) NOT NULL,
            UNIQUE(account_id, market, contract_id)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE positions_history (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            account_id UUID NOT NULL REFERENCES accounts(id),
            snapshot_ts TIMESTAMPTZ NOT NULL,
            market TEXT NOT NULL,
            contract_id UUID,
            quantity INTEGER NOT NULL,
            avg_cost NUMERIC(20,8),
            margin_held NUMERIC(20,4),
            unrealized_pnl NUMERIC(20,4),
            source TEXT NOT NULL
                CHECK (source IN ('qc_objectstore','tws_api','flexquery_eod')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX positions_history_snapshot ON positions_history(snapshot_ts DESC)")

    # ------------------------------------------------------------------
    # balances (backend-spec §3.9)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE balances (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
            account_id UUID NOT NULL REFERENCES accounts(id),
            snapshot_ts TIMESTAMPTZ NOT NULL,
            net_liquidation NUMERIC(20,4) NOT NULL,
            cash_usd NUMERIC(20,4) NOT NULL,
            excess_liquidity NUMERIC(20,4) NOT NULL,
            used_margin_pct NUMERIC(10,8) NOT NULL,
            buying_power NUMERIC(20,4),
            pdt_day_trade_count_5d INTEGER,
            source TEXT NOT NULL
                CHECK (source IN ('qc_objectstore','tws_api','flexquery_eod')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX balances_snapshot ON balances(snapshot_ts DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS balances")
    op.execute("DROP TABLE IF EXISTS positions_history")
    op.execute("DROP TABLE IF EXISTS positions_current")

    for year in _PARTITION_YEARS:
        op.execute(f"DROP TABLE IF EXISTS attribution_y{year}")
    op.execute("DROP TABLE IF EXISTS attribution")

    for year in _PARTITION_YEARS:
        op.execute(f"DROP TABLE IF EXISTS trades_y{year}")
    op.execute("DROP TABLE IF EXISTS trades")

    for year in _PARTITION_YEARS:
        op.execute(f"DROP TABLE IF EXISTS fills_y{year}")
    op.execute("DROP TABLE IF EXISTS fills")

    for year in _PARTITION_YEARS:
        op.execute(f"DROP TABLE IF EXISTS orders_y{year}")
    op.execute("DROP TABLE IF EXISTS orders")

    op.execute("DROP TABLE IF EXISTS signals")
    op.execute("DROP TABLE IF EXISTS contracts")
    op.execute("DROP TABLE IF EXISTS setup_tokens")

    op.execute("ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_account_id_fkey")
    op.execute("DROP TABLE IF EXISTS accounts")
