const MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];

/** True when `expiry` is the LAST occurrence of its weekday in its month --
 * NSE index options only ever expire on one weekday, so "does the same
 * weekday one week later fall in the next month" is a complete monthly-vs-
 * weekly test without needing an exchange contract-type flag we don't have. */
export function isMonthlyExpiry(expiryIso: string): boolean {
  const d = new Date(`${expiryIso}T00:00:00Z`);
  if (isNaN(d.getTime())) return false;
  const nextWeek = new Date(d);
  nextWeek.setUTCDate(d.getUTCDate() + 7);
  return nextWeek.getUTCMonth() !== d.getUTCMonth();
}

/** Broker-desk-style contract label -- e.g. "NIFTY SEP 22900 PE" for the
 * monthly contract, "NIFTY 22SEP 22900 PE" for a weekly one. Built from the
 * position's own underlying/expiry + the leg's strike/type, not by parsing
 * the raw Kite tradingsymbol. */
export function legLabel(
  underlying: string,
  expiry: string,
  leg: { strike: number; instrumentType: string }
): string {
  const root = underlying.split(':').pop() ?? underlying;
  const d = new Date(`${expiry}T00:00:00Z`);
  if (isNaN(d.getTime())) return [root, leg.strike, leg.instrumentType].filter(Boolean).join(' ');
  const mon = MONTHS[d.getUTCMonth()];
  const monthPart = isMonthlyExpiry(expiry) ? mon : `${String(d.getUTCDate()).padStart(2, '0')}${mon}`;
  return [root, monthPart, leg.strike, leg.instrumentType].filter(Boolean).join(' ');
}

export function fmtTime(iso: string): string {
  if (!iso) return '—';
  const d = new Date(iso.replace(' ', 'T'));
  return isNaN(d.getTime()) ? '—' : d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

export function fmtRupees(n: number): string {
  return `₹${Math.round(n).toLocaleString('en-IN')}`;
}

/** "+₹1,234" / "−₹1,234" / "₹0"; "—" when unknown (never a fake 0). */
export function fmtSigned(n: number | null | undefined): string {
  if (n == null) return '—';
  const r = Math.round(n);
  return `${r > 0 ? '+' : r < 0 ? '−' : ''}₹${Math.abs(r).toLocaleString('en-IN')}`;
}

/** Auto-exit level as a signed rupee amount, with its % when known: "+₹1,500 (50%)". */
export function exitText(amount: number | null | undefined, pct: number | null | undefined, sign: 1 | -1): string {
  if (amount == null) return '—';
  return `${fmtSigned(sign * Math.abs(amount))}${pct != null ? ` (${pct.toFixed(0)}%)` : ''}`;
}

export function pnlClass(n: number | null | undefined): string {
  if (n == null || Math.round(n) === 0) return 'text-gray-300';
  return n > 0 ? 'text-green-400' : 'text-red-400';
}

export function rootOf(underlying: string): string {
  return underlying.split(':').pop() ?? underlying;
}
