/**
 * apps/web/src/lib/api/types.ts -- hand-typed mirrors of the Phase-1 schemas.
 *
 * 1:1 with services/api/schemas/* (Pydantic v2 BaseModel with extra='forbid').
 * Decimal fields serialize as strings per dev-guide §3.8. Timestamps are RFC
 * 3339 UTC strings.
 *
 * Phase 1+: replaced by `packages/api-types/` codegen from FastAPI OpenAPI.
 * Until then this file is the single source of truth for the wire contract
 * on the frontend side. Add types here as new endpoints get wired into the
 * UI -- DO NOT spread response shapes across multiple files.
 */

// ---------------------------------------------------------------------------
// Health Score (services/api/schemas/health_score.py)
// ---------------------------------------------------------------------------

export type HealthScoreComponentName =
  | 'live_sharpe_vs_backtest'
  | 'slippage_drift'
  | 'hit_rate'
  | 'capacity_headroom'
  | 'days_since_recon_break';

export interface HealthScoreComponent {
  readonly name: HealthScoreComponentName;
  readonly weight_pct: number;
  readonly window: string;
  readonly score: number | null;
  readonly insufficient_data: boolean;
}

export interface HealthScoreResponse {
  readonly composite: number;
  readonly traffic_light: 'green' | 'yellow' | 'red';
  readonly components: readonly HealthScoreComponent[];
  readonly insufficient_data: boolean;
  readonly computed_at: string;
}

// ---------------------------------------------------------------------------
// Today digest (services/api/schemas/today.py)
// ---------------------------------------------------------------------------

export type ExposureCluster =
  | 'equity_index'
  | 'commodity'
  | 'rates_bonds'
  | 'crypto'
  | 'fx';

export interface PnLSummary {
  readonly daily_pnl: string;
  readonly weekly_pnl: string;
  readonly monthly_pnl: string;
  readonly yearly_pnl: string;
}

export interface ExposureBreakdown {
  readonly by_cluster: Readonly<Record<ExposureCluster, string>>;
  readonly gross_exposure_pct_nav: string;
  readonly net_exposure_pct_nav: string;
}

export type RiskState = 'NORMAL' | 'HALT_NEW' | 'CONVALESCENT' | 'VACATION';
export type RiskSeverity = 'routine' | 'defensive_envelope' | 'incident_review';
export type AgentStatus = 'idle' | 'working' | 'degraded' | 'disabled' | 'errored';
export type EnvName = 'paper' | 'live-small' | 'live-scale' | 'dev';

export interface TodayDigestResponse {
  readonly health_score: HealthScoreResponse;
  readonly pnl: PnLSummary;
  readonly exposure: ExposureBreakdown;
  readonly queued_signals_count: number;
  readonly active_alerts_count_by_severity: Readonly<Record<'P0' | 'P1' | 'P2', number>>;
  readonly state: RiskState;
  readonly state_severity: RiskSeverity | null;
  readonly agent_status: AgentStatus;
  readonly environment: EnvName;
  readonly deployed_strategy_version: string;
}

// ---------------------------------------------------------------------------
// System (services/api/schemas/system.py)
// ---------------------------------------------------------------------------

export interface ReconciliationSummary {
  readonly last_check_utc: string;
  readonly last_check_passed: boolean;
  readonly open_breaks: number;
  readonly breaks_24h: number;
}

export interface SystemStatus {
  readonly risk_state: 'NORMAL' | 'HALT_NEW' | 'CONVALESCENT';
  readonly severity: RiskSeverity | null;
  readonly halt_reason: string | null;
  readonly halt_dwell_session_count: number | null;
  readonly convalescent_session_count: number | null;
  readonly vacation_active: boolean;
  readonly vacation_until_utc: string | null;
  readonly watchdog_last_ping_utc: string;
  readonly reconciliation_summary: ReconciliationSummary;
  readonly is_session_active: boolean;
  readonly server_now: string;
  readonly backend_version: string;
  readonly expected_frontend_version: string;
}

