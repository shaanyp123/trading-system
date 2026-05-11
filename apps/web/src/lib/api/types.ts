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

export interface Position {
  readonly instrument_id: string;
  readonly symbol: string;
  readonly contract_month: string | null;
  readonly qty: number;
  readonly avg_entry_price: string;
  readonly current_price: string;
  readonly unrealized_pnl: string;
  readonly unrealized_pnl_pct_of_nav: string;
  readonly cluster: PositionCluster | null;
  readonly managed_by_strategy_version: string;
}

export interface PositionsResponse {
  readonly positions: readonly Position[];
  readonly as_of: string;
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
// SSE envelope (services/api/sse.py)
// ---------------------------------------------------------------------------

export type SSEEventType =
  | 'signal'
  | 'fill'
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
