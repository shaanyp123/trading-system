"""services/api/repos/funding.py — read surface for ``GET /api/system/funding``.

Crypto-pivot §3.7/§3.9 (``Docs/crypto-pivot-delta-spec.md``): the funding
+ cash-yield telemetry projection. Nine read-only queries over the C0-B1
crypto-pivot tables (``alembic/versions/2026-07-08_crypto_pivot_tables.py``)
+ the USDC-interest capture tables (``2026-07-10_usdc_rewards_capture.py``)
plus the venue-truth joins the funding math needs:

* ``funding_rates`` — latest hourly snapshot per product (7-day window so
  a delisted product ages out of the response instead of pinning its
  final rate forever) + today's hourly rows for the est-funding sum.
* ``positions_current`` ⨝ ``contracts`` — held contracts per product with
  the ``market_root`` (BTC/ETH) + ``multiplier`` the strategy worker's
  ``ensure_contract_row`` wrote from runtime product discovery ([A13]).
* ``cash_sweeps`` — today's §3.6 sweep ledger rows (empty until the C2
  cash_manager activates; the surface ships ahead of its writer).
* ``product_metadata`` — latest daily spec snapshot per product
  (contract_size + raw venue payload for asset labeling).
* ``audit_log`` — the latest ``balance_snapshot_recorded`` event, whose
  payload carries the venue liquidation buffer (written by
  ``services/reconciliation/eod_cycle.py``). The chain is the source of
  truth here — no operational table duplicates the buffer.
* ``cash_balance_snapshots`` + ``usdc_reward_transactions`` — the daily
  00:20 UTC USDC-interest capture (``services/data/usdc_rewards.py``):
  latest spot USD/CBI USDC balances, last reward credit, lifetime
  reward credits total.

READ-ONLY consumers — no writes, no schema changes. Same raw-SQL-over-
``AsyncSession`` + Protocol pattern as ``services/api/repos/cycle.py``;
route handlers are unit-tested with stubs. JSONB/BYTEA payloads are
parsed defensively — a corrupt payload degrades to ``{}``, never a 500.

A02 N/A — ``services/api/**`` is on the §2.3 hot-fix whitelist.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from uuid import UUID

import structlog
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Row-shape dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FundingRateRow:
    """One ``funding_rates`` snapshot row projected for the API layer."""

    product_id: str
    observed_at_utc: datetime
    rate_per_interval: Decimal
    interval_hours: Decimal
    mark_price: Decimal | None


@dataclass(frozen=True, slots=True)
class HeldPositionRow:
    """One held ``positions_current`` row + its ``contracts`` enrichment.

    ``market_root``/``multiplier`` are None when ``contract_id`` is NULL
    (LEFT JOIN miss) — legacy/recon-seeded rows without a contracts row
    still surface their held quantity; the mapper simply can't label or
    size them.
    """

    market: str
    quantity: int
    market_root: str | None
    multiplier: Decimal | None


@dataclass(frozen=True, slots=True)
class CashSweepRow:
    """One ``cash_sweeps`` ledger row (§3.6), newest-first ordering."""

    requested_at_utc: datetime
    completed_at_utc: datetime | None
    direction: str  # 'to_yield' | 'to_margin' (table CHECK)
    amount_usd: Decimal
    status: str  # 'requested' | 'completed' | 'failed' (table CHECK)
    reason: str


@dataclass(frozen=True, slots=True)
class ProductMetadataRow:
    """Latest ``product_metadata`` snapshot per product."""

    product_id: str
    contract_size: Decimal | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CashBalanceSnapshotRow:
    """Latest ``cash_balance_snapshots`` row (daily 00:20 UTC capture).

    ``spot_usd``/``cbi_usdc`` are None when the capture saw no account
    of that currency — distinct from a genuine zero balance.
    """

    snapshot_date_utc: date
    captured_at_utc: datetime
    spot_usd: Decimal | None
    cbi_usdc: Decimal | None


@dataclass(frozen=True, slots=True)
class RewardTransactionRow:
    """One persisted ``usdc_reward_transactions`` credit, projected.

    Until the capture filter locks to the venue's real reward type
    (observe-then-lock, decisions-log 2026-07-10), "reward" here means
    any persisted POSITIVE-amount ledger credit — the repo queries
    filter ``amount > 0`` so persisted forensic debits never surface
    as payouts.
    """

    venue_created_at_utc: datetime
    amount: Decimal
    currency: str
    transaction_type: str


@dataclass(frozen=True, slots=True)
class BalanceSnapshotAuditRow:
    """Latest ``balance_snapshot_recorded`` audit event, payload decoded.

    ``payload`` is the JCS payload verbatim (Decimal values are strings
    per the writer's [A05] discipline — the mapper re-anchors them);
    ``recorded_at_utc`` is the event's ``source_clock_ts`` (the venue
    snapshot pull time, not the ingest clock).
    """

    payload: dict[str, Any]
    recorded_at_utc: datetime


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class FundingQueryRepo(Protocol):
    """The read surface the /api/system/funding handler depends on.

    Stubbed in unit tests (same pattern as ``CycleQueryRepo``).
    """

    async def fetch_latest_funding_rates(self) -> list[FundingRateRow]: ...

    async def fetch_funding_rates_today(
        self, product_ids: Sequence[str]
    ) -> list[FundingRateRow]: ...

    async def fetch_held_positions_with_contract(
        self, account_id: UUID
    ) -> list[HeldPositionRow]: ...

    async def fetch_cash_sweeps_today(self) -> list[CashSweepRow]: ...

    async def fetch_latest_product_metadata(self) -> list[ProductMetadataRow]: ...

    async def fetch_latest_balance_snapshot_audit(self) -> BalanceSnapshotAuditRow | None: ...

    async def fetch_latest_cash_balance_snapshot(self) -> CashBalanceSnapshotRow | None: ...

    async def fetch_last_reward_credit(self) -> RewardTransactionRow | None: ...

    async def fetch_lifetime_reward_credits_total(self) -> Decimal: ...


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------


def _to_decimal(value: object) -> Decimal:
    """DB numeric → Decimal ([A05] boundary re-anchor)."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _to_decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return _to_decimal(value)
    except (InvalidOperation, ValueError):
        return None


