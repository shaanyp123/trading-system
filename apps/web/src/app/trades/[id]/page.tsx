'use client';

/**
 * `/trades/:id` -- per-trade detail (Phase-1 minimum).
 *
 * Per frontend-spec §2.3.4: "minimal page so Discord deep-links don't 404".
 * Phase 1 wire is whatever the `TradeDetail` payload carries; the richer
 * Phase-2 drawer view (signal decision_price, expected_slippage, anomaly
 * reasons, fill qty + price, decision diary, attribution) joins to
 * signals + attribution + decision_diary + agent_actions tables and lands
 * once `/api/trades/:id` is enriched with those joins (Week 8+).
 *
 * 404 handling: `useTrade` surfaces `isError` + `TRADE_NOT_FOUND` envelope
 * from the API; the page renders a graceful "Trade not found" body
 * instead of Next's hard 404. The backend deliberately returns 404 +
 * canonical envelope (not raw 404) for exactly this reason — IG line 417
 * "ensures Discord deep-links don't 404".
 */

import Link from 'next/link';
import { useParams } from 'next/navigation';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useTrade } from '@/lib/api/queries';
import { ApiError } from '@/lib/api';
import { formatDateTimeET, formatPnL, formatPrice, pnlSignClass } from '@/lib/format';

function DetailRow({
  label,
  value,
  mono = true,
  valueClass,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
  valueClass?: string;
}): JSX.Element {
  return (
    <div className="flex flex-wrap gap-4 border-b border-border py-2 last:border-b-0">
      <div className="w-44 text-sm text-text-muted">{label}</div>
      <div
        className={`flex-1 text-sm ${mono ? 'font-mono' : ''} ${valueClass ?? ''}`}
      >
        {value}
      </div>
    </div>
  );
}

export default function TradeDetailPage(): JSX.Element {
  const params = useParams<{ id: string }>();
  const tradeId = params?.id ?? '';
  const { data, isLoading, isError, error } = useTrade(tradeId);

  return (
    <div className="container mx-auto max-w-5xl p-4 md:p-6">
      <header className="mb-6">
        <Link
          href="/trades"
          className="text-sm text-text-muted hover:text-text-primary focus-visible:text-text-primary"
        >
          ← Back to trades
        </Link>
        <h1 className="mt-2 break-all font-mono text-2xl">/trades/{tradeId}</h1>
      </header>

      {isLoading && (
        <Card>
          <CardContent>
            <p className="py-8 text-sm text-text-muted">Loading…</p>
          </CardContent>
        </Card>
      )}

      {isError && (
        <Card>
          <CardContent>
            {error instanceof ApiError && error.errorCode === 'TRADE_NOT_FOUND' ? (
              <div className="py-8 text-center">
                <p className="text-sm text-text-primary">Trade not found.</p>
                <p className="mt-2 text-sm text-text-muted">
                  No trade exists with this ID. The link may be stale or the trade may
                  not have been opened yet. Check{' '}
                  <Link href="/trades" className="underline">
                    /trades
                  </Link>{' '}
                  for the current list.
                </p>
              </div>
            ) : (
              <p className="py-8 text-sm text-severity-p1">
                Failed to load trade{error instanceof Error ? `: ${error.message}` : ''}.
              </p>
            )}
          </CardContent>
        </Card>
      )}

      {data !== undefined && (
        <>
          <Card className="mb-4">
            <CardHeader>
              <CardTitle className="font-mono text-lg">
                {data.market}{' '}
                <span className="text-text-secondary">{data.direction}</span>{' '}
                <span className="text-text-muted">
                  v{data.managed_by_strategy_version}·{data.env}
                </span>
              </CardTitle>
            </CardHeader>
            <CardContent>
              <DetailRow label="Status" value={data.state} />
              <DetailRow label="Opened" value={formatDateTimeET(data.opened_at_utc)} />
              <DetailRow
                label="Closed"
                value={
                  data.closed_at_utc !== null
                    ? formatDateTimeET(data.closed_at_utc)
                    : '—'
                }
              />
            </CardContent>
          </Card>

          <Card className="mb-4">
            <CardHeader className="pb-3">
              <CardTitle className="text-base">Position</CardTitle>
            </CardHeader>
            <CardContent>
              <DetailRow label="Total quantity" value={data.total_quantity} />
              <DetailRow
                label="Avg entry price"
                value={formatPrice(data.avg_entry_price)}
              />
              <DetailRow
                label="Avg exit price"
                value={
                  data.avg_exit_price !== null ? formatPrice(data.avg_exit_price) : '—'
                }
              />
            </CardContent>
          </Card>

          <Card className="mb-4">
            <CardHeader className="pb-3">
              <CardTitle className="text-base">Realized P&amp;L</CardTitle>
            </CardHeader>
            <CardContent>
              <DetailRow
                label="Realized P&L (gross)"
                value={
                  data.realized_pnl_usd !== null
                    ? formatPnL(data.realized_pnl_usd)
                    : '—'
                }
                valueClass={pnlSignClass(data.realized_pnl_usd) ?? ''}
              />
              <DetailRow
                label="Commission"
                value={formatPnL(data.realized_commission_usd)}
              />
              <DetailRow
                label="Dividend P&L"
                value={formatPnL(data.dividend_pnl_usd)}
              />
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">Strategy &amp; provenance</CardTitle>
            </CardHeader>
            <CardContent>
              <DetailRow
                label="Strategy version"
                value={
                  <>
                    <span>{data.managed_by_strategy_version}</span>
                    <span className="ml-2 text-xs text-text-muted">
                      ({data.managed_by_version})
                    </span>
                  </>
                }
              />
              <DetailRow label="Strategy hash" value={data.strategy_hash} />
              <DetailRow label="Parameter set hash" value={data.parameter_set_hash} />
              <DetailRow
                label="Slippage calibration"
                value={data.slippage_calibration_version_id}
              />
              <DetailRow label="Entry signal" value={data.entry_signal_id} />
              <DetailRow label="Entry order" value={data.entry_order_id} />
              <DetailRow
                label="Exit order"
                value={data.exit_order_id !== null ? data.exit_order_id : '—'}
              />
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
