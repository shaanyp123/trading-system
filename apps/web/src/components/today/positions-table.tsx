'use client';

/**
 * Positions Table -- frontend-spec §2.2.2 E.
 *
 * Columns: Market | Qty | Avg Cost | Mark | uPnL | Cluster | Strategy Version.
 * Phase 0 returns positions=[] so the table renders the empty state.
 *
 * Virtualization (TanStack Table + react-virtual) kicks in at >50 rows per
 * spec §2.2.2 E -- deferred since Phase 0 never exceeds 50. When live data
 * lands the virtualization wrapper layers on without changing the cell
 * rendering logic.
 *
 * Row click -> `/trades/:id` per spec; Day 22 just renders the row, no
 * navigation. The `/trades/:id` page is itself a Day 20 placeholder until
 * Week 7 Tue.
 */

import { usePositionsCurrent } from '@/lib/api/queries';
import { formatPrice, formatPnL, pnlSignClass } from '@/lib/format';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

export function PositionsTable(): JSX.Element {
  const { data, isLoading, isError } = usePositionsCurrent();
  const positions = data?.positions ?? [];

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">
          Positions <span className="text-text-muted">({positions.length})</span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading && <p className="text-sm text-text-muted">Loading…</p>}
        {isError && (
          <p className="text-sm text-severity-p1">
            Failed to load positions.
          </p>
        )}
        {!isLoading && !isError && positions.length === 0 && (
          <p className="text-sm text-text-muted">
            No open positions. Filled trades will appear here.
          </p>
        )}
        {positions.length > 0 && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Market</TableHead>
                <TableHead className="text-right">Qty</TableHead>
                <TableHead className="text-right">Avg Cost</TableHead>
                <TableHead className="text-right">Mark</TableHead>
                <TableHead className="text-right">uPnL</TableHead>
                <TableHead>Cluster</TableHead>
                <TableHead>Strategy</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {positions.map((p) => (
                <TableRow key={p.instrument_id}>
                  <TableCell className="font-mono">{p.symbol}</TableCell>
                  <TableCell className="text-right font-mono tabular-nums">
                    {p.qty}
                  </TableCell>
                  <TableCell className="text-right font-mono tabular-nums">
                    {formatPrice(p.avg_entry_price)}
                  </TableCell>
                  <TableCell className="text-right font-mono tabular-nums">
                    {formatPrice(p.current_price)}
                  </TableCell>
                  <TableCell
                    className={`text-right font-mono tabular-nums ${
                      pnlSignClass(p.unrealized_pnl) ?? ''
                    }`}
                  >
                    {formatPnL(p.unrealized_pnl)}
                  </TableCell>
                  <TableCell className="text-text-secondary">
                    {p.cluster ?? '—'}
                  </TableCell>
                  <TableCell className="font-mono text-text-muted">
                    {p.managed_by_strategy_version}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
