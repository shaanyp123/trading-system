"""services/data/usdc_rewards.py — USDC rewards + cash-balance capture job.

Operator feature request (decisions-log 2026-07-09/-10): **measure,
don't estimate** the Coinbase One USDC rewards the idle CBI USDC
balance earns, and snapshot the cash balances the venue's auto-sweep
semantics make load-bearing. One daily job, two captures, fired at
00:20 UTC (after the 00:15 recon cycle) by an api-lifespan scheduler
(``services/api/main.py`` reuses the generic once-per-UTC-day
``ReconciliationScheduler`` primitive):

1. **v2 USDC ledger poll** — reads the operator's Coinbase v2 account
   ledger (``GET /v2/accounts/USDC/transactions``; verified live
   2026-07-09 to be readable with the existing CDP key) and persists
   candidate reward transactions into ``usdc_reward_transactions``.
   The filter is DELIBERATELY LOOSE: rewards accrue daily but PAY OUT
   WEEKLY EVERY FRIDAY (operator app screenshot 2026-07-09), and the
   reward transaction ``type`` is unknown until the first payout is
   observed — so the poller persists EVERY transaction whose type is
   not in :data:`EXCLUDED_TRANSACTION_TYPES` (the observed
   conversion/transfer types: buy/sell/send) and logs a structured
   warning for any type outside :data:`EXPECTED_REWARD_TYPES`
   (candidates only, never a filter). The filter locks to the real
   type in a follow-up PR once the first payout lands. Idempotent via
   ``UNIQUE (venue_transaction_id)`` + ``ON CONFLICT DO NOTHING`` —
   the daily re-poll of an overlapping window re-inserts nothing.

2. **Cash balance snapshot** — the CBI spot USD + USDC ``available``
   balances from ``get_accounts``, one row per UTC calendar day into
   ``cash_balance_snapshots`` (first capture of the day wins). These
   are the two balances the venue semantics finding (decisions-log
   entry riding this PR) makes operationally significant: **spot USD
   IS trading equity** (``futures_balance_summary.equity`` spans the
   whole USD complex; the venue auto-sweeps unused derivatives margin
   back to spot and pulls it in when positions open), while **CBI
   USDC is INVISIBLE to equity** (it surfaces only in buying power).

**Authenticated — second, deliberately narrower client.** This module
hosts the codebase's second authenticated Coinbase client (same CDP
key pair as ``services/execution/coinbase_client.py``, same lazy-SDK +
``asyncio.to_thread`` bridge). It is NOT part of the execution
transport on purpose: the §3.1 ``CoinbaseBrokerClient`` Protocol is
deliberately narrow (its shape is a locked test surface), and a cash
telemetry reader has no business on it — this client exposes ONLY
account-balance and ledger reads, so a defect here structurally
cannot touch orders. ``services/data/**`` is NOT on the [A02]
forbidden whitelist; the tables landed under risk-review with the
``20260710_usdc_rewards_capture`` migration.

Relation to delta spec §3.6: the C2 ``services/risk/cash_manager.py``
sweep worker will ACT on cash; this module only OBSERVES it (and
closes the §3.6 residual "rewards accrue on the full CBI USDC
balance" verification with measured data).

[A05] — all money is ``Decimal``, coerced at the wire boundary.
[A06] — tz-aware UTC everywhere. [A25] — structlog only.
[A13] — no product IDs here at all; account currency codes (USD/USDC)
are stable venue symbols, not derivative IDs.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Protocol

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

log = structlog.get_logger()


#: Advanced Trade API host (same default as the execution transport).
DEFAULT_API_HOST: Final[str] = "api.coinbase.com"

#: Per-venue-call timeout (same default as the execution transport).
DEFAULT_TIMEOUT_S: Final[float] = 30.0

#: Daily capture fire time: 00:20 UTC — after the 00:05 decision and
#: the 00:15 recon cycle, so the day's cash observation never races
#: either. Consumed by the api-lifespan scheduler wiring.
DEFAULT_CASH_CAPTURE_TIME_UTC: Final[time] = time(hour=0, minute=20)

#: The v2 account whose ledger is polled. USDC is where Coinbase One
#: rewards accrue (delta spec §3.6); a stable venue currency code, not
#: a derivative product ID ([A13] does not apply).
REWARDS_ACCOUNT_CURRENCY: Final[str] = "USDC"

#: v2 ledger transaction types NEVER persisted — the conversion +
#: transfer types observed live on 2026-07-09 (USD<->USDC conversions
#: surface as buy/sell rows; send covers transfers). Everything else
#: is a candidate reward row until the real payout type is observed
#: and the filter locks (follow-up PR).
EXCLUDED_TRANSACTION_TYPES: Final[frozenset[str]] = frozenset({"buy", "sell", "send"})

#: Plausible reward-type candidates (v2 taxonomy guesses). Used ONLY
#: to classify log lines — a persisted type outside this set logs
#: ``usdc_ledger_unknown_transaction_type`` so the operator sees it —
#: never as a persistence filter. Do not add types here to silence the
#: warning; observe the real payout type, then LOCK the filter.
EXPECTED_REWARD_TYPES: Final[frozenset[str]] = frozenset(
    {"interest", "reward", "staking_reward", "incentive"}
)

#: Pagination guards: 100 rows/page is the v2 ledger max; 20 pages
#: bounds a runaway cursor loop at 2 000 rows (the account's entire
#: history fits in a couple of pages today — hitting the cap logs).
V2_LEDGER_PAGE_LIMIT: Final[int] = 100
V2_LEDGER_MAX_PAGES: Final[int] = 20

#: get_accounts pagination guards (250 is the documented max limit).
ACCOUNTS_PAGE_LIMIT: Final[int] = 250
ACCOUNTS_MAX_PAGES: Final[int] = 10


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class V2LedgerTransaction:
    """One Coinbase v2 ledger transaction, wire-parsed. Money is Decimal."""

    venue_transaction_id: str
    transaction_type: str
    status: str | None
    amount: Decimal
    currency: str
    native_amount: Decimal | None
    native_currency: str | None
    description: str | None
    venue_created_at_utc: datetime
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CashAccountBalance:
    """One ``get_accounts`` account's available balance, wire-parsed."""

    currency: str
    available: Decimal
    name: str | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CaptureResult:
    """Counts from one capture run — for logs + tests, never re-parsed."""

    ledger_rows_seen: int
    ledger_rows_persisted: int
    ledger_rows_new: int
    snapshot_written: bool


