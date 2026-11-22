import { useState } from 'react';
import { FileText } from 'lucide-react';
import { useApi } from '../hooks/useApi';
import { cn } from '../utils/cn';

type Segment = 'INTRADAY' | 'SWING' | 'OPTIONS';

interface PaperTrade {
  symbol: string;
  direction: string;
  quantity: number;
  product: string;
  segment: Segment;
  entryPrice: number;
  exitPrice: number;
  ltp: number | null;
  pnl: number;
  partialPnlBooked: number;
  unrealizedPnl: number | null;
  strategy: string;
  state: string;
  exitReason: string;
  sl: number;
  target: number;
  initialSl: number | null;
  initialTarget: number | null;
  regime: string;
  setupScore: number | null;
  llmConfidence: number | null;
  llmFlag: string;
  llmReason: string;
  entryAt: string;
  closedAt: string;
  // options only: every leg of the structure, open and already-closed (per-leg stops)
  legs?: PaperLeg[];
}

interface PaperLeg {
  contract: string;
  direction: string;
  quantity: number;
  entry: number;
  price: number | null;
  pnl: number | null;
  state: string;
  exitReason: string;
  exitAt: string;
}

function exitReasonClass(reason: string): string {
  if (reason.includes('TARGET')) return 'text-green-400';
  if (reason.includes('SL')) return 'text-red-400';
  return 'text-gray-400';
}

function llmFlagClass(flag: string): string {
  if (flag === 'CONFIRM') return 'text-green-400';
  if (flag === 'REJECT') return 'text-red-400';
  return 'text-gray-400';
}

function dayKey(iso: string): string {
  // entryAt is an ISO-ish timestamp string; take the date portion.
  return iso ? iso.slice(0, 10) : 'unknown';
}

function pctFromEntry(entry: number, price: number, isLong: boolean): number {
  // Signed % move from entry in the position's profit direction: negative for
  // SL (max loss if hit), positive for Target — matches how "5% loss" reads.
  return isLong ? ((price - entry) / entry) * 100 : ((entry - price) / entry) * 100;
}

function pctToTarget(t: PaperTrade): number | null {
  const current = t.state === 'CLOSED' ? t.exitPrice : t.ltp;
  if (current == null || !t.target || !t.entryPrice) return null;
  const isLong = t.direction?.toLowerCase() === 'long';
  const denominator = isLong ? t.target - t.entryPrice : t.entryPrice - t.target;
  if (!denominator) return null;
  const numerator = isLong ? current - t.entryPrice : t.entryPrice - current;
  return (numerator / denominator) * 100;
}

