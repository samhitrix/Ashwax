import { useMemo, useState } from 'react';
import { Download } from 'lucide-react';
import { useApi } from '../hooks/useApi';
import { cn } from '../utils/cn';
import { legLabel } from './OptionsTrading';

export type ReportMode = 'paper' | 'live';

interface HistoryRow {
  segment: 'INTRADAY' | 'SWING' | 'OPTIONS';
  symbol: string;
  side: string;
  quantity: number;
  entryPrice: number;
  exitPrice: number;
  pnl: number; // net of charges
  gross: number;
  charges: Record<string, number>;
  chargesTotal: number;
  estimated: boolean; // charges estimated from prices at current rates (not recorded at close)
  broker: string;
  strategy: string;
  exitReason: string;
  entryAt: string;
  closedAt: string;
  expiry?: string;
  legs: {
    tradingsymbol: string;
    instrumentType: string;
    strike: number;
    direction: string;
    lots: number;
    quantity: number;
    premium: number;
    exitPremium: number | null;
    pnl: number | null;
  }[];
}

interface Bucket { pnl: number; gross: number; charges: number; trades: number; wins: number; estimatedTrades: number }

interface PnlSummary {
  totalPnl: number;
  grossPnl: number;
  totalCharges: number;
  chargesPctOfGross: number | null;
  chargesByType: Record<string, number>;
  estimatedTrades: number;
  trades: number;
  wins: number;
  byDay: ({ date: string } & Bucket)[];
  bySymbol: ({ symbol: string } & Bucket)[];
  byStrategy: ({ strategy: string } & Bucket)[];
}

const REFRESH_MS = 60000;
const isoDay = (d: Date) => d.toISOString().slice(0, 10);

