import { useModeFilterStore, type ModeFilter } from '../store/modeFilterStore';
import { cn } from '../utils/cn';

const OPTIONS: { value: ModeFilter; label: string; dot: string }[] = [
  { value: 'all', label: 'All', dot: 'bg-gray-500' },
  { value: 'paper', label: 'Paper', dot: 'bg-blue-400' },
  { value: 'live', label: 'Live', dot: 'bg-red-400' },
];

/** Segmented All / Paper / Live control — shared state (see modeFilterStore)
 * so switching it on one tab keeps every other mode-aware tab in sync. */
export function ModeToggle() {
  const { modeFilter, setModeFilter } = useModeFilterStore();

  return (
    <div className="inline-flex items-center gap-1 bg-[#0d1117] border border-gray-800 rounded-xl p-1">
      {OPTIONS.map((opt) => (
        <button
          key={opt.value}
          onClick={() => setModeFilter(opt.value)}
          className={cn(
            'flex items-center gap-2 px-5 py-2.5 rounded-lg text-sm font-bold transition-all',
            modeFilter === opt.value
              ? 'bg-blue-500/20 text-white ring-2 ring-blue-400 shadow-lg scale-105'
              : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800'
          )}
        >
          <span className={cn('w-2 h-2 rounded-full', opt.dot, modeFilter === opt.value && 'ring-2 ring-white/40')} />
          {opt.label}
        </button>
      ))}
    </div>
  );
}