function fmtTime(iso: string): string {
  // Timestamps arrive as "YYYY-MM-DD HH:MM:SS+00:00"; render in the browser's
  // local time zone (IST for the operator). '—' when absent/unparseable.
  if (!iso) return '—';
  const d = new Date(iso.replace(' ', 'T'));
  return isNaN(d.getTime()) ? '—' : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

const SEGMENT_TABS: { key: Segment | 'ALL'; label: string }[] = [
  { key: 'ALL', label: 'All Segments' },
  { key: 'INTRADAY', label: 'Intraday' },
  { key: 'SWING', label: 'Swing' },
  { key: 'OPTIONS', label: 'Options' },
];

function segmentBadgeClass(segment: Segment): string {
  if (segment === 'OPTIONS') return 'text-purple-400 bg-purple-500/10 border-purple-500/30';
  if (segment === 'SWING') return 'text-orange-400 bg-orange-500/10 border-orange-500/30';
  return 'text-cyan-400 bg-cyan-500/10 border-cyan-500/30';
}

export function PaperTrades() {
  const { data } = useApi<PaperTrade[]>('/api/paper_trades', 5000);
  const allTrades = data ?? [];
  const today = new Date().toISOString().slice(0, 10);
  const [open, setOpen] = useState<Record<string, boolean>>({ [today]: true });
  const [segment, setSegment] = useState<Segment | 'ALL'>('ALL');

  const trades = segment === 'ALL' ? allTrades : allTrades.filter((t) => t.segment === segment);

  const byDay = trades.reduce<Record<string, PaperTrade[]>>((acc, t) => {
    // A closed trade's realized P&L belongs to the day it was booked, not the day it was
    // entered (a spread opened yesterday and closed today was hidden under yesterday).
    const k = dayKey(t.state === 'CLOSED' && t.closedAt ? t.closedAt : t.entryAt);
    (acc[k] ??= []).push(t);
    return acc;
  }, {});
  const days = Object.keys(byDay).sort().reverse();

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold text-white">📝 Paper Trades</h2>
        <p className="text-gray-400">Every simulated trade — intraday, swing and options — in one place</p>
      </div>

      <div className="flex gap-2">
        {SEGMENT_TABS.map((s) => (
          <button
            key={s.key}
            onClick={() => setSegment(s.key)}
            className={cn(
              'px-4 py-1.5 rounded-lg text-sm font-medium border transition-colors',
              segment === s.key
                ? 'bg-blue-500/20 border-blue-500/40 text-blue-300'
                : 'bg-[#161b22] border-gray-800 text-gray-400 hover:text-gray-200'
            )}
          >
            {s.label}
          </button>
        ))}
      </div>

      {days.length === 0 && (
        <p className="text-gray-400 text-sm py-8 text-center">
          {allTrades.length === 0 ? 'No paper trades in the last 30 days' : 'No paper trades in this segment'}
        </p>
      )}

      {days.map((day) => {
        const rows = byDay[day];
        const net = rows.reduce(
          (s, t) => s + (t.state === 'CLOSED' ? t.pnl : (t.unrealizedPnl ?? 0)),
          0,
        );
        const isOpen = open[day] ?? false;
        return (
          <div key={day} className="bg-[#161b22] rounded-xl border border-gray-800">
            <button
              className="w-full flex items-center justify-between px-6 py-4"
              onClick={() => setOpen((o) => ({ ...o, [day]: !isOpen }))}
            >
              <span className="font-semibold text-white">
                {day === today ? 'Today' : day} · {rows.length} trade{rows.length === 1 ? '' : 's'}
              </span>
              <span className={cn('font-mono font-bold', net >= 0 ? 'text-green-400' : 'text-red-400')}>
                {net >= 0 ? '+' : ''}₹{net.toLocaleString('en-IN', { maximumFractionDigits: 0 })}
              </span>
            </button>
            {isOpen && (
              <div className="px-6 pb-4 overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-gray-400 text-left border-b border-gray-800">
                      <th className="py-2 pr-4">Symbol</th>
                      <th className="py-2 pr-4">Segment</th>
                      <th className="py-2 pr-4">Dir</th>
                      <th className="py-2 pr-4">Type</th>
                      <th className="py-2 pr-4">Strategy</th>
                      <th className="py-2 pr-4">Regime</th>
                      <th className="py-2 pr-4">Setup</th>
                      <th className="py-2 pr-4">LLM</th>
                      <th className="py-2 pr-4">Qty</th>
                      <th className="py-2 pr-4">Entry</th>
                      <th className="py-2 pr-4">Exit</th>
                      <th className="py-2 pr-4">SL</th>
                      <th className="py-2 pr-4">Target</th>
                      <th className="py-2 pr-4">To Target</th>
                      <th className="py-2 pr-4">LTP</th>
                      <th className="py-2 pr-4">P&L</th>
                      <th className="py-2 pr-4">Entry Time</th>
                      <th className="py-2 pr-4">Exit Time</th>
                      <th className="py-2 pr-4">Exit Reason</th>
                      <th className="py-2 pr-4">State</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.flatMap((t, i) => [
                      <tr key={i} className="border-b border-gray-900 text-gray-200">
                        <td className="py-2 pr-4 font-medium">{t.symbol.split(':').pop()}</td>
                        <td className="py-2 pr-4">
                          <span className={cn('px-2 py-0.5 rounded text-xs font-medium border', segmentBadgeClass(t.segment))}>
                            {t.segment}
                          </span>
                        </td>
                        <td className={cn('py-2 pr-4 font-medium uppercase', t.direction?.toLowerCase() === 'long' ? 'text-green-400' : 'text-red-400')}>{t.direction || '—'}</td>
                        <td className="py-2 pr-4 text-gray-400">{t.product || '—'}</td>
                        <td className="py-2 pr-4 text-gray-400">{t.strategy}</td>
                        <td className="py-2 pr-4 text-gray-400">{t.regime || '—'}</td>
                        <td className="py-2 pr-4 font-mono text-gray-400">{t.setupScore != null ? t.setupScore.toFixed(0) : '—'}</td>
                        <td className={cn('py-2 pr-4', llmFlagClass(t.llmFlag))} title={t.llmReason || undefined}>
                          {t.llmFlag ? `${t.llmFlag}${t.llmConfidence != null ? ` ${t.llmConfidence.toFixed(2)}` : ''}` : '—'}
                        </td>
                        <td className="py-2 pr-4 font-mono">{t.quantity}</td>
                        <td className="py-2 pr-4 font-mono">{t.entryPrice.toFixed(2)}</td>
                        <td className="py-2 pr-4 font-mono">{t.state === 'CLOSED' ? t.exitPrice.toFixed(2) : '—'}</td>
                        <td className="py-2 pr-4 font-mono text-gray-400">
                          {t.initialSl != null ? t.initialSl.toFixed(2) : (t.sl ? t.sl.toFixed(2) : '—')}
                          {t.initialSl != null && t.sl && Math.abs(t.sl - t.initialSl) > 0.01 && (
                            <span className="text-gray-600"> → {t.sl.toFixed(2)}</span>
                          )}
                          {t.sl > 0 && t.entryPrice > 0 && (() => {
                            const slPct = pctFromEntry(t.entryPrice, t.sl, t.direction?.toLowerCase() === 'long');
                            return (
                              <span className={slPct >= 0 ? 'text-green-400' : 'text-red-400'}>
                                {' '}({slPct >= 0 ? '+' : ''}{slPct.toFixed(1)}%)
                              </span>
                            );
                          })()}
                        </td>
                        <td className="py-2 pr-4 font-mono text-gray-400">
                          {t.initialTarget != null ? t.initialTarget.toFixed(2) : (t.target ? t.target.toFixed(2) : '—')}
                          {t.initialTarget != null && t.target && Math.abs(t.target - t.initialTarget) > 0.01 && (
                            <span className="text-gray-600"> → {t.target.toFixed(2)}</span>
                          )}
                          {t.target > 0 && t.entryPrice > 0 && (
                            <span className="text-green-400">
                              {' '}(+{pctFromEntry(t.entryPrice, t.target, t.direction?.toLowerCase() === 'long').toFixed(1)}%)
                            </span>
                          )}
                        </td>
                        {(() => {
                          const pct = pctToTarget(t);
                          if (pct == null) {
                            return <td className="py-2 pr-4 font-mono text-gray-500">—</td>;
                          }
                          return (
                            <td
                              className={cn(
                                'py-2 pr-4 font-mono',
                                pct >= 100 ? 'text-green-400' : pct >= 0 ? 'text-gray-300' : 'text-red-400',
                              )}
                            >
                              {pct.toFixed(0)}%
                            </td>
                          );
                        })()}
                        <td
                          className={cn(
                            'py-2 pr-4 font-mono',
                            t.ltp == null
                              ? 'text-gray-500'
                              : (t.ltp >= t.entryPrice) === (t.direction?.toLowerCase() === 'long')
                                ? 'text-green-400'
                                : 'text-red-400',
                          )}
                        >
                          {t.ltp != null ? t.ltp.toFixed(2) : '—'}
                        </td>
                        {(() => {
                          const isClosed = t.state === 'CLOSED';
                          const val = isClosed ? t.pnl : t.unrealizedPnl;
                          if (val == null) {
                            return <td className="py-2 pr-4 font-mono text-gray-500">—</td>;
                          }
                          return (
                            <td className={cn('py-2 pr-4 font-mono', val >= 0 ? 'text-green-400' : 'text-red-400')}>
                              {`${val >= 0 ? '+' : ''}${val.toFixed(0)}`}
                              {!isClosed && <span className="text-gray-600" title="live unrealized P&L"> ⋯</span>}
                              {t.partialPnlBooked !== 0 && (
                                <span className="block text-xs text-green-400" title={isClosed ? 'Included in the P&L above (booked via partial exit)' : 'Already booked via partial exit'}>
                                  ₹{t.partialPnlBooked.toFixed(0)} booked
                                </span>
                              )}
                            </td>
                          );
                        })()}
                        <td className="py-2 pr-4 font-mono text-gray-400">{fmtTime(t.entryAt)}</td>
                        <td className="py-2 pr-4 font-mono text-gray-400">{fmtTime(t.closedAt)}</td>
                        <td className={cn('py-2 pr-4', exitReasonClass(t.exitReason))}>{t.exitReason || '—'}</td>
                        <td className="py-2 pr-4">{t.state}</td>
                      </tr>
                    ,
                      ...(t.legs ?? []).map((leg) => (
                        <tr key={`${i}-${leg.contract}`} className={cn('border-b border-gray-900 text-xs bg-[#0d1117]', leg.state === 'CLOSED' && t.state !== 'CLOSED' ? 'text-gray-500 opacity-60' : 'text-gray-400')}>
                          <td className="py-1 pl-6 pr-4 font-mono" colSpan={2}>↳ {leg.contract.split(':').pop()}</td>
                          <td className={cn('py-1 pr-4 uppercase', leg.direction === 'BUY' ? 'text-blue-300' : 'text-orange-300')}>{leg.direction}</td>
                          <td className="py-1 pr-4" colSpan={5}>
                            <span className={cn('px-1.5 py-0.5 rounded border', leg.state === 'CLOSED' ? 'text-red-300 border-red-500/30' : 'text-green-300 border-green-500/30')}>
                              {leg.state === 'CLOSED' ? 'CLOSED' : 'OPEN'}
                            </span>
                            {leg.exitReason ? ` ${leg.exitReason}` : ''}
                          </td>
                          <td className="py-1 pr-4 font-mono">{leg.quantity}</td>
                          <td className="py-1 pr-4 font-mono">{leg.entry.toFixed(2)}</td>
                          <td className="py-1 pr-4 font-mono">{leg.state === 'CLOSED' && leg.price != null ? leg.price.toFixed(2) : '—'}</td>
                          <td className="py-1 pr-4" colSpan={3}></td>
                          <td className="py-1 pr-4 font-mono">{leg.state !== 'CLOSED' && leg.price != null ? leg.price.toFixed(2) : '—'}</td>
                          <td className={cn('py-1 pr-4 font-mono', (leg.pnl ?? 0) >= 0 ? 'text-green-400' : 'text-red-400')}>
                            {leg.pnl != null ? `${leg.pnl >= 0 ? '+' : ''}${leg.pnl.toFixed(0)}` : '—'}
                          </td>
                          <td className="py-1 pr-4 font-mono">{leg.exitAt ? fmtTime(leg.exitAt) : ''}</td>
                          <td className="py-1 pr-4" colSpan={3}></td>
                        </tr>
                      )),
                    ])}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        );
      })}
      <div className="flex items-center gap-2 text-xs text-gray-600">
        <FileText className="w-3 h-3" /> Paper trades never reach the broker.
      </div>
    </div>
  );
}