function rupees(v: number) {
  return `${v >= 0 ? '+' : '-'}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`;
}
const pnlClass = (v: number) => (v >= 0 ? 'text-green-400' : 'text-red-400');
const CHARGE_LABELS: Record<string, string> = {
  brokerage: 'Brokerage', stt: 'STT', exchange: 'Exchange', sebi: 'SEBI', stamp: 'Stamp', gst: 'GST', dp: 'DP',
};
const rupeesPlain = (v: number) => `₹${v.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;
const chargeTip = (c: Record<string, number>) =>
  Object.entries(c).filter(([, v]) => v).map(([k, v]) => `${CHARGE_LABELS[k] ?? k}: ${rupeesPlain(v)}`).join('\n');
const shortTime = (iso: string) => iso.slice(0, 16).replace('T', ' ');

function rangeQuery(mode: ReportMode, from: string, to: string) {
  return `mode=${mode}&date_from=${from}&date_to=${to}`;
}

function DateRange({ from, to, setFrom, setTo }: { from: string; to: string; setFrom: (v: string) => void; setTo: (v: string) => void }) {
  const input = 'bg-[#0d1117] border border-gray-800 rounded-lg px-2 py-1 text-sm text-gray-200';
  return (
    <div className="flex items-center gap-2 text-sm text-gray-400">
      <input type="date" value={from} max={to} onChange={(e) => setFrom(e.target.value)} className={input} />
      <span>to</span>
      <input type="date" value={to} onChange={(e) => setTo(e.target.value)} className={input} />
    </div>
  );
}

function useRange() {
  const [from, setFrom] = useState(() => isoDay(new Date(Date.now() - 30 * 86400000)));
  const [to, setTo] = useState(() => isoDay(new Date()));
  return { from, to, setFrom, setTo };
}

function downloadCsv(rows: HistoryRow[], name: string) {
  const head = ['Closed', 'Segment', 'Symbol', 'Side', 'Qty', 'Entry', 'Exit', 'Gross', 'Brokerage', 'STT', 'Exchange', 'SEBI', 'Stamp', 'GST', 'DP', 'Charges', 'Net P&L', 'Charges estimated', 'Broker', 'Strategy', 'Exit reason'];
  const body = rows.map((r) =>
    [r.closedAt, r.segment, r.symbol, r.side, r.quantity, r.entryPrice, r.exitPrice, r.gross, r.charges.brokerage ?? 0, r.charges.stt ?? 0, r.charges.exchange ?? 0, r.charges.sebi ?? 0, r.charges.stamp ?? 0, r.charges.gst ?? 0, r.charges.dp ?? 0, r.chargesTotal, r.pnl, r.estimated ? 'yes' : 'no', r.broker, r.strategy, r.exitReason]
      .map((c) => `"${String(c).replace(/"/g, '""')}"`)
      .join(',')
  );
  const url = URL.createObjectURL(new Blob([[head.join(','), ...body].join('\n')], { type: 'text/csv' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

const SEGMENTS = ['ALL', 'INTRADAY', 'SWING', 'OPTIONS'] as const;

export function TradebookTab({ mode }: { mode: ReportMode }) {
  const range = useRange();
  const { data } = useApi<HistoryRow[]>(`/api/reports/tradebook?${rangeQuery(mode, range.from, range.to)}`, REFRESH_MS);
  const [segment, setSegment] = useState<(typeof SEGMENTS)[number]>('ALL');
  const [strategy, setStrategy] = useState('');

  const all = data ?? [];
  const strategies = useMemo(() => [...new Set(all.map((r) => r.strategy).filter(Boolean))].sort(), [all]);
  const rows = all.filter((r) => (segment === 'ALL' || r.segment === segment) && (!strategy || r.strategy === strategy));

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <DateRange {...range} />
        <select value={segment} onChange={(e) => setSegment(e.target.value as typeof segment)}
          className="bg-[#0d1117] border border-gray-800 rounded-lg px-2 py-1 text-sm text-gray-200">
          {SEGMENTS.map((s) => <option key={s} value={s}>{s === 'ALL' ? 'All segments' : s}</option>)}
        </select>
        <select value={strategy} onChange={(e) => setStrategy(e.target.value)}
          className="bg-[#0d1117] border border-gray-800 rounded-lg px-2 py-1 text-sm text-gray-200">
          <option value="">All strategies</option>
          {strategies.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <button onClick={() => downloadCsv(rows, `tradebook_${mode}_${range.from}_${range.to}.csv`)} disabled={rows.length === 0}
          className="ml-auto flex items-center gap-1 px-3 py-1 rounded-lg text-sm border border-gray-800 text-gray-300 hover:text-white disabled:opacity-40">
          <Download className="w-4 h-4" /> CSV
        </button>
      </div>
      <div className="bg-[#161b22] rounded-xl border border-gray-800 overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-gray-500 border-b border-gray-800">
              {['Closed', 'Segment', 'Symbol', 'Side', 'Qty', 'Entry', 'Exit', 'Gross', 'Charges', 'Net P&L', 'Strategy'].map((h) => (
                <th key={h} className="py-2 px-3 font-medium">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800">
            {rows.map((r, i) => (
              <tr key={i} className="text-gray-200">
                <td className="py-2 px-3 font-mono text-gray-400">{shortTime(r.closedAt)}</td>
                <td className="py-2 px-3 text-xs text-gray-400">{r.segment}</td>
                <td className="py-2 px-3 font-mono">{r.symbol}</td>
                <td className="py-2 px-3">{r.side || '—'}</td>
                <td className="py-2 px-3 font-mono">{r.segment === 'OPTIONS' ? '—' : r.quantity}</td>
                <td className="py-2 px-3 font-mono">{r.entryPrice.toFixed(2)}</td>
                <td className="py-2 px-3 font-mono">{r.segment === 'OPTIONS' ? '—' : r.exitPrice.toFixed(2)}</td>
                <td className="py-2 px-3 font-mono text-gray-300">{rupees(r.gross)}</td>
                <td className="py-2 px-3 font-mono text-amber-400" title={chargeTip(r.charges) + (r.estimated ? '\n(estimated at current rates)' : '')}>
                  {r.estimated ? '~' : ''}₹{r.chargesTotal.toLocaleString('en-IN', { maximumFractionDigits: 2 })}
                </td>
                <td className={cn('py-2 px-3 font-mono font-semibold', pnlClass(r.pnl))}>{rupees(r.pnl)}</td>
                <td className="py-2 px-3 text-gray-400">{r.strategy}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && <p className="text-gray-400 text-sm py-8 text-center">No closed trades in this range</p>}
      </div>
      <p className="text-xs text-gray-600">Today's closes appear here from the next day. Net = Gross − Charges. ~ marks charges estimated at current rates (not recorded when the trade closed); hover a charge for the breakdown.</p>
    </div>
  );
}

export function PnlTab({ mode }: { mode: ReportMode }) {
  const range = useRange();
  const { data } = useApi<PnlSummary>(`/api/reports/pnl?${rangeQuery(mode, range.from, range.to)}`, REFRESH_MS);
  const cell = 'py-2 px-3 font-mono';
  const table = (title: string, head: string, rows: ({ key: string } & Bucket)[]) => (
    <div className="bg-[#161b22] rounded-xl border border-gray-800 overflow-x-auto">
      <h3 className="px-3 pt-3 font-semibold text-white">{title}</h3>
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-gray-500 border-b border-gray-800">
            {[head, 'Trades', 'Win %', 'Gross', 'Charges', 'Net P&L'].map((h) => <th key={h} className="py-2 px-3 font-medium">{h}</th>)}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-800 text-gray-200">
          {rows.map((r) => (
            <tr key={r.key}>
              <td className={cell}>{r.key}</td>
              <td className={cell}>{r.trades}</td>
              <td className={cell}>{Math.round((r.wins / r.trades) * 100)}%</td>
              <td className={cn(cell, 'text-gray-300')}>{rupees(r.gross)}</td>
              <td className={cn(cell, 'text-amber-400')} title={r.estimatedTrades ? `${r.estimatedTrades} of ${r.trades} trades estimated` : undefined}>
                {r.estimatedTrades ? '~' : ''}₹{r.charges.toLocaleString('en-IN', { maximumFractionDigits: 0 })}
              </td>
              <td className={cn(cell, 'font-semibold', pnlClass(r.pnl))}>{rupees(r.pnl)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 && <p className="text-gray-400 text-sm py-6 text-center">No data</p>}
    </div>
  );
  const stat = (label: string, value: string, cls = 'text-gray-200') => (
    <div><span className="text-xs text-gray-500">{label} </span><span className={cn('font-mono text-xl font-bold', cls)}>{value}</span></div>
  );

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-6">
        <DateRange {...range} />
        {stat('Gross P&L', rupees(data?.grossPnl ?? 0), pnlClass(data?.grossPnl ?? 0))}
        {stat('Charges', `₹${(data?.totalCharges ?? 0).toLocaleString('en-IN', { maximumFractionDigits: 0 })}${data?.chargesPctOfGross != null ? ` (${data.chargesPctOfGross.toFixed(1)}% of gross)` : ''}`, 'text-amber-400')}
        {stat('Net P&L', rupees(data?.totalPnl ?? 0), pnlClass(data?.totalPnl ?? 0))}
        <div className="text-sm text-gray-400">{data?.trades ?? 0} trades · {data?.wins ?? 0} wins</div>
      </div>
      {data && Object.keys(data.chargesByType).length > 0 && (
        <div className="flex flex-wrap gap-x-5 gap-y-1 text-sm text-gray-400">
          {Object.entries(data.chargesByType).filter(([, v]) => v).map(([k, v]) => (
            <span key={k}>{CHARGE_LABELS[k] ?? k} <span className="font-mono text-gray-200">{rupeesPlain(Math.round(v))}</span></span>
          ))}
        </div>
      )}
      {table('By strategy', 'Strategy', (data?.byStrategy ?? []).map((d) => ({ key: d.strategy, ...d })))}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        {table('By day', 'Date', (data?.byDay ?? []).map((d) => ({ key: d.date, ...d })))}
        {table('By symbol', 'Symbol', (data?.bySymbol ?? []).map((d) => ({ key: d.symbol, ...d })))}
      </div>
      <p className="text-xs text-gray-600">
        Net = Gross − brokerage, STT, exchange, SEBI, stamp, GST and DP charges at the rates in config/charges.json for the trade's broker.
        {data && data.estimatedTrades > 0 ? ` ~ ${data.estimatedTrades} trade(s) closed before charges were recorded, so their charges are estimated from prices at current rates.` : ''}
      </p>
    </div>
  );
}

export function OptionsHistoryTab({ mode }: { mode: ReportMode }) {
  const range = useRange();
  const { data } = useApi<HistoryRow[]>(`/api/reports/tradebook?${rangeQuery(mode, range.from, range.to)}`, REFRESH_MS);
  const rows = (data ?? []).filter((r) => r.segment === 'OPTIONS');

  return (
    <div className="space-y-4">
      <DateRange {...range} />
      {rows.map((r, i) => (
        <div key={i} className="bg-[#161b22] rounded-xl border border-gray-800 p-4 space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-3">
              <span className="font-semibold text-white">{r.symbol}</span>
              <span className="text-gray-400">{r.strategy}</span>
            </div>
            <span className={cn('font-mono text-lg font-bold', pnlClass(r.pnl))} title="Net of charges">{rupees(r.pnl)}</span>
          </div>
          <div className="overflow-x-auto rounded-lg border border-gray-800">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-gray-500 text-left bg-[#0d1117]">
                  {['Side', 'Contract', 'Qty', 'Entry price', 'Entry time', 'Exit price', 'Exit time', 'Leg P&L'].map((h) => (
                    <th key={h} className="py-1.5 px-3 font-medium">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="text-gray-200">
                {r.legs.map((l) => (
                  <tr key={l.tradingsymbol} className="border-t border-gray-800">
                    <td className="py-1.5 px-3">
                      <span className={cn('font-semibold', l.direction === 'BUY' ? 'text-blue-300' : 'text-orange-300')}>{l.direction}</span>
                    </td>
                    <td className="py-1.5 px-3 font-mono">{legLabel(r.symbol, r.expiry ?? '', l)}</td>
                    <td className="py-1.5 px-3 font-mono text-gray-400">{l.quantity} ({l.lots} lot)</td>
                    <td className="py-1.5 px-3 font-mono">{l.premium.toFixed(2)}</td>
                    <td className="py-1.5 px-3 font-mono text-gray-400">{shortTime(r.entryAt)}</td>
                    <td className="py-1.5 px-3 font-mono">{l.exitPremium != null ? l.exitPremium.toFixed(2) : '—'}</td>
                    <td className="py-1.5 px-3 font-mono text-gray-400">{shortTime(r.closedAt)}</td>
                    <td className={cn('py-1.5 px-3 font-mono', l.pnl == null ? 'text-gray-500' : pnlClass(l.pnl))}>
                      {l.pnl == null ? '—' : rupees(l.pnl)}
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr className="border-t border-gray-700 text-gray-300">
                  <td className="py-1.5 px-3" colSpan={7}>Gross realized P&L</td>
                  <td className={cn('py-1.5 px-3 font-mono', pnlClass(r.gross))}>{rupees(r.gross)}</td>
                </tr>
                <tr className="text-gray-300">
                  <td className="py-1.5 px-3" colSpan={7}>Charges{r.estimated ? ' (estimated)' : ''} <span className="text-xs text-gray-500">{chargeTip(r.charges).replace(/\n/g, ' · ')}</span></td>
                  <td className="py-1.5 px-3 font-mono text-amber-400">−{r.estimated ? '~' : ''}₹{r.chargesTotal.toLocaleString('en-IN', { maximumFractionDigits: 2 })}</td>
                </tr>
                <tr className="border-t border-gray-700 text-gray-300">
                  <td className="py-1.5 px-3 font-semibold" colSpan={7}>Net realized P&L</td>
                  <td className={cn('py-1.5 px-3 font-mono font-bold', pnlClass(r.pnl))}>{rupees(r.pnl)}</td>
                </tr>
              </tfoot>
            </table>
          </div>
          {r.exitReason && <div className="text-xs text-gray-500 text-right">Exit: {r.exitReason}</div>}
          {r.legs.some((l) => l.exitPremium == null) && (
            <div className="text-xs text-gray-600">Per-leg exit prices weren't recorded for this close (made before that was tracked) — only the net P&L is available.</div>
          )}
        </div>
      ))}
      {rows.length === 0 && <p className="text-gray-400 text-sm py-8 text-center">No closed options structures in this range</p>}
    </div>
  );
}
