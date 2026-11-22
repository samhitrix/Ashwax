import { cn } from '../../utils/cn';
import { fmtSigned, pnlClass } from './format';
import type { PnlSummary } from './aggregate';

export function ModeBadge({ isPaper }: { isPaper: boolean }) {
  return (
    <span
      className={cn(
        'inline-block px-2 py-0.5 rounded text-[11px] whitespace-nowrap',
        isPaper ? 'font-semibold text-gray-400 bg-gray-500/15' : 'font-bold text-green-400 bg-green-500/15'
      )}
    >
      {isPaper ? 'PAPER' : 'LIVE'}
    </span>
  );
}

export function ProductBadge({ product }: { product?: 'MIS' | 'NRML' }) {
  if (!product) return null;
  return (
    <span
      className={cn(
        'inline-block px-1.5 py-0.5 rounded text-[11px] font-semibold',
        product === 'MIS' ? 'text-amber-400 bg-amber-500/10' : 'text-blue-400 bg-blue-500/10'
      )}
      title={product === 'MIS' ? 'Intraday (MIS) - squared off at close' : 'Carry-forward (NRML)'}
    >
      {product}
    </span>
  );
}

/** Total / Booked / Unbooked block used in both left panels. */
export function PnlSummaryCard({ s }: { s: PnlSummary }) {
  const cell = (label: string, v: number | null) => (
    <div className="min-w-0">
      <div className="text-xs text-gray-500">{label}</div>
      <div className={cn('text-lg font-semibold tabular-nums mt-1 truncate', pnlClass(v))}>{fmtSigned(v)}</div>
    </div>
  );
  return (
    <div className="grid grid-cols-3 gap-2 text-center bg-[#0d1117] border border-gray-800 rounded-lg px-3 py-3.5">
      {cell('Total P&L', s.total)}
      {cell('Booked', s.booked)}
      {cell('Unbooked', s.unbooked)}
    </div>
  );
}

export function ClosedToggle({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex items-center gap-2.5 text-sm text-gray-300 cursor-pointer select-none min-h-[44px]">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} className="w-[18px] h-[18px] accent-blue-500" />
      Closed positions
    </label>
  );
}
