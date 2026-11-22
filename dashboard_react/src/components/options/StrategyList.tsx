import { cn } from '../../utils/cn';
import { summarize, type StrategyGroup } from './aggregate';
import { exitText, fmtSigned, pnlClass } from './format';
import { ClosedToggle, ModeBadge, PnlSummaryCard } from './widgets';

const COLS = 'grid grid-cols-[minmax(0,2.4fr)_minmax(0,0.8fr)_minmax(0,1.6fr)_minmax(0,0.5fr)_minmax(0,1fr)_minmax(0,1.3fr)_minmax(0,1.3fr)_minmax(0,0.9fr)] gap-3 items-center';

/** Screen 1: every strategy with something to show, one row per strategy + mode. */
export function StrategyList({
  groups,
  showClosed,
  setShowClosed,
  onOpen,
}: {
  groups: StrategyGroup[];
  showClosed: boolean;
  setShowClosed: (v: boolean) => void;
  onOpen: (key: string) => void;
}) {
  const live = summarize(groups.filter((g) => !g.isPaper));
  const paper = summarize(groups.filter((g) => g.isPaper));

  return (
    <div className="flex flex-col lg:flex-row gap-5 items-start">
      <aside className="w-full lg:w-80 shrink-0 bg-[#161b22] border border-gray-800 rounded-xl p-5 space-y-4">
        <div className="text-sm font-semibold text-gray-100">
          {groups.length} open strateg{groups.length === 1 ? 'y' : 'ies'}
        </div>
        <PnlSummaryCard s={summarize(groups)} />
        <div className="space-y-2.5 text-sm border-t border-gray-800 pt-4">
          <div className="flex justify-between items-center">
            <span className="flex items-center gap-2"><ModeBadge isPaper={false} /> {groups.filter((g) => !g.isPaper).length}</span>
            <span className={cn('font-semibold tabular-nums', pnlClass(live.total))}>{fmtSigned(live.total)}</span>
          </div>
          <div className="flex justify-between items-center">
            <span className="flex items-center gap-2"><ModeBadge isPaper /> {groups.filter((g) => g.isPaper).length}</span>
            <span className={cn('font-semibold tabular-nums', pnlClass(paper.total))}>{fmtSigned(paper.total)}</span>
          </div>
        </div>
        <div className="border-t border-gray-800 pt-2">
          <ClosedToggle checked={showClosed} onChange={setShowClosed} />
        </div>
      </aside>

      <div className="flex-1 min-w-0 bg-[#161b22] border border-gray-800 rounded-xl p-5">
        <h3 className="text-lg font-semibold text-white mb-4">Option strategies</h3>
        <div className="overflow-x-auto border border-gray-800 rounded-lg">
          <div className="min-w-[860px]">
            <div className={cn(COLS, 'px-4 py-3 text-xs text-gray-500 bg-[#0d1117] border-b border-gray-800')}>
              <span>Strategy</span>
              <span>Mode</span>
              <span>Underlying · structure</span>
              <span className="text-right">Legs</span>
              <span className="text-right">Total P&amp;L</span>
              <span className="text-right">Target (exit)</span>
              <span className="text-right">Stop Loss (exit)</span>
              <span className="text-right">Booked</span>
            </div>
            {groups.length === 0 && (
              <p className="text-gray-500 text-sm py-8 text-center">
                No open options strategies{showClosed ? ' and nothing closed today' : ''}
              </p>
            )}
            {groups.map((g) => (
              <button
                key={g.key}
                onClick={() => onOpen(g.key)}
                className={cn(COLS, 'w-full text-left px-4 min-h-[52px] text-sm border-b border-gray-800 last:border-0 hover:bg-gray-800 transition-colors')}
              >
                <span className="font-semibold text-indigo-400 truncate">{g.name}</span>
                <span><ModeBadge isPaper={g.isPaper} /></span>
                <span className="text-gray-400 truncate">{g.underlyings} · {g.structures}</span>
                <span className="text-right tabular-nums text-gray-300">{g.openLegs}</span>
                <span className={cn('text-right font-semibold tabular-nums', pnlClass(g.total))}>{fmtSigned(g.total)}</span>
                <span className="text-right tabular-nums text-green-400 truncate">{exitText(g.target, g.targetPct, 1)}</span>
                <span className="text-right tabular-nums text-red-400 truncate">{exitText(g.stop, g.stopPct, -1)}</span>
                <span className={cn('text-right tabular-nums', pnlClass(g.booked))}>{fmtSigned(g.booked)}</span>
              </button>
            ))}
          </div>
        </div>
        <p className="text-xs text-gray-500 mt-3">Click a strategy to see its positions.</p>
      </div>
    </div>
  );
}