def _coerce_json_object(raw: Any, *, event: str) -> dict[str, Any]:
    """Normalize a JSONB/JCS payload to a dict (same defense as cycle repo).

    asyncpg returns JSONB as ``dict``; BYTEA JCS payloads arrive as
    ``bytes``. Anything unparseable degrades to ``{}`` with a structured
    warning rather than a 500.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, bytes | bytearray | memoryview):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            log.warning(event, reason="payload_not_utf8")
            return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            log.warning(event, reason="payload_unparseable")
            return {}
        if isinstance(parsed, dict):
            return parsed
    log.warning(event, reason="payload_not_object", payload_type=type(raw).__name__)
    return {}


# ---------------------------------------------------------------------------
# Postgres implementation
# ---------------------------------------------------------------------------


class PostgresFundingQueryRepo:
    """Raw-SQL implementation over the request-scoped ``AsyncSession``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fetch_latest_funding_rates(self) -> list[FundingRateRow]:
        # 7-day window: a product the hourly logger stopped observing
        # (delisted / classifier change) drops off the surface instead of
        # pinning its final rate as "current" forever.
        rows = (
            await self._session.execute(
                text(
                    "SELECT DISTINCT ON (product_id) "
                    "       product_id, observed_at_utc, rate_per_interval, "
                    "       interval_hours, mark_price "
                    "FROM funding_rates "
                    "WHERE observed_at_utc >= now() - interval '7 days' "
                    "ORDER BY product_id, observed_at_utc DESC"
                )
            )
        ).fetchall()
        return [
            FundingRateRow(
                product_id=str(r.product_id),
                observed_at_utc=r.observed_at_utc,
                rate_per_interval=_to_decimal(r.rate_per_interval),
                interval_hours=_to_decimal(r.interval_hours),
                mark_price=_to_decimal_or_none(r.mark_price),
            )
            for r in rows
        ]

    async def fetch_funding_rates_today(self, product_ids: Sequence[str]) -> list[FundingRateRow]:
        # "Today" = the current UTC calendar day (the hourly logger stamps
        # observed_at_utc at the top of each UTC hour), matching the
        # est-funding-today window the mapper sums over.
        if not product_ids:
            return []
        stmt = text(
            "SELECT product_id, observed_at_utc, rate_per_interval, "
            "       interval_hours, mark_price "
            "FROM funding_rates "
            "WHERE product_id IN :pids "
            "  AND observed_at_utc >= date_trunc('day', now()) "
            "ORDER BY product_id, observed_at_utc ASC"
        ).bindparams(bindparam("pids", expanding=True))
        rows = (await self._session.execute(stmt, {"pids": list(product_ids)})).fetchall()
        return [
            FundingRateRow(
                product_id=str(r.product_id),
                observed_at_utc=r.observed_at_utc,
                rate_per_interval=_to_decimal(r.rate_per_interval),
                interval_hours=_to_decimal(r.interval_hours),
                mark_price=_to_decimal_or_none(r.mark_price),
            )
            for r in rows
        ]

    async def fetch_held_positions_with_contract(self, account_id: UUID) -> list[HeldPositionRow]:
        # LEFT JOIN: a positions_current row with contract_id IS NULL
        # (recon-seeded before contract resolution) still counts as held;
        # it just lacks market_root/multiplier enrichment.
        rows = (
            await self._session.execute(
                text(
                    "SELECT pc.market, pc.quantity, c.market_root, c.multiplier "
                    "FROM positions_current pc "
                    "LEFT JOIN contracts c ON pc.contract_id = c.id "
                    "WHERE pc.account_id = :acct AND pc.quantity != 0"
                ),
                {"acct": account_id},
            )
        ).fetchall()
        return [
            HeldPositionRow(
                market=str(r.market),
                quantity=int(r.quantity),
                market_root=str(r.market_root) if r.market_root is not None else None,
                multiplier=_to_decimal_or_none(r.multiplier),
            )
            for r in rows
        ]

    async def fetch_cash_sweeps_today(self) -> list[CashSweepRow]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT requested_at_utc, completed_at_utc, direction, "
                    "       amount_usd, status, reason "
                    "FROM cash_sweeps "
                    "WHERE requested_at_utc >= date_trunc('day', now()) "
                    "ORDER BY requested_at_utc DESC"
                )
            )
        ).fetchall()
        return [
            CashSweepRow(
                requested_at_utc=r.requested_at_utc,
                completed_at_utc=r.completed_at_utc,
                direction=str(r.direction),
                amount_usd=_to_decimal(r.amount_usd),
                status=str(r.status),
                reason=str(r.reason),
            )
            for r in rows
        ]

    async def fetch_latest_product_metadata(self) -> list[ProductMetadataRow]:
        rows = (
            await self._session.execute(
                text(
                    "SELECT DISTINCT ON (product_id) "
                    "       product_id, contract_size, raw "
                    "FROM product_metadata "
                    "ORDER BY product_id, captured_at_utc DESC"
                )
            )
        ).fetchall()
        return [
            ProductMetadataRow(
                product_id=str(r.product_id),
                contract_size=_to_decimal_or_none(r.contract_size),
                raw=_coerce_json_object(r.raw, event="funding_repo_metadata_raw_invalid"),
            )
            for r in rows
        ]

    async def fetch_latest_balance_snapshot_audit(self) -> BalanceSnapshotAuditRow | None:
        # Latest by sequence_no (chain order), not ingest_clock_ts — a
        # backfilled repair row must never shadow the true latest snapshot.
        row = (
            await self._session.execute(
                text(
                    "SELECT payload_jcs, source_clock_ts "
                    "FROM audit_log "
                    "WHERE event_type = 'balance_snapshot_recorded' "
                    "ORDER BY sequence_no DESC LIMIT 1"
                )
            )
        ).fetchone()
        if row is None:
            return None
        return BalanceSnapshotAuditRow(
            payload=_coerce_json_object(
                row.payload_jcs, event="funding_repo_balance_payload_invalid"
            ),
            recorded_at_utc=row.source_clock_ts,
        )

    async def fetch_latest_cash_balance_snapshot(self) -> CashBalanceSnapshotRow | None:
        row = (
            await self._session.execute(
                text(
                    "SELECT snapshot_date_utc, captured_at_utc, spot_usd, cbi_usdc "
                    "FROM cash_balance_snapshots "
                    "ORDER BY snapshot_date_utc DESC LIMIT 1"
                )
            )
        ).fetchone()
        if row is None:
            return None
        return CashBalanceSnapshotRow(
            snapshot_date_utc=row.snapshot_date_utc,
            captured_at_utc=row.captured_at_utc,
            spot_usd=_to_decimal_or_none(row.spot_usd),
            cbi_usdc=_to_decimal_or_none(row.cbi_usdc),
        )

    async def fetch_last_reward_credit(self) -> RewardTransactionRow | None:
        # amount > 0: the capture persists forensic debits too (loose
        # filter until the real reward type is observed); only credits
        # render as payouts.
        row = (
            await self._session.execute(
                text(
                    "SELECT venue_created_at_utc, amount, currency, transaction_type "
                    "FROM usdc_reward_transactions "
                    "WHERE amount > 0 "
                    "ORDER BY venue_created_at_utc DESC LIMIT 1"
                )
            )
        ).fetchone()
        if row is None:
            return None
        return RewardTransactionRow(
            venue_created_at_utc=row.venue_created_at_utc,
            amount=_to_decimal(row.amount),
            currency=str(row.currency),
            transaction_type=str(row.transaction_type),
        )

    async def fetch_lifetime_reward_credits_total(self) -> Decimal:
        row = (
            await self._session.execute(
                text(
                    "SELECT COALESCE(SUM(amount), 0) AS total "
                    "FROM usdc_reward_transactions "
                    "WHERE amount > 0"
                )
            )
        ).fetchone()
        if row is None:
            return Decimal("0")
        return _to_decimal(row.total)


__all__ = [
    "BalanceSnapshotAuditRow",
    "CashBalanceSnapshotRow",
    "CashSweepRow",
    "FundingQueryRepo",
    "FundingRateRow",
    "HeldPositionRow",
    "PostgresFundingQueryRepo",
    "ProductMetadataRow",
    "RewardTransactionRow",
]
