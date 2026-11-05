import { useState } from 'react';
import { Link2, TrendingUp, TrendingDown } from 'lucide-react';
import { useApi } from '../hooks/useApi';
import { useModeFilterStore } from '../store/modeFilterStore';
import { ModeToggle } from './ModeToggle';
import { cn } from '../utils/cn';

interface OptionLegRow {
  tradingsymbol: string;
  instrumentType: string;
  strike: number;
  direction: string;
  lots: number;
  premium: number;
  ltp: number | null;
  exitPremium: number | null;
  pnl: number | null;
  // 'CLOSED' for a leg already stopped out/closed while its position is still OPEN (per-leg stops).
  state?: string;
  exitReason?: string;
  exitAt?: string;
}

interface Breakeven {
  strike: number;
  pctFromSpot: number | null;
}

interface OptionPosition {
  positionId: string;
  underlying: string;
  strategy: string;
  registryStrategy?: string;
  status: string;
  legs: OptionLegRow[];
  expiry: string;
  dte: number | null;
  netCredit: number;
  maxLoss: number;
  maxProfit: number;
  entryMargin: number | null;
  entryGreeks: { delta: number | null; gamma: number | null; theta: number | null; vega: number | null };
  pnl: number | null;
  pctOfMaxProfit: number | null;
  breakeven: Breakeven[];
  pop: number | null;
  targetAmount?: number | null;
  targetPct?: number | null;
  stopAmount?: number | null;
  stopPct?: number | null;
  todayPnl: number | null;
  bookedPnl: number | null;
  unbookedPnl: number | null;
  spot: number | null;
  exitReason: string;
  openedAt: string;
  closedAt: string;
  isPaper: boolean;
  product?: 'MIS' | 'NRML';
}

interface FundsMarginsSide {
  fundAvailable: number;
  marginAvailable: number;
  sharedRiskCeiling: number;
}

interface FundsMarginsResponse {
  paper: FundsMarginsSide;
  live: FundsMarginsSide;
}

interface LtpQuote {
  last_price?: number;
  ohlc?: { close?: number };
}

const MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];

/** True when `expiry` is the LAST occurrence of its weekday in its month --
 * NSE index options only ever expire on one weekday, so "does the same
 * weekday one week later fall in the next month" is a complete monthly-vs-
 * weekly test without needing an exchange contract-type flag we don't have. */
function isMonthlyExpiry(expiryIso: string): boolean {
  const d = new Date(`${expiryIso}T00:00:00Z`);
  if (isNaN(d.getTime())) return false;
  const nextWeek = new Date(d);
  nextWeek.setUTCDate(d.getUTCDate() + 7);
  return nextWeek.getUTCMonth() !== d.getUTCMonth();
}

/** Broker-desk-style contract label -- e.g. "NIFTY SEP 22900 PE" for the
 * monthly contract, "NIFTY 22SEP 22900 PE" for a weekly one (the day is the
 * only thing that disambiguates one Thursday from another in the same
 * month). Built from the position's own underlying/expiry + the leg's
 * strike/type, not by parsing the raw Kite tradingsymbol. */
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

