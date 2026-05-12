"""Signal-ingestion handler — converts a QC ``signal_emitted`` event into
an audit_log row + a signals row.

Called by ``orchestrator.run_once`` for every :class:`QCEvent` whose
``event_type == "signal_emitted"``. Other event types route to their own
handlers (PR-C: order_placed/filled; PR-D: reconciliation/fill events);
for PR-A the orchestrator logs them and skips the body but STILL appends
a generic audit_log row so the chain stays continuous.

Two-step write contract (mirrors ``services.risk.dispatch.apply_state_transition``
from Day 25; documented at backend-spec §2.10.1):

  1. ``append_audit_event(...)`` — its own SERIALIZABLE transaction
     managed by the audit writer; opens + commits internally.
  2. ``INSERT INTO signals (...)`` — a separate transaction we manage
     here, carrying the freshly-minted ``audit_event_uuid`` as the
     ``decision_diary_entry_id`` linkage column.

Order matters: audit-first per backend-spec §2.10.1. The signals row is
the business-data view; if step 2 fails after step 1 succeeded, the
audit chain still reflects the event landed — the operator can reconcile
via ``decision_diary`` / repair flows.

Phase 0 sentinel identity for the signals row NOT NULL columns:

* ``strategy_hash`` = ``sha1(qc_algorithm_version)``[:40] — deterministic
  per QC algo version string. Once the QC algo's payload includes a real
  ``strategy_hash`` field (PR-B+), this falls back to that.
* ``parameter_set_hash`` = ``sha256(jcs(payload))``[:64] — deterministic
  per signal payload. Real parameter_set_hash arrives when the
  ``parameter_sets`` table head pointer is wired (Phase 1+).
* ``slippage_calibration_version_id`` = head from
  ``slippage_calibration_versions WHERE is_head = TRUE``; auto-bootstraps
  a zero-prior row on first signal arrival per backend-spec §2.14 (no
  calibration row exists until 30 live fills accumulate, so the
  bootstrap row carries trigger='bootstrap' and alpha=0/beta=0 across
  all markets).

Anti-pattern enforcement:
  * A05: ``decision_price`` Decimal-coerced via ``str()`` at the boundary
    (QC emits as JSON number → float). Floats anywhere in the payload
    fed to ``append_audit_event`` raise ``TypeError`` per
    ``services.audit.chain.jcs_serialize``.
  * A06: ``source_clock_ts`` is already tz-aware UTC from
    ``services.qc_adapter.payloads.parse_jsonl_record`` (guards against
    naive tz).
  * A01: audit writes via ``append_audit_event`` exclusively.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final
from uuid import UUID
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.audit.chain import jcs_serialize
from services.audit.event_types import AuditEventType
from services.audit.writer import Environment, PhaseAtEmit, append_audit_event

from . import db as qc_db
from .payloads import QCEvent

log = structlog.get_logger()


#: ET zone for the ``signals.session_date`` derivation.
#:
#: ``session_date`` is the trading-day label; UTC midnight does not
#: cleanly correspond to a CME session boundary, so we anchor on
#: America/New_York wall clock per dev-guide §3.7.
_ET: Final[ZoneInfo] = ZoneInfo("America/New_York")


#: Required keys in the QC ``signal_emitted`` payload per backend-spec
#: §4.5.1. Missing → :class:`SignalIngestError`.
_REQUIRED_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {"market", "direction", "target_contracts", "decision_price", "sizing_trace"}
)


#: Locked directions per the signals.direction CHECK constraint
#: (alembic 0002:138).
_VALID_DIRECTIONS: Final[frozenset[str]] = frozenset({"long", "short", "flat"})


class SignalIngestError(ValueError):
    """Raised on payload-shape problems — caller routes the bad record
    to ``data_quality_events`` instead of into ``signals``."""


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Pair of UUIDs minted during a successful ``ingest_signal_emitted``."""

    audit_event_uuid: UUID
    signal_id: UUID


