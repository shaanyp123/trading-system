'use client';

/**
 * SSE connection-state pill.
 *
 * Renders a compact status indicator in the page header reflecting the
 * `useSSE` hook's connection lifecycle. Maps to the colors operators expect
 * from frontend-spec §1.6 (top-bar binding table) + §4.5 (reconnect strategy).
 *
 * Day 22 doesn't yet have the real TopBar (Week 7 task) -- this pill is the
 * single visible health indicator for the SSE connection on the Today page.
 */

import type { SSEStatus } from '@/lib/sse';

const STATUS_BG: Readonly<Record<SSEStatus, string>> = {
  connecting: 'bg-text-muted',
  connected: 'bg-pnl-positive',
  reconnecting: 'bg-severity-p1',
  degraded: 'bg-severity-p1',
  evicted: 'bg-severity-p0',
  disconnected: 'bg-text-muted',
};

const STATUS_LABEL: Readonly<Record<SSEStatus, string>> = {
  connecting: 'Connecting',
  connected: 'Live',
  reconnecting: 'Reconnecting',
  degraded: 'Degraded',
  evicted: 'Evicted',
  disconnected: 'Offline',
};

interface Props {
  readonly status: SSEStatus;
  readonly lastSequenceNo: number;
}

export function SSEStatusPill({ status, lastSequenceNo }: Props): JSX.Element {
  return (
    <span
      className="inline-flex items-center gap-2 rounded-full border border-border bg-bg-surface px-3 py-1 text-xs"
      role="status"
      aria-label={`SSE connection ${STATUS_LABEL[status]}`}
    >
      <span
        className={`inline-block h-2 w-2 rounded-full ${STATUS_BG[status]}`}
        aria-hidden="true"
      />
      <span>{STATUS_LABEL[status]}</span>
      {status === 'connected' && lastSequenceNo > 0 && (
        <span className="font-mono text-text-muted">#{lastSequenceNo}</span>
      )}
    </span>
  );
}