class CashCaptureError(Exception):
    """A venue call inside the capture job failed terminally.

    Mirrors the execution transport's single-attempt contract: no
    retry loop here — the scheduler's next daily fire (or a restart
    re-fire) is the retry, and every insert is idempotent.
    """

    def __init__(self, *, operation: str, detail: str) -> None:
        self.operation = operation
        self.detail = detail
        super().__init__(f"{operation}: {detail}")


# ---------------------------------------------------------------------------
# Pure wire parsers (dict fixtures pin these in tests)
# ---------------------------------------------------------------------------


def _dec_or_none(value: object) -> Decimal | None:
    """Wire value → Decimal, unwrapping v2 ``{"amount"/"value", "currency"}``."""
    if value is None:
        return None
    if isinstance(value, dict):
        inner = value.get("amount", value.get("value"))
        return _dec_or_none(inner)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _money_currency(value: object) -> str | None:
    if isinstance(value, dict):
        currency = value.get("currency")
        if isinstance(currency, str) and currency:
            return currency
    return None


def _ts_or_none(value: object) -> datetime | None:
    """ISO-8601 wire timestamp → tz-aware UTC datetime (A06)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_v2_transaction(payload: dict[str, Any]) -> V2LedgerTransaction | None:
    """v2 ledger row → DTO; None when a load-bearing field is missing.

    Load-bearing: id (idempotency key), type, a parseable signed
    amount, and created_at. Everything else degrades to None — the raw
    payload is persisted verbatim for forensics either way.
    """
    tx_id = payload.get("id")
    tx_type = payload.get("type")
    amount = _dec_or_none(payload.get("amount"))
    currency = _money_currency(payload.get("amount"))
    created_at = _ts_or_none(payload.get("created_at"))
    if (
        not isinstance(tx_id, str)
        or not tx_id
        or not isinstance(tx_type, str)
        or not tx_type
        or amount is None
        or created_at is None
    ):
        return None
    status = payload.get("status")
    description = payload.get("description")
    return V2LedgerTransaction(
        venue_transaction_id=tx_id,
        transaction_type=tx_type,
        status=status if isinstance(status, str) and status else None,
        amount=amount,
        currency=currency or REWARDS_ACCOUNT_CURRENCY,
        native_amount=_dec_or_none(payload.get("native_amount")),
        native_currency=_money_currency(payload.get("native_amount")),
        description=description if isinstance(description, str) and description else None,
        venue_created_at_utc=created_at,
        raw=payload,
    )


def parse_cash_account(payload: dict[str, Any]) -> CashAccountBalance | None:
    """``get_accounts`` account row → DTO; None when unparseable."""
    currency = payload.get("currency")
    available = _dec_or_none(payload.get("available_balance"))
    if not isinstance(currency, str) or not currency or available is None:
        return None
    name = payload.get("name")
    return CashAccountBalance(
        currency=currency,
        available=available,
        name=name if isinstance(name, str) and name else None,
        raw=payload,
    )


def extract_v2_page(body: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    """v2 list envelope → (data rows, next ``starting_after`` cursor)."""
    data = body.get("data")
    rows = [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []
    pagination = body.get("pagination")
    cursor: str | None = None
    if isinstance(pagination, dict):
        next_starting_after = pagination.get("next_starting_after")
        if isinstance(next_starting_after, str) and next_starting_after:
            cursor = next_starting_after
    return rows, cursor


def extract_accounts_page(body: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    """``get_accounts`` envelope → (account rows, next cursor or None)."""
    accounts = body.get("accounts")
    rows = [row for row in accounts if isinstance(row, dict)] if isinstance(accounts, list) else []
    cursor_value = body.get("cursor")
    has_next = bool(body.get("has_next"))
    cursor = cursor_value if has_next and isinstance(cursor_value, str) and cursor_value else None
    return rows, cursor


def should_persist_transaction(tx: V2LedgerTransaction) -> bool:
    """Persistence policy: everything except the observed conversion set.

    Deliberately a superset of "credits that aren't buy/sell/send"
    (the operator directive): unfamiliar DEBITS are persisted too — a
    reward clawback or fee must be visible in the table, not only in a
    log line that scrolls away. Reward AGGREGATION (the funding-strip
    numbers) sums positive amounts only, so persisted debits never
    inflate the totals.
    """
    return tx.transaction_type.lower() not in EXCLUDED_TRANSACTION_TYPES


def classify_transaction_type(tx_type: str) -> str:
    """Log classification: excluded | expected_reward | unknown."""
    lowered = tx_type.lower()
    if lowered in EXCLUDED_TRANSACTION_TYPES:
        return "excluded"
    if lowered in EXPECTED_REWARD_TYPES:
        return "expected_reward"
    return "unknown"


# ---------------------------------------------------------------------------
# Client — the read-only authenticated seam
# ---------------------------------------------------------------------------


class CoinbaseCashClient(Protocol):
    """The two reads the capture job depends on (faked in unit tests)."""

    async def list_account_balances(self) -> list[CashAccountBalance]: ...

    async def list_v2_transactions(self, account_currency: str) -> list[V2LedgerTransaction]: ...


class SdkCoinbaseCashClient:
    """coinbase-advanced-py-backed impl; balance + ledger reads ONLY.

    Same construction/bridge conventions as the execution transport
    (lazy SDK import, ``asyncio.to_thread`` + timeout, single attempt,
    wire errors wrapped) — but a disjoint, read-only surface: no order
    methods exist here by construction.
    """

    def __init__(
        self,
        *,
        api_key_name: str,
        api_private_key: str,
        api_host: str = DEFAULT_API_HOST,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._api_key_name = api_key_name
        self._api_private_key = api_private_key
        self._api_host = api_host
        self._timeout_s = timeout_s
        self._rest: Any = None
        self._log = log.bind(client="coinbase_cash", api_host=api_host)

    def _rest_client(self) -> Any:
        if self._rest is None:
            from coinbase.rest import RESTClient  # lazy: hosts without the SDK still import

            self._rest = RESTClient(
                api_key=self._api_key_name,
                api_secret=self._api_private_key,
                base_url=self._api_host,
                timeout=int(self._timeout_s),
            )
        return self._rest

    async def _call(self, operation: str, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(fn, *args, **kwargs), timeout=self._timeout_s
            )
        except TimeoutError as exc:
            raise CashCaptureError(
                operation=operation, detail=f"venue call exceeded {self._timeout_s}s"
            ) from exc
        except Exception as exc:
            raise CashCaptureError(operation=operation, detail=repr(exc)) from exc

    @staticmethod
    def _to_dict(response: Any, *, operation: str) -> dict[str, Any]:
        if isinstance(response, dict):
            return response
        to_dict = getattr(response, "to_dict", None)
        if callable(to_dict):
            body = to_dict()
            if isinstance(body, dict):
                return body
        raise CashCaptureError(operation=operation, detail="response is not dict-shaped")

    async def list_account_balances(self) -> list[CashAccountBalance]:
        rest = self._rest_client()
        balances: list[CashAccountBalance] = []
        cursor: str | None = None
        for _page in range(ACCOUNTS_MAX_PAGES):
            body = self._to_dict(
                await self._call(
                    "get_accounts", rest.get_accounts, limit=ACCOUNTS_PAGE_LIMIT, cursor=cursor
                ),
                operation="get_accounts",
            )
            rows, cursor = extract_accounts_page(body)
            for row in rows:
                parsed = parse_cash_account(row)
                if parsed is None:
                    self._log.warning(
                        "coinbase_cash_account_unparseable", currency=row.get("currency")
                    )
                    continue
                balances.append(parsed)
            if cursor is None:
                return balances
        self._log.warning("coinbase_cash_accounts_page_cap_hit", max_pages=ACCOUNTS_MAX_PAGES)
        return balances

    async def list_v2_transactions(self, account_currency: str) -> list[V2LedgerTransaction]:
        # URL-path guard: every production caller passes the module
        # constant ("USDC"), but the path interpolation below must never
        # see anything else unvalidated (risk-review 2026-07-10 note).
        if not account_currency.isalnum():
            raise CashCaptureError(
                operation="v2_ledger_page",
                detail=f"account_currency must be alphanumeric, got {account_currency!r}",
            )
        rest = self._rest_client()
        url_path = f"/v2/accounts/{account_currency}/transactions"
        transactions: list[V2LedgerTransaction] = []
        # order=desc explicitly: newest-first is LOAD-BEARING for the
        # page cap — if the venue served ascending, a >2000-row history
        # would silently hide NEW reward rows behind the cap. Pinned by
        # runbook FC-2 (deploy/usdc_rewards/README.md).
        params: dict[str, Any] = {"limit": V2_LEDGER_PAGE_LIMIT, "order": "desc"}
        for _page in range(V2_LEDGER_MAX_PAGES):
            body = self._to_dict(
                await self._call("v2_ledger_page", rest.get, url_path, params=dict(params)),
                operation="v2_ledger_page",
            )
            rows, cursor = extract_v2_page(body)
            for row in rows:
                parsed = parse_v2_transaction(row)
                if parsed is None:
                    self._log.warning(
                        "coinbase_cash_ledger_row_unparseable",
                        transaction_id=row.get("id"),
                        transaction_type=row.get("type"),
                    )
                    continue
                transactions.append(parsed)
            if cursor is None:
                return transactions
            params["starting_after"] = cursor
        self._log.warning("coinbase_cash_ledger_page_cap_hit", max_pages=V2_LEDGER_MAX_PAGES)
        return transactions


# ---------------------------------------------------------------------------
# The daily capture job
# ---------------------------------------------------------------------------


class UsdcRewardsCaptureJob:
    """One daily run: persist candidate reward rows + the day's balances.

    Both halves are independent best-effort — a ledger failure never
    blocks the balance snapshot (and vice versa); the scheduler's next
    fire retries whatever failed, idempotently.
    """

    def __init__(
        self,
        *,
        client: CoinbaseCashClient,
        session_factory: async_sessionmaker[Any],
        clock: Callable[[], datetime] | None = None,
        account_currency: str = REWARDS_ACCOUNT_CURRENCY,
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(tz=UTC))
        self._account_currency = account_currency
        self._log = log.bind(job="usdc_rewards_capture")

    async def run_capture_once(self, session_date_utc: date) -> CaptureResult:
        """One capture pass for a UTC calendar day. Never raises."""
        ledger_seen = 0
        ledger_persisted = 0
        ledger_new = 0
        try:
            ledger_seen, ledger_persisted, ledger_new = await self._capture_ledger()
        except Exception:
            self._log.exception("usdc_rewards_ledger_capture_failed")

        snapshot_written = False
        try:
            snapshot_written = await self._capture_balance_snapshot(session_date_utc)
        except Exception:
            self._log.exception("usdc_rewards_balance_snapshot_failed")

        result = CaptureResult(
            ledger_rows_seen=ledger_seen,
            ledger_rows_persisted=ledger_persisted,
            ledger_rows_new=ledger_new,
            snapshot_written=snapshot_written,
        )
        self._log.info(
            "usdc_rewards_capture_completed",
            session_date_utc=session_date_utc.isoformat(),
            ledger_rows_seen=result.ledger_rows_seen,
            ledger_rows_persisted=result.ledger_rows_persisted,
            ledger_rows_new=result.ledger_rows_new,
            snapshot_written=result.snapshot_written,
        )
        return result

    async def _capture_ledger(self) -> tuple[int, int, int]:
        """Poll the v2 ledger; persist per the loose filter. Returns counts."""
        transactions = await self._client.list_v2_transactions(self._account_currency)
        to_persist: list[V2LedgerTransaction] = []
        for tx in transactions:
            classification = classify_transaction_type(tx.transaction_type)
            if classification == "unknown":
                # The observe-then-lock loop: the first Friday payout's
                # real type surfaces here, then the filter locks.
                self._log.warning(
                    "usdc_ledger_unknown_transaction_type",
                    transaction_type=tx.transaction_type,
                    transaction_id=tx.venue_transaction_id,
                    amount=str(tx.amount),
                    currency=tx.currency,
                    venue_created_at_utc=tx.venue_created_at_utc.isoformat(),
                )
            if should_persist_transaction(tx):
                to_persist.append(tx)

        new_rows = 0
        if to_persist:
            async with self._session_factory() as session:
                async with session.begin():
                    for tx in to_persist:
                        result = await session.execute(
                            text(
                                "INSERT INTO usdc_reward_transactions ("
                                "    venue_transaction_id, account_currency,"
                                "    transaction_type, transaction_status, amount,"
                                "    currency, native_amount, native_currency,"
                                "    description, venue_created_at_utc, raw"
                                ") VALUES ("
                                "    :venue_transaction_id, :account_currency,"
                                "    :transaction_type, :transaction_status, :amount,"
                                "    :currency, :native_amount, :native_currency,"
                                "    :description, :venue_created_at_utc,"
                                "    CAST(:raw AS JSONB)"
                                ") ON CONFLICT (venue_transaction_id) DO NOTHING"
                            ),
                            {
                                "venue_transaction_id": tx.venue_transaction_id,
                                "account_currency": self._account_currency,
                                "transaction_type": tx.transaction_type,
                                "transaction_status": tx.status,
                                "amount": tx.amount,
                                "currency": tx.currency,
                                "native_amount": tx.native_amount,
                                "native_currency": tx.native_currency,
                                "description": tx.description,
                                "venue_created_at_utc": tx.venue_created_at_utc,
                                "raw": json.dumps(tx.raw, default=str),
                            },
                        )
                        rowcount = getattr(result, "rowcount", 0)
                        if isinstance(rowcount, int) and rowcount > 0:
                            new_rows += 1
        return len(transactions), len(to_persist), new_rows

    async def _capture_balance_snapshot(self, session_date_utc: date) -> bool:
        """Snapshot spot USD + CBI USDC availables; first write of the day wins."""
        balances = await self._client.list_account_balances()
        spot_usd = _sum_available(balances, currency="USD")
        cbi_usdc = _sum_available(balances, currency="USDC")
        # Raw retains only the cash-relevant accounts (USD/USDC or a
        # nonzero available) — the full account list is mostly zero-
        # balance crypto account noise.
        raw_accounts = [
            b.raw for b in balances if b.currency in ("USD", "USDC") or b.available != 0
        ]
        captured_at = self._clock()
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    text(
                        "INSERT INTO cash_balance_snapshots ("
                        "    snapshot_date_utc, captured_at_utc, spot_usd,"
                        "    cbi_usdc, raw"
                        ") VALUES ("
                        "    :snapshot_date_utc, :captured_at_utc, :spot_usd,"
                        "    :cbi_usdc, CAST(:raw AS JSONB)"
                        ") ON CONFLICT (snapshot_date_utc) DO NOTHING"
                    ),
                    {
                        "snapshot_date_utc": session_date_utc,
                        "captured_at_utc": captured_at,
                        "spot_usd": spot_usd,
                        "cbi_usdc": cbi_usdc,
                        "raw": json.dumps({"accounts": raw_accounts}, default=str),
                    },
                )
        rowcount = getattr(result, "rowcount", 0)
        written = isinstance(rowcount, int) and rowcount > 0
        if not written:
            self._log.info(
                "usdc_rewards_snapshot_already_captured",
                snapshot_date_utc=session_date_utc.isoformat(),
            )
        return written


def _sum_available(balances: list[CashAccountBalance], *, currency: str) -> Decimal | None:
    """Sum ``available`` across accounts of one currency; None when absent.

    None (no account seen) is distinct from Decimal("0") (account seen,
    empty) — the strip renders the former as "—".
    """
    matched = [b.available for b in balances if b.currency == currency]
    if not matched:
        return None
    total = Decimal("0")
    for value in matched:
        total += value
    return total


def make_capture_callback(
    job: UsdcRewardsCaptureJob,
) -> Callable[[date], Awaitable[None]]:
    """Adapt the job to the scheduler's ``CycleCallback`` (returns None)."""

    async def callback(session_date_utc: date) -> None:
        await job.run_capture_once(session_date_utc)

    return callback


__all__ = [
    "ACCOUNTS_MAX_PAGES",
    "ACCOUNTS_PAGE_LIMIT",
    "DEFAULT_CASH_CAPTURE_TIME_UTC",
    "EXCLUDED_TRANSACTION_TYPES",
    "EXPECTED_REWARD_TYPES",
    "REWARDS_ACCOUNT_CURRENCY",
    "V2_LEDGER_MAX_PAGES",
    "V2_LEDGER_PAGE_LIMIT",
    "CaptureResult",
    "CashAccountBalance",
    "CashCaptureError",
    "CoinbaseCashClient",
    "SdkCoinbaseCashClient",
    "UsdcRewardsCaptureJob",
    "V2LedgerTransaction",
    "classify_transaction_type",
    "extract_accounts_page",
    "extract_v2_page",
    "make_capture_callback",
    "parse_cash_account",
    "parse_v2_transaction",
    "should_persist_transaction",
]
