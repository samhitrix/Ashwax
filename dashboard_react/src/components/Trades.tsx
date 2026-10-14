import { useState } from 'react';
import { TrendingUp, TrendingDown, DollarSign, BarChart2 } from 'lucide-react';
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from 'recharts';
import { useApi } from '../hooks/useApi';
import { useModeFilterStore } from '../store/modeFilterStore';
import { ModeToggle } from './ModeToggle';
import { cn } from '../utils/cn';

interface WeeklyPnL { weeks: Record<string, number>; consecutive_negative: number; halt_threshold: number; halted: boolean }
interface CostErosion { level: string; cost_ratio: number; trade_count: number; reason: string }
interface PnlBucket { net_pnl: number; trade_count: number; weekly: Record<string, number> }
type PnlBucketSet = Record<'MIS' | 'CNC' | 'OPTIONS' | 'ALL', PnlBucket>;
// mode=live|paper returns a flat PnlBucketSet (back-compat); mode=all returns
// {live, paper} — the two are never summed into one blended figure server-side
// (see /api/pnl_breakdown docstring for the real bug that caused).
type PnlBreakdown = PnlBucketSet | { live: PnlBucketSet; paper: PnlBucketSet };

const EROSION_COLOR: Record<string, string> = {
  GREEN: 'text-green-400 bg-green-500/20 border-green-500/30',
  YELLOW: 'text-yellow-400 bg-yellow-500/20 border-yellow-500/30',
  RED: 'text-red-400 bg-red-500/20 border-red-500/30',
};

const TABS: { key: 'ALL' | 'MIS' | 'CNC' | 'OPTIONS'; label: string }[] = [
  { key: 'ALL', label: 'All' },
  { key: 'MIS', label: 'MIS' },
  { key: 'CNC', label: 'CNC' },
  { key: 'OPTIONS', label: 'Options' },
];

const EMPTY_BUCKET: PnlBucket = { net_pnl: 0, trade_count: 0, weekly: {} };

function isSplit(b: PnlBreakdown | null): b is { live: PnlBucketSet; paper: PnlBucketSet } {
  return !!b && 'live' in b && 'paper' in b;
}

function PnlSummary({ bucket, label, accent }: { bucket: PnlBucket; label: string; accent: string }) {
  const totalNet = bucket.net_pnl;
  return (
    <div className="grid grid-cols-2 gap-4">
      <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
        <div className="flex items-center gap-3">
          <div className={cn('w-10 h-10 rounded-lg flex items-center justify-center', totalNet >= 0 ? 'bg-green-500/20' : 'bg-red-500/20')}>
            <DollarSign className={cn('w-5 h-5', totalNet >= 0 ? 'text-green-400' : 'text-red-400')} />
          </div>
          <div>
            <div className="text-xs text-gray-400">{label} Net P&L</div>
            <div className={cn('text-xl font-bold font-mono', totalNet >= 0 ? 'text-green-400' : 'text-red-400')}>
              {totalNet >= 0 ? '+' : ''}₹{totalNet.toLocaleString('en-IN', { maximumFractionDigits: 0 })}
            </div>
          </div>
        </div>
      </div>
      <div className="bg-[#161b22] rounded-xl p-4 border border-gray-800">
        <div className="flex items-center gap-3">
          <div className={cn('w-10 h-10 rounded-lg flex items-center justify-center', accent)}>
            <BarChart2 className="w-5 h-5" />
          </div>
          <div>
            <div className="text-xs text-gray-400">{label} Trades Tracked</div>
            <div className="text-xl font-bold font-mono">{bucket.trade_count}</div>
          </div>
        </div>
      </div>
    </div>
  );
}

