type Props = { params: { id: string } };

export default function TradeDetailPage({ params }: Props) {
  return (
    <div className="p-8">
      <h1 className="text-2xl font-mono">Trade {params.id}</h1>
      <p className="mt-4 text-text-secondary">
        Page: <code>/trades/{params.id}</code> &mdash; Day 20 scaffold; real per-trade detail
        (signal, market, direction, status, fill_price, fill_qty, P&amp;L) lands Week 7 Tue.
      </p>
    </div>
  );
}
