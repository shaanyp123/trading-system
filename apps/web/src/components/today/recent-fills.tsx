'use client';

/**
 * Recent Fills Feed -- frontend-spec §2.2.2 F.
 *
 * Per-row: time (ET) | market | qty | price | realized P&L. Phase 0 returns
 * fills=[] so the empty state renders.
 *
 * Spec §2.2.2 F: "ARIA: new fill → aria-live=polite announces 'Fill:
 * BIP-20DEC30-CDE +1 at 64,120.00'". Phase 1 SSE-driven announcements layer
 * on once the `<LiveRegion>` component lands. Day 22 ships the static
 * rendering only.
 *
 * Stale threshold: 10s (spec §2.2.2 F; crypto trades 24/7 so the
 * session/off-session split is gone). The query's 10s staleTime in
 * queries.ts matches; the stale yellow-corner-badge UX lands when the SSE
 * connection-status pill wires that into per-card badges (Day 23+).
 */

import { useRecentFills } from '@/lib/api/queries';
import { formatPrice, formatTimeETShort } from '@/lib/format';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

export function RecentFills(): JSX.Element {
  const { data, isLoading, isError } = useRecentFills();
  const fills = data?.fills ?? [];

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">
          Recent Fills <span className="text-text-muted">({fills.length})</span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading && <p className="text-sm text-text-muted">Loading…</p>}
        {isError && (
          <p className="text-sm text-severity-p1">Failed to load fills.</p>
        )}
        {!isLoading && !isError && fills.length === 0 && (
          <p className="text-sm text-text-muted">
            No fills yet. Live executions will appear here.
          </p>
        )}
        {fills.length > 0 && (
          <ul className="space-y-1 text-sm" aria-live="polite" aria-atomic="false">
            {fills.map((f) => (
              <li
                key={f.fill_uuid}
                className="grid grid-cols-[auto_1fr_auto_auto] items-center gap-3 font-mono tabular-nums"
              >
                <span className="text-text-muted">{formatTimeETShort(f.filled_at)}</span>
                <span>{f.instrument_id}</span>
                <span>
                  {f.side === 'buy' ? '+' : '-'}
                  {f.qty}
                </span>
                <span>@ {formatPrice(f.price)}</span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
