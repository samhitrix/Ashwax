import { create } from 'zustand'
import type { DashboardState } from '../types'

const EMPTY: DashboardState = {
  equityCurve: [],
  currentEquity: 0,
  startingCapital: 0,
  dailyPnl: 0,
  dailyLossSoFar: 0,
  positions: [],
  orders: [],
  activity: [],
  alerts: [],
  systemLevel: 'FULL_OPS',
  invariants: [],
  loops: [],
  paperMode: false,
  paperPositions: [],
  paperOrders: [],
  masterSwitch: true,
  autonomyMode: 'assisted',
  lastUpdated: '',
}

interface Store extends DashboardState {
  setMasterSwitch: (on: boolean) => void
  setAutonomyMode: (mode: DashboardState['autonomyMode']) => void
  acknowledgeAlert: (id: string) => void
  setPaperMode: (paper: boolean) => void
}

export const useDashboard = create<Store>((set) => ({
  ...EMPTY,
  setMasterSwitch: (masterSwitch) => set({ masterSwitch }),
  setAutonomyMode: (autonomyMode) => set({ autonomyMode }),
  acknowledgeAlert: (id) =>
    set((s) => ({ alerts: s.alerts.map((a) => (a.id === id ? { ...a, acknowledged: true } : a)) })),
  setPaperMode: (paperMode) => set({ paperMode }),
}))
