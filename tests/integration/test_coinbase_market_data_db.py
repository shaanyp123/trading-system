"""Integration tests: market-data worker writes against real Postgres.

Crypto-pivot C0-B2a. Spins up postgres:16 via testcontainers, migrates
to the C0-B1 crypto tables revision, then exercises the worker's two DB
jobs through a real ``async_sessionmaker``:

1. ``run_funding_snapshot_once`` — one ``funding_rates`` row per perp
   product with a funding rate; re-running the same UTC hour is a no-op
   (``ON CONFLICT (product_id, observed_at_utc) DO NOTHING``); NUMERIC
   round-trips as Decimal ([A05]).
2. ``run_daily_snapshot_once`` — one ``product_metadata`` row per perp
   product per UTC day, idempotent on re-run, ``raw`` JSONB carries the
   full venue payload.

A22 enforced: real Postgres, no mocks. Skips cleanly without Docker.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from services.data.coinbase_market_data import (
    CoinbaseMarketDataConfig,
    CoinbaseMarketDataWorker,
)

pytestmark = pytest.mark.integration

testcontainers = pytest.importorskip("testcontainers.postgres")
PostgresContainer: Any = testcontainers.PostgresContainer

REPO_ROOT = Path(__file__).resolve().parents[2]
_CRYPTO_REVISION = "20260708_crypto_pivot_tables"
NOW = datetime(2026, 7, 9, 12, 34, 56, tzinfo=UTC)


def _require_docker() -> None:
    docker = pytest.importorskip("docker")
    try:
        docker.from_env().ping()
    except Exception as exc:
        pytest.skip(f"Docker daemon unavailable: {exc}")


def _run_alembic(sync_url: str, target: str) -> None:
    from alembic.config import Config

    from alembic import command

    prev_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = sync_url
    try:
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        cfg.set_main_option("sqlalchemy.url", sync_url)
        command.upgrade(cfg, target)
    finally:
        if prev_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev_url


@pytest.fixture(scope="module")
def pg_url() -> Iterator[str]:
    _require_docker()
    with PostgresContainer("postgres:16") as container:
        sync_url = container.get_connection_url()
        _run_alembic(sync_url, _CRYPTO_REVISION)
        yield sync_url


@pytest_asyncio.fixture()
async def session_factory(pg_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async_url = pg_url.replace("+psycopg2", "+asyncpg")
    engine: AsyncEngine = create_async_engine(async_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _perp_raw(product_id: str, funding: str | None = "0.0000125") -> dict[str, Any]:
    perp_details: dict[str, Any] = {}
    if funding is not None:
        perp_details = {"funding_rate": funding}
    return {
        "product_id": product_id,
        "product_type": "FUTURE",
        "product_venue": "CDE",
        "price": "100000.5",
        "price_increment": "5",
        "trading_disabled": False,
        "future_product_details": {
            "contract_size": "0.01",
            "contract_expiry_type": "PERPETUAL",
            "perpetual_details": perp_details,
            "initial_margin_rate": "0.2",
            "maintenance_margin_rate": "0.1",
        },
    }


class _FakeRest:
    def __init__(self, products: list[dict[str, Any]]) -> None:
        self.products = products

    async def get_future_products(self) -> list[dict[str, Any]]:
        return self.products

    async def get_daily_candles(
        self, product_id: str, *, start_unix: int, end_unix: int
    ) -> list[dict[str, Any]]:
        return []


def _worker(
    factory: async_sessionmaker[AsyncSession], products: list[dict[str, Any]]
) -> CoinbaseMarketDataWorker:
    return CoinbaseMarketDataWorker(
        config=CoinbaseMarketDataConfig(startup_grace_s=0.0),
        session_factory=factory,
        rest_client=_FakeRest(products),
        clock=lambda: NOW,
    )


class TestFundingRatesWrites:
    async def test_writes_rows_and_is_idempotent_within_hour(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        products = [_perp_raw("BIP-20DEC30-CDE"), _perp_raw("EIP-20DEC30-CDE", "-0.000002")]
        worker = _worker(session_factory, products)

        written_first = await worker.run_funding_snapshot_once(now_utc=NOW)
        assert written_first == 2
        # same UTC hour again → ON CONFLICT no-ops, no duplicate rows
        await worker.run_funding_snapshot_once(now_utc=NOW)

        async with session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT product_id, observed_at_utc, rate_per_interval,"
                        "       interval_hours, mark_price, source "
                        "FROM funding_rates ORDER BY product_id"
                    )
                )
            ).fetchall()
        assert len(rows) == 2
        bip = rows[0]
        assert bip.product_id == "BIP-20DEC30-CDE"
        assert bip.observed_at_utc == datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
        assert bip.rate_per_interval == Decimal("0.0000125")  # NUMERIC → Decimal (A05)
        assert bip.interval_hours == Decimal("1")
        assert bip.mark_price == Decimal("100000.5")
        assert bip.source == "coinbase_advanced"
        eip = rows[1]
        assert eip.rate_per_interval == Decimal("-0.000002")

    async def test_next_hour_appends_new_rows(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from datetime import timedelta

        worker = _worker(session_factory, [_perp_raw("BIP-20DEC30-CDE")])
        await worker.run_funding_snapshot_once(now_utc=NOW + timedelta(hours=1))
        async with session_factory() as session:
            count = (
                await session.execute(
                    text("SELECT COUNT(*) FROM funding_rates WHERE product_id = 'BIP-20DEC30-CDE'")
                )
            ).scalar_one()
        assert count == 2  # hour 12 (previous test) + hour 13


class TestProductMetadataWrites:
    async def test_writes_snapshot_and_is_idempotent_within_day(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        worker = _worker(session_factory, [_perp_raw("BIP-20DEC30-CDE")])
        written = await worker.run_daily_snapshot_once(now_utc=NOW)
        assert written == 1
        # same UTC day again → no duplicate snapshot
        await worker.run_daily_snapshot_once(now_utc=NOW)

        async with session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT product_id, captured_at_utc, tick_size, contract_size,"
                        "       initial_margin_requirement, maintenance_margin_requirement,"
                        "       trading_disabled, raw "
                        "FROM product_metadata"
                    )
                )
            ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row.product_id == "BIP-20DEC30-CDE"
        assert row.captured_at_utc == datetime(2026, 7, 9, 0, 0, tzinfo=UTC)
        assert row.tick_size == Decimal("5")
        assert row.contract_size == Decimal("0.01")
        assert row.initial_margin_requirement == Decimal("0.2")
        assert row.maintenance_margin_requirement == Decimal("0.1")
        assert row.trading_disabled is False
        # asyncpg returns JSONB as dict; full venue payload preserved
        assert row.raw["product_id"] == "BIP-20DEC30-CDE"
        assert row.raw["future_product_details"]["contract_size"] == "0.01"
