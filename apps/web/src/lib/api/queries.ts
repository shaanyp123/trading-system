/**
 * apps/web/src/lib/api/queries.ts -- TanStack Query hooks for Today-page data.
 *
 * Per frontend-spec §8.1 staleTime table. Each hook calls the matching Phase-1
 * read endpoint (all shipped Day 15, all returning Phase-0 empty/insufficient-
 * data envelopes against the real Phase1QueryRepo). When live data lands
 * Week 7 Mon these hooks need NO changes -- the empty-state UX naturally
 * transitions to populated-state UX as the payloads fill in.
 *
 * staleTime values match the spec's §8.1 table. Crypto trades 24/7
 * (post-pivot there is no session/off-session cadence split), so the
 * session-active values apply unconditionally.
 */

import { useQuery, type UseQueryResult } from '@tanstack/react-query';

import { apiCall } from '../api';
import type {
  AlertsResponse,
  AuditLogFilters,
  AuditLogPageResponse,
  AuthMeResponse,
  FillsResponse,
  HealthScoreResponse,
  KillSwitchStatus,
  PositionsResponse,
  RiskEnvelopeResponse,
  SignalListResponse,
  SystemCycleResponse,
  SystemFundingResponse,
  SystemStatus,
  TodayDigestResponse,
  TradeDetail,
  TradesListResponse,
  TradesQueryFilters,
} from './types';

const KEYS = {
  todayDigest: ['today-digest'] as const,
  healthScore: ['health-score'] as const,
  systemStatus: ['system-status'] as const,
  positions: ['positions'] as const,
  signalsPending: ['signals', { status: 'pending' as const }] as const,
  fills: ['fills'] as const,
  alertsOpen: ['alerts', { status: 'open' as const }] as const,
  trades: (filters: TradesQueryFilters) => ['trades', filters] as const,
  trade: (id: string) => ['trade', id] as const,
  authMe: ['auth-me'] as const,
  killSwitch: ['system', 'kill-switch'] as const,
  riskEnvelope: ['system', 'risk-envelope'] as const,
  systemCycle: ['system', 'cycle'] as const,
  systemFunding: ['system', 'funding'] as const,
  auditLog: (filters: AuditLogFilters) => ['system', 'audit', filters] as const,
};

export function useTodayDigest(): UseQueryResult<TodayDigestResponse> {
  return useQuery({
    queryKey: KEYS.todayDigest,
    queryFn: ({ signal }) =>
      apiCall<TodayDigestResponse>('/api/today/digest', { signal }),
    staleTime: 30_000,
  });
}

export function useHealthScore(): UseQueryResult<HealthScoreResponse> {
  return useQuery({
    queryKey: KEYS.healthScore,
    queryFn: ({ signal }) =>
      apiCall<HealthScoreResponse>('/api/health-score', { signal }),
    staleTime: 60_000,
  });
}

export function useSystemStatus(): UseQueryResult<SystemStatus> {
  return useQuery({
    queryKey: KEYS.systemStatus,
    queryFn: ({ signal }) =>
      apiCall<SystemStatus>('/api/system/status', { signal }),
    staleTime: 60_000,
  });
}

export function usePositionsCurrent(): UseQueryResult<PositionsResponse> {
  return useQuery({
    queryKey: KEYS.positions,
    queryFn: ({ signal }) =>
      apiCall<PositionsResponse>('/api/positions/current', { signal }),
    staleTime: 30_000,
  });
}

export function useSignalsPending(): UseQueryResult<SignalListResponse> {
  return useQuery({
    queryKey: KEYS.signalsPending,
    queryFn: ({ signal }) =>
      apiCall<SignalListResponse>('/api/signals?status=pending&limit=50', { signal }),
    staleTime: 10_000,
  });
}

export function useRecentFills(): UseQueryResult<FillsResponse> {
  return useQuery({
    queryKey: KEYS.fills,
    queryFn: ({ signal }) =>
      apiCall<FillsResponse>('/api/fills?limit=10', { signal }),
    staleTime: 10_000,
  });
}

export function useActiveAlerts(): UseQueryResult<AlertsResponse> {
  return useQuery({
    queryKey: KEYS.alertsOpen,
    queryFn: ({ signal }) =>
      apiCall<AlertsResponse>('/api/alerts?status=open&limit=20', { signal }),
    staleTime: 60_000,
  });
}

/**
 * Build the `/api/trades` URL with optional filters.
 *
 * Spec §4.1.2 query params: from, to, market, state, id_prefix, cursor, limit.
 * Day 26 omits `cursor` from the hook surface — Phase 0 returns `has_more=false`
 * so the page never needs to advance a cursor; "Load more" lands when trades
 * populate (Week 7 Thu paper round-trip).
 */
function buildTradesUrl(filters: TradesQueryFilters): string {
  const params = new URLSearchParams();
  if (filters.from !== undefined) params.set('from', filters.from);
  if (filters.to !== undefined) params.set('to', filters.to);
  if (filters.market !== undefined) params.set('market', filters.market);
  if (filters.state !== undefined) params.set('state', filters.state);
  if (filters.id_prefix !== undefined) params.set('id_prefix', filters.id_prefix);
  if (filters.limit !== undefined) params.set('limit', String(filters.limit));
  const qs = params.toString();
  return qs.length > 0 ? `/api/trades?${qs}` : '/api/trades';
}

/**
 * Trades-page summary list query.
 *
 * No explicit row in spec §8.1 staleTime table — recent fills are 10s and
 * positions are 30s. Trades surface aggregates both; 30s matches the closed-
 * trade cadence (trades only update on fill / exit / capacity-constraint),
 * which is much slower than positions.
 */
