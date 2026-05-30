'use client';

/**
 * P&L Summary -- frontend-spec §2.2.2 B.
 *
 * 4-column grid: Day / Week / Month / Year P&L. Reads from `/api/today/digest`.
 * Period boundary anchoring is server-side (`day = 17:00 ET to 17:00 ET` etc.
 * per spec §2.2.2 B). Frontend just renders the pre-computed strings.
 *
 * IMPORTANT (2026-05-30): the digest's `pnl.*` periods are REALIZED-only
 * (services/api/repos/phase1.py::fetch_realized_pnl_aggregates, by design) --
 * Day reads $0 whenever nothing closed today. The Positions table shows live
 * UNREALIZED P&L on open positions, so the two never reconcile and the card
 * looked broken (operator report). Fix: the period columns are explicitly
 * labelled "Realized", and an "Unrealized (open)" line sums the open positions'
 * unrealized_pnl client-side (same `usePositionsCurrent` source the Positions
 * table renders -- no API change). Together they tell the whole story: closed
 * P&L by period + live mark-to-market on what's still open.
 */

import { usePositionsCurrent, useTodayDigest } from '@/lib/api/queries';
import { formatPnL, pnlSignClass } from '@/lib/format';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

interface Cell {
  readonly label: string;
  readonly value: string;
}

/**
 * Sum the open positions' unrealized P&L Decimal-strings into one
 * Decimal-string. Returns null when positions haven't loaded (so the row can
 * render a neutral placeholder rather than a misleading "$0.00").
 *
 * Values are summed via Number() to match the codebase's display-tier money
 * convention (see lib/format.ts -- formatPnL already coerces Decimal-strings
 * with Number()). The Decimal source-of-truth stays server-side; this is a
 * presentation-only roll-up at ~2dp, well within float precision at account
 * scale.
 */
function sumUnrealized(
  positions: readonly { readonly unrealized_pnl: string }[] | undefined,
): string | null {
  if (positions === undefined) return null;
  const total = positions.reduce((acc, p) => {
    const n = Number(p.unrealized_pnl);
    return Number.isNaN(n) ? acc : acc + n;
  }, 0);
  return total.toFixed(2);
}

export function PnLSummary(): JSX.Element {
  const { data, isLoading, isError } = useTodayDigest();
  const { data: positionsData } = usePositionsCurrent();

  const cells: readonly Cell[] = data
    ? [
        { label: 'Day', value: data.pnl.daily_pnl },
        { label: 'Week', value: data.pnl.weekly_pnl },
        { label: 'Month', value: data.pnl.monthly_pnl },
        { label: 'Year', value: data.pnl.yearly_pnl },
      ]
    : [];

  const unrealized = sumUnrealized(positionsData?.positions);

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
          <>
            <p className="mb-1 text-xs uppercase tracking-wide text-text-muted">
              Realized
            </p>
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
            <dl className="mt-4 flex items-baseline justify-between border-t border-border pt-3">
              <dt className="text-xs uppercase tracking-wide text-text-muted">
                Unrealized (open)
              </dt>
              <dd
                className={`font-mono text-lg tabular-nums ${
                  pnlSignClass(unrealized) ?? ''
                }`}
              >
                {unrealized === null ? '—' : formatPnL(unrealized)}
              </dd>
            </dl>
          </>
        )}
      </CardContent>
    </Card>
  );
}