// ---------------------------------------------------------------------------
// Positions (services/api/schemas/positions.py)
// ---------------------------------------------------------------------------

export type PositionCluster = 'equity_index' | 'commodity' | 'rates_bonds' | 'crypto' | 'fx';

/** Provenance of `current_price`/`unrealized_pnl` — the §3.9 mark ladder,
 *  best rung first: live WS mark → recon-written EOD mark → avg-cost fallback. */
export type MarkSource = 'live_ws' | 'recon_eod' | 'fallback_avg_cost';

export interface Position {
  readonly instrument_id: string;
  /** CDE product id post-pivot (e.g. `BIP-20DEC30-CDE`). */
  readonly symbol: string;
  readonly qty: number;
  readonly avg_entry_price: string;
  readonly current_price: string;
  readonly unrealized_pnl: string;
  readonly unrealized_pnl_pct_of_nav: string;
  readonly cluster: PositionCluster | null;
  readonly managed_by_strategy_version: string;
  /** Base-asset label (BTC/ETH); null without contracts enrichment. */
  readonly asset: string | null;
  /** Worker's client-side 2×ATR soft stop; null when none armed. */
  readonly client_stop_price: string | null;
  /** Resting venue stop-limit backstop price; null when not tracked. */
  readonly native_stop_price: string | null;
  /** True when the worker tracks a venue-resting native stop order id;
   *  null when engine state is absent. */
  readonly native_stop_resting: boolean | null;
  /** RFC 3339 UTC — when the price behind `current_price` was observed. */
  readonly mark_observed_at_utc: string | null;
  readonly mark_source: MarkSource | null;
}

export interface PositionsResponse {
  readonly positions: readonly Position[];
  readonly as_of: string;
}

// ---------------------------------------------------------------------------
// Daily cycle (services/api/schemas/cycle.py — crypto-pivot §3.7)
// ---------------------------------------------------------------------------

/** One asset's slice of the 00:05 UTC decision outcome. Decimals are strings. */
export interface CycleAssetView {
  readonly asset: string;
  readonly trend_score: string | null;
  readonly prior_contracts: number | null;
  readonly engine_target: number | null;
  readonly final_target: number | null;
  readonly action: string | null;
  readonly est_cost_usd: string | null;
  readonly mark: string | null;
  readonly stop_level: string | null;
}

export interface CycleDecisionView {
  /** ISO date (UTC decision day). */
  readonly decision_date: string;
  readonly decided_at_utc: string;
  /** `dispatching` | `completed` | `failed` | `skipped_*`. */
  readonly status: string;
  readonly env: string;
  readonly equity_usd: string | null;
  readonly weekly_halved: boolean;
  readonly m_combined: string | null;
  readonly skip_reason: string | null;
  readonly assets: readonly CycleAssetView[];
}

export interface CycleWorkerView {
  readonly risk_loop_heartbeat_utc: string | null;
  readonly seconds_since_heartbeat: number | null;
  readonly heartbeat_fresh: boolean;
  readonly risk_loop_tick_count: number;
  readonly marks_stale: boolean;
  /** ISO date. */
  readonly last_decision_date: string | null;
}

/** `decision`/`worker` are null on a fresh DB (worker never ran). */
export interface SystemCycleResponse {
  readonly decision: CycleDecisionView | null;
  readonly worker: CycleWorkerView | null;
  readonly next_friday_close_utc: string;
  readonly server_now: string;
}

// ---------------------------------------------------------------------------
// Funding + cash yield (services/api/schemas/funding.py — crypto-pivot §3.7/§3.9)
// ---------------------------------------------------------------------------

export interface FundingProductView {
  readonly product_id: string;
  readonly asset: string | null;
  readonly rate_per_interval: string | null;
  readonly interval_hours: string | null;
  /** rate × (8760 / interval_hours) — annualized funding fraction. */
  readonly annualized_rate: string | null;
  readonly mark_price: string | null;
  readonly observed_at_utc: string | null;
  /** Signed net contracts currently held (0 = telemetry-only). */
  readonly held_contracts: number;
  readonly contract_size: string | null;
  /** Estimated funding paid/received today (negative = the position pays). */
  readonly est_funding_today_usd: string | null;
}