function fmtTime(iso: string): string {
  if (!iso) return '—';
  const d = new Date(iso.replace(' ', 'T'));
  return isNaN(d.getTime()) ? '—' : d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function fmtRupees(n: number): string {
  return `₹${Math.round(n).toLocaleString('en-IN')}`;
}

// Kite index spot symbols, one per underlying this desk trades. Matches
// src/options/nfo_chain.py index_spot_symbol()'s mapping -- keep in sync.
const INDEX_SPOT_SYMBOL: Record<string, string> = {
  NIFTY: 'NSE:NIFTY 50',
  BANKNIFTY: 'NSE:NIFTY BANK',
  FINNIFTY: 'NSE:NIFTY FIN SERVICE',
};

/** Live index ticker -- symbol, last price, day change % -- same /api/ltp
 * quote data and change-% formula the header's NIFTY/VIX ticker already uses
 * (src/store/tradingStore.ts), just reused per-underlying here instead of
 * hardcoding the bare "NIFTY"/"BANKNIFTY" text. */
function IndexTicker({ underlying }: { underlying: string }) {
  const root = underlying.split(':').pop() ?? underlying;
  const symbol = INDEX_SPOT_SYMBOL[root] ?? `NSE:${root}`;
  const { data } = useApi<Record<string, LtpQuote>>(`/api/ltp?symbols=${encodeURIComponent(symbol)}`, 10000);
  const q = data?.[symbol];
  const last = q?.last_price;
  const close = q?.ohlc?.close;
  const changePct = last != null && close ? ((last - close) / close) * 100 : null;

  return (
    <div className="flex items-center gap-2">
      <span className="font-semibold text-white text-base">{root}</span>
      {last != null && (
        <span className="font-mono text-sm text-gray-300">{last.toLocaleString('en-IN', { maximumFractionDigits: 2 })}</span>
      )}
      {changePct != null && (
        <span className={cn('flex items-center gap-0.5 text-xs font-medium', changePct >= 0 ? 'text-green-400' : 'text-red-400')}>
          {changePct >= 0 ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
          {changePct >= 0 ? '+' : ''}{changePct.toFixed(2)}%
        </span>
      )}
    </div>
  );
}

// One distinct color per strategy TYPE (not direction of today's P&L) so the
// strategy name is scannable across a long list -- bullish structures green,
// bearish orange, multi-leg/neutral purple, pure-debit volatility plays cyan.
const STRATEGY_COLORS: Record<string, string> = {
  bull_put_spread: 'text-green-400',
  bull_call_spread: 'text-green-400',
  bear_call_spread: 'text-orange-400',
  bear_put_spread: 'text-orange-400',
  iron_condor: 'text-purple-400',
  iron_butterfly: 'text-purple-400',
  weekly_condor_simple: 'text-purple-400',
  options_expiry_day: 'text-purple-400',
  long_straddle: 'text-cyan-400',
  long_strangle: 'text-cyan-400',
  calendar_spread: 'text-amber-400',
  diagonal_spread: 'text-amber-400',
};

function strategyColor(strategy: string): string {
  return STRATEGY_COLORS[strategy] ?? 'text-gray-300';
}

// Closed history older than today lives in Reports; the API only returns
// today's closes (closed=today), so "Closed today" needs no date logic here.
const STATUS_TABS: { key: 'OPEN' | 'CLOSED'; label: string }[] = [
  { key: 'OPEN', label: 'Open' },
  { key: 'CLOSED', label: 'Closed' },
];

function PnlStat({ label, value, bold }: { label: string; value: number | null; bold?: boolean }) {
  return (
    <div className="text-right">
      <div className="text-xs text-gray-500">{label}</div>
      <div className={cn('font-mono', bold ? 'text-xl font-bold' : 'text-sm font-semibold', value == null ? 'text-gray-500' : value >= 0 ? 'text-green-400' : 'text-red-400')}>
        {value == null ? '—' : `${value >= 0 ? '+' : ''}₹${value.toFixed(0)}`}
      </div>
    </div>
  );
}

function FundsMarginsPanel({ positions, modeFilter }: { positions: OptionPosition[]; modeFilter: 'all' | 'paper' | 'live' }) {
  const { data } = useApi<FundsMarginsResponse>('/api/options_funds_margins', 15000);

  // "all" has no single fund/margin figure to show (paper's sleeve and live's
  // broker cash are different pools) -- default to paper, the mode this desk
  // actually trades in day to day, same fallback the dashboard uses elsewhere.
  const effectiveMode = modeFilter === 'live' ? 'live' : 'paper';
  const thisMode = (p: OptionPosition) => modeFilter === 'all' || p.isPaper === (effectiveMode === 'paper');
  const open = positions.filter((p) => p.status === 'OPEN' && thisMode(p));
  // Positions closed TODAY also belong to "this mode, today" -- without them
  // a profit booked earlier today (closing the position) vanishes from both
  // Total and Today's P&L the moment it closes. allPositions already includes
  // today's closes (the page fetches closed=today).
  const closedToday = positions.filter((p) => p.status === 'CLOSED' && thisMode(p));

  const marginUsed = open.reduce((sum, p) => sum + (p.entryMargin ?? 0), 0);
  const unbookedTotal = open.reduce((sum, p) => sum + (p.unbookedPnl ?? p.pnl ?? 0), 0);
  const bookedTotal = [...open, ...closedToday].reduce((sum, p) => sum + (p.bookedPnl ?? 0), 0);
  const totalPnl = unbookedTotal + bookedTotal;

  const todayUnbooked = open.reduce((sum, p) => sum + (p.todayPnl ?? p.pnl ?? 0), 0);
  const todayBooked = closedToday.reduce((sum, p) => sum + (p.bookedPnl ?? p.pnl ?? 0), 0);
  const todayPnl = todayUnbooked + todayBooked;

  const ceiling = side_ceiling(data, effectiveMode);
  const ceilingPct = ceiling > 0 ? Math.min(999, (marginUsed / ceiling) * 100) : 0;
  const ceilingOver = ceiling > 0 && marginUsed > ceiling;
  const ceilingColor = ceilingOver ? 'text-amber-400' : 'text-green-400';
  const ceilingBarColor = ceilingOver ? 'bg-amber-500' : 'bg-green-500';
  const side = data?.[effectiveMode];

  return (
    <div>
      <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2.5">
        Funds &amp; Margins
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3.5">
        <div className="bg-[#161b22] border border-gray-800 rounded-xl p-4">
          <div className="text-xs text-gray-500 mb-2">Fund Available</div>
          <div className="font-mono text-xl font-bold text-gray-100">{side ? fmtRupees(side.fundAvailable) : '—'}</div>
          <div className="text-[11px] text-gray-600 mt-1">{effectiveMode === 'live' ? 'live, free cash' : 'paper account'}</div>
        </div>

        <div className="bg-[#161b22] border border-gray-800 rounded-xl p-4">
          <div className="text-xs text-gray-500 mb-2">Margin Available</div>
          <div className="font-mono text-xl font-bold text-gray-100">{side ? fmtRupees(side.marginAvailable) : '—'}</div>
          <div className="text-[11px] text-gray-600 mt-1">{effectiveMode === 'live' ? 'broker-reported, live' : 'paper, simulated'}</div>
        </div>

        <div className="bg-[#161b22] border border-gray-800 rounded-xl p-4">
          <div className="text-xs text-gray-500 mb-2">Margin Used — Options</div>
          <div className="font-mono text-xl font-bold text-gray-100">{fmtRupees(marginUsed)}</div>
          <div className="text-[11px] text-gray-600 mt-1">sum of entry margin, {open.length} open</div>
        </div>

        <div className="bg-[#161b22] border border-gray-800 rounded-xl p-4">
          <div className="text-xs text-gray-500 mb-2">Shared-Risk Ceiling</div>
          <div className={cn('font-mono text-lg font-bold', ceilingColor)}>
            {fmtRupees(marginUsed)} / {ceiling ? fmtRupees(ceiling) : '—'}
          </div>
          <div className="flex items-center gap-1.5 mt-2">
            <div className="flex-1 h-1.5 bg-gray-800 rounded-full overflow-hidden">
              <div className={cn('h-full', ceilingBarColor)} style={{ width: `${Math.min(100, ceilingPct)}%` }} />
            </div>
            <span className={cn('text-[11px] whitespace-nowrap', ceilingColor)}>{ceilingPct.toFixed(0)}%</span>
          </div>
        </div>

        <div className="bg-[#161b22] border border-gray-800 rounded-xl p-4">
          <div className="text-xs text-gray-500 mb-2">Total P&amp;L</div>
          <div className={cn('font-mono text-xl font-bold', totalPnl >= 0 ? 'text-green-400' : 'text-red-400')}>
            {totalPnl >= 0 ? '+' : ''}{fmtRupees(totalPnl)}
          </div>
          <div className="flex gap-3 text-[11px] text-gray-500 mt-1">
            <span>Unbooked {unbookedTotal >= 0 ? '+' : ''}{fmtRupees(unbookedTotal)}</span>
            <span>Booked {bookedTotal >= 0 ? '+' : ''}{fmtRupees(bookedTotal)}</span>
          </div>
        </div>

        <div className="bg-[#161b22] border border-gray-800 rounded-xl p-4">
          <div className="text-xs text-gray-500 mb-2">Today's P&amp;L</div>
          <div className={cn('font-mono text-xl font-bold', todayPnl >= 0 ? 'text-green-400' : 'text-red-400')}>
            {todayPnl >= 0 ? '+' : ''}{fmtRupees(todayPnl)}
          </div>
          <div className="flex gap-3 text-[11px] text-gray-500 mt-1">
            <span>Unbooked {todayUnbooked >= 0 ? '+' : ''}{fmtRupees(todayUnbooked)}</span>
            <span>Booked {todayBooked >= 0 ? '+' : ''}{fmtRupees(todayBooked)}</span>
          </div>
        </div>
      </div>
      <StrategyBreakdown open={open} closedToday={closedToday} />
    </div>
  );
}

// Per registry strategy AND mode: each strategy has its own paper/live switch, so one strategy
// can hold old paper positions while it trades live. Paper rows share the paper sleeve, live
// rows the broker's cash (the cards above).
function StrategyBreakdown({ open, closedToday }: { open: OptionPosition[]; closedToday: OptionPosition[] }) {
  const rows = new Map<string, { strategy: string; isPaper: boolean; open: number; margin: number; today: number }>();
  const row = (p: OptionPosition) => {
    const strategy = p.registryStrategy ?? p.strategy;
    const key = `${strategy}|${p.isPaper}`;
    if (!rows.has(key)) rows.set(key, { strategy, isPaper: p.isPaper, open: 0, margin: 0, today: 0 });
    return rows.get(key)!;
  };
  for (const p of open) {
    const r = row(p);
    r.open += 1;
    r.margin += p.entryMargin ?? 0;
    r.today += p.todayPnl ?? p.pnl ?? 0;
  }
  for (const p of closedToday) row(p).today += p.bookedPnl ?? p.pnl ?? 0;
  if (rows.size === 0) return null;
  const sorted = [...rows.values()].sort((a, b) => a.strategy.localeCompare(b.strategy) || Number(a.isPaper) - Number(b.isPaper));

  return (
    <div className="mt-3.5 bg-[#161b22] border border-gray-800 rounded-xl overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-xs text-gray-500 border-b border-gray-800">
            <th className="text-left font-normal px-4 py-2">Strategy</th>
            <th className="text-left font-normal px-4 py-2">Mode</th>
            <th className="text-right font-normal px-4 py-2">Open</th>
            <th className="text-right font-normal px-4 py-2">Margin Used</th>
            <th className="text-right font-normal px-4 py-2">Today's P&amp;L</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => (
            <tr key={`${r.strategy}|${r.isPaper}`} className="border-b border-gray-800/50 last:border-0">
              <td className="px-4 py-2 font-mono text-gray-200">{r.strategy}</td>
              <td className={cn('px-4 py-2 text-xs font-semibold', r.isPaper ? 'text-blue-400' : 'text-amber-400')}>
                {r.isPaper ? 'PAPER' : 'LIVE'}
              </td>
              <td className="px-4 py-2 text-right font-mono text-gray-300">{r.open}</td>
              <td className="px-4 py-2 text-right font-mono text-gray-300">{fmtRupees(r.margin)}</td>
              <td className={cn('px-4 py-2 text-right font-mono', r.today >= 0 ? 'text-green-400' : 'text-red-400')}>
                {r.today >= 0 ? '+' : ''}{fmtRupees(r.today)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function side_ceiling(data: FundsMarginsResponse | null, mode: 'paper' | 'live'): number {
  return data?.[mode]?.sharedRiskCeiling ?? 0;
}

export function OptionsTrading() {
  const { data } = useApi<OptionPosition[]>('/api/options_positions?closed=today', 5000);
  const allPositions = data ?? [];
  const [status, setStatus] = useState<'OPEN' | 'CLOSED'>('OPEN');
  const { modeFilter } = useModeFilterStore();

  const wantPaper = modeFilter === 'paper';
  const byMode = modeFilter === 'all' ? allPositions : allPositions.filter((p) => p.isPaper === wantPaper);
  // A position moves to Closed only when ALL its legs are closed; until then a leg that was stopped out
  // stays in the same card, greyed out.
  const rows = byMode.filter((p) => (status === 'OPEN' ? p.status === 'OPEN' : p.status !== 'OPEN'));
  const openCount = byMode.filter((p) => p.status === 'OPEN').length;

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold text-white">📊 Options Trading</h2>
        <p className="text-gray-400">Every seller-desk structure — legs, live P&L, max profit/loss — paper and live</p>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <div className="flex gap-2">
          {STATUS_TABS.map((s) => (
            <button
              key={s.key}
              onClick={() => setStatus(s.key)}
              className={cn(
                'px-4 py-1.5 rounded-lg text-sm font-medium border transition-colors',
                status === s.key
                  ? 'bg-blue-500/20 border-blue-500/40 text-blue-300'
                  : 'bg-[#161b22] border-gray-800 text-gray-400 hover:text-gray-200'
              )}
            >
              {s.label}
            </button>
          ))}
        </div>
        <ModeToggle />
        {openCount > 0 && (
          <span className="text-xs text-gray-500">{openCount} open</span>
        )}
      </div>

      <FundsMarginsPanel positions={allPositions} modeFilter={modeFilter} />

      {rows.length === 0 && (
        <p className="text-gray-400 text-sm py-8 text-center">
          {status === 'OPEN' ? 'No open options positions' : 'Nothing closed today — earlier trades are in Reports'}
        </p>
      )}

      <div className="space-y-3">
        {rows.map((p) => (
          <div key={p.positionId} className="bg-[#161b22] rounded-xl border border-gray-800 p-4 space-y-3">
            {/* Strategy header — live underlying ticker, strategy, state/mode
                badges, and the position's Total/Unbooked/Booked P&L. */}
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div className="flex items-center gap-3 flex-wrap">
                <IndexTicker underlying={p.underlying} />
                <span className={cn('text-xs font-bold uppercase tracking-wide', strategyColor(p.strategy))}>
                  {p.strategy}
                </span>
                <span
                  className={cn(
                    'px-2 py-0.5 rounded text-xs font-medium border',
                    p.status === 'OPEN'
                      ? 'text-blue-300 bg-blue-500/10 border-blue-500/30'
                      : 'text-gray-400 bg-gray-500/10 border-gray-700'
                  )}
                >
                  {p.status}
                </span>
                <span
                  className={cn(
                    'px-2 py-0.5 rounded text-xs font-medium border',
                    p.isPaper
                      ? 'text-cyan-400 bg-cyan-500/10 border-cyan-500/30'
                      : 'text-purple-400 bg-purple-500/10 border-purple-500/30'
                  )}
                >
                  {p.isPaper ? 'Paper' : 'Live'}
                </span>
                {p.product && (
                  <span
                    className={cn(
                      'px-2 py-0.5 rounded text-xs font-medium border',
                      p.product === 'MIS'
                        ? 'text-amber-400 bg-amber-500/10 border-amber-500/30'
                        : 'text-gray-300 bg-gray-500/10 border-gray-600'
                    )}
                    title={p.product === 'MIS' ? 'Intraday (MIS) - broker squares off near close' : 'Carry-forward (NRML)'}
                  >
                    {p.product}
                  </span>
                )}
              </div>

              <div className="flex gap-5">
                <PnlStat label="Total P&L" value={p.pnl} bold />
                <PnlStat label="Unbooked" value={p.status === 'OPEN' ? (p.unbookedPnl ?? p.pnl) : 0} />
                <PnlStat label="Booked" value={p.bookedPnl ?? 0} />
              </div>
            </div>

            {/* Structure-level stats: margin / max profit / max loss / POP / breakeven
                -- these apply to the whole structure, not any one leg, so they live
                here instead of as rowspan columns inside the per-leg table below. */}
            <div className="flex flex-wrap items-start gap-6 bg-[#0d1117] border border-gray-800 rounded-lg px-4 py-2.5 text-sm">
              <div>
                <div className="text-xs text-gray-500">Margin Used</div>
                <div className="font-mono font-semibold mt-0.5">{p.entryMargin != null ? `₹${p.entryMargin.toFixed(0)}` : '—'}</div>
              </div>
              <div>
                <div className="text-xs text-gray-500">Max Profit</div>
                <div className="font-mono text-green-400 mt-0.5">₹{p.maxProfit.toFixed(0)}</div>
              </div>
              <div>
                <div className="text-xs text-gray-500">Max Loss</div>
                <div className="font-mono text-red-400 mt-0.5">₹{p.maxLoss.toFixed(0)}</div>
              </div>
              <div>
                <div className="text-xs text-gray-500">POP</div>
                <div className="font-mono font-semibold mt-0.5">{p.pop != null ? `${(p.pop * 100).toFixed(2)}%` : '—'}</div>
              </div>
              <div className="border-l border-gray-800 pl-6">
                <div className="text-xs text-gray-500">Breakeven</div>
                <div className="font-mono font-semibold mt-0.5">
                  {!p.breakeven || p.breakeven.length === 0
                    ? '—'
                    : p.breakeven
                        .map((b) =>
                          b.pctFromSpot != null
                            ? `${b.strike.toLocaleString('en-IN')} (${b.pctFromSpot >= 0 ? '+' : ''}${b.pctFromSpot.toFixed(1)}%)`
                            : b.strike.toLocaleString('en-IN')
                        )
                        .join(', ')}
                </div>
              </div>
              {/* Auto-exit levels (open positions): total P&L at which the desk books profit / stops out. */}
              {status === 'OPEN' && (
                <>
                  <div className="border-l border-gray-800 pl-6">
                    <div className="text-xs text-gray-500">Target (exit)</div>
                    <div className="font-mono font-semibold text-green-400 mt-0.5">
                      {p.targetAmount != null
                        ? `+₹${Math.round(p.targetAmount).toLocaleString('en-IN')}${p.targetPct != null ? ` (${p.targetPct.toFixed(0)}%)` : ''}`
                        : '—'}
                    </div>
                  </div>
                  <div>
                    <div className="text-xs text-gray-500">Stop Loss (exit)</div>
                    <div className="font-mono font-semibold text-red-400 mt-0.5">
                      {p.stopAmount != null
                        ? `−₹${Math.round(p.stopAmount).toLocaleString('en-IN')}${p.stopPct != null ? ` (${p.stopPct.toFixed(1)}%)` : ''}`
                        : '—'}
                    </div>
                  </div>
                </>
              )}
            </div>

            {/* Per-leg table: Contract/Side/Lots/Entry/LTP/Leg P&L only. */}
            <div className="overflow-x-auto rounded-lg border border-gray-800">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-gray-500 text-left bg-[#0d1117] border-b border-gray-700">
                    <th className="py-1.5 px-3 font-medium">Contract</th>
                    <th className="py-1.5 px-3 font-medium">Side</th>
                    <th className="py-1.5 px-3 font-medium">Lots</th>
                    <th className="py-1.5 px-3 font-medium">Entry Premium</th>
                    <th className="py-1.5 px-3 font-medium">{status === 'OPEN' ? 'LTP / Exit' : 'Exit Price'}</th>
                    <th className="py-1.5 px-3 font-medium">Leg P&L</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-700">
                  {p.legs.length === 0 && (
                    <tr className="text-gray-200">
                      <td className="py-1.5 px-3 text-gray-600" colSpan={6}>—</td>
                    </tr>
                  )}
                  {p.legs.map((leg) => (
                    <tr
                      key={leg.tradingsymbol}
                      className={leg.state === 'CLOSED' && p.status === 'OPEN' ? 'text-gray-500 opacity-60' : 'text-gray-200'}
                    >
                      <td className="py-1.5 px-3 font-mono">
                        <div className="flex items-center gap-2">
                          <span>{legLabel(p.underlying, p.expiry, leg)}</span>
                          {leg.state === 'CLOSED' && p.status === 'OPEN' && (
                            <span
                              className="px-1.5 py-0.5 rounded text-[10px] font-sans font-medium border shrink-0 whitespace-nowrap text-red-300 bg-red-500/10 border-red-500/30"
                              title={leg.exitReason}
                            >
                              Closed{leg.exitAt ? ` ${fmtTime(leg.exitAt)}` : ''}{leg.exitReason ? ` · ${leg.exitReason}` : ''}
                            </span>
                          )}
                          <span
                            className={cn(
                              'px-1.5 py-0.5 rounded text-[10px] font-sans font-medium border shrink-0 whitespace-nowrap',
                              isMonthlyExpiry(p.expiry)
                                ? 'text-indigo-300 bg-indigo-500/10 border-indigo-500/30'
                                : 'text-amber-300 bg-amber-500/10 border-amber-500/30'
                            )}
                          >
                            {isMonthlyExpiry(p.expiry) ? 'Monthly' : 'Weekly'}
                          </span>
                        </div>
                      </td>
                      <td className="py-1.5 px-3">
                        <span className={cn('font-semibold', leg.direction === 'BUY' ? 'text-blue-300' : 'text-orange-300')}>
                          {leg.direction}
                        </span>
                      </td>
                      <td className="py-1.5 px-3 font-mono text-gray-400">{leg.lots}</td>
                      <td className="py-1.5 px-3 font-mono text-gray-400">{leg.premium.toFixed(2)}</td>
                      <td className="py-1.5 px-3 font-mono text-gray-400">{(() => {
                        const px = leg.state === 'CLOSED' ? leg.exitPremium : p.status === 'OPEN' ? leg.ltp : leg.exitPremium;
                        return px != null ? px.toFixed(2) : '—';
                      })()}</td>
                      <td className="py-1.5 px-3 font-mono">
                        {leg.pnl == null ? (
                          <span className="text-gray-500">—</span>
                        ) : (
                          <span className={leg.pnl >= 0 ? 'text-green-400' : 'text-red-400'}>
                            {leg.pnl >= 0 ? '+' : ''}₹{leg.pnl.toFixed(0)}
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-gray-500">
              <span>Opened {fmtTime(p.openedAt)}{p.closedAt ? ` · Closed ${fmtTime(p.closedAt)}` : ''}</span>
              {p.exitReason && <span>Exit: {p.exitReason}</span>}
            </div>
          </div>
        ))}
      </div>

      <div className="flex items-center gap-2 text-xs text-gray-600">
        <Link2 className="w-3 h-3" /> Max Profit/Max Loss are the real DB-backed figures — this engine has no fixed "target" like equity SL/target.
      </div>
    </div>
  );
}