export function Trades() {
  const { modeFilter } = useModeFilterStore();
  const { data: weeklyPnl } = useApi<WeeklyPnL>('/api/weekly_pnl', 10000);
  const { data: costErosion } = useApi<CostErosion>('/api/cost_erosion', 10000);
  const { data: pnlBreakdown } = useApi<PnlBreakdown>(`/api/pnl_breakdown?mode=${modeFilter}`, 10000);
  const [tab, setTab] = useState<'ALL' | 'MIS' | 'CNC' | 'OPTIONS'>('ALL');

  const split = isSplit(pnlBreakdown);
  const flatBucketSet = split ? null : (pnlBreakdown as PnlBucketSet | null);
  const liveBucket = (split ? pnlBreakdown.live : modeFilter === 'live' ? flatBucketSet : null)?.[tab] ?? EMPTY_BUCKET;
  const paperBucket = (split ? pnlBreakdown.paper : modeFilter === 'paper' ? flatBucketSet : null)?.[tab] ?? EMPTY_BUCKET;
  // The chart shows one series at a time: Paper's when that's the selected
  // mode, Live's otherwise (including "All", where the cards above already
  // show both split — the chart defaulting to Live there matches this tab's
  // original live-only behavior rather than picking arbitrarily).
  const chartBucket = modeFilter === 'paper' ? paperBucket : liveBucket;
  const weekKeys = Object.keys(chartBucket.weekly).sort();
  const chartData = weekKeys.map(k => ({ week: k.replace(/^\d{4}-/, ''), pnl: chartBucket.weekly[k] }));

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-white">Trades & P&L</h2>
          <p className="text-gray-400">After-cost P&L, cost erosion, weekly performance</p>
        </div>
        <ModeToggle />
      </div>

      {/* Product tabs */}
      <div className="flex gap-2">
        {TABS.map(t => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={cn(
              'px-4 py-1.5 rounded-lg text-sm font-medium border transition-colors',
              tab === t.key
                ? 'bg-blue-500/20 border-blue-500/40 text-blue-300'
                : 'bg-[#161b22] border-gray-800 text-gray-400 hover:text-gray-200'
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Summary cards — live and paper are NEVER blended into one number here,
          only ever shown side by side, to avoid repeating a real bug where a
          paper loss silently distorted what looked like real-money P&L. */}
      {modeFilter === 'all' ? (
        <div className="grid grid-cols-2 gap-4">
          <div className="space-y-2">
            <div className="text-xs font-semibold text-red-400 flex items-center gap-1.5"><span className="w-1.5 h-1.5 rounded-full bg-red-400" /> LIVE</div>
            <PnlSummary bucket={liveBucket} label="Live" accent="bg-blue-500/20 text-blue-400" />
          </div>
          <div className="space-y-2">
            <div className="text-xs font-semibold text-blue-400 flex items-center gap-1.5"><span className="w-1.5 h-1.5 rounded-full bg-blue-400" /> PAPER</div>
            <PnlSummary bucket={paperBucket} label="Paper" accent="bg-blue-500/20 text-blue-400" />
          </div>
        </div>
      ) : (
        <PnlSummary
          bucket={modeFilter === 'paper' ? paperBucket : liveBucket}
          label={modeFilter === 'paper' ? 'Paper' : 'Live'}
          accent="bg-blue-500/20 text-blue-400"
        />
      )}

      {/* Cost Erosion status */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 p-6">
        <h3 className="font-semibold text-white flex items-center gap-2 mb-4">
          <TrendingDown className="w-5 h-5 text-orange-400" />
          Cost Erosion Gate
        </h3>
        <div className="flex items-start gap-6">
          <div className={cn('px-6 py-4 rounded-xl border text-center min-w-[120px]', EROSION_COLOR[costErosion?.level ?? 'GREEN'])}>
            <div className="text-2xl font-bold">{costErosion?.level ?? 'GREEN'}</div>
            <div className="text-xs mt-1 opacity-70">Status</div>
          </div>
          <div className="flex-1">
            <p className="text-gray-300 text-sm mb-2">{costErosion?.reason ?? 'No data yet'}</p>
            <div className="text-xs text-gray-500 space-y-1">
              <div>GREEN: costs &lt; 20% of gross — normal trading</div>
              <div>YELLOW: costs 20–30% — elevated, monitor frequency</div>
              <div>RED: costs &gt; 30% — over-trading, quality raised +5</div>
            </div>
          </div>
        </div>
      </div>

      {/* Weekly P&L chart */}
      <div className="bg-[#161b22] rounded-xl border border-gray-800 p-6">
        <h3 className="font-semibold text-white flex items-center gap-2 mb-4">
          <TrendingUp className="w-5 h-5 text-blue-400" />
          Weekly Net P&L ({TABS.find(t => t.key === tab)?.label} · {modeFilter === 'paper' ? 'Paper' : 'Live'})
        </h3>
        {chartData.length === 0 ? (
          <p className="text-gray-400 text-sm py-8 text-center">No closed trades yet</p>
        ) : (
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={chartData} margin={{ top: 4, right: 8, left: 0, bottom: 4 }}>
              <XAxis dataKey="week" tick={{ fill: '#9ca3af', fontSize: 11 }} axisLine={false} tickLine={false} />
              <YAxis tick={{ fill: '#9ca3af', fontSize: 11 }} axisLine={false} tickLine={false}
                tickFormatter={(v: number) => `₹${(v / 1000).toFixed(0)}K`} />
              <Tooltip
                contentStyle={{ background: '#161b22', border: '1px solid #374151', borderRadius: 8 }}
                labelStyle={{ color: '#e5e7eb' }}
                formatter={(v) => [`₹${Number(v).toLocaleString('en-IN')}`, 'Net P&L']}
              />
              <Bar dataKey="pnl" radius={[4, 4, 0, 0]}>
                {chartData.map((_, i) => (
                  <Cell key={i} fill={chartData[i].pnl >= 0 ? '#22c55e' : '#ef4444'} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        )}
        {weeklyPnl && (
          <div className="mt-3 flex items-center gap-2 text-xs text-gray-500">
            <span>{weeklyPnl.consecutive_negative} consecutive negative weeks</span>
            <span>·</span>
            <span>Halt after {weeklyPnl.halt_threshold} weeks</span>
            {weeklyPnl.halted && <span className="text-red-400 font-semibold">· HALTED</span>}
          </div>
        )}
      </div>
    </div>
  );
}
