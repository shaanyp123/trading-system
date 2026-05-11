'use client';

/**
 * Audit-log explorer table -- frontend-spec §2.6.4.
 *
 * Phase 1 columns: seq# | event_type | env | source_clock_ts (ET) |
 * payload_preview. Filter bar: event_type text + env select + date
 * window. Cursor pagination via "Load more".
 *
 * Virtualization deferred: audit_log volume in Phase 0 is 1-10 rows; once
 * Phase 1 fill activity drives volume past ~50 rows the spec calls for
 * `@tanstack/react-virtual`. Day 27 defers that until the volume
 * justifies it.
 *
 * Phase 2 extensions per spec §2.6.4:
 *   - FTS on reason field
 *   - actor filter (operator/agent/system)
 *   - hash-validity badge (green ✓ / amber repaired / red ✗)
 *   - row click → /system/audit/:event_uuid detail page
 *
 * Day 27 ships the read-only table; row click + detail page lands when
 * `/system/audit/:event_uuid` route gets built (Phase 1+).
 */

import { useMemo, useState } from 'react';

import { useAuditLogPage } from '@/lib/api/queries';
import type { AuditLogEntry, AuditLogFilters } from '@/lib/api/types';
import { formatDateTimeET } from '@/lib/format';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

const PAGE_SIZE = 50;

type EnvFilter = 'paper' | 'live-small' | 'live-scale' | '';

export function AuditLogTable(): JSX.Element {
  const [eventType, setEventType] = useState('');
  const [env, setEnv] = useState<EnvFilter>('');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [cursor, setCursor] = useState<string | null>(null);
  // accumulated pages for "Load more" (continuation in DESC seq order)
  const [accumulator, setAccumulator] = useState<readonly AuditLogEntry[]>([]);

  const filters = useMemo<AuditLogFilters>(
    () => ({
      limit: PAGE_SIZE,
      ...(eventType.trim() !== '' ? { event_type: eventType.trim() } : {}),
      ...(env !== '' ? { env } : {}),
      ...(from !== '' ? { from: `${from}T00:00:00Z` } : {}),
      ...(to !== '' ? { to: `${to}T23:59:59Z` } : {}),
      ...(cursor !== null ? { cursor } : {}),
    }),
    [eventType, env, from, to, cursor],
  );

  const { data, isLoading, isError, isFetching } = useAuditLogPage(filters);

  const handleFilterChange = (apply: () => void): void => {
    setAccumulator([]);
    setCursor(null);
    apply();
  };

  const handleClear = (): void => {
    setEventType('');
    setEnv('');
    setFrom('');
    setTo('');
    setCursor(null);
    setAccumulator([]);
  };

  const handleLoadMore = (): void => {
    if (data === undefined) return;
    setAccumulator((prev) => [...prev, ...data.entries]);
    if (data.next_cursor !== null) {
      setCursor(data.next_cursor);
    }
  };

  // Rows shown = previously-loaded accumulator + current page. When the
  // filters change we reset accumulator + cursor in handleFilterChange so
  // the user sees a clean page-1.
  const rows = cursor === null ? data?.entries ?? [] : [...accumulator, ...(data?.entries ?? [])];

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">Audit Log</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-1 gap-3 md:grid-cols-5">
          <label className="text-sm">
            <span className="block text-text-muted text-xs mb-1">Event type</span>
            <input
              type="text"
              value={eventType}
              onChange={(e) => handleFilterChange(() => setEventType(e.target.value))}
              placeholder="e.g. state_transition_normal_to_halt"
              className="w-full rounded-md border border-border bg-bg-elevated px-2 py-1 font-mono text-xs"
            />
          </label>
          <label className="text-sm">
            <span className="block text-text-muted text-xs mb-1">Env</span>
            <select
              value={env}
              onChange={(e) =>
                handleFilterChange(() => setEnv(e.target.value as EnvFilter))
              }
              className="w-full rounded-md border border-border bg-bg-elevated px-2 py-1 text-xs"
            >
              <option value="">All</option>
              <option value="paper">paper</option>
              <option value="live-small">live-small</option>
              <option value="live-scale">live-scale</option>
            </select>
          </label>
          <label className="text-sm">
            <span className="block text-text-muted text-xs mb-1">From</span>
            <input
              type="date"
              value={from}
              onChange={(e) => handleFilterChange(() => setFrom(e.target.value))}
              className="w-full rounded-md border border-border bg-bg-elevated px-2 py-1 text-xs"
            />
          </label>
          <label className="text-sm">
            <span className="block text-text-muted text-xs mb-1">To</span>
            <input
              type="date"
              value={to}
              onChange={(e) => handleFilterChange(() => setTo(e.target.value))}
              className="w-full rounded-md border border-border bg-bg-elevated px-2 py-1 text-xs"
            />
          </label>
          <div className="flex items-end">
            <Button variant="outline" size="sm" onClick={handleClear}>
              Clear
            </Button>
          </div>
        </div>

        {isLoading && cursor === null && (
          <p className="text-sm text-text-muted">Loading…</p>
        )}
        {isError && (
          <p className="text-sm text-severity-p1">Failed to load audit log.</p>
        )}
        {!isLoading && !isError && rows.length === 0 && (
          <p className="text-sm text-text-muted">
            No audit events match the current filters. Phase 0 has one row from
            the Day 25 carryover state_transition_normal_to_halt write.
          </p>
        )}
        {rows.length > 0 && (
          <>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-16">Seq</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead className="w-24">Env</TableHead>
                  <TableHead>Source clock (ET)</TableHead>
                  <TableHead>Payload preview</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((row) => (
                  <TableRow key={row.event_uuid}>
                    <TableCell className="font-mono tabular-nums">
                      {row.sequence_no}
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {row.event_type}
                    </TableCell>
                    <TableCell>
                      <span
                        className={`rounded-md px-2 py-0.5 text-xs ${
                          row.env === 'paper'
                            ? 'bg-env-paper/20 text-env-paper'
                            : row.env === 'live-small'
                              ? 'bg-env-live-small/20 text-env-live-small'
                              : 'bg-env-live-scale/20 text-env-live-scale'
                        }`}
                      >
                        {row.env}
                      </span>
                    </TableCell>
                    <TableCell className="font-mono tabular-nums text-xs">
                      {formatDateTimeET(row.source_clock_ts)}
                    </TableCell>
                    <TableCell className="font-mono text-xs text-text-secondary">
                      {row.payload_preview}
                      {row.payload_preview.length === 80 ? '…' : ''}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {data?.has_more === true && (
              <div className="flex justify-center pt-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={isFetching}
                  onClick={handleLoadMore}
                >
                  {isFetching ? 'Loading…' : 'Load more'}
                </Button>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
