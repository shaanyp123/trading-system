"""services/api/repos/phase1.py — read-only query layer for Phase 1 endpoints.

Day 15 ships the REST scaffold per backend-spec §4.1 + IG §3 Week 5 Mon. The
endpoints' SQL surface is consolidated into ONE repo for the scaffold phase
because every method returns either an empty list or a single nullable row
against tables that are empty in Phase 0 — splitting per-table would be
premature. When real writes start populating these tables (Week 4 Wed
dispatcher + Week 5 Thu calibration + the QC adapter polling loop), each
method can move into its own focused repo without changing the route layer.

Protocol-based design mirrors ``services/api/repos/setup_tokens.py`` so the
route handlers can be unit-tested with stubs (``tests/unit/test_api_phase1_routes.py``)
while integration tests exercise the production repo against testcontainers
Postgres (deferred — no integration tests for Day 15 since every query is a
no-data path).

The ``app_service`` Postgres role used by the api process holds SELECT on
every table touched here per ``alembic/versions/0006_roles.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

#: ``balances.source`` written by the EOD-recon per-cycle balance snapshot
#: (mirrors ``services/reconciliation/eod_cycle.py::BALANCE_SOURCE_FROM_COINBASE``).
#: The reconciliation summary derives ``last_check_utc`` from the latest
#: balances row carrying THIS source: every recon cycle writes one BEFORE the
#: planner runs, so it lands whether or not the cycle finds a break — making it
#: a faithful "when did recon last run" signal. The fill processor writes
#: ``'tws_api'`` balances on every fill; filtering by this source keeps a fill
#: from masquerading as a recon check.
_RECON_BALANCE_SOURCE: str = "coinbase_eod"


# ---------------------------------------------------------------------------
# Row-shape dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RiskStateRow:
    """One row from ``risk_state`` projected for the API layer.

    Mirrors the columns the §4.1.3 ``SystemStatus`` response needs; downstream
    fields (``capital_event_*``, ``monthly_dd_breached_for_calendar_month``)
    are not exposed in this repo since they don't round-trip the Day 15 API.
    """

    state: Literal["NORMAL", "HALT_NEW", "CONVALESCENT"]
    severity: Literal["routine", "defensive_envelope", "incident_review"] | None
    reason: str | None
    entered_at_utc: datetime
    convalescent_session_count: int
    vacation_active: bool
    vacation_until_utc: datetime | None
    audit_event_uuid: UUID


@dataclass(frozen=True, slots=True)
class ReconciliationSummaryRow:
    """§4.1.3 summary block. ``open_breaks`` / ``breaks_24h`` aggregate
    ``reconciliation_breaks``; ``last_check_utc`` is the latest recon-cycle
    balance snapshot (see :data:`_RECON_BALANCE_SOURCE`)."""

    last_check_utc: datetime | None
    last_check_passed: bool
    open_breaks: int
    breaks_24h: int


@dataclass(frozen=True, slots=True)
class AlertCountRow:
    """Open-alerts count grouped by severity."""

    p0: int
    p1: int
    p2: int


@dataclass(frozen=True, slots=True)
class ParameterSetRow:
    """One row from ``parameter_sets`` projected for the §4.1.3 risk envelope.

    The JSONB ``parameters`` column is returned as a plain dict; routing
    handlers walk it into the canonical ``RiskParameterItem`` list. Empty
    Phase 0 returns ``None`` from the repo and the route falls back to spec
    defaults.
    """

    parameter_set_hash: str
    parameters: dict[str, object]
    first_active_at: datetime
    last_active_at: datetime | None


@dataclass(frozen=True, slots=True)
class AuditLogRow:
    """One row from ``audit_log`` projected for the §2.6.4 audit explorer.

    BYTEA columns (``prev_hash``, ``record_hash``, ``payload_jcs``) are
    returned as raw ``bytes`` — the route layer hex-encodes the hashes and
    UTF-8 decodes the payload preview. ``payload_jcs`` is the full payload;
    truncation happens at the route layer so the operator-debug runbook can
    fetch the full row without re-querying.
    """

    event_uuid: UUID
    sequence_no: int
    event_type: str
    env: str
    phase_at_emit: int
    source_clock_ts: datetime
    ingest_clock_ts: datetime
    prev_hash: bytes
    record_hash: bytes
    payload_jcs: bytes
    repaired_for_sequence_no: int | None
    repaired_for_event_timestamp: datetime | None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class Phase1QueryRepo(Protocol):
    """The read surface route handlers depend on. Stubbed in unit tests."""

    async def fetch_active_account_id(self) -> UUID | None: ...

    async def fetch_risk_state_current(self, account_id: UUID) -> RiskStateRow | None: ...

    async def fetch_reconciliation_summary(self, account_id: UUID) -> ReconciliationSummaryRow: ...

    async def fetch_watchdog_last_ping_utc(self, account_id: UUID) -> datetime | None: ...

    async def count_pending_signals(self, account_id: UUID) -> int: ...

    async def count_open_alerts_by_severity(self, account_id: UUID) -> AlertCountRow: ...

    async def fetch_signals_page(
        self,
        account_id: UUID,
        *,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[dict[str, object]], str | None, bool]: ...

    async def fetch_positions_current(
        self,
        account_id: UUID,
    ) -> list[dict[str, object]]: ...

    async def fetch_positions_for_lean_cycle(
        self,
        account_id: UUID,
    ) -> list[dict[str, object]]: ...

    async def fetch_inflight_exit_markets(
        self,
        account_id: UUID,
        env: str,
    ) -> set[str]: ...

    async def fetch_orders_page(
        self,
        account_id: UUID,
        *,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[dict[str, object]], str | None, bool]: ...

    async def fetch_fills_page(
        self,
        account_id: UUID,
        *,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[dict[str, object]], str | None, bool]: ...

    async def fetch_alerts_page(
        self,
        account_id: UUID,
        *,
        status: str | None,
        severity: str | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[dict[str, object]], str | None, bool]: ...

    async def fetch_trades_page(
        self,
        account_id: UUID,
        *,
        from_utc: datetime | None,
        to_utc: datetime | None,
        market: str | None,
        state: str | None,
        id_prefix: str | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[dict[str, object]], str | None, bool]: ...

    async def fetch_trade_by_id(
        self,
        account_id: UUID,
        trade_id: UUID,
    ) -> dict[str, object] | None: ...

    async def fetch_active_parameter_set(self) -> ParameterSetRow | None: ...

    async def fetch_audit_log_page(
        self,
        *,
        event_type: str | None,
        env: str | None,
        from_ingest_utc: datetime | None,
        to_ingest_utc: datetime | None,
        before_sequence_no: int | None,
        limit: int,
    ) -> tuple[list[AuditLogRow], int | None, bool]: ...

    async def fetch_latest_balance_nav(self, account_id: UUID) -> Decimal | None: ...

    async def fetch_realized_pnl_aggregates(
        self,
        account_id: UUID,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal]: ...

    async def fetch_signal_summary(
        self,
        account_id: UUID,
        signal_id: UUID,
    ) -> dict[str, object] | None: ...


# ---------------------------------------------------------------------------
# Postgres implementation
# ---------------------------------------------------------------------------


class PostgresPhase1QueryRepo:
    """Production read repo backed by ``app_service`` role.

    Each method returns the dict-of-columns shape its consumer expects rather
    than a typed dataclass; the routes do their own dataclass→Pydantic
    mapping. This avoids one dataclass-per-row-type ceremony for the Day 15
    scaffold while keeping the SQL contract local to one file.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- accounts -----------------------------------------------------------

    async def fetch_active_account_id(self) -> UUID | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT id FROM accounts WHERE active_to IS NULL "
                    "ORDER BY active_from ASC LIMIT 1"
                )
            )
        ).fetchone()
        return row.id if row else None

    # --- risk_state ---------------------------------------------------------

    async def fetch_risk_state_current(self, account_id: UUID) -> RiskStateRow | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT state, severity, reason, entered_at_utc, "
                    "       convalescent_session_count, vacation_active, "
                    "       vacation_until_utc, audit_event_uuid "
                    "FROM risk_state "
                    "WHERE account_id = :acc AND is_current = TRUE"
                ),
                {"acc": account_id},
            )
        ).fetchone()
        if row is None:
            return None
        return RiskStateRow(
            state=row.state,
            severity=row.severity,
            reason=row.reason,
            entered_at_utc=row.entered_at_utc,
            convalescent_session_count=row.convalescent_session_count,
            vacation_active=row.vacation_active,
            vacation_until_utc=row.vacation_until_utc,
            audit_event_uuid=row.audit_event_uuid,
        )

    # --- reconciliation_breaks ---------------------------------------------

    async def fetch_reconciliation_summary(self, account_id: UUID) -> ReconciliationSummaryRow:
        # ``last_check_utc`` is the timestamp of the latest EOD-recon balance
        # snapshot (``balances.source = 'coinbase_eod'``), NOT
        # MAX(detected_at_utc) over breaks. Every recon cycle writes that
        # snapshot BEFORE the planner runs, so a clean cycle — which inserts NO
        # reconciliation_breaks row — still advances ``last_check_utc``. The
        # prior break-derived value froze on the last detected break, so the
        # /system tile looked stale (and "never checked") after a passing run.
        # ``open_breaks`` / ``breaks_24h`` stay break-derived. When recon has
        # never run the subquery is NULL and the caller substitutes EPOCH_SENTINEL.
        row = (
            await self._session.execute(
                text(
                    "SELECT "
                    "  (SELECT MAX(snapshot_ts) FROM balances "
                    "     WHERE account_id = :acc AND source = :recon_source"
                    "  ) AS last_check_utc, "
                    "  COUNT(*) FILTER ("
                    "    WHERE resolved_at_utc IS NULL"
                    "  ) AS open_breaks, "
                    "  COUNT(*) FILTER ("
                    "    WHERE detected_at_utc > now() - interval '24 hours'"
                    "  ) AS breaks_24h "
                    "FROM reconciliation_breaks "
                    "WHERE account_id = :acc"
                ),
                {"acc": account_id, "recon_source": _RECON_BALANCE_SOURCE},
            )
        ).fetchone()
        # COUNT(*) is non-NULL even on empty tables; the last_check_utc subquery
        # is NULL until the first recon cycle writes a balance snapshot.
        return ReconciliationSummaryRow(
            last_check_utc=row.last_check_utc if row else None,
            last_check_passed=(row.open_breaks == 0) if row else True,
            open_breaks=int(row.open_breaks) if row else 0,
            breaks_24h=int(row.breaks_24h) if row else 0,
        )

    # --- liveness_probes (watchdog last ping) ------------------------------

    async def fetch_watchdog_last_ping_utc(self, account_id: UUID) -> datetime | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT MAX(sent_at_utc) AS last_ping "
                    "FROM liveness_probes "
                    "WHERE account_id = :acc AND channel = 'web'"
                ),
                {"acc": account_id},
            )
        ).fetchone()
        return row.last_ping if row and row.last_ping is not None else None

    # --- signals -----------------------------------------------------------

    async def count_pending_signals(self, account_id: UUID) -> int:
        row = (
            await self._session.execute(
                text(
                    "SELECT COUNT(*) AS n FROM signals "
                    "WHERE account_id = :acc AND status = 'pending'"
                ),
                {"acc": account_id},
            )
        ).fetchone()
        return int(row.n) if row else 0

    async def fetch_signals_page(
        self,
        account_id: UUID,
        *,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[dict[str, object]], str | None, bool]:
        # 2026-05-28 fix: previously this query SELECTed only 6 columns and
        # the route handler discarded ``_rows`` entirely + returned items=[].
        # Now selects the full 14-column projection needed by SignalSummary +
        # the route handler maps each row to the Pydantic model. expires_at_utc
        # is COALESCEd to emitted_at_utc + 24h because the qc_adapter INSERT
        # path doesn't set it (see services/qc_adapter/db.py::insert_signal_row);
        # the synthesized value matches the strategy's session-cycle cadence
        # — a new emission on the next 21:30 UTC cycle supersedes this signal,
        # so 24h is an upper bound on "still actionable".
        params: dict[str, object] = {"acc": account_id, "lim": limit}
        # ``signal_type`` (entry/exit discriminator) + a JSONB extraction of
        # the prior position side power the /signals "Side" pill (green LONG /
        # red SHORT / amber CLOSE). The local LEAN strategy persists the side
        # being closed only inside the exit row's sizing_trace
        # (lean_naive_sizing.prior_position_direction, see
        # lean/v1_strategy.py::_build_exit_sizing_trace) — there is no
        # dedicated column. ``#>>`` returns NULL when the path is absent, so
        # entry rows (and any row missing the key) come back NULL with no error.
        sql = (
            "SELECT id, market, direction, signal_type, target_contracts, "
            "       decision_price, expected_fill_price, expected_slippage_bps, "
            "       unsettled, anomaly_reasons, status, emitted_at_utc, "
            "       COALESCE(expires_at_utc, emitted_at_utc + INTERVAL '24 hours') "
            "         AS expires_at_utc, "
            "       strategy_hash, parameter_set_hash, "
            "       sizing_trace #>> '{lean_naive_sizing,prior_position_direction}' "
            "         AS prior_position_direction "
            "FROM signals WHERE account_id = :acc"
        )
        if status:
            sql += " AND status = :st"
            params["st"] = status
        sql += " ORDER BY emitted_at_utc DESC LIMIT :lim"
        rows = (await self._session.execute(text(sql), params)).fetchall()
        items = [dict(r._mapping) for r in rows]
        # Cursor encoding still deferred — Phase 0 strategies emit at most 11
        # signals per session and the limit defaults to 50, so the first page
        # always contains every row currently in the table. When the cursor
        # truly matters (Phase 1+ multi-strategy), revisit.
        return items, None, False

    # --- positions_current -------------------------------------------------

    async def fetch_positions_current(
        self,
        account_id: UUID,
    ) -> list[dict[str, object]]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT id, market, contract_id, quantity, avg_cost, "
                    "       margin_held, unrealized_pnl, last_mark_ts, "
                    "       managed_by_version "
                    "FROM positions_current WHERE account_id = :acc"
                ),
                {"acc": account_id},
            )
        ).fetchall()
        return [dict(r._mapping) for r in rows]

    async def fetch_positions_for_lean_cycle(
        self,
        account_id: UUID,
    ) -> list[dict[str, object]]:
        """Held positions for the nightly LEAN exit cycle (PR-A2).

        Every non-zero ``positions_current`` row for the account, LEFT-joined
        to the open ``trades`` row so ``opened_at_utc`` can drive the strategy's
        MIN_HOLDING_DAYS gate. The query MIRRORS the operator tool's reviewed
        sourcing (``scripts/operator_tools/trigger_v1_cycle.py::fetch_positions_for_cycle``)
        so the nightly LEAN cycle and the manual trigger see identical positions —
        but without the universe filter: ALL held positions are returned so a
        decommission close can fire even for a sidelined market.

        ``opened_at_utc`` is the MIN open-trade timestamp (a market with two open
        trade rows shares the earliest open date). NULL when no open trade is
        recorded — the strategy then conservatively skips the MIN_HOLDING check
        (per ``strategies/v1_trend_following/strategy.py::generate_exit_candidates``).
        The endpoint converts ``opened_at_utc`` → ET session date for the response.

        **Aggregated per market** (``SUM(quantity) GROUP BY market``), mirroring
        the recon path ``services/reconciliation/eod_cycle.build_backend_view``:
        ``positions_current`` is ``UNIQUE(account_id, market, contract_id)``, so a
        futures roll can legally produce >1 row for the same market. We return the
        NET signed quantity (and a qty-weighted ``avg_cost``) so LEAN sees one
        ``Position`` per market — without this, two rows would collapse in LEAN's
        ``positions_by_market`` dict and one leg would be silently dropped.
        ``HAVING SUM(quantity) <> 0`` drops a market whose legs net flat. The
        qty-weighted ``avg_cost`` is cast back to the column's ``NUMERIC(20,8)``
        so the wire string stays clean + deterministic (numeric division
        otherwise widens the scale).
        """
        rows = (
            await self._session.execute(
                text(
                    "SELECT p.market, "
                    "       SUM(p.quantity) AS quantity, "
                    "       (SUM(p.avg_cost * p.quantity) / NULLIF(SUM(p.quantity), 0))"
                    "         ::numeric(20,8) AS avg_cost, "
                    "       (SELECT MIN(t.opened_at_utc) FROM trades t "
                    "          WHERE t.account_id = p.account_id "
                    "            AND t.market = p.market "
                    "            AND t.state = 'open_position') AS opened_at_utc "
                    "FROM positions_current p "
                    "WHERE p.account_id = :acc "
                    "GROUP BY p.account_id, p.market "
                    "HAVING SUM(p.quantity) <> 0 "
                    "ORDER BY p.market"
                ),
                {"acc": account_id},
            )
        ).fetchall()
        return [dict(r._mapping) for r in rows]

    async def fetch_inflight_exit_markets(
        self,
        account_id: UUID,
        env: str,
    ) -> set[str]:
        """Markets with an exit signal still GENUINELY IN FLIGHT (PR-A2).

        The idempotency key for the nightly exit cycle: once an indicator exit
        is emitted, a trend_flip persists every cycle until the close fills, so
        the strategy would re-emit the same exit each night. This set lets LEAN
        suppress those re-emits (and the server-side ingest guard rejects any
        that slip through).

        **In-flight allowlist, NOT the operator tool's filter.** Because this is
        cross-night (NOT session-scoped, unlike
        ``trigger_v1_cycle.fetch_already_emitted_exits``), we must block ONLY the
        states where an exit is actively trying to close the CURRENT position —
        ``pending/approved/deferred/working/partially_filled``. A *completed*
        exit (``filled/closed/stopped_out``) must NOT block, or a market would be
        suppressed forever and the NEXT exit on a RE-OPENED position would be
        silently dropped (V1 re-entry is a real path — the entry-side
        POSITION_ALREADY_SAME_DIRECTION guard only fires while the position is
        held). ``rejected/cancelled/expired`` (operator declined/withdrew) and
        the never-dispatched drop states (sub_minimum_size / macro_window_drop /
        market_drop_settlement_unavailable, where the position is still held)
        are likewise re-emittable. Keep this set in sync with
        ``services/qc_adapter/signal_ingestion._exit_in_flight_exists``.
        """
        rows = (
            await self._session.execute(
                text(
                    "SELECT DISTINCT market FROM signals "
                    "WHERE account_id = :acc "
                    "  AND env = :env "
                    "  AND signal_type = 'exit' "
                    "  AND status IN ('pending','approved','deferred','working','partially_filled')"
                ),
                {"acc": account_id, "env": env},
            )
        ).fetchall()
        return {str(r.market) for r in rows}

    # --- orders ------------------------------------------------------------

    async def fetch_orders_page(
        self,
        account_id: UUID,
        *,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[dict[str, object]], str | None, bool]:
        params: dict[str, object] = {"acc": account_id, "lim": limit}
        sql = (
            "SELECT id, signal_id, client_order_id, broker_order_id, market, "
            "       direction, order_type, quantity, limit_price, status, "
            "       placed_at_utc "
            "FROM orders WHERE account_id = :acc"
        )
        if status:
            sql += " AND status = :st"
            params["st"] = status
        sql += " ORDER BY placed_at_utc DESC LIMIT :lim"
        rows = (await self._session.execute(text(sql), params)).fetchall()
        items = [dict(r._mapping) for r in rows]
        return items, None, False

    # --- fills -------------------------------------------------------------

    async def fetch_fills_page(
        self,
        account_id: UUID,
        *,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[dict[str, object]], str | None, bool]:
        # Joins fills → orders to project signal_id + side + market onto each
        # row. The spec response shape requires signal_uuid which only the
        # parent order knows.
        rows = (
            await self._session.execute(
                text(
                    "SELECT f.id AS fill_id, f.order_id, o.signal_id, o.market, "
                    "       o.direction AS side, f.fill_quantity, f.fill_price, "
                    "       f.realized_slippage_bps, f.filled_at_utc "
                    "FROM fills f LEFT JOIN orders o ON o.id = f.order_id "
                    "WHERE f.account_id = :acc "
                    "ORDER BY f.filled_at_utc DESC LIMIT :lim"
                ),
                {"acc": account_id, "lim": limit},
            )
        ).fetchall()
        items = [dict(r._mapping) for r in rows]
        return items, None, False

    # --- alerts ------------------------------------------------------------

    async def fetch_alerts_page(
        self,
        account_id: UUID,
        *,
        status: str | None,
        severity: str | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[dict[str, object]], str | None, bool]:
        params: dict[str, object] = {"acc": account_id, "lim": limit}
        sql = (
            "SELECT id, severity, category, message, detail, fired_at_utc, "
            "       acknowledged, acknowledged_at_utc, resolved_at_utc, "
            "       triggering_audit_event_uuid "
            "FROM alerts WHERE account_id = :acc"
        )
        if severity:
            sql += " AND severity = :sev"
            params["sev"] = severity
        if status == "open":
            sql += " AND NOT acknowledged AND resolved_at_utc IS NULL"
        elif status == "acknowledged":
            sql += " AND acknowledged AND resolved_at_utc IS NULL"
        elif status == "resolved":
            sql += " AND resolved_at_utc IS NOT NULL"
        sql += " ORDER BY fired_at_utc DESC LIMIT :lim"
        rows = (await self._session.execute(text(sql), params)).fetchall()
        items = [dict(r._mapping) for r in rows]
        return items, None, False

    # --- trades ------------------------------------------------------------

    async def fetch_trades_page(
        self,
        account_id: UUID,
        *,
        from_utc: datetime | None,
        to_utc: datetime | None,
        market: str | None,
        state: str | None,
        id_prefix: str | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[dict[str, object]], str | None, bool]:
        # Phase 0: trades table is empty until Week 7 Thu paper round-trip
        # populates the first row. Query runs against the partition-pruned
        # table heap; filter pushes down via ``trades_opened_at`` /
        # ``trades_market`` / ``trades_state`` indexes when present.
        params: dict[str, object] = {"acc": account_id, "lim": limit}
        sql = (
            "SELECT id, env, market, direction, state, "
            "       opened_at_utc, closed_at_utc, total_quantity, "
            "       avg_entry_price, avg_exit_price, realized_pnl_usd, "
            "       entry_signal_id, managed_by_version "
            "FROM trades WHERE account_id = :acc"
        )
        if from_utc is not None:
            sql += " AND opened_at_utc >= :from_utc"
            params["from_utc"] = from_utc
        if to_utc is not None:
            sql += " AND opened_at_utc <= :to_utc"
            params["to_utc"] = to_utc
        if market:
            sql += " AND market = :mkt"
            params["mkt"] = market
        if state:
            sql += " AND state = :st"
            params["st"] = state
        if id_prefix:
            # ``id_prefix`` is the command-palette (Phase 2) lookup —
            # backend-spec §4.1.2 specifies ``trade_uuid::text`` prefix
            # match. ``id_prefix`` is operator-supplied so escape the LIKE
            # metacharacters defensively even though UUID strings can't
            # legitimately contain ``%`` or ``_``.
            sanitized = id_prefix.replace("%", r"\%").replace("_", r"\_")
            sql += " AND id::text LIKE :id_prefix || '%' ESCAPE '\\'"
            params["id_prefix"] = sanitized
        sql += " ORDER BY opened_at_utc DESC LIMIT :lim"
        rows = (await self._session.execute(text(sql), params)).fetchall()
        items = [dict(r._mapping) for r in rows]
        # Phase 0 always returns has_more=False because no data exists; the
        # cursor stays None. Real cursor encoding lands when trades populate
        # (Week 7 Thu paper round-trip onwards) — same pattern as signals /
        # orders / fills above.
        return items, None, False

    async def fetch_trade_by_id(
        self,
        account_id: UUID,
        trade_id: UUID,
    ) -> dict[str, object] | None:
        # Detail endpoint — returns the full TradeDetail wire shape's columns.
        # account_id is part of the WHERE clause for defense-in-depth even
        # though Phase 0 has a single operator account; matches the pattern in
        # ``fetch_risk_state_current`` so a future multi-tenant Phase doesn't
        # leak trade rows across accounts via deep-link enumeration.
        row = (
            await self._session.execute(
                text(
                    "SELECT id, account_id, env, market, direction, state, "
                    "       opened_at_utc, closed_at_utc, total_quantity, "
                    "       avg_entry_price, avg_exit_price, realized_pnl_usd, "
                    "       realized_commission_usd, dividend_pnl_usd, "
                    "       entry_signal_id, entry_order_id, exit_order_id, "
                    "       managed_by_version, strategy_hash, "
                    "       parameter_set_hash, slippage_calibration_version_id "
                    "FROM trades "
                    "WHERE account_id = :acc AND id = :tid"
                ),
                {"acc": account_id, "tid": trade_id},
            )
        ).fetchone()
        return dict(row._mapping) if row is not None else None

    async def count_open_alerts_by_severity(self, account_id: UUID) -> AlertCountRow:
        row = (
            await self._session.execute(
                text(
                    "SELECT "
                    "  COUNT(*) FILTER (WHERE severity = 'P0') AS p0, "
                    "  COUNT(*) FILTER (WHERE severity = 'P1') AS p1, "
                    "  COUNT(*) FILTER (WHERE severity = 'P2') AS p2 "
                    "FROM alerts "
                    "WHERE account_id = :acc "
                    "  AND NOT acknowledged "
                    "  AND resolved_at_utc IS NULL"
                ),
                {"acc": account_id},
            )
        ).fetchone()
        return AlertCountRow(
            p0=int(row.p0) if row else 0,
            p1=int(row.p1) if row else 0,
            p2=int(row.p2) if row else 0,
        )

    # --- parameter_sets (Day 27 — risk envelope) ---------------------------

    async def fetch_active_parameter_set(self) -> ParameterSetRow | None:
        # The "active" set is whichever row has the latest ``first_active_at``
        # with ``last_active_at IS NULL`` (= currently active per
        # backend-spec §3.11). Phase 0 returns None — the table stays empty
        # until the Week 4 Wed dispatcher seeds the first parameter set.
        row = (
            await self._session.execute(
                text(
                    "SELECT parameter_set_hash, parameters, "
                    "       first_active_at, last_active_at "
                    "FROM parameter_sets "
                    "WHERE last_active_at IS NULL "
                    "ORDER BY first_active_at DESC LIMIT 1"
                )
            )
        ).fetchone()
        if row is None:
            return None
        return ParameterSetRow(
            parameter_set_hash=row.parameter_set_hash,
            parameters=dict(row.parameters),
            first_active_at=row.first_active_at,
            last_active_at=row.last_active_at,
        )

    # --- audit_log (Day 27 — audit explorer) -------------------------------

    async def fetch_audit_log_page(
        self,
        *,
        event_type: str | None,
        env: str | None,
        from_ingest_utc: datetime | None,
        to_ingest_utc: datetime | None,
        before_sequence_no: int | None,
        limit: int,
    ) -> tuple[list[AuditLogRow], int | None, bool]:
        # Partition-pruned SELECT against the partitioned ``audit_log`` heap.
        # Postgres prunes on ``ingest_clock_ts`` when from/to are bound at
        # plan time (RANGE partitioning per ``alembic/versions/0001_audit_log``).
        # Without from/to the planner still scans only the populated
        # partition (audit_log_y2026 today) but emits a longer plan.
        #
        # Cursor: ``before_sequence_no`` is the highest sequence_no the
        # caller has ALREADY SEEN; the page returns rows with strictly
        # lower sequence_no in DESC order. We fetch ``limit + 1`` rows so
        # we can compute ``has_more`` without a second query.
        params: dict[str, object] = {"lim": limit + 1}
        sql = (
            "SELECT event_uuid, sequence_no, event_type, env, phase_at_emit, "
            "       source_clock_ts, ingest_clock_ts, prev_hash, record_hash, "
            "       payload_jcs, repaired_for_sequence_no, "
            "       repaired_for_event_timestamp "
            "FROM audit_log WHERE 1=1"
        )
        if event_type is not None:
            sql += " AND event_type = :et"
            params["et"] = event_type
        if env is not None:
            sql += " AND env = :env"
            params["env"] = env
        if from_ingest_utc is not None:
            sql += " AND ingest_clock_ts >= :from_ts"
            params["from_ts"] = from_ingest_utc
        if to_ingest_utc is not None:
            sql += " AND ingest_clock_ts <= :to_ts"
            params["to_ts"] = to_ingest_utc
        if before_sequence_no is not None:
            sql += " AND sequence_no < :before_seq"
            params["before_seq"] = before_sequence_no
        sql += " ORDER BY sequence_no DESC LIMIT :lim"

        result = (await self._session.execute(text(sql), params)).fetchall()
        rows = [
            AuditLogRow(
                event_uuid=r.event_uuid,
                sequence_no=int(r.sequence_no),
                event_type=r.event_type,
                env=r.env,
                phase_at_emit=int(r.phase_at_emit),
                source_clock_ts=r.source_clock_ts,
                ingest_clock_ts=r.ingest_clock_ts,
                prev_hash=bytes(r.prev_hash),
                record_hash=bytes(r.record_hash),
                payload_jcs=bytes(r.payload_jcs),
                repaired_for_sequence_no=(
                    int(r.repaired_for_sequence_no)
                    if r.repaired_for_sequence_no is not None
                    else None
                ),
                repaired_for_event_timestamp=r.repaired_for_event_timestamp,
            )
            for r in result
        ]
        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]
        next_cursor = rows[-1].sequence_no if has_more and rows else None
        return rows, next_cursor, has_more

    # --- balances (NAV) ----------------------------------------------------

    async def fetch_latest_balance_nav(self, account_id: UUID) -> Decimal | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT net_liquidation FROM balances "
                    "WHERE account_id = :acc "
                    "ORDER BY snapshot_ts DESC LIMIT 1"
                ),
                {"acc": account_id},
            )
        ).fetchone()
        return Decimal(str(row.net_liquidation)) if row else None

    async def fetch_realized_pnl_aggregates(
        self,
        account_id: UUID,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """Return (daily, weekly, monthly, yearly) realized-PnL sums.

        Aggregates ``trades.realized_pnl_usd`` over UTC-calendar-aligned
        windows ending at ``now()``:

        * daily   — current UTC day (00:00 UTC to now)
        * weekly  — current ISO week (Monday 00:00 UTC to now)
        * monthly — current UTC calendar month (1st 00:00 UTC to now)
        * yearly  — current UTC calendar year (Jan 1 00:00 UTC to now)

        Phase 1 simplification: uses UTC-aligned calendar boundaries
        rather than the spec's 17:00-ET-aligned session boundaries
        (frontend-spec §2.2.2 B). The two interpretations diverge only
        for trades closed between 00:00 UTC and 21:00 UTC of a calendar
        day — a small population for now. A follow-up can swap to
        session-aligned when the operator wants the exact spec match.

        Only ``state='closed'`` trades with ``realized_pnl_usd IS NOT
        NULL`` contribute. Open positions (unrealized PnL) are NOT
        included — that surface lives on
        :func:`PositionsResponse.unrealized_pnl_pct_of_nav` per
        spec §4.1.5b.
        """
        now_utc = datetime.now(tz=UTC)
        day_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = day_start - timedelta(days=day_start.weekday())
        month_start = day_start.replace(day=1)
        year_start = day_start.replace(month=1, day=1)
        row = (
            await self._session.execute(
                text(
                    """
                    SELECT
                      COALESCE(SUM(
                        CASE WHEN closed_at_utc >= :day_start
                             THEN realized_pnl_usd ELSE 0 END
                      ), 0) AS daily_pnl,
                      COALESCE(SUM(
                        CASE WHEN closed_at_utc >= :week_start
                             THEN realized_pnl_usd ELSE 0 END
                      ), 0) AS weekly_pnl,
                      COALESCE(SUM(
                        CASE WHEN closed_at_utc >= :month_start
                             THEN realized_pnl_usd ELSE 0 END
                      ), 0) AS monthly_pnl,
                      COALESCE(SUM(
                        CASE WHEN closed_at_utc >= :year_start
                             THEN realized_pnl_usd ELSE 0 END
                      ), 0) AS yearly_pnl
                    FROM trades
                    WHERE account_id = :acc
                      AND state = 'closed'
                      AND realized_pnl_usd IS NOT NULL
                    """
                ),
                {
                    "acc": account_id,
                    "day_start": day_start,
                    "week_start": week_start,
                    "month_start": month_start,
                    "year_start": year_start,
                },
            )
        ).fetchone()
        if row is None:
            return (Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"))
        return (
            Decimal(str(row.daily_pnl)),
            Decimal(str(row.weekly_pnl)),
            Decimal(str(row.monthly_pnl)),
            Decimal(str(row.yearly_pnl)),
        )

    async def fetch_signal_summary(
        self,
        account_id: UUID,
        signal_id: UUID,
    ) -> dict[str, object] | None:
        """Single-row read of the columns needed for ``signal`` SSE emission.

        Returns the fields ``services.webhook_pusher.sse_subscriber.route_event``
        consumes (market + direction) plus a few related identity fields
        the api emits as SSE data payload so subscribers can deep-link
        to the trade. The account_id filter prevents cross-account
        leakage in the multi-account future (Phase 3+); today the api
        is single-operator.
        """
        row = (
            await self._session.execute(
                text(
                    "SELECT market, direction, signal_type, target_contracts, "
                    "       decision_price, strategy_hash, parameter_set_hash, "
                    "       emitted_at_utc, env, status "
                    "FROM signals "
                    "WHERE id = :sid AND account_id = :acc"
                ),
                {"sid": signal_id, "acc": account_id},
            )
        ).fetchone()
        if row is None:
            return None
        return {
            "market": row.market,
            "direction": row.direction,
            # Exit-pipeline PR-C (2026-05-27): plumbed through to the
            # dispatcher's HALT_NEW gate (L5 — exits bypass).
            "signal_type": row.signal_type,
            "target_contracts": int(row.target_contracts),
            "decision_price": Decimal(str(row.decision_price)),
            "strategy_hash": row.strategy_hash,
            "parameter_set_hash": row.parameter_set_hash,
            "emitted_at_utc": row.emitted_at_utc,
            "env": row.env,
            "status": row.status,
        }


__all__ = [
    "AlertCountRow",
    "AuditLogRow",
    "ParameterSetRow",
    "Phase1QueryRepo",
    "PostgresPhase1QueryRepo",
    "ReconciliationSummaryRow",
    "RiskStateRow",
]
