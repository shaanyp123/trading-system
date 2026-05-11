'use client';

/**
 * `/trades` -- Trades summary table per frontend-spec §2.3.1.
 *
 * Phase 1 surface: filterable summary table (Date / Market / Dir / Size /
 * Status / Entry / Exit / P&L / Vers) + CSV export. Phase 2 will add the
 * drawer-preferred detail view + multi-select market filter populated from
 * `instrument_metadata` + regime filter + search.
 *
 * Filters wire to backend-spec §4.1.2 query params: `from`, `to`, `market`,
 * `state`, `id_prefix`. Date inputs are interpreted as UTC midnight bounds
 * — Phase 1+ may re-anchor to America/New_York wall-clock if the CME-session
 * boundary feels intuitive enough; for now UTC matches the on-the-wire shape
 * of `opened_at_utc`.
 *
 * CSV export: client-side blob. Phase-0 emits a 14-column subset of the
 * frontend-spec §15.1 LOCKED schema (the columns derivable from
 * `TradeSummary` rows the API ships today). The full LOCKED schema (with
 * audit-chain footer for tamper evidence) lands once
 * `/api/trades/export.csv` ships, which depends on the trades table
 * actually having rows + an `audit_chain_anchor_hash` to anchor against.
 * Phase 0 has 0 rows so the export is one header line; once trades populate
 * (Week 7 Thu paper round-trip), the rows render and the client-side blob
 * remains useful until the server endpoint lands.
 *
 * Empty state: per frontend-spec §2.3.5, "Will populate once first signal
 * fills" (Phase 1 day 1) is the locked copy.
 */

import Link from 'next/link';
import { useMemo, useState } from 'react';

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
import { useTradesPage } from '@/lib/api/queries';
import { formatDateTimeET, formatPnL, formatPrice, pnlSignClass } from '@/lib/format';
import type { Trade, TradeState, TradesQueryFilters } from '@/lib/api/types';

const STATE_OPTIONS: ReadonlyArray<{ value: TradeState | ''; label: string }> = [
  { value: '', label: 'All states' },
  { value: 'open_position', label: 'open_position' },
  { value: 'closed', label: 'closed' },
  { value: 'stopped_out', label: 'stopped_out' },
  { value: 'capacity_constrained', label: 'capacity_constrained' },
];

/**
 * CSV header — Phase-0 subset of frontend-spec §15.1 LOCKED columns.
 *
 * Columns derivable from TradeSummary today: trade_id, opened_at_utc,
 * opened_at_et, closed_at_utc, market, direction, state, total_quantity,
 * avg_entry_price, avg_exit_price, realized_pnl_usd, signal_uuid, env,
 * managed_by_strategy_version. The remaining LOCKED columns (anomaly_*,
 * approved_*, expected_*, vol_regime_*, trend_regime_*, decision_diary_*,
 * audit_chain_anchor_hash) require the server-side `/api/trades/export.csv`
 * which joins signals + attribution + decision_diary + audit.
 */
const CSV_HEADER = [
  'trade_id',
  'opened_at_utc',
  'opened_at_et',
  'closed_at_utc',
  'market',
  'direction',
  'state',
  'total_quantity',
  'avg_entry_price',
  'avg_exit_price',
  'realized_pnl_usd',
  'signal_uuid',
  'environment_tag',
  'managed_by_strategy_version',
] as const;

function csvEscape(cell: string | null): string {
  if (cell === null) return '';
  if (cell.includes(',') || cell.includes('"') || cell.includes('\n')) {
    return `"${cell.replace(/"/g, '""')}"`;
  }
  return cell;
}

function tradeToCsvRow(t: Trade): string {
  const cells: (string | null)[] = [
    t.id,
    t.opened_at_utc,
    formatDateTimeET(t.opened_at_utc).replace(' ET', ''),
    t.closed_at_utc,
    t.market,
    t.direction,
    t.state,
    String(t.total_quantity),
    t.avg_entry_price,
    t.avg_exit_price,
    t.realized_pnl_usd,
    t.entry_signal_id,
    t.env,
    t.managed_by_strategy_version,
  ];
  return cells.map(csvEscape).join(',');
}

function buildCsvBlob(trades: readonly Trade[]): Blob {
  const lines = [CSV_HEADER.join(','), ...trades.map(tradeToCsvRow)];
  const body = `${lines.join('\n')}\n`;
  return new Blob([body], { type: 'text/csv;charset=utf-8' });
}

function csvFilename(): string {
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').replace('Z', 'Z');
  return `trades_phase0_${stamp}.csv`;
}

