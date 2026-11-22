import { useMemo, useState } from 'react';
import { ChevronRight } from 'lucide-react';
import { useApi } from '../hooks/useApi';
import { useModeFilterStore } from '../store/modeFilterStore';
import { ModeToggle } from './ModeToggle';
import { groupByStrategy, viewOf, type ViewPosition } from './options/aggregate';
import { FundsMarginsPanel } from './options/FundsMarginsPanel';
import { StrategyDetail, type DetailView } from './options/StrategyDetail';
import { StrategyList } from './options/StrategyList';
import type { OptionPosition } from './options/types';

// Reports imports legLabel from here.
// eslint-disable-next-line react-refresh/only-export-components
export { legLabel } from './options/format';

/** Options Trading tab: strategy list -> click -> positions/groups detail.
 * Data: /api/options_positions?closed=today (open + today's closes, paper AND live). */
export function OptionsTrading() {
  const { data } = useApi<OptionPosition[]>('/api/options_positions?closed=today', 5000);
  const { modeFilter } = useModeFilterStore();
  const [showClosed, setShowClosed] = useState(true);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [view, setView] = useState<DetailView>('positions');

  const allPositions = useMemo(() => data ?? [], [data]);
  const groups = useMemo(() => {
    const byMode = modeFilter === 'all' ? allPositions : allPositions.filter((p) => p.isPaper === (modeFilter === 'paper'));
    const views = byMode.map((p) => viewOf(p, showClosed)).filter((v): v is ViewPosition => v != null);
    return groupByStrategy(views);
  }, [allPositions, modeFilter, showClosed]);

  const inDetail = selectedKey != null;
  // A strategy filtered out by the mode toggle falls back to the first one still shown.
  const selected = inDetail ? groups.find((g) => g.key === selectedKey) ?? groups[0] ?? null : null;

  const open = (key: string) => {
    setSelectedKey(key);
    setView('positions');
  };

  return (
    <div className="max-w-[1600px] mx-auto space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        {inDetail ? (
          <nav aria-label="Breadcrumb" className="flex items-center gap-2 text-[15px] min-w-0">
            <button onClick={() => setSelectedKey(null)} className="px-4 min-h-[44px] rounded-lg bg-[#161b22] border border-gray-800 text-gray-100 font-semibold hover:bg-gray-800">
              Strategies
            </button>
            <ChevronRight className="w-4 h-4 text-gray-500 shrink-0" aria-hidden="true" />
            <span className="px-4 min-h-[44px] flex items-center rounded-lg bg-[#161b22] border border-gray-800 text-gray-200 truncate">
              {view === 'groups' ? 'All groups' : selected?.name ?? '—'}
            </span>
          </nav>
        ) : (
          <div>
            <h2 className="text-2xl font-bold text-white">📊 Options Trading</h2>
            <p className="text-sm text-gray-400">Open strategies, paper and live — click one for its positions</p>
          </div>
        )}
        <ModeToggle />
      </div>

      {inDetail ? (
        <StrategyDetail
          groups={groups}
          selected={selected}
          onSelect={open}
          view={view}
          setView={setView}
          showClosed={showClosed}
          setShowClosed={setShowClosed}
        />
      ) : (
        <>
          <FundsMarginsPanel positions={allPositions} modeFilter={modeFilter} />
          <StrategyList groups={groups} showClosed={showClosed} setShowClosed={setShowClosed} onOpen={open} />
        </>
      )}
    </div>
  );
}
