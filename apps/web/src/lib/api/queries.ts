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
} from './types';

const KEYS = {
  todayDigest: ['today-digest'] as const,
  healthScore: ['health-score'] as const,
  systemStatus: ['system-status'] as const,
  positions: ['positions'] as const,
  signalsPending: ['signals', { status: 'pending' as const }] as const,
  fills: ['fills'] as const,
  alertsOpen: ['alerts', { status: 'open' as const }] as const,
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

export const QUERY_KEYS = KEYS;
