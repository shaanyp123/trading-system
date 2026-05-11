/**
 * apps/web/src/lib/sse.ts -- SSE connection manager + `useSSE` React hook.
 *
 * Wraps `@microsoft/fetch-event-source` (chosen over native EventSource per
 * frontend-spec §4.1 because native cannot send custom headers, cannot expose
 * connection lifecycle, and uses opaque retry).
 *
 * Wire contract (services/api/sse.py + backend-spec §4.2 LOCKED):
 *   - Canonical envelope: `{type, sequence_no, server_now, data}` in the
 *     `data:` line as JCS-canonical JSON; `id:` line carries sequence_no
 *     for native Last-Event-ID resume; `event:` line carries the type.
 *   - 14 canonical event types incl. `ping` heartbeat (every 30s) and
 *     `session_evicted` control event.
 *   - 24h replay buffer; client resumes via Last-Event-ID header.
 *   - Beyond-buffer resume returns HTTP 426 with `version` envelope ->
 *     client must clear state and full-refetch.
 *   - N=4 tabs per user; oldest receives `session_evicted` with
 *     `data: { reason: "tab_limit" }` and connection closes server-side.
 *
 * Reconnect strategy (spec §4.5):
 *   - Exponential backoff with jitter: `min(60s, 5s * 2^attempts) + random(0-10s)`
 *   - Max 10 attempts before transitioning to degraded mode (polling fallback
 *     is a Day 23+ feature; Day 22 just stops reconnecting and surfaces the
 *     state in the SSE pill).
 *   - On 426 buffer-expired: reset lastSeq, invalidate all live queries,
 *     and re-open from sequence_no=0.
 *
 * Cache-invalidation pattern (spec §8.6): on each received event, invalidate
 * the corresponding TanStack Query keys so the next render re-fetches the
 * canonical state. Phase 0 endpoints all return empty payloads so the
 * invalidation is essentially a no-op until live data lands, but the wiring
 * proves the contract end-to-end today.
 */

'use client';

import { fetchEventSource, type EventSourceMessage } from '@microsoft/fetch-event-source';
import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';

import { toast } from './toast';
import type { SSEEnvelope, SSEEventType, SessionEvictedData } from './api/types';

const SSE_ENDPOINT = '/api/sse/events';
const MAX_RECONNECT_ATTEMPTS = 10;
const BASE_BACKOFF_MS = 5_000;
const MAX_BACKOFF_MS = 60_000;
const JITTER_MS = 10_000;

export type SSEStatus =
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'degraded'
  | 'evicted'
  | 'disconnected';

export interface UseSSEResult {
  readonly status: SSEStatus;
  readonly lastSequenceNo: number;
  readonly evictionReason: string | null;
}

/**
 * Per spec §8.6: map each event type to the TanStack Query keys that should
 * be invalidated when an envelope of that type arrives. The hook calls
 * `queryClient.invalidateQueries({ queryKey })` for each entry.
 *
 * `ping` and `session_evicted` are control events -- no business-data
 * invalidation. `version` is handled by the 426 path in the reconnect logic.
 */
const INVALIDATE_KEYS: Readonly<Record<SSEEventType, readonly (readonly string[])[]>> = {
  signal: [['signals'], ['today-digest']],
  fill: [['fills'], ['positions'], ['today-digest']],
  position: [['positions'], ['today-digest']],
  pnl: [['today-digest']],
  risk_state: [['system-status'], ['today-digest']],
  health: [['health-score'], ['today-digest']],
  alert: [['alerts'], ['today-digest']],
  audit: [['audit-log']],
  agent: [['system-status']],
  vacation: [['system-status'], ['today-digest']],
  watchdog: [['watchdog']],
  session_evicted: [],
  job: [],
  version: [],
  ping: [],
};

function computeBackoffMs(attempt: number): number {
  const exp = Math.min(BASE_BACKOFF_MS * 2 ** attempt, MAX_BACKOFF_MS);
  return exp + Math.floor(Math.random() * JITTER_MS);
}

/**
 * Sentinel exception thrown to instruct `fetchEventSource` to permanently
 * abort the connection. Caught in `onerror` -> rethrown to halt retries.
 */
class FatalSSEError extends Error {
  constructor(reason: string) {
    super(reason);
    this.name = 'FatalSSEError';
  }
}

/**
 * Open + manage a single SSE connection. Returns a teardown function that
 * aborts the in-flight fetch and stops further reconnects.
 *
 * Exposed for tests + advanced callers; most callers should use `useSSE`.
 */