async def ingest_signal_emitted(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: UUID,
    env: Environment,
    phase_at_emit: PhaseAtEmit,
    event: QCEvent,
) -> IngestResult:
    """Ingest one ``signal_emitted`` QC event.

    Takes a ``session_factory`` (not a session) so each DB operation
    can run in its own fresh session — necessary because the audit
    writer manages its own SERIALIZABLE transactions and reusing a
    session across multiple writer calls hits "transaction already
    begun" (DP-021 carryover).

    Pipeline:
      1. Validate event_type + payload shape.
      2. Coerce JSON numbers → :class:`Decimal`.
      3. Append the ``SIGNAL_EMITTED`` audit row (fresh session).
      4. Resolve / bootstrap slippage HEAD FK (fresh session + audit row).
      5. INSERT the signals row carrying the audit event_uuid
         (fresh session).

    Raises :class:`SignalIngestError` for shape issues so the orchestrator
    routes the bad record to data_quality_events without writing the
    audit row.
    """
    if event.event_type != AuditEventType.SIGNAL_EMITTED.value:
        raise SignalIngestError(
            f"ingest_signal_emitted called with event_type={event.event_type!r}; "
            f"expected {AuditEventType.SIGNAL_EMITTED.value!r}"
        )

    payload = event.payload
    missing = _REQUIRED_PAYLOAD_KEYS - payload.keys()
    if missing:
        raise SignalIngestError(
            f"signal_emitted payload missing required key(s): {sorted(missing)}"
        )

    market = payload["market"]
    if not isinstance(market, str) or not market:
        raise SignalIngestError(f"market must be non-empty string, got {market!r}")

    direction = payload["direction"]
    if direction not in _VALID_DIRECTIONS:
        raise SignalIngestError(
            f"direction must be one of {sorted(_VALID_DIRECTIONS)}, got {direction!r}"
        )

    target_contracts_raw = payload["target_contracts"]
    if not isinstance(target_contracts_raw, int) or isinstance(target_contracts_raw, bool):
        raise SignalIngestError(
            f"target_contracts must be int, got {type(target_contracts_raw).__name__}"
        )
    target_contracts: int = target_contracts_raw

    decision_price = _to_decimal(payload["decision_price"], field="decision_price")

    sizing_trace_raw = payload["sizing_trace"]
    if not isinstance(sizing_trace_raw, dict):
        raise SignalIngestError(
            f"sizing_trace must be object, got {type(sizing_trace_raw).__name__}"
        )

    # Build the AUDIT payload — must be A05-clean (no floats). Convert
    # the entire payload's money-shaped fields to Decimal via _convert_floats.
    audit_payload: dict[str, Any] = {
        "qc_sequence_no": event.sequence_no,
        "qc_algorithm_version": event.qc_algorithm_version,
        "source_clock_ts": event.source_clock_ts.isoformat(),
        "market": market,
        "direction": direction,
        "target_contracts": target_contracts,
        "decision_price": decision_price,
        "sizing_trace": _convert_floats_to_decimal(sizing_trace_raw),
    }
    # Sanity check: must JCS-serialize without raising A05.
    try:
        jcs_serialize(audit_payload)
    except TypeError as exc:
        raise SignalIngestError(f"audit payload not JCS-serializable: {exc}") from exc

    bound = log.bind(
        service_name="qc_adapter",
        market=market,
        direction=direction,
        qc_sequence_no=event.sequence_no,
    )

    # Step 1 (audit-first): append SIGNAL_EMITTED audit_log row in its
    # own SERIALIZABLE transaction. Fresh session — writer manages txn.
    async with session_factory() as audit_session:
        audit_record = await append_audit_event(
            audit_session,
            AuditEventType.SIGNAL_EMITTED,
            audit_payload,
            account_id=account_id,
            env=env,
            phase_at_emit=phase_at_emit,
            source_clock_ts=event.source_clock_ts,
        )
    bound.info(
        "qc_signal_emitted_audit_appended",
        audit_event_uuid=str(audit_record.event_uuid),
        audit_sequence_no=audit_record.sequence_no,
    )

    # Step 2: resolve the slippage HEAD FK. Per backend-spec §2.14, Phase
    # 0/1-first-30-days has no calibration row; bootstrap one on demand
    # before the first signals INSERT so the FK constraint is satisfied.
    slippage_version_id = await _resolve_or_bootstrap_slippage_head(
        session_factory,
        account_id=account_id,
        env=env,
        phase_at_emit=phase_at_emit,
    )

    # Step 3: INSERT the signals row in a fresh session/transaction.
    async with session_factory() as insert_session:
        async with insert_session.begin():
            signal_row = qc_db.SignalInsertRow(
                account_id=account_id,
                env=env,
                market=market,
                emitted_at_utc=event.source_clock_ts,
                session_date=_session_date_from_utc(event.source_clock_ts),
                direction=direction,
                signal_type="entry",
                strategy_hash=_derive_strategy_hash(event.qc_algorithm_version),
                parameter_set_hash=_derive_parameter_set_hash(payload),
                slippage_calibration_version_id=slippage_version_id,
                decision_price=decision_price,
                target_contracts=target_contracts,
                sizing_trace=sizing_trace_raw,
            )
            signal_id = await qc_db.insert_signal_row(insert_session, signal_row)

    bound.info(
        "qc_signal_emitted_row_inserted",
        signal_id=str(signal_id),
        audit_event_uuid=str(audit_record.event_uuid),
    )

    return IngestResult(
        audit_event_uuid=audit_record.event_uuid,
        signal_id=signal_id,
    )


