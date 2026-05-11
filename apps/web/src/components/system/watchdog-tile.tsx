'use client';

/**
 * Watchdog tile -- frontend-spec §2.6.6.
 *
 * Renders the last-ping timestamp + age + health pill. Phase 0:
 * liveness_probes is empty until the external watchdog VPS starts posting,
 * so `has_ever_pinged=false` paints the "Watchdog not yet wired" state.
 *
 * Pill thresholds per spec §2.6.6:
 *   - healthy   (green):  last_ping < 10 min
 *   - stale     (amber):  10-30 min during CME session
 *   - unhealthy (red):    > 30 min during CME session
 *
 * Phase 0 has no CME-session signal wired, so the pill uses absolute age
 * thresholds. The full session-aware logic lands when SystemStatus.is_session_active
 * gets a real implementation Week 5 Tue+.
 */

import { useWatchdogStatus } from '@/lib/api/queries';
import { formatDateTimeET } from '@/lib/format';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

const STALE_THRESHOLD_S = 10 * 60;
const UNHEALTHY_THRESHOLD_S = 30 * 60;

function classifyAge(seconds: number | null): {
  label: string;
  className: string;
} {
  if (seconds === null) {
    return { label: 'No data', className: 'bg-text-muted/40 text-text-muted' };
  }
  if (seconds <= STALE_THRESHOLD_S) {
    return { label: 'Healthy', className: 'bg-health-green/20 text-health-green' };
  }
  if (seconds <= UNHEALTHY_THRESHOLD_S) {
    return { label: 'Stale', className: 'bg-stale/20 text-stale' };
  }
  return { label: 'Unhealthy', className: 'bg-health-red/20 text-health-red' };
}

function formatAge(seconds: number | null): string {
  if (seconds === null) return '—';
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86_400)}d ago`;
}

export function WatchdogTile(): JSX.Element {
  const { data, isLoading, isError } = useWatchdogStatus();

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">Watchdog</CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading && <p className="text-sm text-text-muted">Loading…</p>}
        {isError && (
          <p className="text-sm text-severity-p1">Failed to load watchdog status.</p>
        )}
        {!isLoading && !isError && data && (
          <WatchdogBody status={data} />
        )}
      </CardContent>
    </Card>
  );
}

function WatchdogBody({
  status,
}: {
  status: NonNullable<ReturnType<typeof useWatchdogStatus>['data']>;
}): JSX.Element {
  if (!status.has_ever_pinged) {
    return (
      <div className="space-y-2">
        <span className="inline-flex items-center gap-2 rounded-full bg-text-muted/20 px-3 py-1 text-xs text-text-muted">
          <span className="inline-block h-2 w-2 rounded-full bg-text-muted" />
          Not yet wired
        </span>
        <p className="text-sm text-text-muted">
          Watchdog has not pinged yet. External watchdog VPS posts to{' '}
          <code className="text-text-secondary">/api/internal/watchdog</code> on a
          5-min cadence.
        </p>
      </div>
    );
  }
  const pill = classifyAge(status.seconds_since_last_ping);
  return (
    <div className="space-y-3">
      <span
        className={`inline-flex items-center gap-2 rounded-full px-3 py-1 text-xs ${pill.className}`}
        role="status"
      >
        <span className="inline-block h-2 w-2 rounded-full bg-current" />
        {pill.label}
      </span>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
        <span className="text-text-muted">Last ping</span>
        <span className="font-mono tabular-nums">
          {formatDateTimeET(status.last_ping_utc)}
        </span>
        <span className="text-text-muted">Age</span>
        <span className="font-mono tabular-nums">
          {formatAge(status.seconds_since_last_ping)}
        </span>
        {status.watchdog_id !== null && (
          <>
            <span className="text-text-muted">Watchdog ID</span>
            <span className="font-mono text-xs">{status.watchdog_id}</span>
          </>
        )}
        {status.region !== null && (
          <>
            <span className="text-text-muted">Region</span>
            <span>{status.region}</span>
          </>
        )}
        {status.consecutive_failures_observed !== null && (
          <>
            <span className="text-text-muted">Consecutive failures</span>
            <span className="font-mono tabular-nums">
              {status.consecutive_failures_observed}
            </span>
          </>
        )}
      </div>
    </div>
  );
}
