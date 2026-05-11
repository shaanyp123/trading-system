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
import type {
  BackupCodesRegenerateResponse,
  DecisionDiaryEntry,
  KillSwitchTransitionResponse,
  KillSwitchTrigger,
} from './types';
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

// ---------------------------------------------------------------------------
// Day 27 — Kill-switch invoke / resume (real, not 501)
// ---------------------------------------------------------------------------

interface InvokeKillSwitchArgs {
  readonly trigger: KillSwitchTrigger;
  readonly reason: string;
}

interface ResumeKillSwitchArgs {
  readonly incidentReviewId?: string;
}

function invalidateKillSwitchSurfaces(qc: ReturnType<typeof useQueryClient>): void {
  void qc.invalidateQueries({ queryKey: QUERY_KEYS.killSwitch });
  void qc.invalidateQueries({ queryKey: QUERY_KEYS.systemStatus });
  void qc.invalidateQueries({ queryKey: QUERY_KEYS.todayDigest });
}

function surfaceKillSwitchError(err: unknown, fallbackTitle: string): void {
  if (err instanceof ApiError) {
    if (err.errorCode === 'ALREADY_HALTED') {
      toast({
        title: 'Already halted',
        description:
          'Kill switch is already engaged. Use Resume to recover.',
        severity: 'p2',
      });
      return;
    }
    if (err.errorCode === 'NOT_HALTED') {
      toast({
        title: 'Not halted',
        description:
          'Resume is only valid from HALT_NEW. The system is already in NORMAL or CONVALESCENT.',
        severity: 'p2',
      });
      return;
    }
    if (err.errorCode === 'RE_AUTH_REQUIRED') {
      // Modal flow re-prompts; toast is a soft hint in case the modal
      // didn't fire (e.g. /api/auth/me hadn't loaded yet when the button
      // was clicked).
      toast({
        title: 'Re-verify required',
        description:
          'Re-prompt the operator for WebAuthn within the 5-minute re-auth window.',
        severity: 'p1',
      });
      return;
    }
    if (err.errorCode === 'INCIDENT_REVIEW_ID_REQUIRED') {
      toast({
        title: 'Incident review required',
        description:
          'Severity=incident_review halts must include an incident_review_id before resume.',
        severity: 'p1',
      });
      return;
    }
    toast({ title: fallbackTitle, description: err.message, severity: 'p1' });
    return;
  }
  toast({ title: fallbackTitle, severity: 'p1' });
}

export function useInvokeKillSwitch(): UseMutationResult<
  KillSwitchTransitionResponse,
  unknown,
  InvokeKillSwitchArgs
> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ trigger, reason }) =>
      apiCall<KillSwitchTransitionResponse>('/api/system/kill-switch/invoke', {
        method: 'POST',
        body: { trigger, reason },
      }),
    onError: (err) => surfaceKillSwitchError(err, 'Kill switch invoke failed'),
    onSettled: () => invalidateKillSwitchSurfaces(queryClient),
  });
}

export function useResumeKillSwitch(): UseMutationResult<
  KillSwitchTransitionResponse,
  unknown,
  ResumeKillSwitchArgs
> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ incidentReviewId }) => {
      const body: Record<string, unknown> = {};
      if (incidentReviewId !== undefined) {
        body['incident_review_id'] = incidentReviewId;
      }
      return apiCall<KillSwitchTransitionResponse>(
        '/api/system/kill-switch/resume',
        { method: 'POST', body },
      );
    },
    onError: (err) => surfaceKillSwitchError(err, 'Kill switch resume failed'),
    onSettled: () => invalidateKillSwitchSurfaces(queryClient),
  });
}

// ---------------------------------------------------------------------------
// Day 27 — Backup codes regenerate (re-auth gated; frontend-spec §2.6.10)
// ---------------------------------------------------------------------------

export function useRegenerateBackupCodes(): UseMutationResult<
  BackupCodesRegenerateResponse,
  unknown,
  void
> {
  return useMutation({
    mutationFn: () =>
      apiCall<BackupCodesRegenerateResponse>(
        '/api/auth/backup-codes/regenerate',
        { method: 'POST', body: {} },
      ),
    onError: (err) => {
      if (err instanceof ApiError && err.errorCode === 'RE_AUTH_REQUIRED') {
        toast({
          title: 'Re-verify required',
          description:
            'Re-prompt for WebAuthn within the 5-minute re-auth window.',
          severity: 'p1',
        });
        return;
      }
      const description = err instanceof Error ? err.message : undefined;
      toast({
        title: 'Regenerate failed',
        ...(description !== undefined ? { description } : {}),
        severity: 'p1',
      });
    },
  });
}