export interface CashSweepView {
  readonly requested_at_utc: string;
  readonly completed_at_utc: string | null;
  readonly direction: string; // 'to_yield' | 'to_margin'
  readonly amount_usd: string;
  readonly status: string; // 'requested' | 'completed' | 'failed'
  readonly reason: string;
}

export interface SystemFundingResponse {
  readonly products: readonly FundingProductView[];
  readonly est_funding_today_total_usd: string | null;
  /** Honesty label — every per-day number is an estimate from hourly
   *  telemetry, NOT the venue's settled funding ledger. */
  readonly funding_basis: 'estimated_from_hourly_telemetry';
  readonly sweeps_today: readonly CashSweepView[];
  readonly swept_to_yield_today_usd: string;
  readonly reclaimed_to_margin_today_usd: string;
  /** Fraction (e.g. "0.035"); null renders as "—". */
  readonly yield_apy: string | null;
  readonly liquidation_buffer_pct: string | null;
  readonly liquidation_buffer_as_of_utc: string | null;
  readonly server_now: string;
}

// ---------------------------------------------------------------------------
// Signals (services/api/schemas/signals.py)
// ---------------------------------------------------------------------------

export type SignalAnomalyReason =
  | 'vol_regime_z_high'
  | 'capacity_above_alert'
  | 'recent_decision_diary_concern'
  | 'slippage_outlier_recent'
  | 'version_baseline_divergence';

export type SignalStatus =
  | 'pending'
  | 'approved'
  | 'rejected'
  | 'deferred'
  | 'expired'
  | 'working'
  | 'partially_filled'
  | 'filled'
  | 'cancelled'
  | 'closed'
  | 'stopped_out'
  | 'sub_minimum_size'
  | 'macro_window_drop'
  | 'market_drop_settlement_unavailable';

export interface SignalSummary {
  readonly id: string;
  readonly market: string;
  readonly direction: 'long' | 'short' | 'flat';
  // Entry-vs-exit discriminator (`signals.signal_type`). Production paper
  // emits 'entry' / 'exit'; 'donchian_breakout' is a sibling backtest-only
  // value that never reaches this table. The Side pill keys off `=== 'exit'`
  // (with `direction === 'flat'` as a secondary tell) so it degrades safely
  // for any unanticipated value.
  readonly signal_type: 'entry' | 'exit' | 'donchian_breakout';
  // For exit signals, the side of the position being closed — lets the UI
  // render "CLOSE · was long/short". `null` for entries (nothing to close)
  // and for exit rows whose sizing_trace omits the prior side.
  readonly prior_position_direction: 'long' | 'short' | null;
  readonly target_contracts: number;
  readonly decision_price: string;
  readonly expected_fill_price: string | null;
  readonly expected_slippage_bps: string | null;
  readonly unsettled: boolean;
  readonly anomaly_reasons: readonly SignalAnomalyReason[];
  readonly status: SignalStatus;
  readonly emitted_at_utc: string;
  readonly expires_at_utc: string;
  readonly strategy_short_hash: string;
  readonly parameter_set_short_hash: string;
}

export interface SignalListResponse {
  readonly items: readonly SignalSummary[];
  readonly next_cursor: string | null;
  readonly has_more: boolean;
}

export interface DecisionDiaryEntry {
  readonly entry_class: 'signal_response' | 'forward_looking' | 'general';
  readonly tag:
    | 'data_concern'
    | 'regime_concern'
    | 'size_concern'
    | 'manual_judgment'
    | 'other';
  readonly reasoning_text: string;
}

// ---------------------------------------------------------------------------
// Fills (services/api/schemas/fills.py)
// ---------------------------------------------------------------------------

export interface Fill {
  readonly fill_uuid: string;
  readonly order_uuid: string;
  readonly signal_uuid: string;
  readonly instrument_id: string;
  readonly side: 'buy' | 'sell';
  readonly qty: number;
  readonly price: string;
  readonly slippage_bps: string;
  readonly expected_price: string;
  readonly filled_at: string;
}

