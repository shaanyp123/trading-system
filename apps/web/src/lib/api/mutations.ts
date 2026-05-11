/**
 * apps/web/src/lib/api/mutations.ts -- approve/reject/defer signal mutations.
 *
 * Backend endpoints (Day 15) all return 501 `SIGNAL_HANDLER_NOT_WIRED` until
 * the Week 4 Wed dispatcher PR (forbidden whitelist) wires the real handlers.
 * Day 22 calls them anyway -- the ApiError surfaces in onError and a toast
 * tells the operator the handler isn't wired yet. When the dispatcher lands
 * these mutations work as-is.
 *
 * No optimistic updates Day 22: per spec §8.5 optimistic UX needs the real
 * server handler to round-trip (otherwise every approve flickers between
 * "optimistically approved" and "501 rolled back"). When the handler lands
 * the optimistic update pattern from §8.5 layers on without breaking
 * existing call sites.
 */

import { useMutation, useQueryClient, type UseMutationResult } from '@tanstack/react-query';

import { apiCall, ApiError } from '../api';
import { toast } from '../toast';
import type { DecisionDiaryEntry } from './types';
import { QUERY_KEYS } from './queries';

interface ApproveArgs {
  readonly signalId: string;
  readonly overrideSize?: number;
}

interface RejectArgs {
  readonly signalId: string;
  readonly diary: DecisionDiaryEntry;
}

interface DeferArgs {
  readonly signalId: string;
  readonly diary: DecisionDiaryEntry;
}

function surfaceMutationError(err: unknown, fallbackTitle: string): void {
  if (err instanceof ApiError) {
    if (err.errorCode === 'SIGNAL_HANDLER_NOT_WIRED') {
      toast({
        title: 'Handler not yet wired',
        description:
          'Signal approve/reject/defer go live when the Week 4 Wed dispatcher PR lands.',
        severity: 'p2',
      });
      return;
    }
    toast({ title: fallbackTitle, description: err.message, severity: 'p1' });
    return;
  }
  toast({ title: fallbackTitle, severity: 'p1' });
}

export function useApproveSignal(): UseMutationResult<void, unknown, ApproveArgs> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ signalId, overrideSize }: ApproveArgs) => {
      const body: Record<string, unknown> =
        overrideSize !== undefined ? { override_size: overrideSize } : {};
      await apiCall<void>(`/api/signals/${signalId}/approve`, {
        method: 'POST',
        body,
      });
    },
    onError: (err) => surfaceMutationError(err, 'Approve failed'),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.signalsPending });
    },
  });
}

export function useRejectSignal(): UseMutationResult<void, unknown, RejectArgs> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ signalId, diary }: RejectArgs) => {
      await apiCall<void>(`/api/signals/${signalId}/reject`, {
        method: 'POST',
        body: { decision_diary_entry: diary },
      });
    },
    onError: (err) => surfaceMutationError(err, 'Reject failed'),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.signalsPending });
    },
  });
}

export function useDeferSignal(): UseMutationResult<void, unknown, DeferArgs> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ signalId, diary }: DeferArgs) => {
      await apiCall<void>(`/api/signals/${signalId}/defer`, {
        method: 'POST',
        body: { decision_diary_entry: diary },
      });
    },
    onError: (err) => surfaceMutationError(err, 'Defer failed'),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: QUERY_KEYS.signalsPending });
    },
  });
}
