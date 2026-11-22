import { useApi } from '../../hooks/useApi';
import { cn } from '../../utils/cn';
import { fmtRupees, fmtSigned, pnlClass } from './format';
import type { FundsMarginsResponse, OptionPosition } from './types';

type Mode = 'paper' | 'live';

/** Margin/P&L for one account (paper sleeve or live broker). Independent of the
 * "Closed positions" toggle: today's booked P&L always counts toward the account. */
function accountFigures(positions: OptionPosition[], mode: Mode) {
  const mine = positions.filter((p) => p.isPaper === (mode === 'paper'));
  const open = mine.filter((p) => p.status === 'OPEN');
  const closedToday = mine.filter((p) => p.status !== 'OPEN');
  const marginUsed = open.reduce((s, p) => s + (p.entryMargin ?? 0), 0);
  const unbooked = open.reduce((s, p) => s + (p.unbookedPnl ?? p.pnl ?? 0), 0);
  const booked = [...open, ...closedToday].reduce((s, p) => s + (p.bookedPnl ?? 0), 0);
  const todayUnbooked = open.reduce((s, p) => s + (p.todayPnl ?? p.pnl ?? 0), 0);
  const todayBooked = closedToday.reduce((s, p) => s + (p.bookedPnl ?? p.pnl ?? 0), 0);
  return { openCount: open.length, marginUsed, unbooked, booked, todayUnbooked, todayBooked };
}

function Card({ label, children, sub }: { label: string; children: React.ReactNode; sub?: React.ReactNode }) {
  return (
    <div className="bg-[#161b22] border border-gray-800 rounded-xl p-4 min-w-0">
      <div className="text-xs text-gray-500">{label}</div>
      <div className="mt-2 truncate">{children}</div>
      {sub != null && <div className="text-[11px] text-gray-500 mt-1.5 truncate">{sub}</div>}
    </div>
  );
}

export function FundsMarginsPanel({ positions, modeFilter }: { positions: OptionPosition[]; modeFilter: 'all' | Mode }) {
  const { data } = useApi<FundsMarginsResponse>('/api/options_funds_margins', 15000);

  // Paper's sleeve and live's broker cash are different pools, so "all" never adds them:
  // it shows the LIVE account (real money) with paper's figure in the sub-line.
  const mode: Mode = modeFilter === 'paper' ? 'paper' : 'live';
  const side = data?.[mode];
  const paperSide = data?.paper;
  const a = accountFigures(positions, mode);
  const paper = accountFigures(positions, 'paper');
  const isAll = modeFilter === 'all';
  const note = (live: string, paperVal: number | undefined) =>
    isAll ? `live · paper ${paperVal != null ? fmtRupees(paperVal) : '—'}` : live;

  // P&L cards follow the filter: "all" = both accounts' P&L together (P&L does add up).
  const both = isAll ? paper : null;
  const unbooked = a.unbooked + (both?.unbooked ?? 0);
  const booked = a.booked + (both?.booked ?? 0);
  const todayUnbooked = a.todayUnbooked + (both?.todayUnbooked ?? 0);
  const todayBooked = a.todayBooked + (both?.todayBooked ?? 0);

  const ceiling = side?.sharedRiskCeiling ?? 0;
  const ceilingPct = ceiling > 0 ? Math.min(999, (a.marginUsed / ceiling) * 100) : 0;
  const ceilingOver = ceiling > 0 && a.marginUsed > ceiling;
  const num = 'text-xl font-bold tabular-nums';

  return (
    <div>
      <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2.5">Funds &amp; Margins</div>
      <div className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-6 gap-3">
        <Card label="Fund Available" sub={note(mode === 'live' ? 'live, free cash' : 'paper account', paperSide?.fundAvailable)}>
          <span className={cn(num, 'text-gray-100')}>{side ? fmtRupees(side.fundAvailable) : '—'}</span>
        </Card>
        <Card label="Margin Available" sub={note(mode === 'live' ? 'broker-reported, live' : 'paper, simulated', paperSide?.marginAvailable)}>
          <span className={cn(num, 'text-gray-100')}>{side ? fmtRupees(side.marginAvailable) : '—'}</span>
        </Card>
        <Card label="Margin Used — Options" sub={isAll ? `live · paper ${fmtRupees(paper.marginUsed)}` : `sum of entry margin, ${a.openCount} open`}>
          <span className={cn(num, 'text-gray-100')}>{fmtRupees(a.marginUsed)}</span>
        </Card>
        <Card
          label="Shared-Risk Ceiling"
          sub={
            <span className="flex items-center gap-1.5">
              <span className="flex-1 h-1.5 bg-gray-800 rounded-full overflow-hidden">
                <span className={cn('block h-full', ceilingOver ? 'bg-amber-500' : 'bg-green-500')} style={{ width: `${Math.min(100, ceilingPct)}%` }} />
              </span>
              <span className={ceilingOver ? 'text-amber-400' : 'text-green-400'}>{ceilingPct.toFixed(0)}%</span>
            </span>
          }
        >
          <span className={cn('text-base font-bold tabular-nums', ceilingOver ? 'text-amber-400' : 'text-green-400')}>
            {fmtRupees(a.marginUsed)} / {ceiling ? fmtRupees(ceiling) : '—'}
          </span>
        </Card>
        <Card label="Total P&L" sub={`Unbooked ${fmtSigned(unbooked)} · Booked ${fmtSigned(booked)}`}>
          <span className={cn(num, pnlClass(unbooked + booked))}>{fmtSigned(unbooked + booked)}</span>
        </Card>
        <Card label="Today's P&L" sub={`Unbooked ${fmtSigned(todayUnbooked)} · Booked ${fmtSigned(todayBooked)}`}>
          <span className={cn(num, pnlClass(todayUnbooked + todayBooked))}>{fmtSigned(todayUnbooked + todayBooked)}</span>
        </Card>
      </div>
    </div>
  );
}