# ---------------------------------------------------------------------------
# Slippage bootstrap (audit-first per backend-spec §2.10.1)
# ---------------------------------------------------------------------------


async def _resolve_or_bootstrap_slippage_head(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: UUID,
    env: Environment,
    phase_at_emit: PhaseAtEmit,
) -> UUID:
    """Return the active slippage HEAD UUID; bootstrap a zero-prior row
    on first call.

    Concurrency: the ``slippage_head`` partial unique index in alembic
    0003 enforces "at most one head row" at the schema level. If two
    qc_adapter workers race the bootstrap, one wins + the other catches
    an IntegrityError on the INSERT — we then re-read the head and
    return that UUID. This makes the bootstrap path idempotent.

    Order matters: audit-first per backend-spec §2.10.1. We append a
    SLIPPAGE_CALIBRATION_RECALIBRATED audit row BEFORE the calibration
    INSERT so the row's audit_event_uuid FK is real.

    Each DB op uses a fresh session from ``session_factory`` to avoid
    DP-021 transaction-state interference.
    """
    async with session_factory() as session:
        head = await qc_db.fetch_slippage_head_version_id(session)
    if head is not None:
        return head

    now = datetime.now(tz=UTC)
    audit_payload: dict[str, Any] = {
        "trigger": "bootstrap",
        "is_head": True,
        "per_market_coefficients": {"_bootstrap": "zero-prior"},
        "version_no": 1,
        "calibrated_at_utc": now.isoformat(),
        "notes": (
            "Phase-0 zero-prior bootstrap auto-generated by qc_adapter on "
            "first signal arrival per backend-spec §2.14."
        ),
    }
    async with session_factory() as audit_session:
        record = await append_audit_event(
            audit_session,
            AuditEventType.SLIPPAGE_CALIBRATION_RECALIBRATED,
            audit_payload,
            account_id=account_id,
            env=env,
            phase_at_emit=phase_at_emit,
        )
    log.info(
        "slippage_calibration_bootstrap_audit_appended",
        service_name="qc_adapter",
        audit_event_uuid=str(record.event_uuid),
        audit_sequence_no=record.sequence_no,
    )

    try:
        async with session_factory() as insert_session:
            async with insert_session.begin():
                bootstrap_id = await qc_db.insert_slippage_bootstrap_row(
                    insert_session,
                    audit_event_uuid=record.event_uuid,
                    calibrated_at_utc=now,
                )
    except Exception:
        # Possible race: another worker won the bootstrap. Re-read.
        async with session_factory() as recheck_session:
            head_after_race = await qc_db.fetch_slippage_head_version_id(recheck_session)
        if head_after_race is not None:
            log.info(
                "slippage_calibration_bootstrap_race_lost",
                service_name="qc_adapter",
                winning_head=str(head_after_race),
            )
            return head_after_race
        raise

    log.info(
        "slippage_calibration_bootstrap_inserted",
        service_name="qc_adapter",
        slippage_calibration_version_id=str(bootstrap_id),
        audit_event_uuid=str(record.event_uuid),
    )
    return bootstrap_id


