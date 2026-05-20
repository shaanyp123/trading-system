# Live-Money Cutover Plan

| Field | Value |
|---|---|
| **Status** | v1.0 — DRAFT (decisions ratified; cutover not scheduled) |
| **Date created** | 2026-05-20 (Operational Day 1 of paper) |
| **Decisions ratified by** | operator + Claude Code brainstorm session 2026-05-20 |
| **Cutover target date** | Operational Day 30 of paper (≈ 2026-06-30) at earliest |
| **Implementation** | This doc is the spec. Code work is enumerated in Appendix A as discrete follow-up PRs. |
| **Reversibility** | Live cutover is partially reversible (can flip back to paper-only), but $ losses + IBKR session damage incurred while live are NOT recoverable. Treat as semi-permanent. |

> **CLAUDE-CODE GUIDANCE.** Whenever this doc disagrees with `Docs/claude-dev-guide.md` §1.5 LOCKED or `Docs/backend-spec.md`, the LOCKED docs win — flag the conflict before acting. This doc layers cutover-specific decisions on top of the standing system spec; it does NOT override it.

---

## Table of Contents

1. [LOCKED Constants — Top-of-Doc Reference](#1-locked-constants)
2. [Starting Capital Tier](#2-starting-capital-tier)
3. [Risk Envelope (Live vs Paper)](#3-risk-envelope-live-vs-paper)
4. [Kill-Switch Threshold Tightening](#4-kill-switch-threshold-tightening)
5. [Operator Readiness Checklist](#5-operator-readiness-checklist)
6. [Failure-Mode Tolerance Matrix](#6-failure-mode-tolerance-matrix)
7. [Capital Event Handling](#7-capital-event-handling)
8. [Live-Only Sops Field Audit](#8-live-only-sops-field-audit)
9. [Live-vs-Paper Config Matrix](#9-live-vs-paper-config-matrix)
10. [Cutover Ceremony — Step-by-Step Checklist](#10-cutover-ceremony)
11. [Rollback / Panic Plan](#11-rollback--panic-plan)
12. [Appendix A — Pre-Cutover Build PRs](#appendix-a)
13. [Appendix B — Open Decisions + Escalation List](#appendix-b)

---

## 1. LOCKED Constants

Settled this session. Cutover happens against these values. Deviating from any one requires a doc revision + a fresh operator sign-off.

| # | Constant | Value | Source / Rationale |
|---|---|---|---|
| L1 | Live IBKR account | `U25655583` | Operator memory `project_trading_identifiers.md`; account opened 2026-05-05 |
| L2 | Starting deposit | **$25,000 USD** | §2; mid-tier so full futures universe activates at Stage 0 |
| L3 | Sizing tier (Stage 0) | **$25k tier** | `services/risk/sizing.py V1_LOT_CAPITAL_THRESHOLDS`; 50%-rule allows /MES /MNQ /MGC /MCL /MBT /M2K /MYM + 4 bond ETFs |
| L4 | Vol target at cutover | **15% annual** (= paper default) | §3; revisit at Operational Day 30 with paper-vs-live realized-vol data |
| L5 | Risk envelope deltas vs paper | **None** (same parameter set) | §3 |
| L6 | Kill-switch tolerance deltas vs paper | **None** (same thresholds + severity matrix) | §4; first 30 days earns the trust to tighten later |
| L7 | Readiness gate | **30 paper sessions + 10 round-trips + 0 unresolved P0/P1/breaks** | §5 |
| L8 | VPS topology | **Same VPS** (Hetzner Ashburn) **+ parallel compose stacks via env-file override** | §9 / §10; CCX13 → CCX23 upgrade probably required (8GB → 16GB) |
| L9 | Live env tag (`audit_log.env`) | **`live-small`** | `backend-spec` §3.2 enum; `live-scale` reserved for future capital-tier graduation |
| L10 | IB Gateway port (live) | **4003** (gnzsnz socat for live) | `services/api/config.py ibkr_port` — 4004 paper, 4003 live |
| L11 | Discord channels | **Shared with paper**; each embed tagged `env=PAPER` or `env=LIVE` | §9 |
| L12 | First-live-signal ceremony | **None — treat identically to paper from signal 1** | §5; operator trusts 30-day paper soak as adequate validation |
| L13 | Capital events wired Day 1 | **All 4** — deposits + withdrawals + dividends + interest accrual | §7 |
| L14 | Panic-flatten path Day 1 | **Web `/system` kill-switch INVOKE + manual TWS flatten** | §11; `/flatten-all` Discord command lands as fast follow (NOT gating) |
| L15 | Pre-cutover defect fix list | **`realized_pnl_usd` multiplier-correctness** (Drill 9/10 follow-up #1) | Appendix A row 1; gates cutover |
| L16 | Vol-target Day-30 revisit | **Calendar trigger only** (no automated tightening) | §3 |

### Not yet locked

| # | Open question | Default if unlocked | Section |
|---|---|---|---|
| O1 | Apex routing post-cutover (paper @ apex + live @ subdomain, or vice versa) | Live takes `live.spratcapital.com`; paper stays at apex | §10 step 4 |
| O2 | CME real-time data subscription (~$1.50/mo) | Enable at cutover (improves stop-fill timing — see Drill 8 lesson #1) | Appendix B |
| O3 | Live FlexQuery template (separate from paper's) | Operator creates pre-cutover in IBKR portal | §8 / §10 |
| O4 | Live WebAuthn re-enrollment vs share-with-paper | Re-enroll (rp_id distinct from paper) | §10 step 7 |

---

## 2. Starting Capital Tier

**Locked: $25,000 USD deposit; runs against the $25k Stage 0 sizing tier.**

### Why $25k

- Mid-tier on the 4-tier sizing schedule (`$15k / $25k / $50k / $100k`). At $25k the Stage 0 single-contract-notional-≤-50%-equity rule admits the full Phase 1 futures universe at conservative notional ratios:

  | Market | Single-contract notional | $25k ceiling (50%) | Activates? |
  |---|---|---|---|
  | `/MES` | ~$26k @ 7,300 | $12,500 | NO (just over; pads via vol-target downsize) |
  | `/MNQ` | ~$36k @ 18,000 | $12,500 | NO at base; activates at $50k tier |
  | `/MYM` | ~$20k @ 40,000 | $12,500 | NO at base; activates at $50k tier |
  | `/M2K` | ~$10k @ 2,000 | $12,500 | **YES** |
  | `/MGC` | ~$24k @ $2,400 | $12,500 | NO at base; activates at $50k tier |
  | `/MCL` | ~$8k @ $80 | $12,500 | **YES** |
  | `/MBT` | ~$10k @ $100k | $12,500 | **YES** |
  | `TLT` | (cash equity) | (no contract floor) | **YES** |
  | `IEF` | (cash equity) | (no contract floor) | **YES** |
  | `SHY` | (cash equity) | (no contract floor) | **YES** |
  | `TIP` | (cash equity) | (no contract floor) | **YES** |

  Active candidate universe at $25k base: 7 of 11 markets (3 micro futures + 4 bond ETFs). `/MES` borderline.

  **Note for operator:** the Stage 0 single-contract rule is a HARD floor, not a vol-target output. Even on a $25k account the Stage 1+ inverse-vol scaling will likely use only `/MCL / /MBT / /M2K` + ETFs in production. `/MES / /MNQ / /MGC` enter the active set at the $50k tier.

- Capital-at-risk on cutover day is bounded by the readiness gate's drawdown ceiling (see §5).

- Operator-side liquidity: $25k is movable to IBKR in a single ACH transfer; doesn't require wiring infrastructure.

### Operator-facing rule of thumb

> "On a typical session at 15% vol target, expect ±$236 daily P&L swings on a $25k account. Worst day in a calendar month: ~$500. Worst calendar month: $1,500 – $2,500. Worst 30-day drawdown the strategy has seen in backtest: $3,000 – $4,500 (12-18% of capital)."

### Capital tier graduation path (Phase 1+)

Once Operational Day 90 closes with live track record CLEAN:
- $25k → $50k upgrade unlocks /MES/MNQ/MGC at base; operator adds $25k deposit, capital_events row INSERTs, 5-session ramp at 0.5× vol multiplier per spec §2.4.4
- $50k → $100k unlocks the next tier; same ceremony
- `live-small` → `live-scale` env-tag promotion happens at the $100k threshold (audit-log env enum change; mostly cosmetic — no logic differs)

---

## 3. Risk Envelope (Live vs Paper)

**Locked: zero deltas. Live runs the same `parameter_sets` row paper has been validating.**

### Vol target

- Live: `VOL_TARGET_PCT_ANNUAL = 0.15` (15% annual; same as paper)
- Rationale: tightening before live data exists would invalidate the paper-vs-live P&L comparison (operator can't tell if early underperformance is the lower target or a real edge regression). The `m_combined` composition rule in `services/risk/state_machine.py` already auto-halves during CONVALESCENT + capital-event mode + monthly DD breach, so defensive layering is in place by construction.
- **Day-30 revisit (locked):** at Operational Day 30 of live, operator runs a paper-vs-live realized-vol analysis (compute trailing 30-session realized portfolio vol; compare to 15% target). If realized > 1.2× target, recommend a tighten via PR or agent action. If realized within ±0.2×, hold. Calendar reminder only — no automated tightening.

### Cluster caps, correlation thresholds, per-trade size

- Same as paper (`backend-spec` §2.4.2 spec defaults, currently sourced via `_DEFAULT_RISK_ENVELOPE` in `services/api/routes/system.py`).
- Per-trade max: 25% capital ceiling at hard floor 50% — unchanged.

### Max drawdown ceiling (annual or monthly)

- Spec has `monthly_dd_breached_for_calendar_month` field on `risk_state` table; triggers `m_monthly_dd = 0.5` multiplier when -10% breached in calendar month.
- For live: keep the -10% breach at 0.5× multiplier. Add an OPERATOR-LEVEL "wake-up call" threshold at -15% drawdown in any 30-day window → P0 alert + recommended manual review. NOT auto-halt (don't want to be locked out at a possible cycle bottom). Codified as a follow-up PR (Appendix A row 11; non-gating).

### Annual loss ceiling — open decision

- Spec is silent on an annual ceiling. Operator should consider a soft "if I'm down $X (probably -15% of starting capital = -$3,750 on $25k) for the calendar year, I review whether the strategy is broken vs in a normal drawdown."
- Doc recommendation: **soft -$3,750 (= -15% of starting capital) annual review trigger**. Not a halt; a P0 alert + an `incident_reviews` row to write up. Codified as part of Appendix A row 11.

---

## 4. Kill-Switch Threshold Tightening

**Locked: zero env-tier differentiation. Live runs paper's existing trigger × severity matrix.**

### Why no env-specific tightening

- Drill 5 (2026-05-18) proved IBKR Error 1100 is recoverable on the existing P1-Discord-alert path; auto-halting on 1100 in live would have hit at least once during paper soak and erodes operator trust.
- Recon tolerance is $5 + 1bp + T+1 grace per paper spec; tightening on live trades risk-of-missed-breaks for benefit-of-fewer-false-alerts. The spec's tolerances already catch the breaks that matter at $25k scale.
- The system is end-to-end LIVE-validated for paper (Drill 10 AC1-AC10 GREEN); the constraints that worked on paper carry over to live by construction.

### Phase 1+ tightening triggers (future deltas)

After Operational Day 30 of live closes CLEAN:
- If 30-day-rolling reconciliation_breaks count = 0: tighten to $3 + 0.7bp tolerance
- If 30-day-rolling IBKR Error 1100 count > 3: tighten 1100 to HALT_NEW after 10-min sustained error
- If `bar_sync_market_failed` count > 0 in 30 days: tighten to HALT_NEW on any data-fetch failure

These are post-cutover policy adjustments, NOT pre-cutover code work. Tracked as Phase 1+ followups.

### Specific failure-mode response (see §6 matrix)

The §6 matrix enumerates ~15 failure modes × paper response × live response. **Every row shows paper-response == live-response** for cutover Day 1. Document for completeness + future tightening.

---

## 5. Operator Readiness Checklist

**Locked: 30 paper sessions + 10 round-trips + 0 unresolved P0/P1/recon-breaks.**

### Gates (all must pass before scheduling cutover)

| # | Gate | Measurement | Hard / Soft |
|---|---|---|---|
| G1 | 30 calendar paper sessions since Operational Day 1 (= 2026-05-20) | LEAN's daily `lean_cycle_heartbeat` count `>= 30` (defensively: count distinct `session_date`s in audit_log) | HARD |
| G2 | 10 completed signal→fill round-trips on the paper account | `SELECT COUNT(*) FROM trades WHERE state='closed' AND env='paper' AND created_at > '2026-05-20'` ≥ 10 | HARD |
| G3 | Zero unresolved reconciliation_breaks at gate time | `SELECT COUNT(*) FROM reconciliation_breaks WHERE resolved_at_utc IS NULL` = 0 | HARD |
| G4 | Zero unresolved P0/P1 alerts at gate time | `SELECT COUNT(*) FROM alerts WHERE severity IN ('P0','P1') AND status='active'` = 0 | HARD |
| G5 | Audit chain CLEAN at gate time | `verify_chain --env paper` exit 0 with row count matching `SELECT COUNT(*) FROM audit_log WHERE env='paper'` | HARD |
| G6 | Operator can describe (verbally or in writing) every signal type V1 has emitted, how it was sized, why approved/rejected | Operator self-attests in `decision_diary` entry tagged `cutover_readiness` | SOFT |
| G7 | Operator has read (or re-read) `Docs/backend-spec.md` §2.4 (Risk Engine) + this doc end-to-end | Operator self-attests | SOFT |
| G8 | All Appendix A "blocks cutover" PRs merged + deployed + smoke-tested on paper | PR list reviewed; each labeled `cutover-blocker` carries a smoke-test screenshot/log | HARD |
| G9 | Live FlexQuery template created in IBKR portal + token validated | Operator runs the `flex_query_fetcher.py` against the live template returning a parseable XML response | HARD |
| G10 | Operator's IBKR live account funded with $25,000 USD and visible in TWS Desktop | Operator-verified balance | HARD |
| G11 | Operator confirmed dual-account session model works (paper + live gateways simultaneously from same Hetzner IP) | Pre-cutover validation per Appendix A row 10 | HARD |

### First-live-signal ceremony

**Locked: none.** Live runs identically to paper from signal 1. Operator approves via the existing `/signals` page or Discord `/approve`. No mandatory decision-diary entries beyond the normal reject/defer flow.

### Rationale

The readiness gates are designed so that on cutover day, paper has been observed long enough to surface all rarely-fired risk paths (recon breaks, kill-switch transitions, capital events, audit-chain serialization retries, etc.). At Operational Day 30 with 10 round-trips, the system has touched every major code path. The soft gates (G6-G7) are explicit invitations to NOT cutover if the operator is hesitating for a reason they haven't articulated.

If a hard gate is failing at the operator's target date, delay cutover. There is no urgency.

---

## 6. Failure-Mode Tolerance Matrix

Each row = a known failure mode + the response severity in paper vs live. Currently every row is **paper-response == live-response** per §4 locked policy. The "live response" column exists so this doc evolves; Phase 1+ tightenings update the live column only.

| # | Failure mode | Paper response | Live response (Day 1) | Notes |
|---|---|---|---|---|
| F1 | IBKR connectivity (Error 1100) sustained | Log WARNING + monitor emits Discord P1 | **Same** | Drill 5 recovery validated; auto-halt deferred to Phase 1+ Day 30 retighten |
| F2 | `bar_sync_market_failed` for 1 market on one cycle | Structlog WARNING; LEAN may skip that market for cycle | **Same** | First failure on live is expected eventually; operator monitors |
| F3 | `bar_sync_market_failed` for ALL markets one cycle | LEAN's `v1_universe_data_missing` log + signal cycle aborted | **Same** | Operator P1 alert via Discord (manual escalation) |
| F4 | Reconciliation break detected | $5 + 1bp + T+1 grace per `services/reconciliation/recon.py` | **Same** | Live tightens at Operational Day 30 if recon-clean |
| F5 | Reconciliation break unresolved past T+2 | HALT_NEW_routine + Discord #alerts P1 | **Same** | |
| F6 | Audit-chain hash break | HALT_NEW_incident + snapshot + Discord #critical P0 + Resend email | **Same** | Should be structurally impossible per Drill 10 validation; if it fires, incident review required |
| F7 | Single fill with slippage > 10bp from decision_price | Logged in `slippage_calibration_versions`; no halt | **Same** | Monthly recalibration self-tunes |
| F8 | Signal storm (>10 emit events per session) | HALT_NEW_routine per spec §2.4.3 | **Same** | Never observed on paper; pre-existing safeguard |
| F9 | Trailing drawdown breach (-10% from peak) | `m_monthly_dd = 0.5` multiplier; no halt | **Same** | At $25k = $2,500 from peak triggers vol-halving |
| F10 | Order rejected by IBKR (Error 201, 110, etc.) | Order rejection audit row; Discord #fills `order_rejected` embed; no halt | **Same** | PR-η bracket protocol makes Error 201 structurally impossible; other rejections operator-handled |
| F11 | LEAN container crash | Docker restart-policy reloads; operator notified via discord_bot `/status` divergence | **Same** | Recovery validated post-Option-C ceremony 2026-05-20 |
| F12 | api container crash | Docker restart-policy reloads; in-flight signals re-processed via SELECT FOR UPDATE SKIP LOCKED | **Same** | Drill 5 + Option-C recovery validated |
| F13 | Postgres outage | api fails closed (returns 503); Discord webhooks dropped; watchdog escalates | **Same** | No multi-AZ replication in Phase 1; operator restores from backups if needed |
| F14 | Operator unavailable for >1 day (no approve actions) | Pending signals queue; vacation_mode NOT auto-engaged | **Same** | Operator pre-engages vacation_mode for planned PTO; otherwise pending signals just sit (no auto-execute by design) |
| F15 | Hetzner VPS outage | Watchdog detects; SMS to operator (via Resend → email-to-SMS or direct); no auto-recovery | **Same** | Manual VPS recovery procedure documented (deploy/sops/README.md) |
| F16 | sops decryption fails on container restart (corrupted age key, etc.) | Container fail-closed at entrypoint; api won't boot | **Same** | Operator recovers from age key backup |
| F17 | Cosmic-ray-level: realized P&L diverges from broker's truth ($> $50 cumulative within session) | Recon catches at EOD; opens reconciliation_break row | **Same** | If pre-EOD detection desired Phase 1+: add intraday reconciliation check (60-min cadence) |

### Phase 1+ tightening review (post-Day 30 live)

Run a fresh failure-mode analysis at Operational Day 30 of live. For each row above with `live response == paper response`, evaluate whether 30 days of live data justifies tightening to a stricter live response. Update this matrix's live column.

---

## 7. Capital Event Handling

**Locked: all 4 capital events wired Day 1.**

### Day 1 wiring scope

| Event type | Trigger | Path | spec ref |
|---|---|---|---|
| **Deposit** | Operator runs `/capital-deposit <amount> <reason>` in Discord | Bot inserts `capital_events` row (event_type='deposit') + recomputes dd_baseline_reset_to + emits `capital_event_recorded` audit row + triggers `m_capital_event = 0.5` for sessions 1-5 | backend-spec §3.20 + §2.4.4 |
| **Withdrawal** | Operator runs `/capital-withdraw <amount> <reason>` in Discord | Bot inserts `capital_events` row (event_type='withdrawal') + drawdown_baseline UNCHANGED + audit row | backend-spec §3.20 |
| **Dividend** | EOD FlexQuery includes CashTransaction → reconciler INSERTs `dividend_history` row | Recon parses CashTransaction (Type=Dividend); INSERT into `dividend_history`; broaden FlexQuery template to include CashTransaction in operator's IBKR portal | backend-spec §3.24 |
| **Interest accrual + funding** | EOD FlexQuery includes CashTransaction → reconciler INSERTs (informational) row | Same parser as dividends; new row types: InterestAccrued, FundingCost | backend-spec §3.20 + §3.21 |

### Cutover-day procedure for the initial deposit

1. Operator wires $25,000 USD to IBKR live account (settle via ACH or Fed wire).
2. Wait for IBKR to credit balance in TWS Desktop (≈ 1-3 business days for ACH; same-day for wire).
3. Once funds visible, operator runs in Discord:
   ```
   /capital-deposit 25000 "initial live funding for live-small cutover"
   ```
4. Discord bot inserts `capital_events` row with `effective_at_utc = NOW()`, `pre_event_equity = 0`, `post_event_equity = 25000`, `pct_of_pre_equity = NULL (divide-by-zero handled)`, `threshold_met = TRUE` (definitionally on first deposit), `dd_baseline_reset_to = 25000`, `capital_event_mode_session_start = <current_session_no>`.
5. Audit row `capital_event_recorded` emitted with the canonical JSON payload.
6. `risk_state` row UPDATEd: `capital_event_active_until_session_no = current + 30`, `capital_event_vol_normalized_at_session_no = current + 5`.
7. Operator verifies via `/system` web page that the capital event registered.
8. First live signal cycle (≤ 24h later) runs at effective vol target `0.15 × 0.5 = 0.075` (7.5%) for next 5 sessions, then `0.15 × 1.0 = 0.15` for sessions 6-30, then normal.

### FlexQuery template extension (Operator action in IBKR portal pre-cutover)

The live FlexQuery template must include:
- Section: `Trades` (existing)
- Section: `Positions` (existing)
- Section: `CashTransaction` (NEW — for dividends + interest + funding cost rows)

Configure the existing paper FlexQuery as a reference; mirror the field selection on the live template. Operator-side action; no code change needed.

### Phase 1+ defer items

- Operator-side bookkeeping (1099-B, mark-to-market accounting) — operator handles with their accountant separately. Spec is silent on this.
- Quarterly tax estimate calculation — out of scope for this system.

---

## 8. Live-Only Sops Field Audit

Walks `secrets/live.enc.yaml`. Identifies every `<TODO_*>` placeholder + the field's expected value source.

### Fields the operator MUST populate before cutover

| # | sops key path | Source | Generated how |
|---|---|---|---|
| S1 | `postgres.app_service_password` | `openssl rand -hex 32` on live VPS | Pre-cutover (alembic 0006 grant migration) |
| S2 | `postgres.app_owner_password` | `openssl rand -hex 32` on live VPS | Pre-cutover |
| S3 | `ibkr.live_account` | `U25655583` (per operator memory) | Already known |
| S4 | `ibkr.live_username` | Operator's IBKR live login | Already known to operator |
| S5 | `ibkr.live_password` | Operator's IBKR live password | Already known to operator |
| S6 | `ibkr.flex_query_id` | Numeric ID emitted when operator creates the live FlexQuery template | IBKR portal — Reports → Flex Queries → Create |
| S7 | `ibkr.flex_query_token` | Auto-generated alongside flex_query_id | Same IBKR portal step |
| S8 | `discord.bot_token` | **SAME as paper** (Discord bot shared across env) | Already populated in paper.enc.yaml |
| S9 | `discord.guild_id` | **SAME as paper** | Already populated |
| S10 | `discord.api_bearer_token` | Distinct from paper (`secrets.token_urlsafe(32)`) | Generate fresh; cutover-day |
| S11 | `discord.webhook_urls.*` (all 7) | **SAME as paper** (channels shared per L11) | Already populated |
| S12 | `lean.api_bearer_token` | Distinct from paper (`secrets.token_urlsafe(32)`) | Generate fresh; cutover-day |
| S13 | `internal.watchdog_bearer_token` | Distinct from paper (`secrets.token_urlsafe(32)`) | Generate fresh |
| S14 | `internal.ipc_bearer_token` | Distinct from paper (`secrets.token_urlsafe(32)`) | Generate fresh |
| S15 | `webauthn.rp_id` | **TBD** — depends on O1 decision (apex vs subdomain for live) | Open-decision section |
| S16 | `webauthn.rp_name` | `trading-system` | Same as paper |
| S17 | `webauthn.origin` | `https://<rp_id>` (matches S15) | Per O1 |
| S18 | `resend.api_key` | **SAME as paper** (Resend account shared) | Already populated |
| S19 | `resend.from_address` | **SAME as paper** | Already populated |
| S20 | `resend.to_address` | **SAME as paper** (operator email) | Already populated |
| S21 | `totp.encryption_key` | Distinct from paper — generate fresh 32-byte AES-256-GCM key | `python3 -c "import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b'='))"` |
| S22 | `anthropic.api_key` | **SAME as paper** (workspace shared) | Already populated |
| S23 | `anthropic.workspace_id` | **SAME as paper** | Already populated |
| S24 | `s3.access_key_id` + `.secret_access_key` + `.bucket` | **SAME as paper** (backup S3 bucket shared) | Already populated |
| S25 | `github.app_id` + `.installation_id` + `.app_private_key` | **SAME as paper** (GitHub app shared) | Already populated |
| S26 | `trading_economics.api_token` | **SAME as paper** | Already populated |

### Fields the operator MUST REMOVE before cutover

These are present in paper.enc.yaml but were vendor-cancelled per the 2026-05-20 Operational Day 1 vendor audit. Live should NOT include them:

| sops key | Reason |
|---|---|
| `databento.api_key` | DataBento subscription cancelled 2026-05-20 |
| `tiingo.api_key` | Tiingo subscription cancelled 2026-05-20 |
| `quantconnect.api_token` + `.user_id` | QC Researcher subscription cancellation pending; the dormant qc_adapter_backfill profile retains compatibility but live shouldn't carry the secrets |

### Sops template patch needed (Appendix A row 12)

The current `deploy/sops/secret_schemas/live.template.yaml` is the Day 3 version. Two updates needed:
1. Add `discord.api_bearer_token` field (post-Day 23)
2. Add `totp.encryption_key` field (post-Day 21)
3. Add `resend.to_address` field (it's in paper but template still has comment about from_address only)
4. Remove `databento` + `tiingo` + `quantconnect` sections (vendor-cancelled)

Codified as Appendix A row 12 — pre-cutover sops template hygiene PR.

---

## 9. Live-vs-Paper Config Matrix

Every config field that differs between paper and live. Bottom-of-doc at-a-glance reference for cutover.

| Field / config path | Paper value | Live value |
|---|---|---|
| `deploy/.env` env_file (`ENV_FILE`) | `paper.enc.yaml` | `live.enc.yaml` |
| `deploy/.env` env tag (`ENVIRONMENT`) | `paper` | `live-small` |
| `deploy/.env` domain (`DOMAIN`) | `spratcapital.com` (current) or `paper.spratcapital.com` (post-O1) | `spratcapital.com` (post-O1) or `live.spratcapital.com` (recommended default per O1) |
| `docker-compose` project name | `trading` (default) or `trading-paper` | `trading-live` |
| Docker volume `lean_data` | `trading-paper_lean_data` (or unprefixed if paper retains default name) | `trading-live_lean_data` (isolated) |
| Docker volume `pgdata` | `trading-paper_pgdata` | `trading-live_pgdata` |
| `ib_gateway` port mapping | host port 4004 (paper socat) → container 4004 | host port 4003 (live socat) → container 4003 |
| `ib_gateway` `TRADING_MODE` env | `paper` | `live` |
| `services/api/config.py ibkr_port` | 4004 (default) | 4003 |
| `services/api/config.py ibkr_account` (env: `API_IBKR_ACCOUNT`) | `DUQ825170` | `U25655583` |
| `services/api/config.py ibkr_client_id` | 1 (default) | 1 (same — different gateway sidecar) |
| `services/api/config.py bar_sync_client_id` | 2 (default) | 2 (same — different gateway sidecar) |
| Caddy site block | `paper.spratcapital.com` (or apex per current) | `spratcapital.com` (or `live.spratcapital.com` per O1) |
| WebAuthn `rp_id` | `paper.spratcapital.com` (post-O1) or `spratcapital.com` (current) | `spratcapital.com` (post-O1) or `live.spratcapital.com` |
| WebAuthn credentials | Re-enrollment may be needed depending on O1 outcome | Operator enrolls fresh during cutover ceremony |
| sops `totp.encryption_key` | Paper value | DIFFERENT (generate fresh) |
| sops `internal.watchdog_bearer_token` | Paper value | DIFFERENT (generate fresh) |
| sops `internal.ipc_bearer_token` | Paper value | DIFFERENT (generate fresh) |
| sops `discord.api_bearer_token` | Paper value | DIFFERENT (generate fresh) |
| sops `lean.api_bearer_token` | Paper value | DIFFERENT (generate fresh) |
| sops `postgres.app_service_password` | Paper value | DIFFERENT (generate fresh) |
| sops `postgres.app_owner_password` | Paper value | DIFFERENT (generate fresh) |
| sops Discord webhook URLs | Same | **SAME** (channels shared per L11) |
| sops `resend.*` | Same | **SAME** (Resend account shared) |
| sops `anthropic.*` | Same | **SAME** (workspace shared) |
| sops `s3.*` | Same | **SAME** (backup bucket shared) |
| sops `github.*` | Same | **SAME** (GitHub app shared) |
| External watchdog target | `https://<paper-domain>/api/health` | `https://<live-domain>/api/health` (watchdog polls BOTH) |
| Audit-log env tag (`audit_log.env`) | `paper` | `live-small` |
| Discord embed env tag | `env=PAPER` | `env=LIVE` |
| Vol target (parameter set) | 15% (= same as live cutover) | 15% (Day 1) — Day 30 revisit (see §3) |
| Reconciliation tolerances | $5 + 1bp + T+1 grace | **Same** (per L6) |
| Kill-switch trigger matrix | Spec §2.4.3 defaults | **Same** (per L6) |
| Risk envelope (cluster caps, correlation thresholds, per-trade cap) | Spec §2.4 defaults | **Same** (per L5) |

### Hardware sizing

Current CCX13 (2 vCPU / 8 GB) runs paper at ~50% RAM utilization. Doubling for parallel live stack pushes RAM to ~90%+ which is over the safe threshold.

**Recommended:** upgrade to **CCX23 (4 vCPU / 16 GB; ~$26/mo)** pre-cutover. Cost delta: +$13/mo over current CCX13. Codified as cutover-ceremony step 0.

---

## 10. Cutover Ceremony

Executable step-by-step. Each step has a verifiable success criterion + a rollback if it fails.

### Pre-cutover (T-30 days through T-1 days)

| # | Step | Success criterion | Rollback |
|---|---|---|---|
| 0 | Upgrade VPS to CCX23 (4vCPU/16GB) | `ssh trading 'free -h'` shows 16GB available | None — Hetzner allows upgrading or downgrading at any time |
| 1 | Operator opens IBKR live account (already done; account = U25655583) | `U25655583` visible in IBKR login | None |
| 2 | Operator funds IBKR live account with $25,000 via ACH or wire | Balance visible in TWS Desktop | Recall transfer if not yet settled |
| 3 | Operator creates live FlexQuery template in IBKR portal | Template ID + token recorded | Delete template in IBKR portal |
| 4 | All Appendix A "cutover blocker" PRs merged + deployed + smoke-tested on paper | Each PR labeled `cutover-blocker` checked off | Re-open PR; do NOT cutover |
| 5 | All readiness gates G1-G11 (§5) GREEN | Operator runs the verification queries | Delay cutover to next eligible window |
| 6 | Operator pre-cutover decision: O1 apex routing locked | Operator inserts the chosen rp_id values into S15 + S17 sops fields | Re-decide O1; update doc + sops |

### Cutover day (T+0) — 4-hour window during a non-trading session (weekend recommended)

| # | Step | Success criterion | Rollback |
|---|---|---|---|
| 7 | sops bundle for live | Operator runs `sops secrets/live.enc.yaml` to populate S1-S26 per §8 audit | Discard sops edit; live.enc.yaml stays placeholder |
| 8 | git commit + push `secrets/live.enc.yaml` from operator workstation | gitleaks doesn't trip; CI green | `git revert HEAD` |
| 9 | SSH to VPS; `git pull --ff-only` | `git log -1` shows the cutover commit | None |
| 10 | Create `deploy/.env.live` from `deploy/.env.example` on VPS | File contains `ENVIRONMENT=live-small`, `ENV_FILE=live.enc.yaml`, distinct DOMAIN, distinct project name | Delete file |
| 11 | Caddy config: add live site block (paper.spratcapital.com + spratcapital.com OR live.spratcapital.com per O1) | `docker compose exec caddy caddy validate /etc/caddy/Caddyfile` exit 0 | Revert Caddyfile + `docker compose restart caddy` |
| 12 | Generate live IB Gateway container config | `docker-compose.live.yml` adds an ib_gateway service at TRADING_MODE=live | Delete the YAML overlay |
| 13 | Boot live stack: `docker compose -p trading-live --env-file deploy/.env.live up -d` | All 8 containers report healthy in 60s (`docker compose -p trading-live ps`) | `docker compose -p trading-live down -v` |
| 14 | Verify live api health: `curl -k https://<live-domain>/api/health` returns 200 | `{"status":"ok"}` JSON | Step 13 rollback |
| 15 | Verify dual-account session: paper api on clientId=1 + live api on clientId=1 (DIFFERENT gateway sidecars) both connected | Operator probes via IBKR Client Portal — both sessions visible + no Error 162 | Stop one stack; investigate which gateway sidecar wedged |
| 16 | Bootstrap live accounts row: operator runs the cutover script that INSERTs `accounts(external_account_id='U25655583', account_type='individual', base_currency='USD', role='owner', active_from=NOW())` | `SELECT * FROM accounts WHERE external_account_id='U25655583'` returns 1 row | Manual `DELETE FROM accounts WHERE external_account_id='U25655583'` |
| 17 | Bootstrap live `risk_state` row: state='NORMAL', severity=NULL, reason='live_cutover', is_current=TRUE | `SELECT * FROM risk_state WHERE account_id=<live_account_uuid> AND is_current=TRUE` returns NORMAL | DELETE the row; investigate |
| 18 | Bootstrap live `parameter_sets` head row: copy paper's current head into live's parameter_sets table | live's active parameter set matches paper's current | DELETE the row |
| 19 | Operator opens `/login` on live URL; WebAuthn ceremony enrolls a fresh credential | Web shows "logged in as operator"; `setup_tokens` row consumed | `DELETE FROM sessions; DELETE FROM webauthn_credentials WHERE account_id=<live_uuid>` |
| 20 | Operator runs `/capital-deposit 25000 "initial live funding"` in Discord | `capital_events` row INSERTed; `risk_state` updated with capital_event_mode_session_start | Discord retry on bot-error; manual SQL UPDATE if persistent |
| 21 | Operator verifies bar_sync_worker scheduled on live | api logs `bar_sync_worker_spawned` at boot | Inspect logs; restart api container |
| 22 | Wait until next 17:00 ET window; observe live `bar_sync_cycle_firing` → `bar_sync_cycle_completed failed_markets=[]` | structlog event sequence in api logs | If failure: replay bar_sync via operator probe script (clientId=80); confirm IBKR data quotes |
| 23 | Wait until 17:30 ET window; observe live LEAN `v1_signals_generated session_date=...` | LEAN structlog event in lean_local logs | LEAN signal cycle didn't fire — restart lean_local; check on-disk bars present |
| 24 | If first signal emitted: operator approves via `/signals` page | `signal_dispatched_to_broker` audit row; broker_order_id populated | Reject signal; investigate why approval failed |
| 25 | Watch first live fill in Discord #fills (env=LIVE tag) | `event_push_delivered channel=fills env=live-small` log + visible embed | If Discord miss: webhook_pusher logs; manual POST |
| 26 | `verify_chain --env live-small` returns CLEAN | exit 0 | Chain break = HALT_NEW_incident; do not proceed; incident review |
| 27 | EOD recon at 18:30 ET runs; live FlexQuery snapshot fetched + diff'd against `positions_current` | structlog `reconciliation_check_passed` | If break: investigate per §6 F4/F5 |

### Post-cutover (T+1 through T+30)

| # | Step | Success criterion |
|---|---|---|
| 28 | Each session: watch Discord #signals + #fills + #alerts; ensure env=LIVE embeds appear when expected | Visual confirmation |
| 29 | Each session: `verify_chain --env live-small` CLEAN | exit 0 |
| 30 | Operational Day 30 of live: paper-vs-live realized-vol analysis per §3 | Operator runs the analysis script (build pending); commits the findings to a decisions-log entry |
| 31 | Optional: enable CME real-time data subscription if AC5 timing issues observed | IBKR portal subscription enabled |

---

## 11. Rollback / Panic Plan

### Soft rollback (≤ 1 hour incident; recover to paper-only state)

When to use: an unrecoverable bug surfaces on live within the first session, before significant $ loss.

| # | Step | Time est. |
|---|---|---|
| R1 | Operator opens `/system` page on live URL → INVOKE kill-switch (reason: "live-cutover-rollback") | 30 sec |
| R2 | Live api confirms HALT_NEW state; cancels any pending working orders | 1 min |
| R3 | Operator opens TWS Desktop; manually flattens any open live positions to market | 5-10 min |
| R4 | Operator wires/journals live capital BACK to operator's bank account | 1-3 business days settlement |
| R5 | `docker compose -p trading-live down -v` (removes containers + volumes) | 1 min |
| R6 | Operator updates Discord/Resend communications: "cutover rolled back, paper continues" | 5 min |
| R7 | Operator writes `incident_reviews` row + decisions-log entry per spec §3.25 | 30 min |
| R8 | Paper continues uninterrupted (separate compose project; not affected) | Verify |

### Hard rollback (> 1 day incident; major $ loss + procedural failure)

When to use: live had multiple sessions; significant track record + $ exposure; suspect strategy edge has regressed or process has gone wrong.

Procedure:
1. INVOKE kill-switch on live + manual TWS flatten (R1-R3 above).
2. Operator pauses BOTH paper + live for a re-evaluation period (vacation_mode for both).
3. Audit chain export to S3 (defensive backup) — already automatic per spec §1.6.
4. Operator schedules a full post-mortem per `incident_reviews` template. May take days or weeks.
5. Live stays HALT_NEW until post-mortem closes. Paper may resume earlier if post-mortem rules out paper-affecting causes.

### Panic-flatten primitives (operator-accessible Day 1)

| Surface | Path | Latency | Notes |
|---|---|---|---|
| Web UI `/system` page | INVOKE kill-switch button + re-auth | < 30 sec | Cancels working orders; does NOT flatten open positions |
| Discord `/halt <reason>` | Bot → api INVOKE kill-switch | < 30 sec | Same as web INVOKE |
| TWS Desktop manual close | Operator opens TWS, closes all positions to market | 5-10 min | The ONLY path to flatten open positions Day 1 |
| `/flatten-all` Discord command (fast-follow PR) | Bot → re-auth → api `flatten_all_positions` endpoint | < 1 min once shipped | Appendix A row 6; non-gating |

**Decision: operator's panic-flatten plan Day 1 is the web INVOKE path + manual TWS flatten.** The Discord `/flatten-all` lands within 1-2 weeks of cutover.

### IBKR account compromise scenarios

If operator suspects IBKR live credentials compromised:
1. INVOKE kill-switch immediately (web or Discord)
2. Log into IBKR Client Portal, change password, revoke API access
3. Rotate `ibkr.live_password` in sops; redeploy api
4. Force-rotate all bearer tokens (S10, S12, S13, S14, S21)
5. Audit-chain export to S3
6. Operator-side investigation; if confirmed compromise: full hard rollback + IBKR support ticket

---

## Appendix A

### Pre-Cutover Build PRs (enumerated)

Each row = a discrete follow-up session. "Blocks cutover?" = the PR must merge + deploy + smoke-test on paper BEFORE the cutover ceremony begins.

| # | Title | Whitelist? | Estimate | Blocks cutover? | Notes |
|---|---|---|---|---|---|
| A1 | Fix `realized_pnl_usd` futures multiplier-correctness (Drill 9/10 follow-up #1) | YES `services/risk/**` + `services/api/**` | 1 session | **YES** | Misleading P&L erodes operator trust; gates cutover per L15 |
| A2 | Live env-tag handling — INSERT `accounts` row for U25655583 + bootstrap risk_state + parameter_sets head | YES `alembic/**` OR operator-script (no migration) | 0.5 session | **YES** | Operator-script preferred (no schema change; small data INSERT) |
| A3 | `deploy/.env.live` template + `docker-compose.live.yml` overlay for parallel-stack project naming | NO (deploy/**) | 0.5 session | **YES** | Includes second `ib_gateway` service for live mode |
| A4 | Caddy config: paper + live site blocks per O1 resolution | NO (deploy/**) | 0.5 session | **YES** | Depends on O1 decision |
| A5 | Discord `/capital-deposit` + `/capital-withdraw` slash commands | NO `services/discord_bot/**` (hot-fix) + YES for the capital-event INSERT path crossing `services/risk/**` | 1 session | **YES** | Day 1 deposit ceremony requires this |
| A6 | Discord `/flatten-all` command (re-auth gated; market-flat all positions) | YES `services/risk/**` + `services/execution/**` | 1 session | NO (fast follow) | §11 fast-follow item |
| A7 | env=PAPER/LIVE tag in Discord embeds (`webhook_pusher`) | NO `services/webhook_pusher/**` | 0.25 session | NO | Recommended pre-cutover for visual clarity but not strictly blocking |
| A8 | FlexQuery reconciliation: add CashTransaction parser (dividend + interest accrual rows) | YES `services/reconciliation/**` | 1 session | NO | First dividend lands ≥ 30 days after cutover usually |
| A9 | Annual loss + extended drawdown soft-trigger (alerts at -15% drawdown in 30-day window + -$3,750 annual review) | YES `services/risk/**` | 1 session | NO | Operator policy from §3; non-blocking |
| A10 | Dual-IBKR-gateway session model validation (operator runs simultaneous paper + live ib_gateways from same VPS IP for 5 min; confirm no Error 162) | NO (validation only) | 0.25 session | **YES** | Cheap; do this 1 week pre-cutover |
| A11 | Phase 1+ failure-mode tolerance re-tightening review (post-Day 30) | YES `services/risk/**` (review only Day 30) | 0 sessions pre-cutover | NO | Calendar item; no code at cutover |
| A12 | sops live template hygiene: remove databento/tiingo/quantconnect; add discord.api_bearer_token + totp.encryption_key + resend.to_address | NO (deploy/sops/**) | 0.25 session | YES | Mechanical fix; needed for cutover sops fill |
| A13 | Vol-target Day-30 paper-vs-live realized-vol analysis script | NO (scripts/operator_tools/**) | 0.5 session | NO | Operator runs at Day 30 of live |

**Total estimated build effort (cutover-blocking subset):** ~4-5 dev sessions.
**Total estimated build effort (incl. fast-follow):** ~6-7 dev sessions.
**Total estimated operator ceremony time:** ~2-4 hours at cutover day + 30 min for sops + 1-3 business days for ACH settlement.

### Risk-review-approved subset

PRs A1, A2 (if migration), A5 (partial), A6, A8, A9 require `risk-review-approved` label per dev-guide §2.2. Operator/agent approves these explicitly via PR review.

---

## Appendix B

### Open Decisions + Escalation List

| # | Open question | Default if unlocked | Escalation deadline |
|---|---|---|---|
| O1 | **Apex routing post-cutover.** Paper currently at `spratcapital.com` apex. Live template assumed apex. Three options: (a) Paper migrates to `paper.spratcapital.com` + live takes apex; (b) Paper stays at apex + live takes `live.spratcapital.com` subdomain; (c) Live displaces paper (paper retires). | **(b) Live takes `live.spratcapital.com` subdomain; paper stays at apex.** Minimal disruption; existing paper WebAuthn enrollments continue working; operator enrolls fresh credentials for live. | Pre-cutover; affects sops S15 + Caddy config |
| O2 | **CME real-time data subscription (~$1.50/mo).** Deferred for paper soak per 2026-05-19 evening discussion. Live with tighter stops may need real-time for AC5 fill validation. | **Enable at cutover.** $1.50/mo is essentially free; eliminates Drill 8 lesson #1 limit-price-drift risk. | Pre-cutover; operator-side IBKR portal action |
| O3 | **Live FlexQuery template.** Spec assumes per-account FlexQuery; paper has one. Live needs a separate template in IBKR portal. | Operator creates pre-cutover; documented in §10 step 3. | Pre-cutover (step 3) |
| O4 | **Live WebAuthn re-enrollment.** O1 outcome dictates whether paper credentials carry over. | **Re-enroll.** If O1 = (b), the rp_id is different (live.spratcapital.com vs spratcapital.com), so paper credentials don't work on live by construction. Operator enrolls fresh at cutover. | Cutover ceremony step 19 |
| O5 | **Live env-tag in lean.json.** Currently lean.json has `paper-internal` environment only. Should live use same env (LEAN runs in PaperBrokerage mode anyway since api owns broker) or add new `live-internal`? | **Reuse `paper-internal`.** LEAN never connects to IBKR under Option C; the env tag is api-side only. lean.json doesn't need changes. | Codified |
| O6 | **CCX13 vs CCX23 upgrade timing.** Upgrade pre-cutover (cleaner) or wait until live ramps capital (cheaper short-term)? | **Pre-cutover.** $13/mo delta is small; avoiding RAM-pressure surprises is worth it. | Cutover step 0 |
| O7 | **Live env `parameter_sets` head row provenance.** Live's first parameter_sets head row — copy paper's current head, OR generate fresh with `parameter_set_hash = SHA-256 of V1_DEFAULTS`? | **Copy paper's current head.** Live should use the same hash + params; paper's `last_active_at` field captures the link. | Cutover step 18 |
| O8 | **Watchdog poll target.** Currently watchdog polls paper's `/api/health`. After cutover, does it poll both? | **Poll both.** Watchdog has a per-target retry budget; doubling targets doesn't dilute it. Each target's escalation independent. | Cutover-day operator-side watchdog config update |
| O9 | **Live S3 backup partition.** Same S3 bucket as paper or separate? | **Same bucket; new prefix `live-small/`.** Object Lock enforces immutability regardless of prefix. | Codified |

### Escalation triggers (during cutover day)

If any of these conditions hit during the ceremony, **stop the cutover** and escalate:
- A readiness gate G1-G11 was missed but operator pushed forward anyway
- A cutover-day step fails its success criterion + the rollback procedure fails too
- Live's first signal cycle emits ZERO signals AND paper emitted ≥ 1 in the same window — data path divergence
- Discord webhook delivery to live channel fails 3+ times consecutively — env-tag wiring or shared-bearer auth issue
- Audit chain breaks on first `verify_chain --env live-small` — STOP IMMEDIATELY; HALT_NEW_incident path

### Cutover NOT-events (things this doc deliberately doesn't do)

- **No env-tier-specific risk envelope.** Live uses the same parameter_sets head as paper. (See L5 + §3.)
- **No env-tier-specific kill-switch trigger matrix.** (See L6 + §4 + §6.)
- **No new alembic migration.** Live just INSERTs an accounts row + risk_state row + parameter_sets head copy.
- **No new sops schema fields.** The live template needs the post-Day-21 + post-Day-23 fields backported (A12) but no fundamentally new schema.
- **No new vendor subscriptions** (CME real-time = O2 is the only candidate; it's ~$1.50/mo IBKR portal).
- **No live-money-cutover-specific tests.** Existing paper tests apply because paper code == live code; the only difference is config.
- **No automated cutover script.** The ceremony is operator-driven step-by-step. A scripted cutover would risk skipping a verification step.

---

## Document maintenance

When live cutover occurs:
- Update Status header from "DRAFT" → "EXECUTED YYYY-MM-DD"
- Append a `Docs/decisions-log.md` entry: `### YYYY-MM-DD — Live-money cutover executed against the cutover plan`
- This doc archives in `Docs/` as historical reference; do NOT delete.

When the plan changes pre-cutover:
- Open a new PR titled `docs(planning): live-money cutover plan vX.Y - <one-line>`
- Bump version in the status header
- The PR description summarizes what changed + why

---

*End of document.*