export function useTradesPage(
  filters: TradesQueryFilters,
): UseQueryResult<TradesListResponse> {
  return useQuery({
    queryKey: KEYS.trades(filters),
    queryFn: ({ signal }) =>
      apiCall<TradesListResponse>(buildTradesUrl(filters), { signal }),
    staleTime: 30_000,
  });
}

/**
 * Per-trade detail query for `/trades/:id`.
 *
 * Detail is invariant for closed trades (post-fill, P&L locked) so a long
 * staleTime is safe. SSE `fill` event invalidates the cache row for the
 * affected trade per spec §8.6 (handled in the SSE manager, not here).
 */
export function useTrade(id: string): UseQueryResult<TradeDetail> {
  return useQuery({
    queryKey: KEYS.trade(id),
    queryFn: ({ signal }) =>
      apiCall<TradeDetail>(`/api/trades/${encodeURIComponent(id)}`, { signal }),
    staleTime: 60_000,
    enabled: id.length > 0,
  });
}

/**
 * `/api/auth/me` — session info for the operator.
 *
 * Day 27: consumed by the re-auth modal to compute `now - last_uv_at` for
 * the 5-min UV gate (dev-guide §1.5 LOCKED). Short staleTime so the page
 * refetches around the gate boundary and the operator doesn't spend
 * minutes in stale-session UI before being prompted.
 */
export function useAuthMe(): UseQueryResult<AuthMeResponse> {
  return useQuery({
    queryKey: KEYS.authMe,
    queryFn: ({ signal }) =>
      apiCall<AuthMeResponse>('/api/auth/me', { signal }),
    staleTime: 30_000,
  });
}

/**
 * `/api/system/kill-switch` — narrow projection consumed by the kill-switch
 * tile on `/system`. Same content as `/api/system/status.risk_state` etc;
 * separate hook so the tile can be invalidated independently after an
 * invoke/resume mutation without bouncing the whole page.
 */
export function useKillSwitchStatus(): UseQueryResult<KillSwitchStatus> {
  return useQuery({
    queryKey: KEYS.killSwitch,
    queryFn: ({ signal }) =>
      apiCall<KillSwitchStatus>('/api/system/kill-switch', { signal }),
    staleTime: 30_000,
  });
}

/**
 * `/api/system/risk-envelope` — read-only Phase 1 tile per spec §2.6.3.
 *
 * 1-min staleTime: the envelope is essentially static today (LOCKED spec
 * defaults until the dispatcher writes the first parameter set). When
 * Phase 1+ parameter-change PRs land this stays a slow-changing surface,
 * so the cache window is conservative.
 */
export function useRiskEnvelope(): UseQueryResult<RiskEnvelopeResponse> {
  return useQuery({
    queryKey: KEYS.riskEnvelope,
    queryFn: ({ signal }) =>
      apiCall<RiskEnvelopeResponse>('/api/system/risk-envelope', { signal }),
    staleTime: 60_000,
  });
}

/**
 * `/api/system/cycle` — daily-cycle status (crypto-pivot §3.7): last 00:05 UTC
 * decision + 30 s risk-loop heartbeat + next Friday CDE close. Feeds the Today
 * pipeline-freshness strip. 30 s staleTime tracks the heartbeat cadence; SSE
 * position/pnl/risk_state events also invalidate `['system','cycle']`.
 */
export function useSystemCycle(): UseQueryResult<SystemCycleResponse> {
  return useQuery({
    queryKey: KEYS.systemCycle,
    queryFn: ({ signal }) =>
      apiCall<SystemCycleResponse>('/api/system/cycle', { signal }),
    staleTime: 30_000,
  });
}

/**
 * `/api/system/funding` — funding + cash-yield telemetry (crypto-pivot
 * §3.7/§3.9): per-product funding rates, estimated funding today (from hourly
 * telemetry, not venue-settled), sweep totals, yield rate, liquidation buffer.
 * Hourly-resolution data so a 60 s staleTime is conservative; SSE
 * pnl/position/fill events also invalidate `['system','funding']`.
 */
export function useSystemFunding(): UseQueryResult<SystemFundingResponse> {
  return useQuery({
    queryKey: KEYS.systemFunding,
    queryFn: ({ signal }) =>
      apiCall<SystemFundingResponse>('/api/system/funding', { signal }),
    staleTime: 60_000,
  });
}

function buildAuditLogUrl(filters: AuditLogFilters): string {
  const params = new URLSearchParams();
  if (filters.event_type !== undefined) params.set('event_type', filters.event_type);
  if (filters.env !== undefined) params.set('env', filters.env);
  if (filters.from !== undefined) params.set('from', filters.from);
  if (filters.to !== undefined) params.set('to', filters.to);
  if (filters.cursor !== undefined) params.set('cursor', filters.cursor);
  if (filters.limit !== undefined) params.set('limit', String(filters.limit));
  const qs = params.toString();
  return qs.length > 0 ? `/api/system/audit?${qs}` : '/api/system/audit';
}

/**
 * `/api/system/audit` — paginated audit-log table per spec §2.6.4.
 *
 * Long staleTime (5 min) because audit_log is append-only by construction
 * (immutability triggers from migration 0005). New rows show up on next
 * refetch — SSE-event invalidation per spec §8.6 is wired in the SSE
 * manager (Phase 1+).
 */
export function useAuditLogPage(
  filters: AuditLogFilters,
): UseQueryResult<AuditLogPageResponse> {
  return useQuery({
    queryKey: KEYS.auditLog(filters),
    queryFn: ({ signal }) =>
      apiCall<AuditLogPageResponse>(buildAuditLogUrl(filters), { signal }),
    staleTime: 5 * 60_000,
  });
}

export const QUERY_KEYS = KEYS;