export interface FillsResponse {
  readonly fills: readonly Fill[];
  readonly next_cursor: string | null;
  readonly has_more: boolean;
}

// ---------------------------------------------------------------------------
// Alerts (services/api/schemas/alerts.py)
// ---------------------------------------------------------------------------

export type AlertSeverity = 'P0' | 'P1' | 'P2';
export type AlertStatus = 'open' | 'acknowledged' | 'resolved';

export interface Alert {
  readonly alert_uuid: string;
  readonly severity: AlertSeverity;
  readonly category: string;
  readonly title: string;
  readonly body_md: string;
  readonly status: AlertStatus;
  readonly fired_at: string;
  readonly acknowledged_at: string | null;
  readonly resolved_at: string | null;
  readonly audit_event_uuid: string | null;
}

export interface AlertsResponse {
  readonly alerts: readonly Alert[];
  readonly next_cursor: string | null;
  readonly has_more: boolean;
}

// ---------------------------------------------------------------------------
// Trades (services/api/schemas/trades.py)
// ---------------------------------------------------------------------------

export type TradeDirection = 'long' | 'short';
export type TradeState =
  | 'open_position'
  | 'closed'
  | 'stopped_out'
  | 'capacity_constrained';

export interface Trade {
  readonly id: string;
  readonly env: string;
  readonly market: string;
  readonly direction: TradeDirection;
  readonly state: TradeState;
  readonly opened_at_utc: string;
  readonly closed_at_utc: string | null;
  readonly total_quantity: number;
  readonly avg_entry_price: string;
  readonly avg_exit_price: string | null;
  readonly realized_pnl_usd: string | null;
  readonly entry_signal_id: string;
  readonly managed_by_strategy_version: string;
}

export interface TradesListResponse {
  readonly trades: readonly Trade[];
  readonly next_cursor: string | null;
  readonly has_more: boolean;
}

export interface TradeDetail {
  readonly id: string;
  readonly account_id: string;
  readonly env: string;
  readonly market: string;
  readonly direction: TradeDirection;
  readonly state: TradeState;
  readonly opened_at_utc: string;
  readonly closed_at_utc: string | null;
  readonly total_quantity: number;
  readonly avg_entry_price: string;
  readonly avg_exit_price: string | null;
  readonly realized_pnl_usd: string | null;
  readonly realized_commission_usd: string;
  readonly dividend_pnl_usd: string;
  readonly entry_signal_id: string;
  readonly entry_order_id: string;
  readonly exit_order_id: string | null;
  readonly managed_by_version: string;
  readonly managed_by_strategy_version: string;
  readonly strategy_hash: string;
  readonly parameter_set_hash: string;
  readonly slippage_calibration_version_id: string;
}

export interface TradesQueryFilters {
  readonly from?: string; // ISO 8601 UTC
  readonly to?: string;
  readonly market?: string;
  readonly state?: TradeState;
  readonly id_prefix?: string;
  readonly limit?: number;
}

// ---------------------------------------------------------------------------
// SSE envelope (services/api/sse.py)
// ---------------------------------------------------------------------------

export type SSEEventType =
  | 'signal'
  | 'fill'
  | 'order'
  | 'position'
  | 'pnl'
  | 'risk_state'
  | 'health'
  | 'alert'
  | 'audit'
  | 'agent'
  | 'vacation'
  | 'watchdog'
  | 'session_evicted'
  | 'job'
  | 'version'
  | 'ping';

export interface SSEEnvelope {
  readonly type: SSEEventType;
  readonly sequence_no: number;
  readonly server_now: string;
  readonly data: unknown;
}

export interface SessionEvictedData {
  readonly reason: 'tab_limit' | 'explicit_logout' | 'breakglass_kill' | 'creds_rotated';
}

export interface VersionData {
  readonly must_reload: boolean;
  readonly reason: string;
}

// ---------------------------------------------------------------------------
// Auth /me (services/api/schemas/auth.py)
// ---------------------------------------------------------------------------

