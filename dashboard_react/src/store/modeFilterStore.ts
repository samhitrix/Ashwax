import { create } from 'zustand';

export type ModeFilter = 'all' | 'paper' | 'live';

const STORAGE_KEY = 'ashwax-mode-filter';

function loadModeFilter(): ModeFilter {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    return saved === 'paper' || saved === 'live' ? saved : 'all';
  } catch {
    return 'all';
  }
}

interface ModeFilterStore {
  modeFilter: ModeFilter;
  setModeFilter: (mode: ModeFilter) => void;
}

/**
 * Shared across Order Monitor+ and Trades & P&L so a Paper/Live choice made on
 * one tab can't be forgotten and misread as the other on a different tab —
 * the whole point of separating paper from live in the first place.
 */
export const useModeFilterStore = create<ModeFilterStore>((set) => ({
  modeFilter: loadModeFilter(),
  setModeFilter: (mode) => {
    try {
      localStorage.setItem(STORAGE_KEY, mode);
    } catch {
      // localStorage unavailable — filter still applies for this session
    }
    set({ modeFilter: mode });
  },
}));