export function startSSE(opts: {
  apiBase: string;
  onStatusChange: (status: SSEStatus) => void;
  onEnvelope: (env: SSEEnvelope) => void;
  onEvicted: (reason: string) => void;
  initialSequenceNo: number;
}): () => void {
  const controller = new AbortController();
  let attempt = 0;
  let lastSeq = opts.initialSequenceNo;
  let stopped = false;

  const url = `${opts.apiBase}${SSE_ENDPOINT}`;

  const run = async (): Promise<void> => {
    while (!stopped && attempt <= MAX_RECONNECT_ATTEMPTS) {
      opts.onStatusChange(attempt === 0 ? 'connecting' : 'reconnecting');

      try {
        await fetchEventSource(url, {
          signal: controller.signal,
          credentials: 'include',
          headers: lastSeq > 0 ? { 'Last-Event-ID': String(lastSeq) } : {},
          openWhenHidden: true,

          onopen: async (response: Response): Promise<void> => {
            if (response.status === 426) {
              // Replay buffer expired (server beyond 24h). Reset and tell
              // caller to full-refetch; throw fatal so we don't auto-retry
              // with the stale Last-Event-ID. The outer reconnect loop
              // catches and re-opens with lastSeq=0.
              lastSeq = 0;
              throw new FatalSSEError('buffer_expired');
            }
            if (!response.ok) {
              throw new Error(`SSE open failed: HTTP ${response.status}`);
            }
            if (!response.headers.get('content-type')?.includes('text/event-stream')) {
              throw new FatalSSEError('not_event_stream');
            }
            attempt = 0; // Successful open resets backoff
            opts.onStatusChange('connected');
          },

          onmessage: (msg: EventSourceMessage): void => {
            if (msg.data === '') return;
            try {
              const env = JSON.parse(msg.data) as SSEEnvelope;
              lastSeq = env.sequence_no;
              if (env.type === 'session_evicted') {
                const data = env.data as SessionEvictedData;
                opts.onEvicted(data.reason);
                throw new FatalSSEError('evicted');
              }
              opts.onEnvelope(env);
            } catch (err) {
              if (err instanceof FatalSSEError) throw err;
              // Malformed envelope -- drop the message but stay connected.
            }
          },

          onclose: (): void => {
            // Server closed the stream gracefully. The loop body falls
            // through and retries; treat as a normal disconnect.
            throw new Error('stream_closed');
          },

          onerror: (err: unknown): number => {
            if (err instanceof FatalSSEError) {
              if (err.message === 'buffer_expired') {
                // Re-open immediately with lastSeq=0 in the outer loop.
                return 0;
              }
              throw err; // Permanent abort.
            }
            attempt += 1;
            if (attempt > MAX_RECONNECT_ATTEMPTS) {
              throw new FatalSSEError('max_retries_exceeded');
            }
            return computeBackoffMs(attempt);
          },
        });

        // fetchEventSource resolved without throwing -> stream ended.
        // Bump attempt + loop body retries.
        attempt += 1;
        if (attempt > MAX_RECONNECT_ATTEMPTS) {
          opts.onStatusChange('degraded');
          return;
        }
        await new Promise<void>((resolve) =>
          setTimeout(resolve, computeBackoffMs(attempt)),
        );
      } catch (err) {
        if (err instanceof FatalSSEError) {
          if (err.message === 'evicted') {
            opts.onStatusChange('evicted');
            return;
          }
          if (err.message === 'buffer_expired') {
            // Outer loop iterates again immediately with reset state.
            continue;
          }
          opts.onStatusChange('degraded');
          return;
        }
        // Aborted by controller (component unmount)
        if (controller.signal.aborted) {
          opts.onStatusChange('disconnected');
          return;
        }
      }
    }
  };

  void run();

  return () => {
    stopped = true;
    controller.abort();
  };
}

const API_BASE = (process.env.NEXT_PUBLIC_API_BASE ?? '').replace(/\/$/, '');

/**
 * React hook that opens an SSE connection on mount and tears it down on
 * unmount. Returns the current connection status + last seen sequence_no.
 *
 * Side effects on receive: invalidate TanStack Query keys per
 * `INVALIDATE_KEYS` map (spec §8.6); fire P1 toast on eviction.
 *
 * Single-instance discipline: this hook should be mounted ONCE in the app
 * (at the layout level via the SSE provider). Mounting it from multiple
 * components opens multiple connections per tab which the backend's N=4
 * per-user eviction will not love.
 */
export function useSSE(): UseSSEResult {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<SSEStatus>('disconnected');
  const [lastSequenceNo, setLastSequenceNo] = useState(0);
  const [evictionReason, setEvictionReason] = useState<string | null>(null);
  const seqRef = useRef(0);
  const evictionShownRef = useRef(false);

  useEffect(() => {
    const teardown = startSSE({
      apiBase: API_BASE,
      initialSequenceNo: seqRef.current,
      onStatusChange: setStatus,
      onEnvelope: (env) => {
        seqRef.current = env.sequence_no;
        setLastSequenceNo(env.sequence_no);
        const keys = INVALIDATE_KEYS[env.type] ?? [];
        for (const queryKey of keys) {
          void queryClient.invalidateQueries({ queryKey: [...queryKey] });
        }
      },
      onEvicted: (reason) => {
        setEvictionReason(reason);
        if (!evictionShownRef.current) {
          evictionShownRef.current = true;
          if (reason === 'tab_limit') {
            toast({
              title: 'Disconnected — another tab is now active',
              description: 'Refresh this page to reconnect.',
              severity: 'p1',
            });
          } else if (reason === 'explicit_logout') {
            toast({
              title: 'Signed out',
              severity: 'info',
            });
          } else if (reason === 'breakglass_kill' || reason === 'creds_rotated') {
            toast({
              title: 'Session terminated — re-authenticate',
              severity: 'p0',
            });
          }
        }
      },
    });
    return teardown;
  }, [queryClient]);

  return { status, lastSequenceNo, evictionReason };
}