export interface AuthMeResponse {
  readonly user_id: string;
  readonly username: string;
  readonly role: 'owner' | 'reader';
  readonly auth_strength: 'weak' | 'strong';
  readonly last_uv_at: string | null;
  readonly session_expires_at: string;
  readonly webauthn_enrolled: boolean;
  readonly totp_enrolled: boolean;
}

// ---------------------------------------------------------------------------
// Kill switch (services/api/schemas/system.py)
// ---------------------------------------------------------------------------

export type KillSwitchTrigger =
  | 'trailing_dd_breach'
  | 'daily_loss_breach'
  | 'signal_storm'
  | 'recon_mismatch'
  | 'broker_disconnect_5m'
  | 'vol_regime_z_gt_2'
  | 'corr_gt_0_85'
  | 'unhandled_exception'
  | 'calendar_unratified'
  | 'heartbeat_engagement_fail'
  | 'qc_objstore_stale_10m'
  | 'watchdog_and_discord_both_fail'
  | 'audit_write_fail'
  | 'hash_chain_break'
  | 'decommission_floor'
  | 'manual_judgment';

export interface KillSwitchStatus {
  readonly risk_state: 'NORMAL' | 'HALT_NEW' | 'CONVALESCENT';
  readonly severity: RiskSeverity | null;
  readonly halt_reason: string | null;
  readonly last_transition_utc: string | null;
  readonly last_transition_audit_event_uuid: string | null;
}

export interface KillSwitchTransitionResponse {
  readonly risk_state: 'NORMAL' | 'HALT_NEW' | 'CONVALESCENT';
  readonly severity: RiskSeverity | null;
  readonly halt_reason: string | null;
  readonly audit_event_uuid: string;
  readonly sse_sequence_no: number;
}

// ---------------------------------------------------------------------------
// Risk envelope (services/api/schemas/system.py — Day 27)
// ---------------------------------------------------------------------------

export type RiskParameterCluster =
  | 'equity_index'
  | 'rates_bonds'
  | 'commodity'
  | 'crypto'
  | 'fx';

export interface RiskParameterItem {
  readonly key: string;
  readonly label: string;
  readonly value: string; // Decimal-as-string
  readonly unit: 'percent' | 'ratio';
  readonly cluster: RiskParameterCluster | null;
}

export interface RiskEnvelopeResponse {
  readonly items: readonly RiskParameterItem[];
  readonly parameter_set_hash: string | null;
  readonly parameter_set_active_since_utc: string | null;
  readonly source: 'parameters_table' | 'spec_defaults';
  readonly server_now: string;
}

// ---------------------------------------------------------------------------
// Audit explorer (services/api/schemas/system.py — Day 27)
// ---------------------------------------------------------------------------

export interface AuditLogEntry {
  readonly event_uuid: string;
  readonly sequence_no: number;
  readonly event_type: string;
  readonly env: 'paper' | 'live-small' | 'live-scale';
  readonly phase_at_emit: number;
  readonly source_clock_ts: string;
  readonly ingest_clock_ts: string;
  readonly prev_hash_hex: string;
  readonly record_hash_hex: string;
  readonly repaired_for_sequence_no: number | null;
  readonly repaired_for_event_timestamp: string | null;
  readonly payload_preview: string;
}

export interface AuditLogPageResponse {
  readonly entries: readonly AuditLogEntry[];
  readonly next_cursor: string | null;
  readonly has_more: boolean;
}

export interface AuditLogFilters {
  readonly event_type?: string;
  readonly env?: 'paper' | 'live-small' | 'live-scale';
  readonly from?: string; // ISO 8601 UTC
  readonly to?: string;
  readonly cursor?: string;
  readonly limit?: number;
}

// ---------------------------------------------------------------------------
// Backup codes regenerate (services/api/schemas/auth.py)
// ---------------------------------------------------------------------------

export interface BackupCodesRegenerateResponse {
  readonly codes: readonly string[];
  readonly generation_id: string;
}