function downloadCsv(trades: readonly Trade[]): void {
  if (typeof document === 'undefined') return;
  const blob = buildCsvBlob(trades);
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = csvFilename();
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function fmtDirection(dir: Trade['direction']): string {
  return dir === 'long' ? 'L' : 'S';
}

export default function TradesPage(): JSX.Element {
  const [fromDate, setFromDate] = useState<string>('');
  const [toDate, setToDate] = useState<string>('');
  const [market, setMarket] = useState<string>('');
  const [state, setState] = useState<TradeState | ''>('');
  const [idPrefix, setIdPrefix] = useState<string>('');

  const filters = useMemo<TradesQueryFilters>(
    () => ({
      ...(fromDate !== '' && { from: `${fromDate}T00:00:00Z` }),
      ...(toDate !== '' && { to: `${toDate}T23:59:59Z` }),
      ...(market.trim() !== '' && { market: market.trim() }),
      ...(state !== '' && { state }),
      ...(idPrefix.trim() !== '' && { id_prefix: idPrefix.trim() }),
    }),
    [fromDate, toDate, market, state, idPrefix],
  );

  const { data, isLoading, isError, error } = useTradesPage(filters);
  const trades = data?.trades ?? [];

  function clearFilters(): void {
    setFromDate('');
    setToDate('');
    setMarket('');
    setState('');
    setIdPrefix('');
  }

  return (
    <div className="container mx-auto max-w-7xl p-4 md:p-6">
      <header className="mb-6 flex items-center justify-between">
        <h1 className="font-mono text-2xl">Trades</h1>
        <div className="text-sm text-text-muted">
          {trades.length > 0
            ? `${trades.length} ${trades.length === 1 ? 'trade' : 'trades'}`
            : ''}
        </div>
      </header>

      <Card className="mb-4">
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Filters</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col text-xs text-text-muted">
              From
              <input
                type="date"
                value={fromDate}
                onChange={(e) => setFromDate(e.target.value)}
                className="mt-1 rounded-md border border-border bg-bg-elevated px-2 py-1 font-mono text-sm text-text-primary"
                aria-label="From date"
              />
            </label>
            <label className="flex flex-col text-xs text-text-muted">
              To
              <input
                type="date"
                value={toDate}
                onChange={(e) => setToDate(e.target.value)}
                className="mt-1 rounded-md border border-border bg-bg-elevated px-2 py-1 font-mono text-sm text-text-primary"
                aria-label="To date"
              />
            </label>
            <label className="flex flex-col text-xs text-text-muted">
              Market
              <input
                type="text"
                value={market}
                onChange={(e) => setMarket(e.target.value)}
                placeholder="/MES"
                maxLength={32}
                className="mt-1 w-32 rounded-md border border-border bg-bg-elevated px-2 py-1 font-mono text-sm text-text-primary"
                aria-label="Market"
              />
            </label>
            <label className="flex flex-col text-xs text-text-muted">
              State
              <select
                value={state}
                onChange={(e) => setState(e.target.value as TradeState | '')}
                className="mt-1 rounded-md border border-border bg-bg-elevated px-2 py-1 font-mono text-sm text-text-primary"
                aria-label="State"
              >
                {STATE_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col text-xs text-text-muted">
              ID prefix
              <input
                type="text"
                value={idPrefix}
                onChange={(e) => setIdPrefix(e.target.value)}
                placeholder="0190a1b2"
                maxLength={36}
                className="mt-1 w-40 rounded-md border border-border bg-bg-elevated px-2 py-1 font-mono text-sm text-text-primary"
                aria-label="Trade ID prefix"
              />
            </label>
            <Button variant="outline" size="sm" onClick={clearFilters}>
              Clear
            </Button>
            <div className="grow" />
            <Button
              variant="default"
              size="sm"
              onClick={() => downloadCsv(trades)}
              disabled={trades.length === 0}
              aria-label="Export trades as CSV"
            >
              Export CSV
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent>
          {isLoading && <p className="py-8 text-sm text-text-muted">Loading…</p>}
          {isError && (
            <p className="py-8 text-sm text-severity-p1">
              Failed to load trades{error instanceof Error ? `: ${error.message}` : ''}.
            </p>
          )}
          {!isLoading && !isError && trades.length === 0 && (
            <p className="py-8 text-sm text-text-muted">
              No trades yet — will populate once first signal fills.
            </p>
          )}
          {trades.length > 0 && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Date</TableHead>
                  <TableHead>Market</TableHead>
                  <TableHead>Dir</TableHead>
                  <TableHead className="text-right">Size</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">Entry</TableHead>
                  <TableHead className="text-right">Exit</TableHead>
                  <TableHead className="text-right">P&amp;L</TableHead>
                  <TableHead>Vers</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {trades.map((t) => (
                  <TableRow key={t.id}>
                    <TableCell className="font-mono text-text-secondary">
                      <Link
                        href={`/trades/${encodeURIComponent(t.id)}`}
                        className="hover:underline focus-visible:underline"
                      >
                        {formatDateTimeET(t.opened_at_utc)}
                      </Link>
                    </TableCell>
                    <TableCell className="font-mono">{t.market}</TableCell>
                    <TableCell className="font-mono">{fmtDirection(t.direction)}</TableCell>
                    <TableCell className="text-right font-mono tabular-nums">
                      {t.total_quantity}
                    </TableCell>
                    <TableCell className="font-mono text-text-secondary">{t.state}</TableCell>
                    <TableCell className="text-right font-mono tabular-nums">
                      {formatPrice(t.avg_entry_price)}
                    </TableCell>
                    <TableCell className="text-right font-mono tabular-nums">
                      {formatPrice(t.avg_exit_price)}
                    </TableCell>
                    <TableCell
                      className={`text-right font-mono tabular-nums ${
                        pnlSignClass(t.realized_pnl_usd) ?? ''
                      }`}
                    >
                      {formatPnL(t.realized_pnl_usd)}
                    </TableCell>
                    <TableCell className="font-mono text-text-muted">
                      {t.managed_by_strategy_version}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
