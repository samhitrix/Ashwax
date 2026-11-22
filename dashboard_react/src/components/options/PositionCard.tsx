import { Clock, TrendingDown, TrendingUp } from 'lucide-react';
import { useApi } from '../../hooks/useApi';
import { cn } from '../../utils/cn';
import type { ViewPosition } from './aggregate';
import { exitText, fmtRupees, fmtSigned, fmtTime, legLabel, pnlClass, rootOf } from './format';
import { ModeBadge, ProductBadge } from './widgets';

interface LtpQuote {
  last_price?: number;
  ohlc?: { close?: number };
}

// Kite index spot symbols -- matches src/options/nfo_chain.py index_spot_symbol(); keep in sync.
const INDEX_SPOT_SYMBOL: Record<string, string> = {
  NIFTY: 'NSE:NIFTY 50',
  BANKNIFTY: 'NSE:NIFTY BANK',
  FINNIFTY: 'NSE:NIFTY FIN SERVICE',
  SENSEX: 'BSE:SENSEX',
};

/** Live index ticker -- same /api/ltp quote + change-% formula as the header ticker. */
function IndexTicker({ underlying, spot }: { underlying: string; spot: number | null }) {
  const root = rootOf(underlying);
  const symbol = INDEX_SPOT_SYMBOL[root] ?? `NSE:${root}`;
  const { data } = useApi<Record<string, LtpQuote>>(`/api/ltp?symbols=${encodeURIComponent(symbol)}`, 10000);
  const q = data?.[symbol];
  // A missed quote poll falls back to the position's own server-side spot (same Kite LTP, 5s refresh).
  const last = q?.last_price ?? spot ?? undefined;
  const close = q?.ohlc?.close;
  const changePct = last != null && close ? ((last - close) / close) * 100 : null;
  return (
    <span className="flex items-center gap-2 text-[15px]">
      <span className="font-bold text-white">{root}</span>
      {last != null && <span className="tabular-nums text-gray-300">{last.toLocaleString('en-IN', { maximumFractionDigits: 2 })}</span>}
      {changePct != null && (
        <span className={cn('flex items-center gap-0.5 font-semibold', changePct >= 0 ? 'text-green-400' : 'text-red-400')}>
          {changePct >= 0 ? <TrendingUp className="w-3.5 h-3.5" /> : <TrendingDown className="w-3.5 h-3.5" />}
          {changePct >= 0 ? '+' : ''}
          {changePct.toFixed(2)}%
        </span>
      )}
    </span>
  );
}

function Stat({ label, value, sub, valueClass, tint }: { label: string; value: string; sub?: string; valueClass?: string; tint?: string }) {
  return (
    <div className={cn('flex-1 shrink-0 px-3 py-3 text-center whitespace-nowrap border-r border-gray-800 last:border-0', tint)}>
      <div className="text-xs text-gray-500">{label}</div>
      <div className={cn('text-lg font-semibold tabular-nums mt-1', valueClass ?? 'text-gray-100')}>{value}</div>
      {sub && <div className="text-[11px] text-gray-500 mt-0.5">{sub}</div>}
    </div>
  );
}

/** Human structure name ("Bull Put Spread"). The API's `strategy` holds the structure for the seller
 * desk but just repeats the registry name for short_premium -- there it is read off the legs. */
function structureName(p: ViewPosition): string | null {
  if (p.strategy && p.strategy !== p.registryStrategy) {
    return p.strategy.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  }
  const legs = p.legs;
  const ce = legs.filter((l) => /CE$/.test(l.tradingsymbol));
  const pe = legs.filter((l) => /PE$/.test(l.tradingsymbol));
  if (legs.length === 2 && ce.length === 1 && pe.length === 1 && legs.every((l) => l.direction === 'SELL')) {
    return ce[0].strike === pe[0].strike ? 'Short Straddle' : 'Short Strangle';
  }
  return null;
}

const LEG_COLS ='grid grid-cols-[minmax(0,2.6fr)_minmax(0,0.6fr)_minmax(0,0.9fr)_minmax(0,0.9fr)_minmax(0,0.9fr)_minmax(0,0.9fr)_minmax(0,0.9fr)] gap-3 items-center';

