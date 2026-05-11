/**
 * apps/web/src/lib/format.ts -- display-only adapters.
 *
 * Per frontend-spec §8.9 LOCKED: "All metrics computed backend-side; frontend
 * renders only." These helpers transform pre-computed strings into display
 * surface. They do NOT do arithmetic.
 *
 * Decimal-string convention: every monetary/price/percent field arrives from
 * the API as a decimal-formatted string (per backend-spec §4.1.6 + dev-guide
 * §3.8). Phase 0 payloads return "0" zero-Decimals which render as "$0.00"
 * with no special-casing.
 *
 * Datetime convention: all timestamps are RFC 3339 UTC; frontend renders in
 * America/New_York wall-clock per spec §4.5 (24h format throughout). The
 * native `Intl.DateTimeFormat` with `timeZone: 'America/New_York'` is used
 * directly -- no date-fns / dayjs dependency needed for Day 22.
 *
 * Phase 1 will need `decimal.js` if frontend arithmetic becomes unavoidable
 * (spec §8.10). Day 22 has no arithmetic so the dep is deferred.
 */

const ET_TIME_FMT = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
});

const ET_DATETIME_FMT = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'America/New_York',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
});

const ET_TIME_NO_SECONDS_FMT = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
});

/**
 * Format an ISO timestamp as `HH:mm:ss` America/New_York.
 *
 * Returns "—" for empty / unparseable input rather than throwing -- the Today
 * page renders many timestamps and one bad payload field shouldn't crash the
 * tile.
 */
export function formatTimeET(iso: string | null | undefined): string {
  if (iso == null || iso === '') return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return `${ET_TIME_FMT.format(d)} ET`;
}

/** Format as `HH:mm ET` (no seconds) -- compact form for high-density feeds. */
export function formatTimeETShort(iso: string | null | undefined): string {
  if (iso == null || iso === '') return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return `${ET_TIME_NO_SECONDS_FMT.format(d)} ET`;
}

/** Format as `YYYY-MM-DD HH:mm:ss ET`. */
export function formatDateTimeET(iso: string | null | undefined): string {
  if (iso == null || iso === '') return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  // en-CA gives `2026-05-14, 17:32:00` -- normalize to space-separated.
  return `${ET_DATETIME_FMT.format(d).replace(', ', ' ')} ET`;
}

/**
 * Format a Decimal-string P&L value with sign prefix + thousands separator.
 *
 * Phase 0 payloads return "0" which renders as "$0.00" (no sign). Negative
 * values get a leading `-`; positive values get a leading `+`.
 */
export function formatPnL(decimalStr: string | null | undefined): string {
  if (decimalStr == null || decimalStr === '') return '—';
  const n = Number(decimalStr);
  if (Number.isNaN(n)) return '—';
  const abs = Math.abs(n).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  if (n > 0) return `+$${abs}`;
  if (n < 0) return `-$${abs}`;
  return `$${abs}`;
}

/** Format a Decimal-string as `+X.XX%` / `-X.XX%`. */
export function formatPnLPct(decimalStr: string | null | undefined): string {
  if (decimalStr == null || decimalStr === '') return '—';
  const n = Number(decimalStr);
  if (Number.isNaN(n)) return '—';
  const sign = n > 0 ? '+' : n < 0 ? '-' : '';
  return `${sign}${Math.abs(n).toFixed(2)}%`;
}

/**
 * Format a price/quantity with a fixed decimal count + thousands separator.
 * Used for the Positions table's avg cost / mark columns.
 */
export function formatPrice(
  decimalStr: string | null | undefined,
  decimals = 2,
): string {
  if (decimalStr == null || decimalStr === '') return '—';
  const n = Number(decimalStr);
  if (Number.isNaN(n)) return '—';
  return n.toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

/** Format an integer with thousands separator. Used for contract sizes etc. */
export function formatInt(value: number | null | undefined): string {
  if (value == null) return '—';
  return value.toLocaleString('en-US');
}

/** Return the sign class for a Decimal-string ("pnl-positive" / "pnl-negative" / null). */
export function pnlSignClass(decimalStr: string | null | undefined): string | null {
  if (decimalStr == null || decimalStr === '') return null;
  const n = Number(decimalStr);
  if (Number.isNaN(n)) return null;
  if (n > 0) return 'text-pnl-positive';
  if (n < 0) return 'text-pnl-negative';
  return null;
}
