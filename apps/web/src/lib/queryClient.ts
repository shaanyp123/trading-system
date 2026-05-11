/**
 * apps/web/src/lib/queryClient.ts -- global TanStack Query configuration.
 *
 * Per frontend-spec §8.1 LOCKED defaults:
 *   - staleTime: 30s default (per-resource overrides land in api/queries.ts)
 *   - gcTime: 10min
 *   - retry: skip 4xx (401/403/404/422/426); else 3 retries with exponential backoff
 *   - refetchOnWindowFocus + refetchOnReconnect: 'always'
 *   - mutations.retry: 0 (never auto-retry mutations)
 *
 * Phase 1 Cache Layer (§8.2 LOCKED) = in-memory only. IndexedDB persistence
 * lands Phase 2 for instrument_metadata + calendar + immutable backtest
 * results; live trading state (positions, P&L, signals) is NEVER persisted.
 */

import { QueryClient } from '@tanstack/react-query';

import { ApiError } from './api';

const NON_RETRYABLE_STATUSES = new Set<number>([401, 403, 404, 422, 426]);

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      gcTime: 10 * 60_000,
      retry: (failureCount, error) => {
        if (error instanceof ApiError && NON_RETRYABLE_STATUSES.has(error.status)) {
          return false;
        }
        return failureCount < 3;
      },
      retryDelay: (attemptIndex) => Math.min(1000 * 2 ** attemptIndex, 30_000),
      refetchOnWindowFocus: 'always',
      refetchOnReconnect: 'always',
    },
    mutations: {
      retry: 0,
    },
  },
});
