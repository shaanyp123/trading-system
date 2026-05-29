'use client';

/**
 * Reconciliation tile -- frontend-spec §2.6.5.
 *
 * Reads `/api/system/status.reconciliation_summary`. `last_check_utc` is the
 * latest EOD-recon balance snapshot, so a passing cycle (which writes no break
 * row) still advances it; the epoch sentinel renders "No checks yet" only
 * before recon has ever run.
 *
 * `open_breaks` surfaces the red "requires operator review" banner. The link
 * to break detail lands when the per-break detail surface is built (Phase 1+).
 */

import { useSystemStatus } from '@/lib/api/queries';
import { formatDateTimeET } from '@/lib/format';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

export function ReconciliationTile(): JSX.Element {
  const { data, isLoading, isError } = useSystemStatus();

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">Reconciliation</CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading && <p className="text-sm text-text-muted">Loading…</p>}
        {isError && (
          <p className="text-sm text-severity-p1">
            Failed to load reconciliation status.
          </p>
        )}
        {!isLoading && !isError && data && (
          <ReconciliationBody summary={data.reconciliation_summary} />
        )}
      </CardContent>
    </Card>
  );
}

function ReconciliationBody({
  summary,
}: {
  summary: {
    readonly last_check_utc: string;
    readonly last_check_passed: boolean;
    readonly open_breaks: number;
    readonly breaks_24h: number;
  };
}): JSX.Element {
  const isEpoch = summary.last_check_utc.startsWith('1970-01-01');
  const hasOpenBreaks = summary.open_breaks > 0;

  return (
    <div className="space-y-3">
      {hasOpenBreaks ? (
        <div className="rounded-md border border-health-red/40 bg-health-red/10 px-3 py-2 text-sm text-health-red">
          {summary.open_breaks} open break{summary.open_breaks === 1 ? '' : 's'}
          {' — requires operator review'}
        </div>
      ) : (
        <span
          className={`inline-flex items-center gap-2 rounded-full px-3 py-1 text-xs ${
            summary.last_check_passed
              ? 'bg-health-green/20 text-health-green'
              : 'bg-stale/20 text-stale'
          }`}
        >
          <span className="inline-block h-2 w-2 rounded-full bg-current" />
          {summary.last_check_passed ? 'Passing' : 'No data'}
        </span>
      )}
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
        <span className="text-text-muted">Last check</span>
        <span className="font-mono tabular-nums">
          {isEpoch ? 'No checks yet' : formatDateTimeET(summary.last_check_utc)}
        </span>
        <span className="text-text-muted">Open breaks</span>
        <span className="font-mono tabular-nums">{summary.open_breaks}</span>
        <span className="text-text-muted">Breaks (24h)</span>
        <span className="font-mono tabular-nums">{summary.breaks_24h}</span>
        <span className="text-text-muted">Source</span>
        <span>
          <span className="rounded-md bg-env-paper/20 px-2 py-0.5 text-xs text-env-paper">
            IBKR FlexQuery
          </span>
        </span>
      </div>
    </div>
  );
}
