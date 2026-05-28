'use client';

/**
 * `WatchingSection` — daily proximity table for `/signals`.
 *
 * Renders the V1 universe with at-a-glance per-gate state (Donchian /
 * Trend / Hurst) so the operator can see which markets are about to
 * fire on the next LEAN signal cycle (~21:30 UTC). Sits above the
 * existing pending-signals list on the `/signals` page.
 *
 * Data source: `GET /api/signals/proximity` (PR-B). The response is
 * latest-per-market across the V1 universe; this component sorts +
 * groups + colors per `Docs/signal-proximity-design.md` §3.3 + §7.
 *
 * Refresh model (design D7): TanStack Query staleTime + the
 * existing `signal` SSE event handler in `lib/sse.ts` prefix-
 * invalidates `['signals']`, which transitively refetches this hook's
 * `['signals', 'proximity']` key. No new SSE event type is introduced
 * (forbidden by CLAUDE.md locked rules).
 *
 * Decimal handling: headroom + last_close arrive as Decimal-strings;
 * we never do arithmetic on them. `parseFloat` is used ONLY at the
 * display/sort layer where values are fractional + bounded — see the
 * top-of-file note in `lib/api/signals-proximity.ts`.
 */

import {
  bucketOf,
  sortProximityMarkets,
  useSignalsProximity,
  type GateStateLiteral,
  type GateView,
  type MarketProximityView,
  type ProximityBucket,
} from '@/lib/api/signals-proximity';
import { formatTimeETShort } from '@/lib/format';
import { cn } from '@/lib/utils';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

export function WatchingSection(): JSX.Element {
  const { data, isLoading, isError } = useSignalsProximity();

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">
          Watching{' '}
          {data !== undefined && (
            <span className="text-text-muted">({data.markets.length})</span>
          )}
        </CardTitle>
        <p className="text-xs text-text-muted">
          Per-gate proximity for the V1 universe — updated each LEAN cycle
          (~21:30&nbsp;UTC). PASS&nbsp;= cleared this session, CLOSE&nbsp;=
          within the gate&apos;s band, FAIL&nbsp;= firmly below.
        </p>
      </CardHeader>
      <CardContent>
        {isLoading && (
          <p className="text-sm text-text-muted">Loading…</p>
        )}
        {isError && (
          <p className="text-sm text-severity-p1">
            Failed to load proximity data.
          </p>
        )}
        {!isLoading && !isError && data !== undefined && (
          <WatchingBody response={data} />
        )}
      </CardContent>
    </Card>
  );
}

function WatchingBody({
  response,
}: {
  response: { as_of_cycle_ts_utc: string | null; markets: readonly MarketProximityView[] };
}): JSX.Element {
  if (response.markets.length === 0) {
    return (
      <p className="text-sm text-text-muted">
        Waiting for first LEAN cycle today (next at 21:30&nbsp;UTC).
      </p>
    );
  }
  const sorted = sortProximityMarkets(response.markets);
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Market</TableHead>
          <TableHead>Donchian</TableHead>
          <TableHead>Trend</TableHead>
          <TableHead>Hurst</TableHead>
          <TableHead>Closest gate</TableHead>
          <TableHead className="text-right">Last close · cycle</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {sorted.map((m) => (
          <WatchingRow key={m.market} market={m} />
        ))}
      </TableBody>
    </Table>
  );
}

