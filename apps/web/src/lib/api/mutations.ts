/**
 * apps/web/src/lib/api/mutations.ts -- operator mutations (kill switch,
 * backup codes).
 *
 * Crypto-pivot §3.9: the approve/reject/defer signal mutations are GONE.
 * The system is announce-only by operator mandate (delta spec §3.8 — no
 * per-trade approval); the backend endpoints were deleted with the LEAN
 * dispatcher, so the hooks could only ever 404. Trades are announced in
 * Discord `#fills` and rendered read-only on `/signals` + the Today page.
 */

import { useMutation, useQueryClient, type UseMutationResult } from '@tanstack/react-query';

import { apiCall, ApiError } from '../api';
import { toast } from '../toast';
import type {
  BackupCodesRegenerateResponse,
  KillSwitchTransitionResponse,
  KillSwitchTrigger,
} from './types';
import { QUERY_KEYS } from './queries';

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