/** One options structure: header, single-line stats row, legs. */
export function PositionCard({ p, title }: { p: ViewPosition; title: string }) {
  const isOpen = p.status === 'OPEN';
  const away = (level: number | null | undefined) =>
    level != null && isOpen && p.pnl != null ? `${fmtRupees(Math.abs(level - p.pnl))} away` : undefined;
  const breakeven = p.breakeven?.length
    ? p.breakeven
        .map((b) =>
          b.pctFromSpot != null
            ? `${b.strike.toLocaleString('en-IN')} (${b.pctFromSpot >= 0 ? '+' : '−'}${Math.abs(b.pctFromSpot).toFixed(1)}%)`
            : b.strike.toLocaleString('en-IN')
        )
        .join(' · ')
    : '—';

  return (
    <section className="bg-[#161b22] border border-gray-800 rounded-xl p-5 min-w-0">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 space-y-1.5">
          <div className="flex items-center gap-2.5 flex-wrap">
            <h3 className="text-xl font-semibold text-indigo-400">{title}</h3>
            {structureName(p) && <span className="text-base font-semibold text-gray-200">· {structureName(p)}</span>}
            <ModeBadge isPaper={p.isPaper} />
            <ProductBadge product={p.product} />
            {!isOpen && <span className="px-2 py-0.5 rounded text-[11px] font-semibold text-gray-400 bg-gray-500/10">CLOSED</span>}
          </div>
          <IndexTicker underlying={p.underlying} spot={p.spot} />
          <div className="text-sm text-gray-500">
            Breakeven: <span className="font-semibold text-gray-200">{breakeven}</span>
          </div>
        </div>
        <div className="text-sm text-gray-400 flex items-center gap-1.5">
          <Clock className="w-4 h-4" aria-hidden="true" />
          {isOpen ? `Opened ${fmtTime(p.openedAt)}` : `Closed ${fmtTime(p.closedAt)}${p.exitReason ? ` · ${p.exitReason}` : ''}`}
        </div>
      </div>

      <div className="flex flex-nowrap overflow-x-auto border border-gray-800 rounded-lg mt-4">
        <Stat label="Total P&L" value={fmtSigned(p.vTotal)} valueClass={pnlClass(p.vTotal)} />
        <Stat label="Booked" value={fmtSigned(p.vBooked)} valueClass={pnlClass(p.vBooked)} />
        <Stat label="Unbooked" value={fmtSigned(p.vUnbooked)} valueClass={pnlClass(p.vUnbooked)} />
        <Stat label="Max profit" value={fmtSigned(p.maxProfit)} />
        <Stat label="Max loss" value={fmtSigned(-Math.abs(p.maxLoss))} />
        <Stat
          label="Target (exit)"
          value={isOpen ? exitText(p.targetAmount, p.targetPct, 1) : '—'}
          sub={away(p.targetAmount)}
          valueClass="text-green-400"
          tint="bg-green-500/5"
        />
        <Stat
          label="Stop Loss (exit)"
          value={isOpen ? exitText(p.stopAmount, p.stopPct, -1) : '—'}
          sub={p.stopAmount != null ? away(-Math.abs(p.stopAmount)) : undefined}
          valueClass="text-red-400"
          tint="bg-red-500/5"
        />
        <Stat label="Margin used" value={p.entryMargin != null ? fmtRupees(p.entryMargin) : '—'} sub="at entry" />
      </div>

      <div className="overflow-x-auto mt-4">
        <div className="min-w-[680px] text-sm">
          <div className={cn(LEG_COLS, 'px-2 py-2.5 text-xs text-gray-500 border-b border-gray-800')}>
            <span>Name</span>
            <span className="text-right">Lots</span>
            <span className="text-right">Avg</span>
            <span className="text-right">{isOpen ? 'LTP' : 'Exit'}</span>
            <span className="text-right">Booked</span>
            <span className="text-right">Unbooked</span>
            <span className="text-right">P/L</span>
          </div>
          {p.visibleLegs.length === 0 && <div className="px-2 py-3 text-gray-500">—</div>}
          {p.visibleLegs.map((leg) => {
            const closedLeg = leg.state === 'CLOSED' || !isOpen;
            const px = closedLeg ? leg.exitPremium : leg.ltp;
            const sell = leg.direction !== 'BUY';
            return (
              <div
                key={`${leg.tradingsymbol}-${leg.state ?? ''}-${leg.exitAt ?? ''}`}
                className={cn(LEG_COLS, 'px-2 min-h-[50px] border-b border-gray-800 last:border-0', closedLeg && isOpen ? 'text-gray-500 bg-[#0d1117]' : 'text-gray-200')}
              >
                <span className="flex items-center gap-2 min-w-0">
                  <span className="shrink-0">
                    <ProductBadge product={p.product} />
                  </span>
                  <span className="truncate font-semibold">{legLabel(p.underlying, p.expiry, leg)}</span>
                  {closedLeg && isOpen && (
                    <span className="shrink-0 text-xs text-gray-500" title={leg.exitReason}>
                      · closed{leg.exitAt ? ` ${fmtTime(leg.exitAt)}` : ''}
                    </span>
                  )}
                </span>
                <span className="text-right tabular-nums">{closedLeg && isOpen ? 0 : `${sell ? '−' : ''}${leg.lots}`}</span>
                <span className="text-right tabular-nums">{leg.premium.toFixed(2)}</span>
                <span className="text-right tabular-nums">{px != null ? px.toFixed(2) : '—'}</span>
                <span className={cn('text-right tabular-nums', closedLeg ? pnlClass(leg.pnl) : '')}>{closedLeg ? fmtSigned(leg.pnl) : '0'}</span>
                <span className={cn('text-right tabular-nums', closedLeg ? '' : pnlClass(leg.pnl))}>{closedLeg ? '0' : fmtSigned(leg.pnl)}</span>
                <span className={cn('text-right font-semibold tabular-nums', pnlClass(leg.pnl))}>{fmtSigned(leg.pnl)}</span>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
