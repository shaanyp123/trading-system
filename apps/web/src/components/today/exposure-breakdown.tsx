'use client';

/**
 * Exposure Breakdown -- frontend-spec §2.2.2 D.
 *
 * Horizontal bar groups (one per ring/cluster). Each bar = filled portion to
 * current % + gray remainder to limit + vertical tick at limit. Phase 0
 * payload has all-zero clusters + zero gross/net so the bars all render at
 * 0% width, which is the correct empty-state visual.
 *
 * Per spec §2.2.2 D: "simple HTML/CSS bars (no chart library on Today --
 * bundle budget); reserve Recharts for /performance".
 *
 * Cluster limits below are placeholders mirroring the spec example layout.
 * The real limits come from the risk envelope (backend-spec §2.7) which the
 * `/api/system/risk-envelope` endpoint exposes -- a Phase 1 wiring.
 */

import { useTodayDigest } from '@/lib/api/queries';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import type { ExposureCluster } from '@/lib/api/types';

interface BarRow {
  readonly label: string;
  readonly current: number;
  readonly limit: number;
}

// Limits from frontend-spec §2.2.1 layout example. Real limits land Phase 1
// when /api/system/risk-envelope is wired.
const CLUSTER_LIMITS: Readonly<Record<ExposureCluster, number>> = {
  equity_index: 60,
  rates_bonds: 80,
  commodity: 80,
  crypto: 40,
  fx: 60,
};

const CLUSTER_LABEL: Readonly<Record<ExposureCluster, string>> = {
  equity_index: 'Equity Idx',
  rates_bonds: 'Rates/Bonds',
  commodity: 'Commodity',
  crypto: 'Crypto',
  fx: 'FX',
};

export function ExposureBreakdown(): JSX.Element {
  const { data, isLoading, isError } = useTodayDigest();

  const rows: readonly BarRow[] = data
    ? [
        {
          label: 'Gross',
          current: Number(data.exposure.gross_exposure_pct_nav),
          limit: 300,
        },
        {
          label: 'Net',
          current: Number(data.exposure.net_exposure_pct_nav),
          limit: 150,
        },
        ...(Object.keys(CLUSTER_LIMITS) as readonly ExposureCluster[]).map(
          (cluster) => ({
            label: CLUSTER_LABEL[cluster],
            current: Number(data.exposure.by_cluster[cluster] ?? '0'),
            limit: CLUSTER_LIMITS[cluster],
          }),
        ),
      ]
    : [];

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">Exposure</CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading && <p className="text-sm text-text-muted">Loading…</p>}
        {isError && (
          <p className="text-sm text-severity-p1">Failed to load exposure.</p>
        )}
        {data !== undefined && (
          <ul className="space-y-2">
            {rows.map((r) => (
              <ExposureBar key={r.label} row={r} />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function ExposureBar({ row }: { row: BarRow }): JSX.Element {
  const filledPct = row.limit > 0 ? (Math.abs(row.current) / row.limit) * 100 : 0;
  const headroomPct = Math.max(0, 100 - filledPct);
  let color = 'bg-pnl-positive';
  if (headroomPct < 25) color = 'bg-severity-p0';
  else if (headroomPct < 50) color = 'bg-severity-p1';

  return (
    <li>
      <div className="flex justify-between text-xs">
        <span className="text-text-secondary">{row.label}</span>
        <span className="font-mono tabular-nums text-text-muted">
          {row.current.toFixed(0)}% / {row.limit}%
        </span>
      </div>
      <div
        className="mt-1 h-2 w-full overflow-hidden rounded-full bg-bg-elevated"
        role="progressbar"
        aria-label={`${row.label} exposure`}
        aria-valuenow={Math.round(filledPct)}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div
          className={`h-full ${color}`}
          style={{ width: `${Math.min(100, filledPct)}%` }}
        />
      </div>
    </li>
  );
}
