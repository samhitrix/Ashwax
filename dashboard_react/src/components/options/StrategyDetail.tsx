import { cn } from '../../utils/cn';
import { summarize, type StrategyGroup } from './aggregate';
import { fmtSigned, pnlClass } from './format';
import { PositionCard } from './PositionCard';
import { ClosedToggle, ModeBadge, PnlSummaryCard } from './widgets';

export type DetailView = 'positions' | 'groups';

/** Screen 2: left panel (view toggle, summary, clickable strategy cards) + position cards. */
export function StrategyDetail({
  groups,
  selected,
  onSelect,
  view,
  setView,
  showClosed,
  setShowClosed,
}: {
  groups: StrategyGroup[];
  selected: StrategyGroup | null;
  onSelect: (key: string) => void;
  view: DetailView;
  setView: (v: DetailView) => void;
  showClosed: boolean;
  setShowClosed: (v: boolean) => void;
}) {
  const shown = view === 'groups' ? groups : selected ? [selected] : [];

  return (
    <div className="flex flex-col lg:flex-row gap-5 items-start">
      <aside className="w-full lg:w-80 shrink-0 bg-[#161b22] border border-gray-800 rounded-xl p-4 space-y-4">
        <div className="grid grid-cols-2 gap-1 p-1 border border-gray-800 rounded-lg" role="tablist">
          {(['positions', 'groups'] as const).map((v) => (
            <button
              key={v}
              role="tab"
              aria-selected={view === v}
              onClick={() => setView(v)}
              className={cn(
                'min-h-[40px] rounded-md text-sm font-semibold capitalize transition-colors',
                view === v ? 'bg-blue-600 text-white' : 'text-gray-400 hover:text-gray-200'
              )}
            >
              {v}
            </button>
          ))}
        </div>
        {/* Totals for everything the All/Paper/Live filter shows, not just the selected strategy. */}
        <PnlSummaryCard s={summarize(groups)} />
        <ClosedToggle checked={showClosed} onChange={setShowClosed} />
        <div className="space-y-2.5">
          {groups.map((g) => {
            const active = view === 'positions' && selected?.key === g.key;
            return (
              <button
                key={g.key}
                onClick={() => onSelect(g.key)}
                aria-label={`Open ${g.name} positions`}
                className={cn(
                  'w-full text-left rounded-lg p-3.5 border transition-colors',
                  active ? 'border-blue-500 border-2 bg-blue-500/10' : 'border-gray-800 hover:bg-gray-800'
                )}
              >
                <span className="flex items-center gap-2 flex-wrap">
                  <span className="text-sm font-semibold text-indigo-400 break-all">{g.name}</span>
                  <ModeBadge isPaper={g.isPaper} />
                </span>
                <span className="flex justify-between mt-1.5 text-sm text-gray-500">
                  <span>{g.openLegs} leg{g.openLegs === 1 ? '' : 's'} open</span>
                  <span className={cn('font-semibold tabular-nums', pnlClass(g.total))}>{fmtSigned(g.total)}</span>
                </span>
              </button>
            );
          })}
        </div>
      </aside>

      <div className="flex-1 min-w-0 space-y-4">
        {shown.length === 0 && <p className="text-gray-500 text-sm py-8 text-center">Nothing to show for this filter</p>}
        {shown.flatMap((g) => g.positions.map((p) => <PositionCard key={p.positionId} p={p} title={g.name} />))}
      </div>
    </div>
  );
}
