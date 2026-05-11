"""Slippage calibration fit — pure-policy plan-then-apply (backend-spec §2.14 + §3.12).

Same shape as ``services/risk/sizing.py``, ``services/risk/state_machine.py``,
``services/scheduler/vacation.py``, ``services/scheduler/calendar_import.py``,
``services/qc_adapter/poll.py``, and ``services/reconciliation/recon.py``:
``plan_calibration_fit`` returns a :class:`CalibrationPlan` struct as data;
the caller (Week 4 dispatcher) owns the I/O — INSERT into
``slippage_calibration_versions`` (with the planner-supplied
``new_version_id``, ``version_no``, ``per_market_coefficients`` JSONB, and
``data_window_*`` columns), flush :class:`PendingAuditEvent` via
``services.audit.writer.append_audit_event``, capture the returned
``audit_event_uuid``, and finally update the HEAD pointer (``UPDATE
slippage_calibration_versions SET is_head = FALSE WHERE is_head`` then
``UPDATE ... SET is_head = TRUE WHERE id = :new_version_id`` inside one
transaction; the ``slippage_head`` partial unique index defined in
``alembic/versions/0003_risk_tables.py`` enforces single-HEAD invariant
across writers).

Why split policy from I/O:

- Anti-pattern A22: unit tests must NOT write audit events. Pure policy is
  trivially testable with zero mocking — tests inspect the plan struct.
- Anti-pattern A01: only ``services.audit.writer.append_audit_event`` may
  write to ``audit_log``. The planner returns a payload; the dispatcher
  invokes the writer.
- The dispatcher that wires this module to the writer + HEAD-pointer swap
  lives behind a separate forbidden-whitelist PR (Week 4); shipping the
  policy module early lets reviewers verify the fit shape independently.
- The plan struct IS the spec contract — a reviewer can inspect what the
  calibration WILL persist without running it.

Functional form (locked — backend-spec §2.14 row "Functional form"):

    slippage_bps_market = alpha_market + beta_market * (order_size / ADV_30d)

The fit is **per-market** (not portfolio-level), so :class:`CalibrationPlan`
carries a ``per_market_coefficients`` Mapping that mirrors the §3.12
JSONB column shape (``{"MES": {"alpha": ..., "beta": ..., "n_obs": ...,
"r_squared": ...}, ...}``).

Two fit methods (locked — backend-spec §2.14 + IG Week 5 Thu task brief):

- :attr:`CalibrationFitMethod.ZERO_PRIOR`. The Phase-1 bootstrap (first 30
  days). All markets get alpha=0, beta=0, r_squared=None. Fired when
  ``trigger == CalibrationTrigger.BOOTSTRAP`` OR when the caller has no
  fills to fit on (``len(fills) == 0``). The dispatcher's monthly cron
  also falls back to zero-prior on a per-market basis when a market has
  fewer observations than :data:`MIN_OBSERVATIONS_FOR_OLS`.
- :attr:`CalibrationFitMethod.OLS`. Ordinary least squares per market on
  realized fills (compared to the LEAN-emitted ``expected_price`` at
  signal time). The fit-method label on the plan is OLS when ANY market
  in ``per_market_coefficients`` was OLS-fit; ZERO_PRIOR when every market
  fell back (or when the bootstrap branch was taken).

Slippage sign convention (locked — backend-spec §2.14 implicit):

- For a BUY: ``slippage_bps = (realized_price - expected_price)
  / expected_price * 10_000``. A buy filling above the expected mid is
  positive slippage (cost to the trader).
- For a SELL: ``slippage_bps = (expected_price - realized_price)
  / expected_price * 10_000``. A sell filling below the expected mid is
  positive slippage (cost to the trader).
- Direction sign is therefore fold-able into a single signed bps value:
  positive bps = trader paid more than mid; negative bps = price
  improvement (trader received better than mid).

OLS within-market math (Decimal arithmetic only — A05):

    x_i  = order_size_contracts_i / adv_30d_contracts_i
    y_i  = signed_slippage_bps_i (per the convention above)
    xbar = mean(x_i)
    ybar = mean(y_i)
    Sxx  = sum((x_i - xbar)**2)
    Sxy  = sum((x_i - xbar) * (y_i - ybar))
    Syy  = sum((y_i - ybar)**2)
    beta  = Sxy / Sxx               (if Sxx > 0; else beta = 0 by convention)
    alpha = ybar - beta * xbar
    r2    = 1 - sum((y_i - yhat_i)**2) / Syy   (if Syy > 0; else None)

When Sxx == 0 (all x_i identical, which happens in synthetic fills or
markets with constant block size), beta collapses to 0 and alpha = ybar.
r_squared is None in that branch since the regressor explains no variance
by construction. When Syy == 0 (all y_i identical, which means
zero-variance slippage — very rare in real fills), r_squared is None
even with non-zero Sxx. Both branches keep the plan numerically stable
with Decimal arithmetic; no float promotion.

Anti-patterns enforced:

- A01: planner does NOT call ``services.audit.writer.append_audit_event``.
  The dispatcher does, with the payload from :attr:`CalibrationPlan.audit_event`.
- A02: this file lives under ``services/calibration/**`` (forbidden
  whitelist). PR carries ``risk-review-approved`` label.
- A04: ``slippage_calibration_recalibrated`` is in the locked taxonomy
  enum at ``services/audit/event_types.py:107``. The audit-event type
  literal here is therefore safe and does NOT introduce a new value.
- A05: every monetary / coefficient value is :class:`Decimal`. Float
  inputs raise :class:`CalibrationError`. Audit-event payload serializes
  Decimals as strings.
- A06: ``calibrated_at_utc`` and ``FillObservation.fill_at_utc`` MUST
  be timezone-aware. Naive datetimes raise :class:`CalibrationError`.
- A22: tests must NOT actually emit audit events. This module returns
  :class:`PendingAuditEvent` as data; tests inspect the plan struct.
- A27 does NOT bind: pure-policy + Python; no third-party platform
  contract under exercise here. The future dispatcher's HEAD-pointer
  swap is a Postgres-trigger surface and is exercised by the Week 4
  testcontainers integration test, not this module.

Schema mapping (``alembic/versions/0003_risk_tables.py:97``,
``slippage_calibration_versions``):

- ``id`` ↔ :attr:`CalibrationPlan.new_version_id` (caller passes UUIDv7;
  planner echoes it into the audit-event payload).
- ``version_no`` ↔ :attr:`CalibrationPlan.version_no` (planner returns
  ``parent_version_no + 1`` or ``1`` if no parent).
- ``is_head`` ↔ :attr:`CalibrationPlan.becomes_head` (caller-driven
  signal; default True for Phase 1 monthly cron + bootstrap; False is
  reserved for a "compute-but-don't-promote" research path in Phase 1+).
- ``calibrated_at_utc`` ↔ ``calibrated_at_utc`` parameter.
- ``trigger`` ↔ :class:`CalibrationTrigger` (CHECK enum mirror).
- ``per_market_coefficients`` ↔ :attr:`CalibrationPlan.per_market_coefficients`
  (Mapping serializes to JSONB at INSERT time; payload conversion in
  :func:`_build_audit_event` already produces the canonical str-Decimal
  shape).
- ``data_window_start`` / ``data_window_end`` ↔
  :attr:`CalibrationPlan.data_window_start_utc` /
  :attr:`CalibrationPlan.data_window_end_utc` (None for empty fills).
- ``audit_event_uuid`` is caller-side (filled AFTER the writer returns).
- ``notes`` is caller-side (operational free-form).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Literal
from uuid import UUID

# ---------------------------------------------------------------------------
# Locked constants (backend-spec §2.14 + IG Week 5 Thu task brief)
# ---------------------------------------------------------------------------

#: Phase-1 bootstrap intercept (backend-spec §2.14 row "Bootstrap":
#: "initial alpha=0, beta=0 (zero-slippage prior)"). NOT operator-tunable.
BOOTSTRAP_ALPHA: Final[Decimal] = Decimal("0")

#: Phase-1 bootstrap slope. See :data:`BOOTSTRAP_ALPHA`.
BOOTSTRAP_BETA: Final[Decimal] = Decimal("0")

#: Per-market observation floor below which OLS falls back to zero-prior.
#: Aligns with the spec's "30 days of Phase 1 LIVE fills" lock — the
#: dispatcher's monthly cron will fan out across markets, and a market
#: that hasn't accumulated 30 fills yet keeps its zero-prior coefficients
#: rather than fitting on a noisy small sample.
MIN_OBSERVATIONS_FOR_OLS: Final[int] = 30

#: Bps multiplier applied when computing signed slippage from
#: ``(realized_price - expected_price) / expected_price``. 10_000 = 1 bp
#: per 0.01% by definition; lifted to a constant for grep-ability and to
#: keep the slippage formula self-documenting.
BPS_PER_UNIT: Final[Decimal] = Decimal("10000")


# ---------------------------------------------------------------------------
# Enums (mirroring schema CHECK constraints + spec language)
# ---------------------------------------------------------------------------


class CalibrationFitMethod(StrEnum):
    """Plan-level label of which fit branch was taken.

    The plan label is :attr:`OLS` when ANY market in
    ``per_market_coefficients`` was OLS-fit; :attr:`ZERO_PRIOR` when
    every market fell back (or when the bootstrap branch was taken).
    The per-market label lives implicitly in
    :attr:`MarketCoefficient.r_squared` — ``None`` means the market got
    a zero-prior (or zero-variance) fit.
    """

    ZERO_PRIOR = "zero_prior"
    OLS = "ols"


class CalibrationTrigger(StrEnum):
    """``slippage_calibration_versions.trigger`` CHECK constraint mirror.

    From ``alembic/versions/0003_risk_tables.py:102-103``:
        ``trigger TEXT NOT NULL CHECK (trigger IN
            ('bootstrap','scheduled_monthly','scheduled_quarterly','manual'))``
    """

    BOOTSTRAP = "bootstrap"
    SCHEDULED_MONTHLY = "scheduled_monthly"
    SCHEDULED_QUARTERLY = "scheduled_quarterly"
    MANUAL = "manual"


# Locked-taxonomy event types (backend-spec §3.30, mirrored in
# services/audit/event_types.py — SLIPPAGE_CALIBRATION_RECALIBRATED).
CalibrationAuditEventType = Literal["slippage_calibration_recalibrated"]


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FillObservation:
    """One realized fill used as an OLS input row.

    Decimal-only per A05; tz-aware per A06. The dispatcher materializes a
    sequence of these from the ``fills`` table at recalibration time
    (joined to ``signals`` for the LEAN-emitted ``expected_price`` and
    to a 30-day rolling ADV view).

    - ``market``: e.g. ``"MES"`` or ``"TLT"``. Non-empty string.
    - ``order_size_contracts``: integer-valued :class:`Decimal` (use
      ``Decimal("3")`` not ``Decimal(3.0)``). Must be > 0.
    - ``adv_30d_contracts``: 30-day average daily volume (contracts).
      :class:`Decimal`; MUST be > 0 (zero ADV means "untraded market"
      and the fill should never have happened).
    - ``expected_price``: LEAN-emitted ``signals.expected_fill_price``
      (or ``decision_price`` in bootstrap when slippage model was
      disabled). MUST be > 0 — the spec's slippage formula divides by
      this value.
    - ``realized_price``: ``fills.fill_price``. MUST be > 0.
    - ``direction``: ``"buy"`` or ``"sell"``. Drives the sign convention
      for ``slippage_bps`` (see module docstring).
    - ``fill_at_utc``: ``fills.fill_time_utc``. MUST be tz-aware.
      Used to compute ``data_window_start`` / ``data_window_end``.
    """

    market: str
    order_size_contracts: Decimal
    adv_30d_contracts: Decimal
    expected_price: Decimal
    realized_price: Decimal
    direction: Literal["buy", "sell"]
    fill_at_utc: datetime


# ---------------------------------------------------------------------------
# Outputs (caller flushes these as data)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MarketCoefficient:
    """One row of the per-market coefficients dict (backend-spec §3.12).

    Mirrors the JSONB shape:

        ``{"alpha": "0.5", "beta": "12.3", "n_obs": 84, "r_squared": "0.61"}``

    Decimal serialized as string in audit payload (A05); the live JSONB
    serialization at INSERT time is a dispatcher concern (asyncpg's
    ``jsonb`` codec handles the cast). ``r_squared`` is ``None`` under
    two branches: zero-prior fit (no regressor) and zero-variance OLS
    (Sxx == 0 OR Syy == 0; see module docstring).
    """

    alpha: Decimal
    beta: Decimal
    n_obs: int
    r_squared: Decimal | None


@dataclass(frozen=True, slots=True)
class PendingAuditEvent:
    """An audit event the caller writes via ``append_audit_event()``.

    Same shape as ``services.risk.sizing.PendingAuditEvent``,
    ``services.reconciliation.recon.PendingAuditEvent``, etc. — single
    flush helper across modules. The writer's ``_normalize_event_type``
    accepts both string and ``AuditEventType`` enum forms, so the
    Literal-string shape here doesn't need a retrofit when the writer's
    enum becomes the preferred form.
    """

    event_type: CalibrationAuditEventType
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CalibrationPlan:
    """The full result of a slippage-calibration fit.

    Caller (Week 4 dispatcher), per the backend-spec §2.14 line 2550-2553
    sequence:

    1. ``append_audit_event(...)`` for ``audit_event``; capture the
       returned ``audit_event_uuid``.
    2. INSERT one row into ``slippage_calibration_versions`` with the
       captured uuid + the planner-supplied ``new_version_id``,
       ``version_no``, ``trigger.value``, ``per_market_coefficients``
       (serialize Decimal-as-str to JSONB), ``data_window_*``,
       ``calibrated_at_utc``.
    3. If ``becomes_head``: UPDATE ``slippage_calibration_versions
       SET is_head = FALSE WHERE is_head`` then UPDATE ... SET
       is_head = TRUE WHERE id = :new_version_id`` inside ONE
       transaction. The ``slippage_head`` partial unique index in
       ``alembic/versions/0003_risk_tables.py:113`` enforces the
       single-HEAD invariant so a partial swap can't leave two HEADs.

    The order matters — the audit row pins the ``audit_event_uuid``
    that the slippage_calibration_versions row references (§3.12 NOT
    NULL column), so the audit write MUST precede the version INSERT.
    The HEAD-pointer swap is a separate concern (transactional with
    the INSERT, not with the audit write — the audit chain stays
    immutable across the rare edge case where the swap fails post-INSERT).
    """

    new_version_id: UUID
    version_no: int
    fit_method: CalibrationFitMethod
    trigger: CalibrationTrigger
    per_market_coefficients: Mapping[str, MarketCoefficient]
    data_window_start_utc: datetime | None
    data_window_end_utc: datetime | None
    fill_count: int
    parent_version_id: UUID | None
    becomes_head: bool
    audit_event: PendingAuditEvent


class CalibrationError(ValueError):
    """Raised on caller bugs (naive datetime, non-positive price/ADV, float input).

    ``ValueError``-subclass so FastAPI handlers can trap with one branch
    and return the canonical 400 envelope (dev-guide §3.6).
    """


# ---------------------------------------------------------------------------
# Plan: calibration fit
# ---------------------------------------------------------------------------


def plan_calibration_fit(
    *,
    new_version_id: UUID,
    trigger: CalibrationTrigger,
    calibrated_at_utc: datetime,
    fills: Iterable[FillObservation] = (),
    parent_version_id: UUID | None = None,
    parent_version_no: int | None = None,
    becomes_head: bool = True,
    notes: str | None = None,
) -> CalibrationPlan:
    """Compute the calibration plan for a (trigger, fills) pair.

    Pure function — no I/O. Caller writes the audit event + the
    ``slippage_calibration_versions`` row + (conditionally) the
    HEAD-pointer swap per the contract on :class:`CalibrationPlan`.

    Parameters
    ----------
    new_version_id:
        Caller-generated UUIDv7 for the new
        ``slippage_calibration_versions`` row. The planner does NOT
        invoke ``uuid_v7()`` itself — keeping ID generation in the
        caller boundary is part of the plan-then-apply contract (the
        ID flows into the audit event payload AND the row INSERT, so
        both must agree).
    trigger:
        Why this calibration was invoked. ``CalibrationTrigger.BOOTSTRAP``
        forces the zero-prior branch unconditionally; the three
        scheduled/manual triggers OLS-fit when fills are sufficient
        and zero-prior-fall-back otherwise.
    calibrated_at_utc:
        Wall-clock for the calibration run — flows into the row's
        ``calibrated_at_utc`` and the audit event's ``ts``. MUST be
        timezone-aware (anti-pattern A06).
    fills:
        Iterable of :class:`FillObservation`. Default empty for the
        bootstrap path. The planner consumes the iterable once into a
        tuple internally so callers can pass a generator without
        re-iteration concerns.
    parent_version_id:
        ``slippage_calibration_versions.id`` of the previously-active
        HEAD, or ``None`` if this is the first calibration ever (typical
        for the bootstrap path). Flows into the audit event payload for
        chain-of-custody.
    parent_version_no:
        ``slippage_calibration_versions.version_no`` of the prior HEAD.
        The plan's ``version_no`` returns ``parent_version_no + 1`` or
        ``1`` when ``None``. The planner does NOT consult the DB for
        the next free version_no — caller owns that read and passes it
        in.
    becomes_head:
        Whether the dispatcher should swap the HEAD pointer to the new
        version after INSERT. Default True (operational norm). Reserved
        ``False`` for a "compute-but-don't-promote" research path that
        Phase 1+ may add — the audit row + version row still get
        written, but ``is_head`` stays ``FALSE``.
    notes:
        Operational free-form text included in the audit-event payload
        (and persisted to the row's ``notes`` column at the dispatcher
        boundary).

    Raises
    ------
    CalibrationError
        If ``calibrated_at_utc`` is naive (no tzinfo).
        If any ``FillObservation.fill_at_utc`` is naive.
        If any ``FillObservation`` has non-positive ADV / size /
        expected_price / realized_price.
        If any ``FillObservation`` field is float (A05; we check
        Decimal-typing on the four numeric columns).
        If ``market`` is empty / not a string.

    Returns
    -------
    CalibrationPlan
        See class docstring for the caller contract.
    """
    if calibrated_at_utc.tzinfo is None:
        raise CalibrationError("calibrated_at_utc must be timezone-aware (anti-pattern A06)")

    fill_tuple: tuple[FillObservation, ...] = tuple(fills)
    for fill in fill_tuple:
        _validate_fill(fill)

    version_no: int = 1 if parent_version_no is None else parent_version_no + 1

    if trigger == CalibrationTrigger.BOOTSTRAP or not fill_tuple:
        per_market = _bootstrap_coefficients(fill_tuple)
        fit_method = CalibrationFitMethod.ZERO_PRIOR
    else:
        per_market = _ols_per_market(fill_tuple)
        # Plan-level OLS label = at least one market crossed the
        # MIN_OBSERVATIONS_FOR_OLS threshold and ran through the estimator
        # code path (even when Sxx==0 collapsed it to a constant intercept,
        # the estimator still ran — that's still OLS for label purposes).
        # Markets that fell back to zero-prior have n_obs < threshold AND
        # alpha=beta=0 by construction; the n_obs gate is unambiguous.
        fit_method = (
            CalibrationFitMethod.OLS
            if any(c.n_obs >= MIN_OBSERVATIONS_FOR_OLS for c in per_market.values())
            else CalibrationFitMethod.ZERO_PRIOR
        )

    if fill_tuple:
        timestamps = sorted(f.fill_at_utc for f in fill_tuple)
        data_window_start: datetime | None = timestamps[0]
        data_window_end: datetime | None = timestamps[-1]
    else:
        data_window_start = None
        data_window_end = None

    audit_event = _build_audit_event(
        new_version_id=new_version_id,
        version_no=version_no,
        trigger=trigger,
        fit_method=fit_method,
        per_market=per_market,
        data_window_start=data_window_start,
        data_window_end=data_window_end,
        fill_count=len(fill_tuple),
        parent_version_id=parent_version_id,
        becomes_head=becomes_head,
        calibrated_at_utc=calibrated_at_utc,
        notes=notes,
    )

    return CalibrationPlan(
        new_version_id=new_version_id,
        version_no=version_no,
        fit_method=fit_method,
        trigger=trigger,
        per_market_coefficients=per_market,
        data_window_start_utc=data_window_start,
        data_window_end_utc=data_window_end,
        fill_count=len(fill_tuple),
        parent_version_id=parent_version_id,
        becomes_head=becomes_head,
        audit_event=audit_event,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_fill(fill: FillObservation) -> None:
    """Caller-input sanity checks. Raises :class:`CalibrationError` on bug.

    Decimal-typing of the four numeric columns is part of the contract —
    a caller that accidentally passes ``float`` (anti-pattern A05) gets a
    crisp error here rather than mysterious downstream behavior. We
    intentionally do NOT auto-convert; the caller is responsible for
    constructing the dataclass with Decimal values.
    """
    if not isinstance(fill.market, str) or not fill.market:
        raise CalibrationError(f"market must be a non-empty string; got {fill.market!r}")
    for name in ("order_size_contracts", "adv_30d_contracts", "expected_price", "realized_price"):
        value = getattr(fill, name)
        if not isinstance(value, Decimal):
            raise CalibrationError(
                f"{name} must be Decimal (anti-pattern A05); got {type(value).__name__}"
            )
        if value <= Decimal("0"):
            raise CalibrationError(f"{name} must be > 0; got {value} for market={fill.market}")
    if fill.fill_at_utc.tzinfo is None:
        raise CalibrationError(
            f"fill_at_utc must be timezone-aware (anti-pattern A06); "
            f"got naive datetime for market={fill.market}"
        )
    if fill.direction not in ("buy", "sell"):
        raise CalibrationError(
            f"direction must be 'buy' or 'sell'; got {fill.direction!r} for market={fill.market}"
        )


def _signed_slippage_bps(fill: FillObservation) -> Decimal:
    """Bps slippage signed so positive = trader paid worse than expected.

    See module docstring "Slippage sign convention" block.
    """
    raw = (fill.realized_price - fill.expected_price) / fill.expected_price * BPS_PER_UNIT
    if fill.direction == "buy":
        return raw
    return -raw


def _size_to_adv_ratio(fill: FillObservation) -> Decimal:
    """The OLS regressor x_i = order_size / ADV_30d. ADV > 0 already validated."""
    return fill.order_size_contracts / fill.adv_30d_contracts


def _bootstrap_coefficients(
    fills: tuple[FillObservation, ...],
) -> Mapping[str, MarketCoefficient]:
    """Zero-prior coefficients per market.

    For BOOTSTRAP trigger, every market that appears in ``fills`` (or no
    markets at all if ``fills`` is empty) gets alpha=0, beta=0,
    r_squared=None. Markets are sorted alphabetically in the resulting
    dict for deterministic audit-event payloads (the JCS canonicalizer
    downstream sorts keys anyway, but determinism at the source layer
    keeps test fixtures stable across plan_calibration_fit invocations).
    """
    markets = sorted({f.market for f in fills})
    return {
        market: MarketCoefficient(
            alpha=BOOTSTRAP_ALPHA,
            beta=BOOTSTRAP_BETA,
            n_obs=sum(1 for f in fills if f.market == market),
            r_squared=None,
        )
        for market in markets
    }


def _ols_per_market(
    fills: tuple[FillObservation, ...],
) -> Mapping[str, MarketCoefficient]:
    """Per-market OLS fit; markets below :data:`MIN_OBSERVATIONS_FOR_OLS` fall back to zero-prior.

    Markets are sorted alphabetically for determinism (see
    :func:`_bootstrap_coefficients`).
    """
    by_market: dict[str, list[FillObservation]] = {}
    for f in fills:
        by_market.setdefault(f.market, []).append(f)

    return {
        market: _fit_one_market(tuple(by_market[market])) for market in sorted(by_market.keys())
    }


def _fit_one_market(fills: tuple[FillObservation, ...]) -> MarketCoefficient:
    """OLS for one market; falls back to zero-prior when n < threshold or Sxx == 0.

    See module docstring "OLS within-market math" block.
    """
    n = len(fills)
    if n < MIN_OBSERVATIONS_FOR_OLS:
        return MarketCoefficient(
            alpha=BOOTSTRAP_ALPHA,
            beta=BOOTSTRAP_BETA,
            n_obs=n,
            r_squared=None,
        )

    xs = [_size_to_adv_ratio(f) for f in fills]
    ys = [_signed_slippage_bps(f) for f in fills]
    n_dec = Decimal(n)
    x_bar = sum(xs, Decimal("0")) / n_dec
    y_bar = sum(ys, Decimal("0")) / n_dec

    sxx = sum(((x - x_bar) * (x - x_bar) for x in xs), Decimal("0"))
    sxy = sum(((x - x_bar) * (y - y_bar) for x, y in zip(xs, ys, strict=True)), Decimal("0"))
    syy = sum(((y - y_bar) * (y - y_bar) for y in ys), Decimal("0"))

    if sxx == Decimal("0"):
        # No variance in the regressor -> beta collapses to 0; intercept = ybar.
        # r_squared is None because the regressor explains nothing by construction.
        return MarketCoefficient(
            alpha=y_bar,
            beta=Decimal("0"),
            n_obs=n,
            r_squared=None,
        )

    beta = sxy / sxx
    alpha = y_bar - beta * x_bar

    if syy == Decimal("0"):
        # Zero-variance response -> r_squared is undefined (beta explains 0 of 0).
        return MarketCoefficient(
            alpha=alpha,
            beta=beta,
            n_obs=n,
            r_squared=None,
        )

    ss_res = sum(
        ((y - (alpha + beta * x)) ** Decimal("2") for x, y in zip(xs, ys, strict=True)),
        Decimal("0"),
    )
    r_squared = Decimal("1") - ss_res / syy

    return MarketCoefficient(
        alpha=alpha,
        beta=beta,
        n_obs=n,
        r_squared=r_squared,
    )


def _build_audit_event(
    *,
    new_version_id: UUID,
    version_no: int,
    trigger: CalibrationTrigger,
    fit_method: CalibrationFitMethod,
    per_market: Mapping[str, MarketCoefficient],
    data_window_start: datetime | None,
    data_window_end: datetime | None,
    fill_count: int,
    parent_version_id: UUID | None,
    becomes_head: bool,
    calibrated_at_utc: datetime,
    notes: str | None,
) -> PendingAuditEvent:
    """Build the single ``slippage_calibration_recalibrated`` audit event.

    Decimal values serialize as strings (A05). Datetimes serialize as
    ISO 8601 with tz suffix. ``per_market_coefficients`` mirrors the
    §3.12 JSONB shape so the audit payload + the row's JSONB column
    stay byte-identical (which makes audit-to-row reconstruction trivial
    in the chain verifier).
    """
    return PendingAuditEvent(
        event_type="slippage_calibration_recalibrated",
        payload={
            "new_version_id": str(new_version_id),
            "version_no": version_no,
            "trigger": trigger.value,
            "fit_method": fit_method.value,
            "per_market_coefficients": _coefficients_to_payload(per_market),
            "data_window_start_utc": (
                data_window_start.isoformat() if data_window_start is not None else None
            ),
            "data_window_end_utc": (
                data_window_end.isoformat() if data_window_end is not None else None
            ),
            "fill_count": fill_count,
            "parent_version_id": (str(parent_version_id) if parent_version_id else None),
            "becomes_head": becomes_head,
            "notes": notes,
            "ts": calibrated_at_utc.isoformat(),
        },
    )


def _coefficients_to_payload(
    per_market: Mapping[str, MarketCoefficient],
) -> dict[str, dict[str, Any]]:
    """Convert MarketCoefficient values to the JSONB-shaped payload dict.

    Keys are sorted alphabetically for determinism (matches the
    bootstrap + ols path's market sort). Decimal → str per A05;
    r_squared None → JSON null.
    """
    return {
        market: {
            "alpha": str(coef.alpha),
            "beta": str(coef.beta),
            "n_obs": coef.n_obs,
            "r_squared": (str(coef.r_squared) if coef.r_squared is not None else None),
        }
        for market, coef in sorted(per_market.items())
    }


__all__ = [
    "BOOTSTRAP_ALPHA",
    "BOOTSTRAP_BETA",
    "BPS_PER_UNIT",
    "MIN_OBSERVATIONS_FOR_OLS",
    "CalibrationAuditEventType",
    "CalibrationError",
    "CalibrationFitMethod",
    "CalibrationPlan",
    "CalibrationTrigger",
    "FillObservation",
    "MarketCoefficient",
    "PendingAuditEvent",
    "plan_calibration_fit",
]
