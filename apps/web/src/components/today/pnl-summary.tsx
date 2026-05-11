'use client';

/**
 * P&L Summary -- frontend-spec §2.2.2 B.
 *
 * 4-column grid: Day / Week / Month / Year net P&L. Phase 0 reads from
 * `/api/today/digest` (zero values) -- when live data lands the digest
 * endpoint denormalizes the same shape so the component has zero changes
 * to make. The spec's full 4x2 grid (Net Liq + $-vs-Bench rows) lands Phase
 * 1 once the equity-curve endpoints exist; Day 22 ships the net P&L row.
 *
 * Period boundary anchoring is server-side (`day = 17:00 ET to 17:00 ET`
 * etc. per spec §2.2.2 B). Frontend just renders the pre-computed strings.
 */

import { useTodayDigest } from '@/lib/api/queries';
import { formatPnL, pnlSignClass } from '@/lib/format';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

interface Cell {
  readonly label: string;
  readonly value: string;
}

export function PnLSummary(): JSX.Element {
  const { data, isLoading, isError } = useTodayDigest();

  const cells: readonly Cell[] = data
    ? [
        { label: 'Day', value: data.pnl.daily_pnl },
        { label: 'Week', value: data.pnl.weekly_pnl },
        { label: 'Month', value: data.pnl.monthly_pnl },
        { label: 'Year', value: data.pnl.yearly_pnl },
      ]
    : [];

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">P&amp;L Summary</CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading && <p className="text-sm text-text-muted">Loading…</p>}
        {isError && (
          <p className="text-sm text-severity-p1">Failed to load P&amp;L.</p>
        )}
        {data !== undefined && (
          <dl className="grid grid-cols-4 gap-3">
            {cells.map((c) => (
              <div key={c.label}>
                <dt className="text-xs uppercase tracking-wide text-text-muted">
                  {c.label}
                </dt>
                <dd
                  className={`mt-1 font-mono text-lg tabular-nums ${
                    pnlSignClass(c.value) ?? ''
                  }`}
                >
                  {formatPnL(c.value)}
                </dd>
              </div>
            ))}
          </dl>
        )}
      </CardContent>
    </Card>
  );
}