function WatchingRow({ market }: { market: MarketProximityView }): JSX.Element {
  const bucket = bucketOf(market);
  const warming = bucket === 'warming';
  const rowTitle = warming
    ? market.gate_status === 'decommissioned'
      ? 'Strategy decommissioned — view-only'
      : 'Warming up — insufficient daily bars'
    : undefined;
  return (
    <TableRow className={cn(warming && 'opacity-60')}>
      <TableCell className="font-mono">
        <div className="flex items-center gap-2">
          <span>{market.market}</span>
          <BucketBadge bucket={bucket} />
        </div>
      </TableCell>
      <TableCell>
        <GateCell
          long={market.long_donchian}
          short={market.short_donchian}
          kind="donchian"
          warming={warming}
        />
      </TableCell>
      <TableCell>
        <GateCell
          long={market.long_trend}
          short={market.short_trend}
          kind="trend"
          warming={warming}
        />
      </TableCell>
      <TableCell>
        <GateCell hurst={market.hurst} kind="hurst" warming={warming} />
      </TableCell>
      <TableCell>
        <span
          className="rounded-md border border-border bg-bg-elevated px-2 py-0.5 text-xs font-mono"
          title={rowTitle}
        >
          {market.closest_gate}
        </span>
      </TableCell>
      <TableCell className="text-right text-xs text-text-muted">
        <div className="font-mono tabular-nums">
          {formatLastClose(market.last_close)}
        </div>
        <div className="font-mono">{formatTimeETShort(market.cycle_ts_utc)}</div>
      </TableCell>
    </TableRow>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function BucketBadge({ bucket }: { bucket: ProximityBucket }): JSX.Element | null {
  if (bucket === 'fired') {
    return (
      <span className="rounded-md bg-health-green/20 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-health-green">
        fired
      </span>
    );
  }
  if (bucket === 'warming') {
    return (
      <span className="rounded-md bg-bg-elevated px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-text-muted">
        warming up
      </span>
    );
  }
  return null;
}

type GateKind = 'donchian' | 'trend' | 'hurst';

interface GateCellProps {
  readonly kind: GateKind;
  readonly long?: GateView;
  readonly short?: GateView;
  readonly hurst?: GateView;
  readonly warming: boolean;
}

/**
 * Render a single gate cell. For directional gates (donchian, trend) we
 * pick the BETTER of the two sides — that's the side closer to firing
 * and therefore the operator-relevant view. Per §3.2 the underlying
 * value of the other direction is still in the wire payload (long_/short_
 * pair) so future iterations can surface it on hover; V0 shows the
 * better side only to keep the table narrow.
 */
function GateCell({ kind, long, short, hurst, warming }: GateCellProps): JSX.Element {
  if (warming) {
    return <GateChip state="fail" label="—" muted />;
  }
  if (kind === 'hurst') {
    if (hurst === undefined) return <GateChip state="fail" label="—" muted />;
    return <GateChip state={hurst.state} label={formatHurstHeadroom(hurst.headroom)} />;
  }
  // Directional: prefer the better of long/short, falling back to either if
  // the other is missing (shouldn't happen in practice — the schema sends
  // both — but render defensively).
  const better = pickBetterGate(long, short);
  if (better === null) return <GateChip state="fail" label="—" muted />;
  const label =
    kind === 'donchian'
      ? formatDonchianHeadroom(better.headroom)
      : formatTrendHeadroom(better.headroom);
  return <GateChip state={better.state} label={label} />;
}

interface GateChipProps {
  readonly state: GateStateLiteral;
  readonly label: string;
  readonly muted?: boolean;
}

function GateChip({ state, label, muted = false }: GateChipProps): JSX.Element {
  if (muted) {
    return (
      <span className="inline-flex items-center rounded-md bg-bg-elevated px-2 py-0.5 font-mono text-xs text-text-muted">
        {label}
      </span>
    );
  }
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-md px-2 py-0.5 font-mono text-xs',
        state === 'pass' && 'bg-health-green/20 text-health-green',
        state === 'close' && 'bg-health-yellow/20 text-health-yellow',
        state === 'fail' && 'bg-health-red/20 text-health-red',
      )}
    >
      <span className="uppercase tracking-wide">{state}</span>
      <span className="tabular-nums">{label}</span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Formatters — Decimal-string in, display-string out. parseFloat per the
// precision note in signals-proximity.ts (fractional values, no rounding).
// ---------------------------------------------------------------------------

const STATE_RANK: Readonly<Record<GateStateLiteral, number>> = {
  pass: 2,
  close: 1,
  fail: 0,
};

function pickBetterGate(
  a: GateView | undefined,
  b: GateView | undefined,
): GateView | null {
  if (a === undefined && b === undefined) return null;
  if (a === undefined) return b ?? null;
  if (b === undefined) return a;
  return STATE_RANK[a.state] >= STATE_RANK[b.state] ? a : b;
}

function formatDonchianHeadroom(decimalStr: string | null): string {
  if (decimalStr === null || decimalStr === '') return '—';
  const n = parseFloat(decimalStr);
  if (Number.isNaN(n)) return '—';
  // PASS-side headroom comes through negative (already broke the channel);
  // FAIL/CLOSE-side comes through positive (distance still to travel).
  // Display the signed value as a percent of last close.
  const pct = n * 100;
  const sign = pct > 0 ? '+' : pct < 0 ? '−' : '';
  return `${sign}${Math.abs(pct).toFixed(2)}%`;
}

function formatTrendHeadroom(decimalStr: string | null): string {
  if (decimalStr === null || decimalStr === '') return '—';
  const n = parseFloat(decimalStr);
  if (Number.isNaN(n)) return '—';
  // Trend gap_pct: positive when passing, negative when failing. Symmetric
  // display to Donchian.
  const pct = n * 100;
  const sign = pct > 0 ? '+' : pct < 0 ? '−' : '';
  return `${sign}${Math.abs(pct).toFixed(2)}%`;
}

function formatHurstHeadroom(decimalStr: string | null): string {
  if (decimalStr === null || decimalStr === '') return '—';
  const n = parseFloat(decimalStr);
  if (Number.isNaN(n)) return '—';
  // Hurst headroom is value-minus-threshold in the same units (~0..1
  // range). Not a percentage — 3 sig digits is enough for the operator.
  const sign = n > 0 ? '+' : n < 0 ? '−' : '';
  return `${sign}${Math.abs(n).toFixed(3)}`;
}

function formatLastClose(decimalStr: string | null): string {
  if (decimalStr === null || decimalStr === '') return '—';
  const n = parseFloat(decimalStr);
  if (Number.isNaN(n)) return '—';
  return n.toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}
