'use client';

/**
 * Funding + Cash-Yield Strip — the Today tile added by crypto-pivot §3.9.
 *
 * Full-width strip (same visual pattern as PipelineFreshnessStrip) with:
 *
 *   - Funding today (est.) — summed per-held-product estimate from the
 *     hourly telemetry. ESTIMATED, not venue-settled — the tooltip says
 *     so (the wire's `funding_basis` honesty label).
 *   - Per-held-product annualized funding chips (BTC +9.8%/yr …).
 *   - Swept to yield today — completed §3.6 cash sweeps.
 *   - Yield rate — the USDC rewards APY; "—" until configured.
 *   - Liq buffer — venue liquidation buffer from the last 00:15 UTC
 *     recon; "—" when flat / no recon yet.
 *
 * Every number arrives as a server-computed Decimal-string
 * (frontend-spec §8.9 LOCKED — no arithmetic here); rendering goes
 * through the existing `lib/format.ts` helpers plus a local
 * fraction→percent display formatter (parseFloat at the display layer
 * only, same idiom as ExitWatchingSection).
 */

import { useSystemFunding } from '@/lib/api/queries';
import { formatDateTimeET, formatPnL, formatPrice } from '@/lib/format';
import { cn } from '@/lib/utils';

function StripItem({
  label,
  value,
  title,
  valueClass,
}: {
  readonly label: string;
  readonly value: string;
  readonly title: string;
  readonly valueClass?: string | undefined;
}): JSX.Element {
  return (
    <div className="flex items-center gap-1.5" title={title}>
      <span className="text-text-muted">{label}</span>
      <span className={cn('font-mono tabular-nums text-text-primary', valueClass)}>
        {value}
      </span>
    </div>
  );
}

export function FundingStrip(): JSX.Element {
  const { data } = useSystemFunding();

  const heldProducts = (data?.products ?? []).filter((p) => p.held_contracts !== 0);

  return (
    <div className="mb-4 flex flex-wrap items-center gap-x-6 gap-y-2 rounded-md border border-border bg-bg-surface px-4 py-2 text-xs">
      <span className="font-mono uppercase tracking-wide text-text-muted">
        Funding &amp; yield
      </span>
      <StripItem
        label="Funding today (est.)"
        value={formatPnL(data?.est_funding_today_total_usd)}
        title="Estimated from hourly telemetry, not venue-settled"
        valueClass={
          data?.est_funding_today_total_usd != null &&
          parseFloat(data.est_funding_today_total_usd) < 0
            ? 'text-pnl-negative'
            : undefined
        }
      />
      {heldProducts.map((p) => (
        <span
          key={p.product_id}
          className="inline-flex items-center gap-1 rounded-md bg-bg-elevated px-2 py-0.5 font-mono"
          title={`${p.product_id} — latest annualized funding rate (held ${p.held_contracts})`}
        >
          <span className="text-text-secondary">{p.asset ?? p.product_id}</span>
          <span className="tabular-nums text-text-primary">
            {formatFracPct(p.annualized_rate)}/yr
          </span>
        </span>
      ))}
      <StripItem
        label="Swept to yield today"
        value={
          data?.swept_to_yield_today_usd != null
            ? `$${formatPrice(data.swept_to_yield_today_usd)}`
            : '—'
        }
        title="Completed cash sweeps to USDC yield today (UTC)"
      />
      <StripItem
        label="Yield rate"
        value={formatFracPct(data?.yield_apy ?? null, { signed: false })}
        title="USDC rewards APY (Coinbase One); — until configured"
      />
      <StripItem
        label="Liq buffer"
        value={
          data?.liquidation_buffer_pct != null
            ? `${formatPrice(data.liquidation_buffer_pct)}%`
            : '—'
        }
        title={
          data?.liquidation_buffer_as_of_utc != null
            ? `As of last recon — ${formatDateTimeET(data.liquidation_buffer_as_of_utc)}`
            : 'As of last recon; — when flat or no recon yet'
        }
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Formatter — Decimal-string fraction in (e.g. "0.035"), display percent out
// ("3.50%"). parseFloat at the display layer only (frontend-spec §8.9).
// ---------------------------------------------------------------------------

function formatFracPct(
  decimalStr: string | null,
  opts: { readonly signed?: boolean } = {},
): string {
  const { signed = true } = opts;
  if (decimalStr === null || decimalStr === '') return '—';
  const n = parseFloat(decimalStr);
  if (Number.isNaN(n)) return '—';
  const pct = n * 100;
  const sign = !signed ? '' : pct > 0 ? '+' : pct < 0 ? '−' : '';
  return `${sign}${Math.abs(pct).toFixed(2)}%`;
}
