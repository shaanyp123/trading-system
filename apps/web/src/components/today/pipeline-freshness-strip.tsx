'use client';

/**
 * Pipeline Freshness Strip — "did the backend run today?" at a glance.
 *
 * Shows the three daily cycle timestamps in order — bar_sync (price date) →
 * LEAN cycle (signals + exits) → EOD recon (balances) — each with a coloured
 * dot: green = ran recently, amber = stale (didn't run in the last cycle),
 * grey = unknown / no data yet.
 *
 * Assembled purely from data the Today page already fetches — no new endpoint:
 *   - bar_sync  ← positions `prices_as_of` (the latest synced bar's date)
 *   - LEAN      ← exit-proximity `as_of_cycle_ts_utc` (the strategy cycle ts)
 *   - recon     ← system-status `reconciliation_summary.last_check_utc`
 * "now" is the server clock (`system_status.server_now`), so freshness is
 * judged against backend time, not the browser's.
 */

import { useExitProximity } from '@/lib/api/exit-proximity';
import { usePositionsCurrent, useSystemStatus } from '@/lib/api/queries';
import { formatDateTimeET, isWithinHours } from '@/lib/format';

type Fresh = boolean | null;

function dotClass(fresh: Fresh): string {
  if (fresh == null) return 'bg-text-muted';
  return fresh ? 'bg-health-green' : 'bg-health-yellow';
}

function FreshnessItem({
  label,
  value,
  fresh,
  title,
}: {
  readonly label: string;
  readonly value: string;
  readonly fresh: Fresh;
  readonly title: string;
}): JSX.Element {
  return (
    <div className="flex items-center gap-1.5" title={title}>
      <span className={`inline-block h-2 w-2 shrink-0 rounded-full ${dotClass(fresh)}`} aria-hidden />
      <span className="text-text-muted">{label}</span>
      <span className="font-mono tabular-nums text-text-primary">{value}</span>
    </div>
  );
}

export function PipelineFreshnessStrip(): JSX.Element {
  const { data: positions } = usePositionsCurrent();
  const { data: exitProximity } = useExitProximity();
  const { data: systemStatus } = useSystemStatus();

  const now = systemStatus?.server_now ?? null;

  // bar_sync: prices_as_of is a YYYY-MM-DD date; anchor it at ~21:00 UTC (the
  // bar_sync fire time) to judge recency. Wider window (50h) tolerates a
  // Monday viewing Friday's bar over a weekend gap.
  const barDate = positions?.prices_as_of ?? null;
  const barTs = barDate != null ? `${barDate}T21:00:00Z` : null;

  const leanTs = exitProximity?.as_of_cycle_ts_utc ?? null;
  const reconTs = systemStatus?.reconciliation_summary?.last_check_utc ?? null;

  return (
    <div className="mb-4 flex flex-wrap items-center gap-x-6 gap-y-2 rounded-md border border-border bg-bg-surface px-4 py-2 text-xs">
      <span className="font-mono uppercase tracking-wide text-text-muted">Today’s pipeline</span>
      <FreshnessItem
        label="Bars"
        value={barDate ?? '—'}
        fresh={isWithinHours(barTs, 50, now)}
        title="bar_sync — session date of the latest synced bars (drives the marks)"
      />
      <span className="text-text-muted" aria-hidden>
        →
      </span>
      <FreshnessItem
        label="LEAN cycle"
        value={formatDateTimeET(leanTs)}
        fresh={isWithinHours(leanTs, 30, now)}
        title="The daily strategy cycle — emits signals + evaluates exits"
      />
      <span className="text-text-muted" aria-hidden>
        →
      </span>
      <FreshnessItem
        label="EOD recon"
        value={formatDateTimeET(reconTs)}
        fresh={isWithinHours(reconTs, 30, now)}
        title="Reconciliation — positions/balances checked against the broker"
      />
    </div>
  );
}
