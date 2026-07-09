'use client';

/**
 * `WatchingSection` — per-asset hysteresis-flip proximity for `/signals`
 * (crypto-pivot §3.9).
 *
 * One row per asset (BTC / ETH) showing the direction the engine holds,
 * how far a pending direction flip has progressed against the hysteresis
 * window, and any entry blocks (post-stop lockout, vol-block). All
 * derived numbers (`days_to_flip`, `lockout_days_left`) are computed
 * SERVER-side; this component only renders them (frontend-spec §8.9
 * LOCKED — no Decimal arithmetic in JS).
 *
 * Data source: `GET /api/signals/proximity` — recomposed from the
 * strategy worker's persisted engine state + the latest 00:05 UTC daily
 * decision (`useSignalsProximity`).
 *
 * Refresh model: TanStack Query staleTime + the existing `signal` SSE
 * event handler in `lib/sse.ts` prefix-invalidates `['signals']`, which
 * transitively refetches this hook's `['signals', 'proximity']` key. No
 * new SSE event type is introduced (forbidden by CLAUDE.md locked rules).
 */

import {
  bucketOf,
  sortProximityAssets,
  useSignalsProximity,
  type AssetProximityView,
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
            <span className="text-text-muted">({data.assets.length})</span>
          )}
        </CardTitle>
        <p className="text-xs text-text-muted">
          Distance to a hysteresis direction flip per asset — evaluated at the
          daily 00:05&nbsp;UTC decision. A pending flip must persist for the
          full hysteresis window before the engine changes direction.
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
          <WatchingBody assets={data.assets} />
        )}
      </CardContent>
    </Card>
  );
}

function WatchingBody({
  assets,
}: {
  assets: readonly AssetProximityView[];
}): JSX.Element {
  if (assets.length === 0) {
    return (
      <p className="text-sm text-text-muted">
        Waiting for the strategy worker&apos;s first daily decision (00:05&nbsp;UTC).
      </p>
    );
  }
  const sorted = sortProximityAssets(assets);
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Asset</TableHead>
          <TableHead>Direction</TableHead>
          <TableHead>Flip proximity</TableHead>
          <TableHead>Blocks</TableHead>
          <TableHead className="text-right">Trend score</TableHead>
          <TableHead className="text-right">Mark · as of</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {sorted.map((a) => (
          <WatchingRow key={a.asset} asset={a} />
        ))}
      </TableBody>
    </Table>
  );
}

function WatchingRow({ asset }: { asset: AssetProximityView }): JSX.Element {
  return (
    <TableRow>
      <TableCell className="font-mono">{asset.asset}</TableCell>
      <TableCell>
        <DirectionPill dir={asset.current_dir} />
      </TableCell>
      <TableCell>
        <FlipChip asset={asset} />
      </TableCell>
      <TableCell>
        <BlockChips asset={asset} />
      </TableCell>
      <TableCell className="text-right font-mono tabular-nums text-xs">
        {formatTrendScore(asset.trend_score)}
      </TableCell>
      <TableCell className="text-right text-xs text-text-muted">
        <div className="font-mono tabular-nums" title="Mark at the last 00:05 UTC decision (not a live tick)">
          {formatMark(asset.mark)}
        </div>
        <div className="font-mono">{formatTimeETShort(asset.as_of_utc)}</div>
      </TableCell>
    </TableRow>
  );
}

// ---------------------------------------------------------------------------
// Sub-components — chip shape mirrors the pre-pivot GateChip visual pattern
// (`bg-{token}/20 text-{token}` rounded font-mono chips).
// ---------------------------------------------------------------------------

function DirectionPill({ dir }: { dir: number }): JSX.Element {
  if (dir > 0) {
    return (
      <span className="inline-flex items-center rounded-md bg-health-green/20 px-2 py-0.5 text-xs font-medium uppercase tracking-wide text-health-green">
        LONG
      </span>
    );
  }
  if (dir < 0) {
    return (
      <span className="inline-flex items-center rounded-md bg-health-red/20 px-2 py-0.5 text-xs font-medium uppercase tracking-wide text-health-red">
        SHORT
      </span>
    );
  }
  return (
    <span className="inline-flex items-center rounded-md bg-bg-elevated px-2 py-0.5 text-xs font-medium uppercase tracking-wide text-text-secondary">
      FLAT
    </span>
  );
}

/**
 * Flip-progress chip. A pending flip renders yellow with the server-computed
 * days-to-flip countdown ("flips short in 1d · 1/2"); a flip due at the next
 * decision renders red; no pending flip renders a muted "steady" chip.
 */
function FlipChip({ asset }: { asset: AssetProximityView }): JSX.Element {
  if (asset.days_to_flip === null || asset.pending_dir === null) {
    return (
      <span className="inline-flex items-center rounded-md bg-bg-elevated px-2 py-0.5 font-mono text-xs text-text-muted">
        steady
      </span>
    );
  }
  const target = asset.pending_dir > 0 ? 'long' : 'short';
  const dueNext = asset.days_to_flip <= 0;
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-md px-2 py-0.5 font-mono text-xs',
        dueNext
          ? 'bg-health-red/20 text-health-red'
          : 'bg-health-yellow/20 text-health-yellow',
      )}
      title={`Pending ${target} has persisted ${asset.pending_count} of ${asset.hysteresis_days} hysteresis days`}
    >
      <span>
        {dueNext ? `flips ${target} next decision` : `flips ${target} in ${asset.days_to_flip}d`}
      </span>
      <span className="tabular-nums">
        {asset.pending_count}/{asset.hysteresis_days}
      </span>
    </span>
  );
}

/** Lockout / vol-block chips; "—" when no block gates the asset. */
function BlockChips({ asset }: { asset: AssetProximityView }): JSX.Element {
  if (!asset.lockout_active && !asset.vol_blocked) {
    return <span className="text-text-muted">—</span>;
  }
  return (
    <div className="flex flex-wrap items-center gap-1">
      {asset.lockout_active && (
        <span
          className="inline-flex items-center rounded-md bg-health-red/20 px-2 py-0.5 font-mono text-xs text-health-red"
          title="Post-stop re-entry lockout — no new entry until it clears"
        >
          lockout
          {asset.lockout_days_left !== null && (
            <span className="ml-1 tabular-nums">{asset.lockout_days_left}d</span>
          )}
        </span>
      )}
      {asset.vol_blocked && (
        <span
          className="inline-flex items-center rounded-md bg-health-yellow/20 px-2 py-0.5 font-mono text-xs text-health-yellow"
          title="Vol-ratio block latched — entries resume when the ratio recovers"
        >
          vol block
        </span>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Formatters — Decimal-string in, display-string out. parseFloat only at the
// display layer on bounded values (frontend-spec §8.9; no arithmetic).
// ---------------------------------------------------------------------------

function formatTrendScore(decimalStr: string | null): string {
  if (decimalStr === null || decimalStr === '') return '—';
  const n = parseFloat(decimalStr);
  if (Number.isNaN(n)) return '—';
  const sign = n > 0 ? '+' : n < 0 ? '−' : '';
  return `${sign}${Math.abs(n).toFixed(3)}`;
}

function formatMark(decimalStr: string | null): string {
  if (decimalStr === null || decimalStr === '') return '—';
  const n = parseFloat(decimalStr);
  if (Number.isNaN(n)) return '—';
  return n.toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}