# ---------------------------------------------------------------------------
# Helpers (pure)
# ---------------------------------------------------------------------------


def _to_decimal(value: Any, *, field: str) -> Decimal:
    """Convert a JSON-emitted number to :class:`Decimal` via ``str()``.

    ``Decimal(float_value)`` introduces binary-rounding noise — go
    through ``str(value)`` to keep the literal exactly as QC emitted it
    (matches dev-guide §3.8 + anti-pattern A05).
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool is subclass of int — reject explicitly
        raise SignalIngestError(f"{field}: bool not accepted, got {value!r}")
    if isinstance(value, (int, float, str)):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise SignalIngestError(f"{field}: cannot coerce {value!r} to Decimal: {exc}") from exc
    raise SignalIngestError(f"{field}: unsupported type {type(value).__name__}")


def _convert_floats_to_decimal(obj: Any) -> Any:
    """Recursively convert every float in ``obj`` to :class:`Decimal`.

    JCS rejects floats per A05; QC emits prices as JSON numbers which
    Python parses as floats. We walk the tree and reconstruct so the
    audit payload is A05-clean before passing to ``append_audit_event``.
    """
    if isinstance(obj, dict):
        return {k: _convert_floats_to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_convert_floats_to_decimal(v) for v in obj]
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return Decimal(str(obj))
    return obj


def _derive_strategy_hash(qc_algorithm_version: str) -> str:
    """Return a deterministic 40-char hex string identifying the strategy.

    If the QC algorithm version already ends with a 40-char hex SHA
    (e.g. ``v1-trend-following-9d2f7a1c20...``), extract it; otherwise
    SHA-1 the whole version string. Either way the output matches the
    ``signals.strategy_hash CHAR(40)`` constraint.
    """
    suffix = qc_algorithm_version.rsplit("-", 1)[-1].lower()
    if len(suffix) == 40 and all(c in "0123456789abcdef" for c in suffix):
        return suffix
    return hashlib.sha1(qc_algorithm_version.encode("utf-8")).hexdigest()


def _derive_parameter_set_hash(payload: dict[str, Any]) -> str:
    """Return a deterministic 64-char hex hash of the signal payload.

    SHA-256 of the JCS-canonicalized payload (after Decimal coercion so
    the JCS step doesn't trip A05). Stable across operator deploys
    because JCS sorts keys lexicographically.
    """
    canonical = _convert_floats_to_decimal(payload)
    return hashlib.sha256(jcs_serialize(canonical)).hexdigest()


def _session_date_from_utc(ts: datetime) -> datetime:
    """Compute the trading-day ``session_date`` for a UTC timestamp.

    Anchored on America/New_York wall clock; matches the QC algorithm's
    17:30 ET cycle convention (a signal emitted at 17:30 ET on Tuesday
    has session_date = Tuesday).
    """
    if ts.tzinfo is None:
        # Belt-and-braces — payloads.parse_jsonl_record already enforces.
        ts = ts.replace(tzinfo=UTC)
    et = ts.astimezone(_ET)
    return datetime(et.year, et.month, et.day, tzinfo=_ET)


__all__ = [
    "IngestResult",
    "SignalIngestError",
    "ingest_signal_emitted",
]
