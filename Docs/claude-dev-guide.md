# CLAUDE CODE DEV GUIDE
## Solo-Operator Algorithmic Trading System — Coding Conventions and Patterns
**For:** Claude Code sessions. Every session reads this at start.
**Not:** a spec. The specs define WHAT. This defines HOW you write the code.
**Authority:** specs win on architecture. This guide wins on implementation patterns.

---

# TABLE OF CONTENTS

1. [Session Protocol](#1-session-protocol)
2. [Repo Layout](#2-repo-layout)
3. [Python Backend Coding Standards](#3-python-backend-coding-standards)
4. [TypeScript Frontend Coding Standards](#4-typescript-frontend-coding-standards)
5. [Architecture Patterns](#5-architecture-patterns)
6. [Testing Patterns](#6-testing-patterns)
7. [Database Patterns](#7-database-patterns)
8. [Frontend Patterns](#8-frontend-patterns)
9. [Cross-Cutting Patterns](#9-cross-cutting-patterns)
10. [Phase-Specific Dev Priorities](#10-phase-specific-dev-priorities)
11. [Anti-Patterns](#11-anti-patterns)
12. [PR-Drafting Templates](#12-pr-drafting-templates)
13. [Operator Review Checklist](#13-operator-review-checklist)
14. [Living Document Protocol](#14-living-document-protocol)

---

# 1. Session Protocol

## 1.1 Session Start

Execute in order at the start of every coding session:

1. `cat CLAUDE.md` if present — operator-level overrides always trump this guide.
2. `git status && git log --oneline -10` — verify branch state; detect uncommitted changes or mid-flight work from prior sessions.
3. Check for open draft PRs: `gh pr list --state open` — if a prior session left a draft, read it before starting new work.
4. Read the relevant spec section for today's task (`backend-spec.md §N` or `frontend-spec.md §N`).
5. Read the relevant implementation-guide week/month block (`implementation-guide.md §3` for Phase 0, `§4` for Phase 1, etc.).
6. Read the relevant source files for the component you are about to touch.

Never start writing code until you have confirmed: (a) you are on the right branch, (b) no in-flight work conflicts with your task, (c) you understand the spec contract for the component.

## 1.2 Session End

Commit discipline:
- One logical change per commit. Never batch unrelated changes.
- Conventional-commits format: `feat(scope): description`, `fix(scope): description`, `test(scope): description`, `chore(scope): description`.
- Scope matches the service or package: `feat(audit): append_audit_event with retry loop`, `fix(risk): m_combined floor applied before MIN`.
- Run `make test` (or equivalent) before every commit. CI also gates but local verification is non-negotiable.

After completing a Phase 0 verification gate:
- Mark the gate item as complete in `implementation-guide.md` by checking its checkbox.
- Open a PR if the gate requires it; use the PR template from §12.

What to leave for the operator:
- Any decision requiring strategy logic judgment → document in PR description, not in code comments.
- Any new canonical pattern introduced → PR must update this dev guide (§14).

## 1.3 Ambiguity Protocol

| Ambiguity type | Rule |
|---|---|
| Strategy logic | ALWAYS escalate. Open a draft PR describing the ambiguity; no code. |
| Risk engine parameters or thresholds | ALWAYS escalate. |
| Pattern choice within an existing pattern family | Decide using the existing nearest example in the repo. Document the choice in the PR description. |
| File/directory naming within a service | Decide per existing conventions in that service. No escalation. |
| New canonical pattern needed (no existing example) | Escalate. Propose in PR. Once approved, add to §5 of this guide. |

**Escalate** means: stop coding, create a draft PR with a description of the two+ options, label it `needs-operator-input`, and end the session. Do not write partial implementations of strategy or risk logic.

## 1.4 Test-Before-Commit Rule

Every code change must pass locally before commit:

```bash
# Python services
cd services/<service>
make test          # runs ruff check, mypy --strict, pytest --cov

# Frontend
cd apps/web
pnpm typecheck && pnpm lint && pnpm test
```

No exceptions. If CI is the only gate, a broken test blocks the PR and wastes time. Run locally first.

---

# 1.5 Locked Decisions Quick Reference

These are decisions you must NOT re-derive or contradict. Memorize:

**Authentication & session:**
- WebAuthn user-verification: **`required`** for register and login (NOT `preferred`)
- Backup codes: **8 single-use codes**, format **10-char base32 in 2 groups of 5** (`ABCDE-FGHIJ`); Argon2id-hashed via `argon2-cffi` (NOT bcrypt)
- TOTP: `pyotp`; secrets AES-encrypted at column-level (separate key from sops)
- Re-auth window: **5 min** of `last_uv_at` for risk-loosening actions; web-only by construction
- Session lifetime: **30 min idle / 24h absolute / 7d refresh token**
- CSRF: SameSite=Strict cookies + double-submit pattern with `X-CSRF-Token` header
- TOTP-only weak session: in-place upgrade when WebAuthn added; `auth_strength: weak → strong` server-side, no re-login

**SSE / web push:**
- Single multiplexed channel: `GET /api/sse/events`
- Envelope: `{type, sequence_no, server_now, data}` where `sequence_no` is GLOBAL monotonic across all event types
- `server_now`: RFC 3339 UTC ms-precision (`2026-05-04T17:30:00.123Z`)
- Replay buffer: **24h** backend retention; client resumes via `last-event-id` header
- Tab limit: **N=4** connections per user; eviction via `session_evicted` control event

**Endpoints:**
- External watchdog push: `POST /api/internal/watchdog` (Bearer auth + Caddy IP allowlist)
- Deep health for watchdog: `GET /internal/health/deep` (Bearer auth)
- Public health: `GET /api/health`
- WebAuthn ceremonies are JS-driven via `navigator.credentials.*`; NO OAuth-style `/auth/callback`

**Phase 1 architecture (CRITICAL):**
- Backend has **NO direct IBKR connection**. Market data + broker state via QC ObjectStore push.
- Defensive trims via instruction protocol (`/instructions/<seq>.json` + 5s poll + ack); Phase 1 round-trip ~20s p99
- Phase 2 transitions to direct `ib-async`; instruction protocol retired

**Backtest authority:**
- LEAN authoritative for PR review surface backtest delta
- vectorbt research-only (parameter sweeps, fast iteration)
- Weekly cron parity test: per-trade slippage ≤5bps, aggregate cumulative P&L ≤0.5% starting equity, trade count within 5%

**Domain placeholder:**
- Use `<your-domain>` (NOT bare `<domain>`) — operator substitutes apex registrable domain at deployment
- WebAuthn `rpID = <your-domain>` (apex); credentials work at production + `paper.<your-domain>` staging via suffix matching

**Email:**
- **Resend** is locked (NOT SES, NOT SendGrid)

If a session asks you to deviate from any of these, escalate per §1.3 — do NOT decide unilaterally.

---

# 2. Repo Layout

## 2.1 Full Structure

```
<repo-root>/
├── apps/
│   └── web/                          # Next.js 14+ App Router + TypeScript
│       ├── src/
│       │   ├── app/                  # App Router routes (page.tsx, layout.tsx)
│       │   ├── components/           # Shared UI components
│       │   └── lib/
│       │       ├── routes.config.ts  # Phase-gate per route
│       │       ├── sse.ts            # SSE connection manager
│       │       ├── queryClient.ts    # TanStack Query global config
│       │       ├── stores/           # Zustand stores
│       │       ├── api/              # Typed API client (consumes packages/api-types)
│       │       ├── format.ts         # formatET, formatPnL, formatNumber, formatPct
│       │       ├── auth.ts
│       │       ├── toast.ts
│       │       └── sentry.ts
│       ├── tailwind.config.ts
│       └── package.json
├── services/
│   ├── api/                          # FastAPI HTTP + SSE  [HOT-FIX whitelist]
│   ├── discord-bot/                  # discord.py          [HOT-FIX whitelist]
│   ├── risk/                         # Risk engine         [FORBIDDEN — PR required]
│   ├── signal/                       # Signal generation   [FORBIDDEN — PR required]
│   ├── audit/                        # Hash-chain writer   [FORBIDDEN — PR required]
│   ├── execution/                    # Order placement     [FORBIDDEN — PR required]
│   ├── reconciliation/               # Recon service       [FORBIDDEN — PR required]
│   ├── calibration/                  # Slippage calib      [FORBIDDEN — PR required]
│   ├── qc_adapter/                   # ObjectStore poll    [HOT-FIX whitelist]
│   └── agent/
│       ├── decisions/                # [FORBIDDEN — PR required]
│       ├── risk_actions/             # [FORBIDDEN — PR required]
│       ├── parameter_changes/        # [FORBIDDEN — PR required]
│       ├── prompts/
│       │   ├── decision/             # [FORBIDDEN — PR required]
│       │   └── system/               # [HOT-FIX whitelist]
│       ├── reporting/                # [HOT-FIX whitelist]
│       ├── monitoring/               # [HOT-FIX whitelist]
│       └── integrations/             # [HOT-FIX whitelist]
├── infrastructure/
│   ├── retry/                        # [HOT-FIX whitelist]
│   ├── broker_reconnect/             # [HOT-FIX whitelist]
│   └── logging/                      # [HOT-FIX whitelist]
├── services/observability/           # [HOT-FIX whitelist]
├── services/monitoring/              # [HOT-FIX whitelist]
├── packages/
│   ├── api-types/                    # TypeScript types codegen'd from FastAPI OpenAPI
│   └── discord-types/                # Pydantic mirrors (manual)
├── alembic/                          # DB migrations       [FORBIDDEN — PR required]
├── deploy/
│   ├── docker-compose.yml
│   ├── docker-compose.staging.yml
│   └── Caddyfile
├── secrets/
│   ├── dev.enc.yaml                  # sops-encrypted; NEVER commit plaintext
│   ├── paper.enc.yaml
│   └── live.enc.yaml
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
└── scripts/
    ├── rotate-secrets.sh
    └── verify_export.py
```

## 2.2 Forbidden Whitelist (require `risk-review-approved` PR label)

Any file matching these globs requires `risk-review-approved` before merge. The pre-merge linter enforces this mechanically.

```
services/risk/**
services/signal/**
services/audit/**
services/execution/**
services/reconciliation/**
services/calibration/**
services/agent/decisions/**
services/agent/risk_actions/**
services/agent/parameter_changes/**
services/agent/prompts/decision/**
alembic/**
```

If your task touches any of these, open a PR immediately. Do not attempt to merge without the label.

## 2.3 Hot-Fix Whitelist (auto-deploy allowed; no required PR)

```
services/api/**
services/discord-bot/**
services/qc_adapter/**
services/agent/reporting/**
services/agent/monitoring/**
services/agent/integrations/**
services/agent/prompts/system/**
services/observability/**
services/monitoring/**
infrastructure/retry/**
infrastructure/broker_reconnect/**
infrastructure/logging/**
```

Hot-fixes in these paths can auto-deploy. Auto-rollback fires within 30 min if metrics breach. Still run tests locally before pushing.

---

# 3. Python Backend Coding Standards

## 3.1 Toolchain

| Tool | Command | Mandatory |
|---|---|---|
| Python | 3.11+ | yes |
| Formatter | `ruff format` | yes |
| Linter | `ruff check --fix` | yes |
| Type checker | `mypy --strict` | yes — zero errors |
| Test runner | `pytest --cov --cov-fail-under=90` for audit/risk/execution; `70` elsewhere | yes |

`mypy --strict` means: `--disallow-untyped-defs`, `--disallow-any-generics`, `--warn-return-any`, `--warn-unused-ignores`. No `# type: ignore` without a comment explaining why.

## 3.2 Naming Conventions

```python
snake_case_function_name()       # functions, variables, module names
PascalCaseClassName              # classes, Pydantic models, Enums
UPPER_CASE_CONSTANT = 42         # module-level constants
_leading_underscore_private()    # module-private functions/vars
```

## 3.3 Type Hints

Every function has a complete signature. No bare `Any`. Pydantic v2 for all boundary types (FastAPI request/response models, event payloads, config).

```python
from datetime import datetime, timezone
from decimal import Decimal
from typing import Final
from uuid import UUID

import structlog
from pydantic import BaseModel, ConfigDict

log = structlog.get_logger()

UTC: Final = timezone.utc

class SignalPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    signal_id: UUID
    market: str
    direction: str
    target_contracts: int
    decision_price: Decimal
    source_clock_ts: datetime

async def process_signal(
    session: AsyncSession,
    payload: SignalPayload,
    *,
    audit_event_uuid: UUID,
) -> None:
    log.info(
        "signal_processing_started",
        signal_id=str(payload.signal_id),
        market=payload.market,
        audit_event_uuid=str(audit_event_uuid),
    )
    ...
```

## 3.4 Async-First

Every IO function is `async def`. Never block the event loop. Use `asyncio.gather` for concurrent independent calls.

```python
import asyncio
from sqlalchemy.ext.asyncio import AsyncSession

async def load_market_state(
    session: AsyncSession,
    markets: list[str],
) -> tuple[list[Bar], list[Position]]:
    bars, positions = await asyncio.gather(
        fetch_bars(session, markets),
        fetch_positions(session, markets),
    )
    return bars, positions
```

## 3.5 Logging with structlog

Always use `structlog`. Never `print()`. Never `import logging` in service code.

Canonical fields: `event`, `level`, `timestamp_utc`, `monotonic_ns`, `service_name`. Add `audit_event_uuid`, `signal_uuid`, `strategy_hash` when relevant.

```python
import structlog

log = structlog.get_logger()

# Configure at service startup (services/<service>/main.py):
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp_utc"),
        structlog.processors.add_log_level,
        structlog.processors.CallsiteParameterAdder(
            [structlog.processors.CallsiteParameter.FUNC_NAME]
        ),
        structlog.dev.ConsoleRenderer() if is_dev() else structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG if is_dev() else logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

# Usage:
async def write_order(order: OrderRequest) -> OrderResponse:
    bound = log.bind(
        service_name="execution",
        market=order.market,
        signal_uuid=str(order.signal_id),
    )
    bound.info("order_write_started")
    try:
        result = await _write_order_inner(order)
        bound.info("order_write_completed", order_id=str(result.order_id))
        return result
    except Exception:
        bound.exception("order_write_failed")
        raise
```

## 3.6 Error Envelope

All FastAPI errors return `{"error_code": str, "message": str, "details": dict | None}`.

```python
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

class ErrorEnvelope(BaseModel):
    error_code: str
    message: str
    details: dict | None = None

class AppError(Exception):
    def __init__(
        self,
        error_code: str,
        message: str,
        status_code: int = 400,
        details: dict | None = None,
    ) -> None:
        self.error_code = error_code
        self.message = message
        self.status_code = status_code
        self.details = details

app = FastAPI()

@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorEnvelope(
            error_code=exc.error_code,
            message=exc.message,
            details=exc.details,
        ).model_dump(),
    )

# Usage:
raise AppError(
    error_code="SIGNAL_NOT_FOUND",
    message="Signal does not exist or has expired.",
    status_code=404,
    details={"signal_id": str(signal_id)},
)
```

## 3.7 Datetime Discipline

```python
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
ET = ZoneInfo("America/New_York")

# ALWAYS store UTC:
now_utc: datetime = datetime.now(tz=UTC)

# ALWAYS render in ET at presentation layer (or use frontend formatET):
def format_et(dt: datetime) -> str:
    return dt.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S ET")

# NEVER:
# datetime.now()              — no timezone
# datetime.utcnow()           — deprecated; no timezone
# datetime.now(tz=ET)         — only for ET wall-clock anchored scheduled jobs
```

## 3.8 Decimal Precision

```python
from decimal import Decimal, ROUND_HALF_EVEN

# All money/price values:
price = Decimal("5234.50")
equity = Decimal("25000.00")

# Banker's rounding for lot sizing:
def banker_round(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_HALF_EVEN))

# API serialization: always string, never float:
class FillResponse(BaseModel):
    fill_price: str        # Decimal serialized as string
    commission: str
    realized_pnl_usd: str
```

## 3.9 UUIDv7 for All Writes

```python
from uuid import UUID
import uuid_utils as uuid7_lib   # pip install uuid-utils

def new_uuid() -> UUID:
    return uuid7_lib.uuid7()

# client_order_id (33 chars, deterministic):
def make_client_order_id(
    strategy_short: str,   # 8 chars
    paramset_short: str,   # 8 chars
    signal_short: str,     # 12 chars
    retry_n: int,          # 1-2 chars
) -> str:
    return f"{strategy_short[:8]}-{paramset_short[:8]}-{signal_short[:12]}-{retry_n}"
    # total: 8 + 1 + 8 + 1 + 12 + 1 + (1-2) = 32-33 chars; pad retry_n if needed
```

---

# 4. TypeScript Frontend Coding Standards

## 4.1 tsconfig.json (strict — do not relax)

```json
{
  "compilerOptions": {
    "strict": true,
    "noImplicitAny": true,
    "strictNullChecks": true,
    "strictFunctionTypes": true,
    "noUncheckedIndexedAccess": true,
    "exactOptionalPropertyTypes": true
  }
}
```

## 4.2 Naming Conventions

```typescript
camelCaseFunctionOrVariable      // functions, variables
PascalCaseComponent              // React components, types, interfaces, enums
kebab-case-file-name.tsx         // filenames (all files)
SCREAMING_SNAKE_CONSTANT         // module-level constants
```

## 4.3 React Patterns

- Server components by default. `"use client"` only for: event handlers, hooks, browser APIs, animated components.
- No class components.
- Components ≤ 300 lines. If larger, split into sub-components.
- One component per file.

```typescript
// server component (default) — no "use client"
import { formatET } from '@/lib/format';

type Props = { signalId: string; emittedAt: string };

export default function SignalRow({ signalId, emittedAt }: Props) {
  return (
    <tr>
      <td className="font-mono tabular-nums">{signalId.slice(0, 8)}</td>
      <td className="font-mono tabular-nums">{formatET(emittedAt, "HH:mm:ss 'ET'")}</td>
    </tr>
  );
}
```

## 4.4 Import Order

```typescript
// 1. External packages
import { useQuery } from '@tanstack/react-query';
import { fetchEventSource } from '@microsoft/fetch-event-source';

// 2. @/ aliased internal (absolute imports)
import { queryClient } from '@/lib/queryClient';
import { formatET } from '@/lib/format';
import { useSSEStore } from '@/lib/stores/sse-store';

// 3. Relative imports
import { SignalRow } from './signal-row';
```

## 4.5 Datetime — formatET Only

```typescript
// ONLY valid pattern for date display:
import { formatET, fmtETTimestamp } from '@/lib/format';

// Good:
<td>{formatET(signal.emitted_at, "yyyy-MM-dd HH:mm:ss 'ET'")}</td>
<td>{fmtETTimestamp(fill.executed_at)}</td>

// NEVER:
<td>{new Date(signal.emitted_at).toLocaleString()}</td>
<td>{new Intl.DateTimeFormat('en-US').format(new Date(ts))}</td>
```

## 4.6 Numbers — Format Helpers Only

```typescript
import { formatPnL, formatPrice, formatPct, formatNumber } from '@/lib/format';
import Decimal from 'decimal.js';

// All monetary display:
<span className="font-mono tabular-nums">{formatPnL(fill.realized_pnl_usd)}</span>
<span className="font-mono tabular-nums">{formatPrice(signal.decision_price, instrument.decimals_price)}</span>

// Arithmetic uses decimal.js:
const totalPnL = fills.reduce(
  (acc, f) => acc.plus(new Decimal(f.realized_pnl_usd)),
  new Decimal(0),
);

// Native Number only for chart library data (acceptable rounding):
const chartData = fills.map(f => ({ x: Date.parse(f.ts), y: Number(f.realized_pnl_usd) }));
```

---

# 5. Architecture Patterns

## 5.1 Audit-Log Writer

This is the most critical function in the entire codebase. Use `append_audit_event()` exclusively — never write to `audit_log` directly.

```python
# services/audit/writer.py
from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timezone
from typing import Any, Final
from uuid import UUID

import jcs  # pyjcs package — RFC 8785 JCS
import structlog
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from services.audit.event_types import AuditEventType
from services.audit.models import AuditLogRecord
from services.risk.state import halt_new  # import lazily in actual code to avoid circular

log = structlog.get_logger()

AUDIT_CHAIN_LOCK_ID: Final[int] = 0x6175646974636861  # "auditcha" as bigint
GENESIS_HASH: Final[bytes] = b"\x00" * 32
RETRY_DELAYS: Final[list[float]] = [0.01, 0.05, 0.25, 1.25, 6.0]


class AuditWriteFailure(Exception):
    pass


async def append_audit_event(
    session: AsyncSession,
    event_type: AuditEventType,
    payload: dict[str, Any],
    *,
    event_uuid: UUID | None = None,
    repaired_for_sequence_no: int | None = None,
    repaired_for_event_timestamp: datetime | None = None,
) -> AuditLogRecord:
    """Append-only audit log writer with hash-chain integrity.

    Concurrency: SERIALIZABLE isolation + advisory lock on AUDIT_CHAIN_LOCK_ID.
    Canonical serialization: JCS (RFC 8785) for hash determinism.
    Retry: 5× exponential backoff on serialization failure; HALT_NEW (incident_review) after 5.
    Idempotency: caller may pass event_uuid; duplicate produces UniqueViolation → return existing row.
    """
    from uuid_utils import uuid7

    if event_uuid is None:
        event_uuid = uuid7()

    canonical_payload: bytes = jcs.canonicalize(payload)
    source_clock_ts: datetime = datetime.now(tz=timezone.utc)
    ingest_clock_ts: datetime = datetime.now(tz=timezone.utc)
    monotonic_ns: int = time.monotonic_ns()

    for attempt in range(5):
        try:
            async with session.begin():
                # SERIALIZABLE required for prev_hash read + insert atomicity
                await session.execute(
                    text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                )
                # Advisory lock serializes concurrent writers at the app level
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_id)"),
                    {"lock_id": AUDIT_CHAIN_LOCK_ID},
                )
                row = (
                    await session.execute(
                        text(
                            "SELECT record_hash FROM audit_log "
                            "ORDER BY sequence_no DESC LIMIT 1"
                        )
                    )
                ).fetchone()
                prev_hash: bytes = row.record_hash if row else GENESIS_HASH
                record_hash: bytes = hashlib.sha256(
                    prev_hash + canonical_payload
                ).digest()

                result = await session.execute(
                    text(
                        "INSERT INTO audit_log "
                        "(event_uuid, event_type, source_clock_ts, ingest_clock_ts, "
                        " prev_hash, record_hash, payload_jcs, "
                        " repaired_for_sequence_no) "
                        "VALUES (:event_uuid, :event_type, :source_ts, :ingest_ts, "
                        " :prev_hash, :record_hash, :payload_jcs, "
                        " :repaired_for_sequence_no) "
                        "RETURNING sequence_no, event_uuid, ingest_clock_ts"
                    ),
                    {
                        "event_uuid": event_uuid,
                        "event_type": event_type.value,
                        "source_ts": source_clock_ts,
                        "ingest_ts": ingest_clock_ts,
                        "prev_hash": prev_hash,
                        "record_hash": record_hash,
                        "payload_jcs": canonical_payload,
                        "repaired_for_sequence_no": repaired_for_sequence_no,
                    },
                )
                inserted = result.fetchone()

            log.info(
                "audit_event_appended",
                event_type=event_type.value,
                sequence_no=inserted.sequence_no,
                event_uuid=str(event_uuid),
                monotonic_ns=monotonic_ns,
            )
            return AuditLogRecord(
                sequence_no=inserted.sequence_no,
                event_uuid=event_uuid,
                event_type=event_type,
                ingest_clock_ts=inserted.ingest_clock_ts,
            )

        except IntegrityError as e:
            # Idempotency: duplicate event_uuid means a prior attempt succeeded
            if "audit_log_event_uuid" in str(e.orig):
                existing = (
                    await session.execute(
                        text("SELECT * FROM audit_log WHERE event_uuid = :uuid"),
                        {"uuid": event_uuid},
                    )
                ).fetchone()
                if existing:
                    log.info(
                        "audit_event_already_exists",
                        event_uuid=str(event_uuid),
                    )
                    return AuditLogRecord.from_row(existing)
            raise

        except OperationalError:
            # Serialization failure — retry with backoff
            if attempt < 4:
                await asyncio.sleep(RETRY_DELAYS[attempt])
                continue
            log.error(
                "audit_write_serialization_failed_5x",
                event_uuid=str(event_uuid),
                event_type=event_type.value,
            )
            await halt_new(severity="incident_review", reason="audit_log_write_failure")
            raise AuditWriteFailure("5 serialization retries exhausted; HALT_NEW invoked")

    raise AuditWriteFailure("unreachable")
```

## 5.2 SSE Event Emitter

```python
# services/api/sse.py
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import jcs
import structlog
from fastapi import Request
from fastapi.responses import StreamingResponse

log = structlog.get_logger()

_sequence_counter: int = 0
_subscribers: list[asyncio.Queue[bytes]] = []
_REPLAY_BUFFER_MAX_SECONDS: int = 86_400   # 24h
_replay_buffer: list[tuple[int, bytes]] = []  # (sequence_no, raw_bytes)


def _next_seq() -> int:
    global _sequence_counter
    _sequence_counter += 1
    return _sequence_counter


async def emit_sse(event_type: str, data: dict[str, Any]) -> int:
    """Emit a JCS-canonical SSE event to all connected subscribers."""
    seq = _next_seq()
    envelope = {
        "type": event_type,
        "sequence_no": seq,
        "server_now": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "data": data,
    }
    # JCS canonicalization for deterministic wire format
    raw: bytes = jcs.canonicalize(envelope)
    line = b"id: " + str(seq).encode() + b"\ndata: " + raw + b"\n\n"

    # Append to replay buffer; prune old entries
    _replay_buffer.append((seq, line))
    now_ts = time.time()
    cutoff_seq = seq  # placeholder; real impl tracks timestamps alongside
    _replay_buffer[:] = [
        (s, b) for s, b in _replay_buffer
        if s > seq - 100_000  # keep last 100k events as heuristic; real impl uses timestamps
    ]

    dead: list[asyncio.Queue[bytes]] = []
    for q in list(_subscribers):
        try:
            q.put_nowait(line)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _subscribers.remove(q)

    log.debug("sse_event_emitted", event_type=event_type, sequence_no=seq)
    return seq


async def sse_endpoint(request: Request) -> StreamingResponse:
    """FastAPI endpoint handler: GET /api/sse/events"""
    last_event_id_header = request.headers.get("Last-Event-ID")
    resume_from: int = int(last_event_id_header) if last_event_id_header else 0

    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)
    _subscribers.append(queue)

    async def generator():
        try:
            # Replay buffered events since last_event_id
            for seq, line in _replay_buffer:
                if seq > resume_from:
                    yield line
            # Stream new events
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield data
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
        finally:
            if queue in _subscribers:
                _subscribers.remove(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
```

## 5.3 Kill-Switch State Machine

```python
# services/risk/kill_switch.py
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from services.audit.event_types import AuditEventType
from services.audit.writer import append_audit_event
from services.api.sse import emit_sse

log = structlog.get_logger()


class KillSwitchSeverity(str, Enum):
    ROUTINE = "routine"
    DEFENSIVE_ENVELOPE = "defensive_envelope"
    INCIDENT_REVIEW = "incident_review"


class RiskState(str, Enum):
    NORMAL = "NORMAL"
    HALT_NEW = "HALT_NEW"
    CONVALESCENT = "CONVALESCENT"


# Valid transitions: (from_state, to_state) → allowed
_VALID_TRANSITIONS: frozenset[tuple[RiskState, RiskState]] = frozenset({
    (RiskState.NORMAL, RiskState.HALT_NEW),
    (RiskState.HALT_NEW, RiskState.CONVALESCENT),
    (RiskState.CONVALESCENT, RiskState.NORMAL),
    (RiskState.CONVALESCENT, RiskState.HALT_NEW),  # trigger resets counter
})
# NORMAL → CONVALESCENT is NOT in this set — it is an invalid transition.


async def invoke_kill_switch(
    session: AsyncSession,
    reason: str,
    severity: KillSwitchSeverity,
    triggered_by: Literal["risk_engine", "agent", "operator", "watchdog"],
) -> None:
    """Transition from NORMAL or CONVALESCENT → HALT_NEW."""
    current_state = await _load_current_state(session)

    if current_state == RiskState.HALT_NEW:
        log.info("kill_switch_already_halted", reason=reason)
        return  # idempotent; already halted

    new_state = RiskState.HALT_NEW
    _assert_valid_transition(current_state, new_state)

    await _atomic_state_update(session, new_state, severity=severity.value)

    audit_record = await append_audit_event(
        session,
        AuditEventType.KILL_SWITCH_TRIGGERED,
        {
            "prior_state": current_state.value,
            "new_state": new_state.value,
            "severity": severity.value,
            "reason": reason,
            "triggered_by": triggered_by,
            "ts": datetime.now(tz=timezone.utc).isoformat(),
        },
    )

    await emit_sse(
        "risk_state",
        {
            "state": new_state.value,
            "severity": severity.value,
            "reason": reason,
            "audit_event_uuid": str(audit_record.event_uuid),
        },
    )

    await _route_alert(severity, reason)
    log.critical(
        "kill_switch_invoked",
        new_state=new_state.value,
        severity=severity.value,
        reason=reason,
        triggered_by=triggered_by,
        audit_event_uuid=str(audit_record.event_uuid),
    )


async def resume_from_halt(
    session: AsyncSession,
    operator_session_id: str,
    incident_review_id: str | None = None,
) -> None:
    """Transition HALT_NEW → CONVALESCENT (human-only; re-auth required at API layer)."""
    current_state = await _load_current_state(session)
    if current_state != RiskState.HALT_NEW:
        raise ValueError(f"Cannot resume from state={current_state.value}")

    severity = await _load_current_severity(session)
    if severity == KillSwitchSeverity.INCIDENT_REVIEW and not incident_review_id:
        raise ValueError("incident_review_id required for severity=incident_review")

    _assert_valid_transition(current_state, RiskState.CONVALESCENT)
    await _atomic_state_update(session, RiskState.CONVALESCENT, severity=None, reset_counter=True)

    audit_record = await append_audit_event(
        session,
        AuditEventType.STATE_TRANSITION_HALT_TO_CONVALESCENT,
        {
            "operator_session_id": operator_session_id,
            "incident_review_id": incident_review_id,
            "ts": datetime.now(tz=timezone.utc).isoformat(),
        },
    )

    await emit_sse(
        "risk_state",
        {"state": RiskState.CONVALESCENT.value, "severity": None,
         "audit_event_uuid": str(audit_record.event_uuid)},
    )


def _assert_valid_transition(from_state: RiskState, to_state: RiskState) -> None:
    if (from_state, to_state) not in _VALID_TRANSITIONS:
        raise ValueError(
            f"Invalid kill-switch transition: {from_state.value} → {to_state.value}"
        )


async def _load_current_state(session: AsyncSession) -> RiskState:
    from sqlalchemy import text
    row = (await session.execute(
        text("SELECT state FROM risk_state ORDER BY id DESC LIMIT 1")
    )).fetchone()
    return RiskState(row.state) if row else RiskState.NORMAL


async def _load_current_severity(session: AsyncSession) -> KillSwitchSeverity | None:
    from sqlalchemy import text
    row = (await session.execute(
        text("SELECT severity FROM risk_state ORDER BY id DESC LIMIT 1")
    )).fetchone()
    return KillSwitchSeverity(row.severity) if row and row.severity else None


async def _atomic_state_update(
    session: AsyncSession,
    new_state: RiskState,
    severity: str | None,
    reset_counter: bool = False,
) -> None:
    from sqlalchemy import text
    await session.execute(
        text(
            "UPDATE risk_state SET state = :state, severity = :severity, "
            "convalescent_session_count = CASE WHEN :reset THEN 0 ELSE convalescent_session_count END, "
            "updated_at = now() WHERE id = (SELECT id FROM risk_state ORDER BY id DESC LIMIT 1)"
        ),
        {"state": new_state.value, "severity": severity, "reset": reset_counter},
    )


async def _route_alert(severity: KillSwitchSeverity, reason: str) -> None:
    from services.monitoring.alerts import fire_alert
    p_severity = "P0" if severity == KillSwitchSeverity.INCIDENT_REVIEW else "P1"
    await fire_alert(severity=p_severity, category="kill_switch", message=reason)
```

## 5.4 QC Instruction Processor

```python
# services/qc_adapter/instruction_processor.py
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID

import structlog
from pydantic import BaseModel, ConfigDict

from services.audit.event_types import AuditEventType
from services.audit.writer import append_audit_event

log = structlog.get_logger()


class InstructionType(str, Enum):
    DEFENSIVE_TRIM = "defensive_trim"
    FORCE_CLOSE = "force_close"
    CANCEL_PENDING = "cancel_pending"


class Instruction(BaseModel):
    model_config = ConfigDict(frozen=True)

    instruction_id: UUID
    instruction_type: InstructionType
    issued_at_utc: datetime
    expires_at_utc: datetime
    payload: dict[str, Any]
    audit_log_sequence_no: int


class AcknowledgmentStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


class Acknowledgment(BaseModel):
    instruction_id: UUID
    status: AcknowledgmentStatus
    executed_at_utc: datetime | None
    result: dict[str, Any] | None
    error_message: str | None


INSTRUCTION_PATH_PREFIX = "/instructions"
ACK_PATH_PREFIX = "/instruction_acks"
POLL_INTERVAL_S: float = 5.0


async def write_instruction(
    qc_client: "QCObjectStoreClient",
    session: "AsyncSession",
    instruction: Instruction,
) -> None:
    """Write instruction to QC ObjectStore and emit audit event."""
    path = f"{INSTRUCTION_PATH_PREFIX}/{instruction.audit_log_sequence_no}.json"
    await qc_client.write_json(path, instruction.model_dump(mode="json"))

    await append_audit_event(
        session,
        AuditEventType.INSTRUCTION_ISSUED,
        {
            "instruction_id": str(instruction.instruction_id),
            "instruction_type": instruction.instruction_type.value,
            "path": path,
            "expires_at_utc": instruction.expires_at_utc.isoformat(),
        },
    )
    log.info(
        "instruction_written",
        instruction_id=str(instruction.instruction_id),
        instruction_type=instruction.instruction_type.value,
    )


async def poll_for_ack(
    qc_client: "QCObjectStoreClient",
    instruction: Instruction,
    timeout_s: float = 300.0,
) -> Acknowledgment:
    """Poll /instruction_acks/<seq>.json until ack or timeout. Idempotent on re-poll."""
    path = f"{ACK_PATH_PREFIX}/{instruction.audit_log_sequence_no}.json"
    elapsed = 0.0
    while elapsed < timeout_s:
        now = datetime.now(tz=timezone.utc)
        if now > instruction.expires_at_utc:
            return Acknowledgment(
                instruction_id=instruction.instruction_id,
                status=AcknowledgmentStatus.EXPIRED,
                executed_at_utc=None,
                result=None,
                error_message="Instruction expired before ack received",
            )
        try:
            raw = await qc_client.read_json(path)
            if raw:
                return Acknowledgment.model_validate(raw)
        except FileNotFoundError:
            pass
        await asyncio.sleep(POLL_INTERVAL_S)
        elapsed += POLL_INTERVAL_S

    return Acknowledgment(
        instruction_id=instruction.instruction_id,
        status=AcknowledgmentStatus.EXPIRED,
        executed_at_utc=None,
        result=None,
        error_message=f"Ack not received within {timeout_s}s",
    )
```

## 5.5 Reconciliation Diff

```python
# services/reconciliation/reconciler.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from services.audit.event_types import AuditEventType
from services.audit.writer import append_audit_event
from services.risk.kill_switch import KillSwitchSeverity, invoke_kill_switch

log = structlog.get_logger()

# Reconciliation Tolerances (locked — per backend-spec §2.6 + §4 test table)
_POSITION_QTY_TOLERANCE: Decimal = Decimal("0")         # exact match required
_CASH_ABS_TOLERANCE: Decimal = Decimal("5.00")          # $5 absolute
_CASH_PCT_TOLERANCE: Decimal = Decimal("0.0001")        # 1bps
_DIVIDEND_WIDENING_FACTOR: Decimal = Decimal("2")       # 2× for +24h after ex-date
_T1_GRACE_HOURS: int = 24                               # T+1 for fees/dividends


@dataclass
class ReconciliationBreak:
    metric: str
    market: str | None
    expected: Decimal
    actual: Decimal
    delta: Decimal
    tolerance: Decimal
    within_grace_period: bool


async def run_reconciliation(
    session: AsyncSession,
    portfolio_snapshot: dict[str, Any],     # from QC ObjectStore /state/portfolio.json
    flexquery_eod: dict[str, Any] | None,   # from /state/flexquery/<date>.xml (parsed)
    is_dividend_ex_date: bool = False,
) -> list[ReconciliationBreak]:
    """Diff backend state vs broker state; trigger kill-switch on any breach."""
    breaks: list[ReconciliationBreak] = []
    position_tol = _POSITION_QTY_TOLERANCE
    cash_tol = max(_CASH_ABS_TOLERANCE, _effective_cash_tol(portfolio_snapshot))
    if is_dividend_ex_date:
        position_tol *= _DIVIDEND_WIDENING_FACTOR
        cash_tol *= _DIVIDEND_WIDENING_FACTOR

    # Check positions
    backend_positions = await _load_backend_positions(session)
    broker_positions = portfolio_snapshot.get("positions", {})
    for market, backend_qty in backend_positions.items():
        broker_qty = Decimal(str(broker_positions.get(market, {}).get("quantity", 0)))
        delta = abs(backend_qty - broker_qty)
        if delta > position_tol:
            breaks.append(ReconciliationBreak(
                metric="position_qty",
                market=market,
                expected=backend_qty,
                actual=broker_qty,
                delta=delta,
                tolerance=position_tol,
                within_grace_period=False,
            ))

    # Check cash (EOD only)
    if flexquery_eod:
        backend_cash = await _load_backend_cash(session)
        broker_cash = Decimal(str(flexquery_eod.get("net_liquidation_value", 0)))
        cash_delta = abs(backend_cash - broker_cash)
        if cash_delta > cash_tol:
            breaks.append(ReconciliationBreak(
                metric="cash_usd",
                market=None,
                expected=backend_cash,
                actual=broker_cash,
                delta=cash_delta,
                tolerance=cash_tol,
                within_grace_period=_is_within_t1_grace(flexquery_eod),
            ))

    actionable_breaks = [b for b in breaks if not b.within_grace_period]

    if actionable_breaks:
        for brk in actionable_breaks:
            await append_audit_event(
                session,
                AuditEventType.RECONCILIATION_BREAK_DETECTED,
                {
                    "metric": brk.metric,
                    "market": brk.market,
                    "expected": str(brk.expected),
                    "actual": str(brk.actual),
                    "delta": str(brk.delta),
                    "tolerance": str(brk.tolerance),
                    "ts": datetime.now(tz=timezone.utc).isoformat(),
                },
            )
        await invoke_kill_switch(
            session,
            reason=f"reconciliation_break: {actionable_breaks[0].metric}",
            severity=KillSwitchSeverity.ROUTINE,
            triggered_by="risk_engine",
        )
    else:
        await append_audit_event(
            session,
            AuditEventType.RECONCILIATION_CHECK_PASSED,
            {"ts": datetime.now(tz=timezone.utc).isoformat()},
        )

    return breaks


def _effective_cash_tol(snapshot: dict[str, Any]) -> Decimal:
    nlv = Decimal(str(snapshot.get("net_liquidation_value", 1)))
    return nlv * _CASH_PCT_TOLERANCE


def _is_within_t1_grace(flexquery: dict[str, Any]) -> bool:
    eod_ts_str = flexquery.get("report_date")
    if not eod_ts_str:
        return False
    eod_ts = datetime.fromisoformat(eod_ts_str).replace(tzinfo=timezone.utc)
    return datetime.now(tz=timezone.utc) < eod_ts + timedelta(hours=_T1_GRACE_HOURS)


async def _load_backend_positions(session: AsyncSession) -> dict[str, Decimal]:
    from sqlalchemy import text
    rows = (await session.execute(
        text("SELECT market, quantity FROM positions WHERE account_id = current_account_id()")
    )).fetchall()
    return {r.market: Decimal(str(r.quantity)) for r in rows}


async def _load_backend_cash(session: AsyncSession) -> Decimal:
    from sqlalchemy import text
    row = (await session.execute(
        text("SELECT net_liquidation FROM balances ORDER BY recorded_at DESC LIMIT 1")
    )).fetchone()
    return Decimal(str(row.net_liquidation)) if row else Decimal("0")
```

## 5.6 Position-Sizing Algorithm (Stages 0–5)

```python
# services/risk/sizing.py
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any

import numpy as np
import structlog

log = structlog.get_logger()

_UNIVERSE_NOTIONAL_CAP_PCT: Decimal = Decimal("0.50")
_PER_POSITION_TARGET_PCT: Decimal = Decimal("0.25")
_PER_POSITION_HARD_FLOOR_PCT: Decimal = Decimal("0.50")
_GROSS_EXPOSURE_CAP: Decimal = Decimal("3.0")
_NET_EXPOSURE_CAP: Decimal = Decimal("1.5")
_SUB_MINIMUM_THRESHOLD: Decimal = Decimal("0.5")
_CLUSTER_MAX_ITER: int = 10
_CLUSTER_TOLERANCE: float = 0.001   # 0.1%


@dataclass
class SizingTrace:
    stage_0_universe: dict[str, Any] = field(default_factory=dict)
    stage_1_inverse_vol: dict[str, Any] = field(default_factory=dict)
    stage_2_per_position_cap: dict[str, Any] = field(default_factory=dict)
    stage_3_cluster: dict[str, Any] = field(default_factory=dict)
    stage_4_gross_net: dict[str, Any] = field(default_factory=dict)
    stage_5_lot_rounding: dict[str, Any] = field(default_factory=dict)


def stage_0_universe_filter(
    candidates: list[str],
    contract_notionals: dict[str, Decimal],
    equity: Decimal,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Filter markets where 1-contract notional > 50% equity.

    Pre: equity > 0
    Post: every market in active has contract_notionals[m] <= 0.5 * equity
    """
    active, excluded = [], []
    threshold = equity * _UNIVERSE_NOTIONAL_CAP_PCT
    for market in candidates:
        notional = contract_notionals[market]
        if notional <= threshold:
            active.append(market)
        else:
            excluded.append({
                "market": market,
                "reason": "single_contract_notional_exceeds_50pct_equity",
                "single_contract_notional": str(notional),
                "current_equity": str(equity),
            })
    return active, excluded


def stage_1_inverse_vol(
    active_markets: list[str],
    sigma_per_market: dict[str, float],
    equity: Decimal,
    vol_target_annual: Decimal,
    m_combined: Decimal,
    sigma_matrix: np.ndarray,
) -> dict[str, Decimal]:
    """Inverse-vol weighting scaled to effective vol target.

    Pre: all sigma > 0; sigma_matrix is PSD (caller ensures via nearest_psd)
    Post: sum of unconstrained_notional ~ equity * vol_target_daily * m_combined / portfolio_realized_vol
    """
    vol_target_daily = float(vol_target_annual) / np.sqrt(252) * float(m_combined)
    raw_weights = {m: 1.0 / sigma_per_market[m] for m in active_markets}
    total_raw = sum(raw_weights.values())
    normalized = {m: w / total_raw for m, w in raw_weights.items()}

    w_vec = np.array([normalized[m] for m in active_markets])
    portfolio_vol = float(np.sqrt(w_vec @ sigma_matrix @ w_vec))
    scale = vol_target_daily / portfolio_vol if portfolio_vol > 0 else 1.0

    return {
        m: Decimal(str(normalized[m] * scale * float(equity)))
        for m in active_markets
    }


def stage_2_per_position_cap(
    unconstrained_notional: dict[str, Decimal],
    equity: Decimal,
) -> dict[str, Decimal]:
    """Cap each position at 25% of equity; hard floor 50% for single-contract markets.

    Pre: unconstrained_notional values >= 0
    Post: each value <= equity * 0.50
    """
    capped = {}
    target_cap = equity * _PER_POSITION_TARGET_PCT
    hard_floor_cap = equity * _PER_POSITION_HARD_FLOOR_PCT
    for market, notional in unconstrained_notional.items():
        capped[market] = min(notional, hard_floor_cap if notional > target_cap else target_cap)
    return capped


def stage_3_cluster_shrink(
    capped_notional: dict[str, Decimal],
    equity: Decimal,
    cluster_map: dict[str, str],
    cluster_cap_pct: dict[str, Decimal],
    sigma_per_market: dict[str, float],
) -> tuple[dict[str, Decimal], dict[str, Any]]:
    """Iterative cluster shrink-to-fit. ≤10 iterations; 0.1% tolerance.

    Non-convergence: drop lowest-momentum signal in binding cluster; restart.
    Pre: capped_notional >= 0; cluster_cap_pct <= 1.0
    Post: sum of notionals within any cluster <= equity * cluster_cap_pct[cluster]
    """
    notionals = dict(capped_notional)
    trace: dict[str, Any] = {"iterations": 0, "dropped_due_to_non_convergence": []}

    for iteration in range(_CLUSTER_MAX_ITER):
        trace["iterations"] = iteration + 1
        converged = True
        for cluster, cap_pct in cluster_cap_pct.items():
            cluster_markets = [m for m, c in cluster_map.items() if c == cluster and m in notionals]
            cluster_total = sum(notionals[m] for m in cluster_markets)
            cluster_cap = equity * cap_pct
            if cluster_total > cluster_cap * Decimal(str(1 + _CLUSTER_TOLERANCE)):
                scale = cluster_cap / cluster_total
                for m in cluster_markets:
                    notionals[m] *= scale
                converged = False

        if converged:
            trace["convergence_tolerance_met"] = True
            return notionals, trace

    # Non-convergence: drop lowest-momentum (by rolling 60d return z-score ascending)
    binding_cluster = max(
        cluster_cap_pct.keys(),
        key=lambda c: sum(notionals[m] for m in notionals if cluster_map.get(m) == c),
    )
    cluster_markets = [m for m, c in cluster_map.items() if c == binding_cluster and m in notionals]
    lowest_momentum = min(cluster_markets, key=lambda m: sigma_per_market.get(m, 0))
    del notionals[lowest_momentum]
    trace["dropped_due_to_non_convergence"].append(lowest_momentum)
    log.warning("cluster_shrink_non_convergence_drop", market=lowest_momentum, cluster=binding_cluster)
    # Recursive restart with dropped market removed
    return stage_3_cluster_shrink(
        notionals, equity, cluster_map, cluster_cap_pct, sigma_per_market
    )


def stage_4_gross_net_cap(
    notionals: dict[str, Decimal],
    equity: Decimal,
    directions: dict[str, int],  # +1 long, -1 short
) -> dict[str, Decimal]:
    """Apply gross (3.0×) and net (1.5×) exposure caps.

    Post: gross_exposure <= 3.0 * equity; |net_exposure| <= 1.5 * equity
    """
    gross = sum(notionals.values())
    net = sum(notionals[m] * Decimal(directions.get(m, 1)) for m in notionals)
    result = dict(notionals)

    if gross > equity * _GROSS_EXPOSURE_CAP:
        scale = (equity * _GROSS_EXPOSURE_CAP) / gross
        result = {m: v * scale for m, v in result.items()}

    net_recomputed = sum(result[m] * Decimal(directions.get(m, 1)) for m in result)
    if abs(net_recomputed) > equity * _NET_EXPOSURE_CAP:
        scale = (equity * _NET_EXPOSURE_CAP) / abs(net_recomputed)
        result = {m: v * scale for m, v in result.items()}

    return result


def stage_5_lot_rounding(
    notionals: dict[str, Decimal],
    contract_notionals: dict[str, Decimal],
) -> tuple[dict[str, int], list[str]]:
    """Banker's rounding to integer contracts; drop sub-minimum (< 0.5 contract).

    Pre: contract_notionals[m] > 0 for all m in notionals
    Post: returned contract counts are non-negative integers; sub_minimum_drops excluded
    """
    rounded: dict[str, int] = {}
    sub_minimum_drops: list[str] = []

    for market, notional in notionals.items():
        contracts = notional / contract_notionals[market]
        if contracts < _SUB_MINIMUM_THRESHOLD:
            sub_minimum_drops.append(market)
            continue
        rounded[market] = int(contracts.to_integral_value(rounding=ROUND_HALF_EVEN))

    return rounded, sub_minimum_drops
```

## 5.7 Vol-Target Multiplier Composition

```python
# services/risk/multipliers.py
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import structlog

log = structlog.get_logger()

_M_CAPITAL_EVENT: Decimal = Decimal("0.5")
_M_CONVALESCENT: Decimal = Decimal("0.5")
_M_MONTHLY_DD: Decimal = Decimal("0.5")
_CAPITAL_EVENT_SESSIONS: int = 5
_MONTHLY_DD_THRESHOLD: Decimal = Decimal("-0.10")  # -10% in calendar month


def m_combined(
    capital_event_session_count: int,
    is_convalescent: bool,
    monthly_dd_pct: Decimal,
) -> Decimal:
    """MIN-of-multipliers. NOT compounded. Implicit floor of 1.0 when no multiplier active.

    Unit test cases:
    - capital_event(sessions=3) + convalescent → min(0.5, 0.5) = 0.5  NOT 0.25
    - capital_event(sessions=6) + convalescent → min(1.0, 0.5) = 0.5
    - no active events → min() → 1.0 (floor)
    - monthly_dd=-12% alone → min(0.5) = 0.5
    """
    multipliers: list[Decimal] = []

    if 1 <= capital_event_session_count <= _CAPITAL_EVENT_SESSIONS:
        multipliers.append(_M_CAPITAL_EVENT)
        log.debug("m_capital_event_active", sessions=capital_event_session_count)

    if is_convalescent:
        multipliers.append(_M_CONVALESCENT)
        log.debug("m_convalescent_active")

    if monthly_dd_pct < _MONTHLY_DD_THRESHOLD:
        multipliers.append(_M_MONTHLY_DD)
        log.debug("m_monthly_dd_active", monthly_dd_pct=str(monthly_dd_pct))

    result = min(multipliers) if multipliers else Decimal("1.0")
    log.debug("m_combined_computed", m_combined=str(result), active_multipliers=len(multipliers))
    return result
```

## 5.8 PR Review Surface Artifact Generator

```python
# services/agent/reporting/pr_artifacts.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from anthropic import AsyncAnthropic

log = structlog.get_logger()

PLAIN_ENGLISH_SYSTEM_PROMPT = """You are generating a plain-English PR summary for a solo trading system operator.
The operator reviews code by checking that conventions are followed, not by reading diffs.
Your summary will appear in the in-app PR review surface.

Rules:
- Maximum 200 words
- Structure: (1) What changed — one sentence. (2) Why — one to two sentences. (3) Behavior change — what is different at runtime.
- Use plain English. No jargon. No code. No file paths.
- If this touches risk limits or position sizing, say so explicitly and state the new values.
- If this is infrastructure-only (logging, retry, metrics), say "No behavior change to trading logic."
- Do not summarize what tests do. Do not list files changed."""


@dataclass
class PRReviewArtifacts:
    plain_english_summary: str
    risk_impact_summary: str
    backtest_delta: dict[str, Any]   # equity curve, trade count, max DD, Sharpe
    test_results: dict[str, Any]     # pass/fail counts, coverage pcts
    files_affected: list[str]
    diff_url: str
    slippage_calibration_version_id: str


async def generate_pr_artifacts(
    pr_number: int,
    diff_text: str,
    backtest_results: dict[str, Any],
    test_results: dict[str, Any],
    files_affected: list[str],
    slippage_calibration_version_id: str,
    client: AsyncAnthropic,
) -> PRReviewArtifacts:
    plain_english = await _generate_plain_english(diff_text, client)
    risk_impact = await _generate_risk_impact(diff_text, backtest_results, client)

    return PRReviewArtifacts(
        plain_english_summary=plain_english,
        risk_impact_summary=risk_impact,
        backtest_delta=_extract_backtest_delta(backtest_results),
        test_results=test_results,
        files_affected=files_affected,
        diff_url=f"https://github.com/operator/trading/pull/{pr_number}/files",
        slippage_calibration_version_id=slippage_calibration_version_id,
    )


async def _generate_plain_english(diff_text: str, client: AsyncAnthropic) -> str:
    response = await client.messages.create(
        model="claude-opus-4-7",
        max_tokens=512,
        system=[
            {"type": "text", "text": PLAIN_ENGLISH_SYSTEM_PROMPT,
             "cache_control": {"type": "ephemeral"}},
        ],
        messages=[{"role": "user", "content": f"Generate the PR summary for this diff:\n\n{diff_text[:8000]}"}],
    )
    return response.content[0].text.strip()


async def _generate_risk_impact(
    diff_text: str, backtest_results: dict[str, Any], client: AsyncAnthropic
) -> str:
    prompt = (
        f"Summarize risk impact of this change. Include specific numbers.\n"
        f"Diff (first 4k chars):\n{diff_text[:4000]}\n\n"
        f"Backtest delta: {backtest_results}"
    )
    response = await client.messages.create(
        model="claude-opus-4-7",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def _extract_backtest_delta(results: dict[str, Any]) -> dict[str, Any]:
    return {
        "sharpe_delta": results.get("sharpe_after", 0) - results.get("sharpe_before", 0),
        "max_dd_delta": results.get("max_dd_after", 0) - results.get("max_dd_before", 0),
        "trade_count_delta": results.get("trade_count_after", 0) - results.get("trade_count_before", 0),
        "sharpe_after": results.get("sharpe_after"),
        "slippage_calibration_version_id": results.get("slippage_calibration_version_id"),
    }
```

---

# 6. Testing Patterns

## 6.1 Test Naming Convention

```
test_<function_name>_<condition>_<expected_outcome>

Examples:
test_append_audit_event_concurrent_writers_serialize_correctly
test_m_combined_capital_event_and_convalescent_returns_min_not_product
test_stage_0_universe_filter_notional_exceeds_50pct_excludes_market
test_reconciliation_cash_within_tolerance_passes
test_kill_switch_normal_to_convalescent_direct_raises_error
```

## 6.2 Unit Test Structure

```python
# tests/unit/test_multipliers.py
import pytest
from decimal import Decimal
from services.risk.multipliers import m_combined


class TestMCombined:
    def test_capital_event_and_convalescent_returns_min_not_product(self):
        result = m_combined(
            capital_event_session_count=3,
            is_convalescent=True,
            monthly_dd_pct=Decimal("0"),
        )
        assert result == Decimal("0.5"), "MIN(0.5, 0.5) = 0.5, NOT 0.5 * 0.5 = 0.25"

    def test_no_active_events_returns_one(self):
        result = m_combined(
            capital_event_session_count=6,  # session 6 means capital_event expired
            is_convalescent=False,
            monthly_dd_pct=Decimal("-0.05"),
        )
        assert result == Decimal("1.0")

    def test_monthly_dd_breach_alone(self):
        result = m_combined(
            capital_event_session_count=0,
            is_convalescent=False,
            monthly_dd_pct=Decimal("-0.12"),
        )
        assert result == Decimal("0.5")

    def test_all_three_active_returns_min(self):
        result = m_combined(
            capital_event_session_count=2,
            is_convalescent=True,
            monthly_dd_pct=Decimal("-0.15"),
        )
        assert result == Decimal("0.5")  # all are 0.5, MIN is 0.5
```

## 6.3 Integration Test with Testcontainers

```python
# tests/integration/test_audit_writer.py
import asyncio
import pytest
from testcontainers.postgres import PostgresContainer
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from services.audit.writer import append_audit_event
from services.audit.event_types import AuditEventType


@pytest.fixture(scope="module")
def postgres():
    with PostgresContainer("postgres:16") as pg:
        yield pg


@pytest.fixture(scope="module")
async def engine(postgres):
    url = postgres.get_connection_url().replace("postgresql+psycopg2", "postgresql+asyncpg")
    eng = create_async_engine(url)
    async with eng.begin() as conn:
        await conn.run_sync(_apply_migrations)
    yield eng
    await eng.dispose()


@pytest.mark.asyncio
async def test_append_audit_event_concurrent_writers_serialize_correctly(engine):
    """Two concurrent writers must serialize; no hash chain gaps."""
    session_factory = async_sessionmaker(engine, class_=AsyncSession)

    async def write_one(i: int):
        async with session_factory() as s:
            return await append_audit_event(
                s,
                AuditEventType.KILL_SWITCH_TRIGGERED,
                {"index": i, "test": True},
            )

    results = await asyncio.gather(*[write_one(i) for i in range(5)])
    sequence_nos = sorted(r.sequence_no for r in results)

    # All must be unique, no gaps
    assert len(set(sequence_nos)) == 5
    assert sequence_nos == list(range(sequence_nos[0], sequence_nos[0] + 5))
```

## 6.4 E2E Test (Playwright + WebAuthn)

```typescript
// tests/e2e/auth.spec.ts
import { test, expect } from '@playwright/test';

test('webauthn registration and login', async ({ page, context }) => {
  // Enable virtual authenticator via Chrome DevTools Protocol
  const cdpSession = await context.newCDPSession(page);
  await cdpSession.send('WebAuthn.enable');
  await cdpSession.send('WebAuthn.addVirtualAuthenticator', {
    options: {
      protocol: 'ctap2',
      transport: 'internal',
      hasResidentKey: true,
      hasUserVerification: true,
      isUserVerified: true,
    },
  });

  // Registration flow
  await page.goto('/setup');
  await page.fill('[name="setup_token"]', process.env.SETUP_TOKEN!);
  await page.click('[data-testid="register-passkey"]');
  await expect(page.locator('[data-testid="totp-step"]')).toBeVisible();

  // Login with passkey
  await page.goto('/login');
  await page.click('[data-testid="sign-in-passkey"]');
  await expect(page).toHaveURL('/');
});
```

## 6.5 Golden Test for QC Adapter Parity

```python
# tests/unit/test_qc_parity.py
"""
Golden test: byte-for-byte JCS payload parity between QC algorithm push
and backend ingestion. Metadata fields (ingest_clock_ts, ingest_uuid, sequence_no)
validated for shape only.
"""
import json
import jcs
import pytest
from pathlib import Path


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "qc_events"
EXCLUDED_METADATA_FIELDS = {"ingest_clock_ts", "ingest_uuid", "sequence_no"}


@pytest.mark.parametrize("fixture_file", list(FIXTURE_DIR.glob("*.json")))
def test_qc_event_jcs_parity(fixture_file: Path):
    data = json.loads(fixture_file.read_text())
    raw_payload = data["raw_qc_payload"]
    expected_jcs_b64 = data["expected_payload_jcs_b64"]

    # Strip metadata fields before canonical comparison
    payload_for_hash = {
        k: v for k, v in raw_payload.items()
        if k not in EXCLUDED_METADATA_FIELDS
    }
    computed_jcs = jcs.canonicalize(payload_for_hash)

    import base64
    expected_bytes = base64.b64decode(expected_jcs_b64)
    assert computed_jcs == expected_bytes, (
        f"JCS mismatch for {fixture_file.name}:\n"
        f"  computed: {computed_jcs!r}\n"
        f"  expected: {expected_bytes!r}"
    )

    # Shape-only checks for metadata
    assert isinstance(raw_payload.get("ingest_clock_ts"), str)
    assert isinstance(raw_payload.get("sequence_no"), int)
```

## 6.6 vectorbt-vs-LEAN Parity Test

```python
# tests/integration/test_vbt_lean_parity.py
"""
Compare vectorbt backtest vs LEAN backtest for identical strategy + data.
Pass criteria (locked):
- Per-trade slippage diff: <= 5bps
- Aggregate P&L divergence: <= 0.5% of starting equity
- Trade count divergence: <= 5%
"""
import pytest
from decimal import Decimal


@pytest.mark.slow
def test_vectorbt_lean_parity(vbt_results, lean_results, starting_equity):
    vbt_trades = vbt_results["trades"]
    lean_trades = lean_results["trades"]

    # Trade count
    count_pct_diff = abs(len(vbt_trades) - len(lean_trades)) / max(len(lean_trades), 1)
    assert count_pct_diff <= 0.05, f"Trade count divergence {count_pct_diff:.1%} exceeds 5%"

    # Per-trade slippage
    for vbt_t, lean_t in _matched_trades(vbt_trades, lean_trades):
        vbt_slip_bps = _compute_slippage_bps(vbt_t)
        lean_slip_bps = _compute_slippage_bps(lean_t)
        assert abs(vbt_slip_bps - lean_slip_bps) <= 5, (
            f"Slippage diff {abs(vbt_slip_bps - lean_slip_bps):.1f}bps > 5bps "
            f"for trade {vbt_t['id']}"
        )

    # Aggregate P&L
    vbt_pnl = sum(Decimal(str(t["pnl"])) for t in vbt_trades)
    lean_pnl = sum(Decimal(str(t["pnl"])) for t in lean_trades)
    pnl_diff_pct = abs(vbt_pnl - lean_pnl) / Decimal(str(starting_equity))
    assert pnl_diff_pct <= Decimal("0.005"), (
        f"Aggregate P&L divergence {pnl_diff_pct:.3%} exceeds 0.5%"
    )
```

## 6.7 Coverage Requirements

| Service | Minimum coverage |
|---|---|
| `services/risk/` | 90% |
| `services/audit/` | 90% |
| `services/execution/` | 90% |
| All others | 70% |

CI runs unit + integration on every PR. Weekly cron runs golden-test + vectorbt-vs-LEAN parity.

---

# 7. Database Patterns

## 7.1 Alembic Migration Conventions

**Filename convention (hybrid; locked 2026-05-05 Day 3):**

- **Foundational migrations** — initial schema bootstrap, applied as one closed batch. Filename: `NNNN_<short_description>.py`, monotonic from `0001_`. Used for the Day 3 Phase 0 migrations 0001–0006 (`audit_log`, core tables, risk tables, ops tables, immutability, roles). Do NOT extend the numeric series with `0007_` later — append-only filenames continue under the operational scheme below.
- **Operational migrations** — every migration authored Day 4 onward. Filename: `YYYY-MM-DD_<short_description>.py`. The filename carries the authorship date; same-day migrations disambiguate by suffix (`_part2`, `_v2`).

**Other rules:**

- One logical change per migration.
- Both `upgrade()` AND `downgrade()` always implemented and tested.
- Additive changes (add column with default, add index) deploy without downtime.
- Transformative changes (rename column, change type, drop column) require maintenance window: Saturday 17:00 ET → Sunday 18:00 ET.
- Migration runner MUST abort if a CME session is active outside the maintenance window.
- All `alembic/**` paths are forbidden-whitelist (§2.2). Every migration PR requires the `risk-review-approved` label; the `forbidden-paths` CI workflow gates the merge.

Example operational migration (numeric foundational migrations look the same modulo filename):

```python
# alembic/versions/2026-05-05_add_reconciliation_breaks.py
"""Add reconciliation_breaks table.

Revision ID: 3f7a1b2c9d4e
Revises: 2e6b0a1c8d3f
Create Date: 2026-05-05 09:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "3f7a1b2c9d4e"
down_revision = "2e6b0a1c8d3f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reconciliation_breaks",
        sa.Column("id", sa.UUID(), nullable=False, server_default=sa.text("uuid_generate_v7()")),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("detected_at_utc", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=True),
        sa.Column("expected", sa.Numeric(30, 8), nullable=True),
        sa.Column("actual", sa.Numeric(30, 8), nullable=True),
        sa.Column("delta", sa.Numeric(30, 8), nullable=True),
        sa.Column("tolerance", sa.Numeric(30, 8), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("resolved_at_utc", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resolution_path", sa.Text(), nullable=True),
        sa.Column("audit_event_uuid", sa.UUID(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "resolution_path IN ('grace_period','manual','kill_switch','tolerance_widened_dividend')",
            name="ck_resolution_path",
        ),
    )
    op.create_index(
        "ix_reconciliation_breaks_detected",
        "reconciliation_breaks",
        ["detected_at_utc"],
    )


def downgrade() -> None:
    op.drop_index("ix_reconciliation_breaks_detected", table_name="reconciliation_breaks")
    op.drop_table("reconciliation_breaks")
```

## 7.2 asyncpg Connection Pool

```python
# services/api/db.py
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from typing import AsyncGenerator

engine = create_async_engine(
    settings.database_url,
    pool_size=5,
    max_overflow=15,        # total max: 5 + 15 = 20
    pool_timeout=30,
    pool_pre_ping=True,
    connect_args={
        "statement_timeout": "30000",   # 30s for app queries
        "server_settings": {
            "application_name": "api_service",
        },
    },
    echo=False,
)

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
```

For slippage calibration jobs, use 60s statement timeout via a separate engine or `SET LOCAL statement_timeout = '60000'` in the session.

SERIALIZABLE isolation only for `audit_log` writes, via `SET TRANSACTION ISOLATION LEVEL SERIALIZABLE` inside the advisory-locked transaction (§5.1). Default is READ COMMITTED.

## 7.3 Hash-Chain Helpers

```python
# services/audit/chain.py
from __future__ import annotations

import hashlib
import jcs
from typing import Any


def jcs_serialize(payload: dict[str, Any]) -> bytes:
    """RFC 8785 JCS canonical serialization. Deterministic across runtimes."""
    return jcs.canonicalize(payload)


def compute_record_hash(prev_hash: bytes, payload_jcs: bytes) -> bytes:
    """SHA-256(prev_hash || payload_jcs). Chain integrity check."""
    return hashlib.sha256(prev_hash + payload_jcs).digest()


async def verify_chain(session: AsyncSession) -> tuple[bool, int | None]:
    """Verify hash chain integrity. Returns (ok, first_bad_sequence_no)."""
    from sqlalchemy import text
    rows = (await session.execute(
        text("SELECT sequence_no, prev_hash, record_hash, payload_jcs FROM audit_log ORDER BY sequence_no ASC")
    )).fetchall()

    expected_prev = b"\x00" * 32
    for row in rows:
        computed = compute_record_hash(row.prev_hash, row.payload_jcs)
        if row.record_hash != computed or row.prev_hash != expected_prev:
            return False, row.sequence_no
        expected_prev = row.record_hash
    return True, None
```

## 7.4 Immutability Triggers (apply in migration)

```sql
-- Apply in initial Alembic migration for audit_log
CREATE OR REPLACE FUNCTION audit_log_immutability_trigger()
RETURNS TRIGGER AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is append-only; UPDATE/DELETE forbidden (TG_OP=%)', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_immutability
BEFORE UPDATE OR DELETE ON audit_log
FOR EACH ROW EXECUTE FUNCTION audit_log_immutability_trigger();

CREATE OR REPLACE FUNCTION block_audit_truncate()
RETURNS event_trigger AS $$
DECLARE r record;
BEGIN
  FOR r IN SELECT * FROM pg_event_trigger_ddl_commands() LOOP
    IF r.command_tag = 'TRUNCATE TABLE' AND r.objid::regclass::text LIKE 'audit_log%' THEN
      RAISE EXCEPTION 'TRUNCATE forbidden on audit_log';
    END IF;
  END LOOP;
END;
$$ LANGUAGE plpgsql;

CREATE EVENT TRIGGER block_audit_truncate
ON ddl_command_start
WHEN TAG IN ('TRUNCATE TABLE')
EXECUTE FUNCTION block_audit_truncate();

REVOKE TRUNCATE ON audit_log FROM PUBLIC, app_service, app_owner;
GRANT TRUNCATE ON audit_log TO dba_breakglass;
```

---

# 8. Frontend Patterns

## 8.1 Next.js App Router Conventions

```
apps/web/src/app/
├── layout.tsx                  # root layout — providers only
├── (pre-auth)/
│   ├── login/page.tsx
│   ├── setup/page.tsx
│   └── recover/page.tsx
├── (post-auth)/
│   ├── layout.tsx              # auth guard + nav + SSE setup
│   ├── page.tsx                # /today
│   ├── trades/
│   │   ├── page.tsx
│   │   └── [id]/page.tsx
│   ├── performance/page.tsx
│   ├── system/
│   │   ├── page.tsx
│   │   └── audit/[id]/page.tsx
│   └── calendar/page.tsx
├── api/                        # Next.js route handlers (thin wrappers only)
└── middleware.ts               # phase gate check
```

`middleware.ts` enforces route availability:

```typescript
// apps/web/src/middleware.ts
import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';
import { ROUTES } from '@/lib/routes.config';

const CURRENT_PHASE = parseInt(process.env.NEXT_PUBLIC_PHASE ?? '0', 10);

export function middleware(request: NextRequest) {
  const pathname = request.nextUrl.pathname;
  const route = ROUTES.find(r => pathname.startsWith(r.path));
  if (route && route.available_from > CURRENT_PHASE) {
    return new NextResponse(null, { status: 404 });
  }
  return NextResponse.next();
}
```

## 8.2 TanStack Query Hook Pattern

```typescript
// apps/web/src/lib/api/use-positions.ts
'use client';
import { useQuery } from '@tanstack/react-query';
import { apiClient } from '@/lib/api/client';
import { useSessionAware } from '@/lib/hooks/use-session-aware';
import type { Position } from '@trading/api-types';

export function usePositions() {
  const staleTime = useSessionAware(30_000, 300_000);   // 30s during session, 5min off
  return useQuery<Position[]>({
    queryKey: ['positions'],
    queryFn: () => apiClient.get('/api/positions/current'),
    staleTime,
    refetchOnWindowFocus: 'always',
  });
}
```

## 8.3 SSE Consumer Hook

```typescript
// apps/web/src/lib/sse.ts — singleton; initialized once in root provider
'use client';
import { fetchEventSource } from '@microsoft/fetch-event-source';
import { useSSEStore } from '@/lib/stores/sse-store';
import { queryClient } from '@/lib/queryClient';

const BACKOFF_BASE_MS = 5_000;
const BACKOFF_MAX_MS = 60_000;
const MAX_ATTEMPTS_BEFORE_POLL = 10;

let attempts = 0;
let abortController: AbortController | null = null;

function computeBackoff(): number {
  const base = Math.min(BACKOFF_BASE_MS * Math.pow(2, attempts), BACKOFF_MAX_MS);
  return base + Math.random() * 10_000;  // jitter 0-10s
}

export function connectSSE() {
  abortController?.abort();
  abortController = new AbortController();
  const { setConnection, ingest, lastSeq } = useSSEStore.getState();

  fetchEventSource('/api/sse/events', {
    signal: abortController.signal,
    headers: { 'Last-Event-ID': String(lastSeq) },
    onopen: async (resp) => {
      if (resp.status === 426) {
        // Buffer expired or version mismatch — full refetch
        await queryClient.invalidateQueries();
        throw new Error('client_must_full_refetch');
      }
      if (!resp.ok) throw new Error(`SSE open failed: ${resp.status}`);
      attempts = 0;
      setConnection('connected');
    },
    onmessage: (ev) => {
      if (!ev.data) return;
      try {
        const envelope = JSON.parse(ev.data);
        ingest(envelope);
      } catch {
        // malformed event — log and continue
      }
    },
    onerror: (err) => {
      attempts++;
      if (attempts >= MAX_ATTEMPTS_BEFORE_POLL) {
        setConnection('polling');
        abortController?.abort();
        return;  // stop retrying SSE; polling mode activated
      }
      setConnection('disconnected');
      return computeBackoff();  // tell fetchEventSource to retry after N ms
    },
  });
}
```

## 8.4 Zustand SSE Store

```typescript
// apps/web/src/lib/stores/sse-store.ts
import { create } from 'zustand';
import { queryClient } from '@/lib/queryClient';
import type { SSEEnvelope, PnLEvent, RiskStateEvent, HealthEvent, AlertEvent } from '@trading/api-types';

type SSEStore = {
  lastSeq: number;
  lastEventAtMs: number;
  isConnected: boolean;
  isPolling: boolean;
  evictionReason: string | null;
  latestPnL: PnLEvent | null;
  latestRiskState: RiskStateEvent | null;
  latestHealth: HealthEvent | null;
  latestAlert: AlertEvent | null;
  ingest: (envelope: SSEEnvelope) => void;
  setConnection: (state: 'connected' | 'polling' | 'disconnected') => void;
};

export const useSSEStore = create<SSEStore>((set, get) => ({
  lastSeq: 0,
  lastEventAtMs: 0,
  isConnected: false,
  isPolling: false,
  evictionReason: null,
  latestPnL: null,
  latestRiskState: null,
  latestHealth: null,
  latestAlert: null,

  ingest: (envelope) => {
    set({ lastSeq: envelope.sequence_no, lastEventAtMs: performance.now() });
    switch (envelope.type) {
      case 'pnl':
        set({ latestPnL: envelope.data as PnLEvent });
        break;
      case 'risk_state':
        set({ latestRiskState: envelope.data as RiskStateEvent });
        queryClient.invalidateQueries({ queryKey: ['system', 'status'] });
        break;
      case 'health':
        set({ latestHealth: envelope.data as HealthEvent });
        queryClient.invalidateQueries({ queryKey: ['health-score'] });
        break;
      case 'alert':
        set({ latestAlert: envelope.data as AlertEvent });
        queryClient.invalidateQueries({ queryKey: ['alerts'] });
        break;
      case 'fill':
        queryClient.invalidateQueries({ queryKey: ['fills'] });
        queryClient.invalidateQueries({ queryKey: ['positions'] });
        queryClient.invalidateQueries({ queryKey: ['pnl'] });
        break;
      case 'signal':
        queryClient.invalidateQueries({ queryKey: ['signals'] });
        break;
      case 'session_evicted':
        set({ evictionReason: (envelope.data as { reason: string }).reason });
        window.location.href = '/login?reason=evicted';
        break;
      case 'version':
        if ((envelope.data as { must_reload: boolean }).must_reload) {
          window.location.reload();
        }
        break;
    }
  },

  setConnection: (state) => set({
    isConnected: state === 'connected',
    isPolling: state === 'polling',
  }),
}));
```

## 8.5 Loading / Error / Stale States

Every data-driven component handles all four states. No exceptions.

```typescript
// Example: positions tile on /today
'use client';
import { usePositions } from '@/lib/api/use-positions';
import { useSSEStore } from '@/lib/stores/sse-store';
import { formatPrice } from '@/lib/format';

export function PositionsTile() {
  const { data, isLoading, isError, dataUpdatedAt } = usePositions();
  const lastEventAtMs = useSSEStore(s => s.lastEventAtMs);
  const isStale = dataUpdatedAt && (performance.now() - lastEventAtMs) > 90_000;

  if (isLoading) {
    return <TileSkeleton rows={3} />;
  }

  if (isError) {
    return <TileError message="Positions unavailable" />;
  }

  return (
    <div className="relative">
      {isStale && (
        <span
          className="absolute top-2 right-2 size-2 rounded-full bg-yellow-400"
          title={`Last updated: ${new Date(dataUpdatedAt).toLocaleTimeString()}`}
        />
      )}
      <table>
        <tbody>
          {(data ?? []).map(pos => (
            <tr key={pos.market}>
              <td className="font-mono tabular-nums">{pos.market}</td>
              <td className="font-mono tabular-nums">{pos.quantity}</td>
              <td className="font-mono tabular-nums">{formatPrice(pos.unrealized_pnl, 2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
```

## 8.6 Form Pattern (react-hook-form + zod)

```typescript
// apps/web/src/components/parameter-change-form.tsx
'use client';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { useMutation } from '@tanstack/react-query';
import { Form, FormField, FormItem, FormLabel, FormMessage } from '@/components/ui/form';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { apiClient } from '@/lib/api/client';

const ParameterChangeSchema = z.object({
  parameter_name: z.string().min(1),
  new_value: z.coerce.number().positive(),
  rationale: z.string().min(50, 'Rationale must be at least 50 characters'),
});

type ParameterChangeFields = z.infer<typeof ParameterChangeSchema>;

export function ParameterChangeForm() {
  const form = useForm<ParameterChangeFields>({
    resolver: zodResolver(ParameterChangeSchema),
  });

  const mutation = useMutation({
    mutationFn: (data: ParameterChangeFields) =>
      apiClient.post('/api/parameters/change', data),
    onError: (err) => {
      toast.error(`Parameter change failed: ${err.message}`);
    },
  });

  return (
    <Form {...form}>
      <form onSubmit={form.handleSubmit(v => mutation.mutate(v))}>
        <FormField
          control={form.control}
          name="rationale"
          render={({ field }) => (
            <FormItem>
              <FormLabel>Rationale (min 50 chars)</FormLabel>
              <Input {...field} />
              <FormMessage />
            </FormItem>
          )}
        />
        <Button type="submit" disabled={mutation.isPending}>
          {mutation.isPending ? 'Submitting...' : 'Submit Change'}
        </Button>
      </form>
    </Form>
  );
}
```

---

# 9. Cross-Cutting Patterns

## 9.1 Secrets Workflow

- sops decrypts `secrets/<env>.enc.yaml` at container start via init container.
- Secrets are exposed as env vars only. Never written to disk in plaintext.
- Never write secrets to source files, comments, logs, or tests.
- Rotation: `scripts/rotate-secrets.sh` — rotates age key + re-encrypts all three env files.
- `gitleaks` pre-commit hook rejects any commit containing secret-like strings.

```python
# services/api/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str
    anthropic_api_key: str
    discord_bot_token: str
    resend_api_key: str
    qc_user_id: str
    qc_api_token: str
    sentry_dsn: str | None = None

    # Phase gate
    current_phase: int = 0

    # Feature: never runtime feature flags — deployment-controlled only
    # Add fields here when new env-controlled config needed


settings = Settings()
```

## 9.2 Feature Flags

No runtime feature flags. Phase gating is deployment-controlled via `NEXT_PUBLIC_PHASE` env var and `routes.config.ts`. Adding a runtime flag requires escalation + PR.

## 9.3 Error Envelopes (frontend)

TanStack Query errors surface via toast with `error_code` mapped to a human-readable message. Never show raw error text.

```typescript
// apps/web/src/lib/api/client.ts
import { HTTPError } from 'ky';
import { toast } from '@/lib/toast';

const ERROR_MESSAGES: Record<string, string> = {
  SIGNAL_NOT_FOUND: 'Signal not found or has expired.',
  KILL_SWITCH_ALREADY_HALTED: 'System is already halted.',
  REAUTH_REQUIRED: 'Re-authentication required for this action.',
  // add entries as new error_codes are introduced
};

export async function handleApiError(err: unknown): Promise<never> {
  if (err instanceof HTTPError) {
    const body = await err.response.json().catch(() => ({}));
    const errorCode = body?.error_code ?? 'UNKNOWN';
    const message = ERROR_MESSAGES[errorCode] ?? `An error occurred (${errorCode})`;
    toast.error(message);
  }
  throw err;
}
```

## 9.4 Audit Emission Discipline

Every state-changing API endpoint emits an audit event before returning. The response includes `audit_event_uuid`.

```python
# Pattern: every mutation endpoint follows this structure
@router.post("/api/signals/{signal_id}/approve")
async def approve_signal(
    signal_id: UUID,
    session: AsyncSession = Depends(get_session),
    current_user = Depends(require_auth),
) -> dict:
    # 1. Business logic
    signal = await get_signal_or_404(session, signal_id)
    await _approve_signal(session, signal)

    # 2. Audit BEFORE returning (not after)
    audit_record = await append_audit_event(
        session,
        AuditEventType.SIGNAL_APPROVED,
        {
            "signal_id": str(signal_id),
            "operator_session_id": current_user.session_id,
            "ts": datetime.now(tz=UTC).isoformat(),
        },
    )

    # 3. SSE broadcast
    await emit_sse("signal", {"signal_id": str(signal_id), "status": "approved"})

    # 4. Response includes audit_event_uuid for traceability
    return {
        "signal_id": str(signal_id),
        "status": "approved",
        "audit_event_uuid": str(audit_record.event_uuid),
    }
```

---

# 10. Phase-Specific Dev Priorities

## 10.1 Phase 0 (Weeks 0–8)

| Week | Build first | Integration points | Test gate |
|---|---|---|---|
| 1 | Repo scaffold (pnpm workspace, ruff/mypy/pytest CI, pre-commit hooks); v1 strategy code authored on QC by Claude Code with operator review; Hetzner VPS provisioned (Ashburn primary + Falkenstein watchdog) | None (mostly setup) | Pre-commit passes; v1 strategy commits clean; VPS reachable via SSH; CI green on first PR |
| 2 | Phase 1 sub-universe verification (data executability + 50% single-contract-notional rule per current equity); sops + age key generated, encrypted env files committed; QC paper trading kicks off (paper-day clock starts) | QC paper data | Active universe enumerated for current equity tier; `sops -d secrets/dev.enc.yaml` decrypts; first QC paper session logged |
| 3 | FastAPI `/api/health` + `/internal/health/deep`; Postgres connection pool (asyncpg); structlog JSON setup; `audit_log` DDL migration with hash chain; `append_audit_event()` with advisory lock + SERIALIZABLE retry loop; immutability triggers (BEFORE UPDATE/DELETE per row + BEFORE TRUNCATE per statement on parent + every yearly partition; spec §2.10.2's EVENT TRIGGER pattern doesn't fire on TRUNCATE — see `Docs/decisions-log.md` 2026-05-05 Day 3) | DB connectivity | `GET /api/health` returns 200; `test_audit_append_concurrent_writers_serialize_correctly` passes; TRUNCATE on `audit_log` (parent + any yearly partition) blocked by `BEFORE TRUNCATE` trigger |
| 4 | QC ObjectStore client scaffold; `qc_adapter_cursor` table; golden-test harness; QC parity fixtures for 5 session event types; JCS canonicalization helper | QC mock + ObjectStore | Golden test passes for all 5 fixtures byte-for-byte modulo `{ingest_clock_ts, ingest_uuid, sequence_no}` |
| 5 | REST scaffolding for Phase 1 endpoints; SSE channel `/api/sse/events` with multiplexed event types; instruction round-trip processor (Phase 1); reconciliation service skeleton | DB + QC mock | SSE connection + 24h replay via `last-event-id` works; instruction round-trip < 20s p99 against mock |
| 6 | Next.js scaffold; WebAuthn registration/login backend handlers (UV `required`); route phase-gate middleware via `routes.config.ts`; Caddy reverse proxy with SSE flush directives | Web ↔ API | `/setup` + `/login` passkey flow works end-to-end; tab eviction (N=4) emits `session_evicted` |
| 7 | Signal-to-paper-fill end-to-end via QC instruction protocol; Discord bot scaffold (`/positions`, `/halt`); 30th CME paper session completed | QC paper trading + Discord | 30 CME paper sessions clean; audit chain verified end-to-end; Discord `/positions` returns live data |
| 8 | Frontend Today + Trades minimal + System minimal; operator competence assessment (deploy, restart, read logs, invoke kill-switch from Discord) | Full stack | Operator passes competence checklist; Phase 1 surfaces ship; ready for live trading month 2 |

Build order within each week: **tests first** (write failing tests), then implementation.

## 10.2 Phase 1 (Months 2–5)

| Month | Build first | Why |
|---|---|---|
| 2 | Prometheus + Grafana stack; alert routing (Discord + Resend) | Monitoring BEFORE live money |
| 2 | Live cutover to `live-small`; first reconciliation pass against FlexQuery | |
| 3 | Slippage recalibration cron; vectorbt-vs-LEAN parity CI gate | |
| 4 | PR review surface artifacts (§5.8); operator-friendly UI on `/system/pr/:id` | Operator review workflow must work |
| 5 | Phase 2 prep: `ib-async` integration tests; LEAN Local docker | |

## 10.3 Phase 2 (Months 5–9)

- `ib-async` integration with comprehensive test coverage BEFORE cutover.
- Direct IBKR market data feed replaces QC ObjectStore for intraday.
- LEAN Local docker replaces QC Cloud.
- Cutover execution: positions = 0 pre-cutover verified; first Phase 2 signal-to-fill round trip ≤ 5s SLO.
- Vol-carry strategy: sequential addition; new strategy version starts 30-day paper before live.

## 10.4 Phase 3 (Months 9–12)

- Investor PDF generation (Typst templating).
- CPA reader role middleware (redaction of cost-basis fields).
- Family money onboarding flow (requires LLC/legal structure first).

---

# 11. Anti-Patterns

Each entry: what NOT to do, why, what to do instead.

**[A01]** DO NOT write to `audit_log` directly via INSERT. Silent hash chain corruption under concurrency. Use `append_audit_event()` from `services/audit/writer.py` exclusively.

**[A02]** DO NOT modify any file in the forbidden whitelist (`services/risk/**`, `services/signal/**`, `services/audit/**`, `services/execution/**`, `services/reconciliation/**`, `services/calibration/**`, `services/agent/decisions/**`, `services/agent/risk_actions/**`, `services/agent/parameter_changes/**`, `services/agent/prompts/decision/**`, `alembic/**`) without the `risk-review-approved` PR label. The pre-merge linter is mechanical and will block the merge.

**[A03]** DO NOT introduce a new SSE `event_type` without adding it to: (a) the locked enum in `services/audit/event_types.py`, (b) the TypeScript `SSEEnvelope` type in `packages/api-types/`. Type drift silently breaks the multiplexed channel. Add to both + regenerate + write test.

**[A04]** DO NOT introduce a new audit `event_type` without adding it to the locked taxonomy enum in `services/audit/event_types.py` and writing at least one test that emits and reads back.

**[A05]** DO NOT use `float` for money or price values. Use `decimal.Decimal` in Python; `Decimal` from `decimal.js` in TypeScript. Serialize as strings in API payloads. Float arithmetic accumulates rounding errors that break P&L reconciliation.

**[A06]** DO NOT call `datetime.now()` without a timezone. Always `datetime.now(tz=UTC)` or `datetime.now(tz=timezone.utc)`. Never `datetime.utcnow()` (deprecated; no tzinfo). Storage always UTC; rendering always via `formatET()`.

**[A07]** DO NOT use `Date.toLocaleString()`, `Intl.DateTimeFormat`, or raw Date methods in components. Always `formatET()` from `apps/web/src/lib/format.ts`. Timezone display is a contract with the operator.

**[A08]** DO NOT trust the browser absolute clock for stale-data calculations. Use `performance.now()` for elapsed time only (`lastEventAtMs` is a `performance.now()` value, not a wall-clock timestamp).

**[A09]** DO NOT import Recharts or Lightweight Charts in `/today`. They go on `/performance` and `/research` only, via dynamic imports (`next/dynamic` with `ssr: false`). CI bundle analyzer gates `/today` bundle size.

**[A10]** DO NOT blend `paper`, `live-small`, or `live-scale` environment data in a single number or chart. Cross-environment segregation is a hard invariant. Backend enforces via `account_id`; frontend must never aggregate across them.

**[A11]** DO NOT inline secrets in any file: source, config, test fixture, comment, Docker environment. Env vars only. `gitleaks` pre-commit hook will reject the commit.

**[A12]** DO NOT manually edit `paper_days_completed` in the `strategy_versions` table to bypass the 30-paper-day gate. The CI gate is mechanical for a reason: live trading with an untested strategy version is a capital risk.

**[A13]** DO NOT write Python code that calls IBKR TWS API directly in Phase 1. Phase 1 has no direct IBKR connection. All broker interaction passes through the QC instruction protocol (`/instructions/<n>.json` → poll ack). Use `write_instruction()` + `poll_for_ack()`.

**[A14]** DO NOT use SES for email. Resend (`resend.com`) is locked. The `resend_api_key` in secrets; `Resend` Python SDK or HTTP client.

**[A15]** DO NOT use bcrypt for backup code hashing. Argon2id is locked. `argon2-cffi` package in Python.

**[A16]** DO NOT introduce a new Alembic migration without implementing `downgrade()`. A migration with only `upgrade()` is untestable in rollback scenarios. CI runs `alembic downgrade -1` as a gate.

**[A17]** DO NOT apply transformative Alembic migrations (rename, type change, DROP) outside the maintenance window (Saturday 17:00 ET → Sunday 18:00 ET). Add the CME-session-active check to the migration runner.

**[A18]** DO NOT use the bare `<your-domain>`, `<operator_email>`, `<operator_username>`, `<discord_guild_id>`, or `<watchdog_static_ip>` placeholders in any code or config. Substitute actual values at deployment via sops env vars.

**[A19]** DO NOT call `audit.write()` from within the same SERIALIZABLE transaction that writes business data, unless the business data write and the audit write must be atomic. Usually audit writes are a separate transaction immediately after. Mixing them inside a long-running SERIALIZABLE transaction increases contention.

**[A20]** DO NOT use `useEffect` + `fetch` for server state. TanStack Query exclusively. `useEffect` + `fetch` bypasses stale-data thresholds, retry logic, and cache invalidation.

**[A21]** DO NOT persist live state or auth state in Zustand `persist` middleware (localStorage). Only persist: `FiltersStore` (per-page filter state), `authStrength` + `role` metadata from `AuthStore`. Never positions, P&L, signals, orders, fills.

**[A22]** DO NOT emit `audit` events from within tests unless you are specifically testing the audit chain. Use mocked `append_audit_event()` in unit tests; real DB only in integration tests via testcontainers.

**[A23]** DO NOT add a new route to `apps/web/src/app/` without adding it to `routes.config.ts` with the correct `available_from` phase. Unregistered routes bypass the phase gate middleware.

**[A24]** DO NOT use `asyncio.sleep()` inside a synchronous function to "wait for async work." If you need async waiting, the caller must be `async def`. Sync sleeps block the event loop.

**[A25]** DO NOT write Python code using `logging` module. Only `structlog`. Using both creates two log streams, breaks log aggregation, and may leak audit-relevant data to the wrong stream.

**[A26]** DO NOT compute metrics (Sharpe, drawdown, hit rate, health score, exposure pcts) in the frontend. Backend computes and returns pre-computed values. Frontend renders only. This is a hard architectural constraint for reader-redaction simplicity.

---

# 12. PR-Drafting Templates

## 12.1 Strategy Logic Change (Forbidden Whitelist — PR Required)

```markdown
## Summary
<!-- one sentence: what changed in the strategy logic -->

## Why
<!-- operator rationale for this change; minimum 2 sentences -->

## Behavior Change at Runtime
<!-- what executes differently after this merge:
     - which signals are affected
     - direction of position size change (up/down/neutral)
     - expected backtest delta (provide numbers) -->

## Backtest Delta
- Sharpe before: X.XX → after: X.XX (Δ: +/-X.XX)
- Max DD before: X.X% → after: X.X%
- Trade count before: NNN → after: NNN
- Slippage calibration version: <version_id>

## Tests
- [ ] Unit tests pass
- [ ] Integration tests pass
- [ ] mypy --strict passes
- [ ] ruff passes
- [ ] gitleaks passes

## Review Surface
In-app artifacts generated: plain-English summary, risk impact, backtest delta, test results.

---
**Labels:** `strategy-logic`, `risk-review-approved` (required before merge)
**Paper days gate:** new strategy_version will require 30 paper sessions before live eligibility
```

## 12.2 Parameter Change Within Agent-Mutable Range

```markdown
## Summary
Change `<parameter_name>` from `<old_value>` to `<new_value>`.

## Why
<rationale — at least 50 characters>

## Behavior Change
<!-- which direction sizing moves; estimated effect on position count -->

## Tests
- [ ] Unit tests pass for services/risk/multipliers.py and services/risk/sizing.py

---
**Labels:** `parameter-change`
**Auto-revert:** parameter_changes service will auto-revert within 2 sessions if metrics breach
```

## 12.3 Hot-Fix Infrastructure

```markdown
## Summary
<!-- what changed in the hot-fix-whitelist path -->

## Why
<!-- the bug or degradation being fixed -->

## Behavior Change
No behavior change to trading logic.
<!-- OR: describe the infra behavior change -->

## Tests
- [ ] Unit tests pass
- [ ] Integration tests pass
- [ ] mypy --strict passes

---
**Labels:** `hot-fix`, `infrastructure`
**Deploy:** auto-deploy eligible; auto-rollback armed for 30 min post-deploy
```

## 12.4 Bug Fix (General)

```markdown
## Summary
Fix: <one-sentence description of the bug and fix>

## Root Cause
<!-- what caused the bug; reference audit log event_uuid or sequence_no if relevant -->

## Fix
<!-- what specifically changed; reference file:line -->

## Tests Added
<!-- new test that would have caught this bug -->

---
**Labels:** `bug`
```

## 12.5 New Feature (Phase-Aligned)

```markdown
## Summary
Add: <feature name> (Phase <N>)

## Spec Reference
backend-spec §N.N / frontend-spec §N.N

## What This Enables
<!-- what the operator can do that they couldn't before -->

## Files Changed
<!-- list key files; full diff linked below -->

## Tests
- [ ] Unit tests: N new tests
- [ ] Integration tests: N new tests
- [ ] E2E test (if UI): test file + assertion
- [ ] Type coverage: mypy --strict passes

---
**Labels:** `feature`, `phase-<N>`
```

---

# 13. Operator Review Checklist

The operator reviews the in-app PR review surface on `/system/pr/:id`. Each item is yes/no verifiable from the surface artifacts without reading the diff.

- [ ] **Plain-English summary present and ≤ 200 words?** Reject if absent or > 200 words — the summary is required for review.
- [ ] **Risk impact summary auto-generated and shows specific numbers?** (Sharpe delta, max DD delta, exposure change.) A vague "no impact" is not acceptable for strategy/parameter PRs.
- [ ] **Backtest delta runs against the locked `slippage_calibration_version_id`?** Version ID shown in artifacts must match the current pinned version in `slippage_calibration_versions`.
- [ ] **All tests pass?** Unit + integration + lint + type-check + gitleaks. CI status shown in artifacts.
- [ ] **Files affected list matches plain-English summary?** If summary says "only observability changes" but files affected includes `services/risk/`, that is a mismatch — investigate.
- [ ] **No files in forbidden whitelist modified without `risk-review-approved` label?** Pre-merge linter enforces this; double-check in artifacts.
- [ ] **If new SSE event type: enum updated in both Python `AuditEventType` and TypeScript `SSEEnvelope`?** Confirm in files affected.
- [ ] **If new audit event type: enum + test that emits and reads back?** Confirm test file in files affected.
- [ ] **If new API endpoint: endpoint matches the spec's API contract?** Path, method, auth requirement, response shape.
- [ ] **If Alembic migration: additive (no maintenance window required) or transformative (scheduled for window)?** Migration file visible in files affected; type stated in PR body.
- [ ] **If strategy logic change: `paper_days_completed` reset to 0 for the new strategy_version?** Confirmed in plain-English summary.
- [ ] **If parameter change: new value within agent-mutable range?** Stated explicitly in risk impact summary.
- [ ] **If new dependency added: package version pinned in pyproject.toml or package.json?** No `^` or `~` version ranges in production dependencies for critical packages.
- [ ] **PR description is not empty?** A PR with no description is not reviewable.

---

# 14. Living Document Protocol

## 14.1 When to Update This Guide

| Trigger | Action |
|---|---|
| New canonical pattern needed (approved via escalation) | Add to §5 with full runnable code snippet; add anti-pattern entry to §11 if relevant |
| Pattern proves incorrect (postmortem) | Mark old pattern as `[DEPRECATED — see §5.X]`; add new pattern; add anti-pattern with postmortem reference |
| New locked tooling adopted | Update §3 or §4 standards table; add migration example if breaking |
| Anti-pattern surfaces from a real bug | Add to §11 with date and brief incident description |
| Operator review checklist gains items | Add to §13 |
| Phase boundary crossed | Update §10 to mark prior phase complete; clarify current-phase priorities |

## 14.2 Update Process

1. PR touching only `claude-dev-guide.md`.
2. In-app review surface generated (plain-English summary describes what convention changed).
3. Operator approves.
4. No `risk-review-approved` label required unless the change relates to risk or strategy patterns.

## 14.3 Staleness Signals

If you are implementing something and the guide's canonical pattern doesn't match what the existing codebase actually does, escalate. Do not silently drift. The guide and the code must stay in sync.

---

*Claude Code Dev Guide — generated from `backend-spec.md`, `frontend-spec.md`, and `implementation-guide.md`. Update via PR per §14.*
