/**
 * apps/web/src/lib/api/queries.ts -- TanStack Query hooks for Today-page data.
 *
 * Per frontend-spec §8.1 staleTime table. Each hook calls the matching Phase-1
 * read endpoint (all shipped Day 15, all returning Phase-0 empty/insufficient-
 * data envelopes against the real Phase1QueryRepo). When live data lands
 * Week 7 Mon these hooks need NO changes -- the empty-state UX naturally
 * transitions to populated-state UX as the payloads fill in.
 *
 * staleTime values match the spec's table for an active CME session. The
 * "off-session" cadence (longer staleTime when markets are closed) lands
 * Phase 1+ via `useSessionAware` -- Day 22 picks the session-active value
 * unconditionally, which is conservative (more frequent refetches than
 * needed off-session, but never less frequent than needed during a
 * session).
 */

import { useQuery, type UseQueryResult } from '@tanstack/react-query';

import { apiCall } from '../api';
import type {
  AlertsResponse,
  FillsResponse,
  HealthScoreResponse,
  PositionsResponse,
  SignalListResponse,
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

export const QUERY_KEYS = KEYS;
